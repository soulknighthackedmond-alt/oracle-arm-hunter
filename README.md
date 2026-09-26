# oracle-arm-hunter

A small Python worker that sits in Coolify, repeatedly asks Oracle Cloud for an
Always Free Ampere A1 instance, and Telegram-messages you the moment it gets one
— with the IP, so you can SSH straight in.

It is built around the two things that actually matter with this shape:

- **Capacity is the bottleneck, not your script.** Oracle's own docs tell you to
  retry in a different availability domain or "wait a while, then try again" [1].
  So it cycles every AD in your region, paced, forever.
- **It must not win twice.** The moment an instance is running it stops
  launching and idles, so a container restart can never leave you holding two
  instances and a surprise bill.

---

## Read this before you set anything up

**The free allowance is no longer 4 OCPU / 24 GB.** Oracle quietly halved the
Always Free Ampere A1 entitlement on 15 June 2026 to **2 OCPUs and 12 GB** [2][3],
and began enforcing it on 18 August 2026 — instances above the new limit were
terminated automatically [4].

Oracle's documentation now reads: *"All tenancies get the first 1,500 OCPU hours
and 9,000 GB hours per month for free for VM instances using the
VM.Standard.A1.Flex shape... For Always Free tenancies, this is equivalent to 2
OCPUs and 12 GB of memory."* [1]

So this script asks for 2 OCPU / 12 GB by default. If you ask for 4/24 on a free
tenancy you will get `LimitExceeded` and never win, no matter how long you wait.

Two other things from the same page worth knowing:

- **Home region only.** Always Free compute instances must be created in your
  tenancy's home region [1]. `OCI_REGION` has to be that region.
- **Idle instances get reclaimed.** Oracle may reclaim an Always Free instance
  if, over a 7-day window, 95th-percentile CPU is under 20%, network is under
  20%, and (for A1) memory is under 20% [1]. A VM you win and then leave empty is
  a VM you can lose. Put something on it.

### The Pay-As-You-Go question

There is a real chance PAYG accounts keep 4 OCPU / 24 GB. Oracle's price list
says *"Each paid tenancy gets the first 3,000 OCPU hours and 18,000 GB hours per
month"*, while the docs page still says "all tenancies... 1,500" — those cannot
both be read literally [4][5]. Users report human support agents confirming by
email that the new cap applies only to free-tier accounts, but Oracle has never
published that clarification [2].

Upgrading to PAYG is also the standard advice for getting capacity at all [1],
and Always Free resources stay free after upgrading [1]. I have not verified what
your specific account would be charged, so treat this as something to confirm
with Oracle support in writing before you switch — not as a step in this guide.
If you do go PAYG and it turns out you still have 4/24, just set
`OCI_OCPUS=4` and `OCI_MEMORY_GB=24`.

---

## What you need first

1. **An OCI API key.** Console → Identity → Users → your user → *API keys* →
   *Add API key* → *Generate API key pair* → download the private `.pem`. Note
   the **fingerprint** and the config preview (user OCID, tenancy OCID, region).
2. **A VCN with a public subnet.** If you have ever launched an instance from the
   console, you have one. Otherwise: Networking → Virtual Cloud Networks →
   *Create VCN* → *Create VCN with Internet Connectivity*. Leave `OCI_SUBNET_ID`
   empty and the script finds a public subnet for you.
3. **An SSH public key** to put on the instance (`ssh-ed25519 AAAA...`).
4. **A Telegram bot.** Talk to [@BotFather](https://t.me/BotFather) → `/newbot` →
   copy the token. Then send your new bot any message and read the chat id:

   ```bash
   curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | grep -o '"chat":{"id":[-0-9]*'
   ```

   (Group chats give a negative id. If the result is empty, message the bot again
   and retry — Telegram only shows updates it has received.)

---

## Files

| File | What it is |
|---|---|
| `hunter.py` | The whole thing. Stdlib + `oci` + `requests`. |
| `Dockerfile` | Python 3.12-slim, non-root, healthcheck on a heartbeat file. |
| `docker-compose.yaml` | For running it yourself, or as a Coolify Service. |
| `.env.example` | Every setting, commented. Copy to `.env`. |
| `README.md` | This. |

---

## Deploy it in Coolify

### Option A — as a Coolify Application (recommended)

1. Push the `oracle-arm-hunter/` folder to a Git repo. Private is fine; Coolify
   handles private repos with a deploy key.
2. Coolify → your project → **+ New Resource** → **Application** → **Git
   Repository** → pick the repo and branch.
3. On the build screen:
   - **Build Pack:** `Dockerfile`
   - **Base Directory:** `/oracle-arm-hunter` if you pushed it as a subfolder,
     otherwise `/`
   - **Ports Exposes:** leave **empty** — this is a background worker, it serves
     nothing and should be reachable from nowhere.
   - **Domains:** leave empty. Do not let Coolify put Traefik in front of it.
4. **Environment Variables** tab → add the variables from `.env.example`.
   Two things that matter here:
   - Use **`OCI_KEY_B64`, not a raw PEM.** Coolify stores env vars as a
     `.env` file, and a PEM's newlines will break it. Base64 is one line:
     ```bash
     # Linux / macOS
     base64 -w0 oci_api_key.pem
     # PowerShell
     [Convert]::ToBase64String([IO.File]::ReadAllBytes("oci_api_key.pem"))
     ```
   - Leave `OCI_SUBNET_ID` and `OCI_IMAGE_ID` empty and the script picks sensible
     ones itself. Fill them in only if it picks the wrong one.
5. **Persistent Storage** tab → add a volume, **destination path `/data`**. That
   holds the heartbeat and the record of the instance you won; without it, a
   redeploy forgets and could try to launch a second instance.
6. **Restart Policy:** `unless-stopped`, so it survives a server reboot.
7. Deploy, then open the **Logs** tab. You want to see it resolve the region, ADs,
   subnet and image, then start attempting.
8. Validate properly from the **Terminal** tab of the container:

   ```bash
   python hunter.py --check
   ```

   That prints what it resolved, asks Oracle for current capacity in each AD, and
   fires a test Telegram message. If that output looks right, you are done.

### Option B — no Git, just run it on the box

Two minutes, but Coolify is not managing it:

```bash
scp -r oracle-arm-hunter root@<coolify-host>:/root/
ssh root@<coolify-host>
cd /root/oracle-arm-hunter
cp .env.example .env && nano .env
docker compose up -d --build
docker compose exec hunter python hunter.py --check
docker compose logs -f
```

### Option C — let Coolify's scheduler drive it

Coolify's **Scheduled Tasks** can run a command in a container on a cron. Point
one at the image with `python hunter.py --once` every 5 minutes. It works, but a
long-running container reacts within the minute instead of up to five, so it wins
more races. Prefer A or B.

---

## Settings you will actually touch

Full list with comments in `.env.example`. The ones that matter:

| Variable | Default | Why you would change it |
|---|---|---|
| `OCI_REGION` | — | Must be your tenancy's **home region**. |
| `OCI_OCPUS` / `OCI_MEMORY_GB` | `2` / `12` | The current free maximum. Raise only if you know you have more. |
| `OCI_AVAILABILITY_DOMAINS` | all in region | Comma-separated. More ADs = more chances per round. |
| `OCI_BOOT_VOLUME_GB` | `50` | 47 GB is Oracle's stated minimum [1]; 50 GB is the console default. |
| `RETRY_INTERVAL` | `300` | Seconds between rounds. Floored at 60 — do not hammer. |
| `RETRY_JITTER` | `90` | Random extra seconds, so your pattern is not a metronome. |
| `ROUNDS_BEFORE_COOLDOWN` / `COOLDOWN_SECONDS` | `12` / `1200` | Every 12 rounds it takes a 20-minute break. Long runs look less like abuse. |
| `HEARTBEAT_HOURS` | `12` | "Still hunting" ping. Set `0` to silence it. |
| `USE_CAPACITY_REPORT` | `true` | Asks Oracle which ADs look free and tries those first. Advisory only. |
| `RETRY_ON_LIMIT_EXCEEDED` | `false` | Leave off. `LimitExceeded` means fix the account, not wait. |
| `DRY_RUN` | `false` | Exercises the whole loop without launching anything. |
| `INSTANCE_NAME` | `arm-hunter` | Also the idempotency key — the script looks for a live instance by this name. |

---

## How it behaves

**Pacing.** One round = try every AD once, then sleep `RETRY_INTERVAL + random
jitter`. Every 12th round it adds a 20-minute cooldown. The floor is 60 seconds
and you cannot set it lower, because a tight loop against the launch API is how
accounts get flagged.

**No SDK-level retries.** The `oci` client is constructed with
`NoneRetryStrategy`. The default strategy would retry the 500
`Out of host capacity` response eight times per attempt with its own backoff —
exactly the hammering this script exists to avoid.

**Winning.** On success it waits for `RUNNING`, looks up the public IP, writes
`/data/state.json`, sends the Telegram message, and then **idles forever**
instead of exiting. That is deliberate: if the process exited, Coolify would
restart it and it would immediately try to launch a second instance.

**Restarts are safe.** On boot it looks for a live instance named
`INSTANCE_NAME`. If one exists it reports it and idles rather than launching
another.

**Stopping it.** `SIGTERM` (a Coolify stop) is caught and handled cleanly. Once
you have your instance, stop or delete the container — it is doing nothing but
holding a heartbeat.

**It stops loudly rather than looping pointlessly.** A credentials or OCID
problem (`NotAuthorizedOrNotFound`, `NotAuthenticated`) and a quota problem
(`LimitExceeded`) both exit with a Telegram message explaining the cause, because
retrying cannot fix either.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `LimitExceeded` / `QuotaExceeded` | You already hold A1 capacity, or you are asking for more than 2 OCPU / 12 GB. Check *Limits, Quotas and Usage* in the console. |
| `NotAuthorizedOrNotFound` | A wrong OCID (compartment, subnet or image), or the API key's user lacks permission. Run `--check` to see what it resolved. |
| `Out of host capacity` forever | This is the normal state of affairs, not a bug. Add more ADs to `OCI_AVAILABILITY_DOMAINS`, and read the PAYG note above. |
| `InvalidParameter` | Usually a boot volume under 47 GB, or a non-integer OCPU count. |
| Container restart-looping | A config error — it exited 2. Read the logs; the reason is logged. |
| No Telegram messages at all | Run `python hunter.py --check`; it tests the token and chat id directly. |
| Telegram messages but the send failed | Check the container logs for `telegram rejected the message` — usually a bad chat id (negative for groups). |
| Won the instance, then lost it weeks later | Oracle's idle reclamation [1]. Run something on it. |

---

## Sources

1. Oracle, *Always Free Resources* — limits, home-region rule, the "out of host capacity" guidance, minimum boot volume, idle reclamation: <https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm>
2. InfoQ, *Oracle Quietly Halves Free Tier Ampere A1 Compute*, 3 Jul 2026 — the 15 June change, and the unconfirmed PAYG carve-out: <https://www.infoq.com/news/2026/07/oracle-cloud-free-tier-limits/>
3. Linuxiac, *Oracle Quietly Cuts Free Tier Ampere A1 Resources in Half*, 14 Jun 2026: <https://linuxiac.com/oracle-quietly-cuts-free-tier-ampere-a1-resources-in-half/>
4. Hacker News discussion, 5 Aug 2026 — reproduces Oracle's notification email and the 18 Aug enforcement date, and quotes the price list wording: <https://news.ycombinator.com/item?id=49183750>
5. picklog, *Oracle Free Tier ARM Changes: What the Docs Don't Say*, 12 Aug 2026 — docs-vs-price-list contradiction: <https://picklog.cc/blog/oracle-free-tier-arm-changes>
6. OCI Python SDK, `LaunchInstanceDetails`: <https://docs.oracle.com/en-us/iaas/tools/python/latest/api/core/models/oci.core.models.LaunchInstanceDetails.html>
7. OCI Python SDK, `CreateComputeCapacityReportDetails`: <https://docs.oracle.com/en-us/iaas/tools/python/latest/api/core/models/oci.core.models.CreateComputeCapacityReportDetails.html>
