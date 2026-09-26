#!/usr/bin/env python3
"""
oracle-arm-hunter
=================

Polls Oracle Cloud Infrastructure for Always Free Ampere A1 (VM.Standard.A1.Flex)
host capacity, launches the instance the moment a host frees up, then pings you
on Telegram. Built to run unattended as a long-lived container under Coolify.

Modes
-----
    python hunter.py            hunt forever (default; what Coolify runs)
    python hunter.py --check    validate config, print a summary, test Telegram
    python hunter.py --once     one round across every availability domain, exit

Exit codes
----------
    0   success (instance exists / was created), or --check / --once finished
    2   configuration or credentials are wrong - retrying will not help
    3   tenancy quota problem (e.g. LimitExceeded) - fix the account, not the code
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import logging
import os
import random
import signal
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import oci
import requests
from oci.exceptions import RequestException, ServiceError

LOG = logging.getLogger("hunter")

TERMINAL_STATES = {"TERMINATED", "TERMINATING"}

# Substrings Oracle puts in the message when it simply has no host to give you.
# Treated as "come back later", never as an error worth alerting about.
CAPACITY_HINTS = (
    "out of host capacity",
    "outofhostcapacity",
    "host capacity",
)


def launch_shape_config_cls():
    """The shape-config model was renamed in the 2.x SDK line; accept either."""
    for name in ("LaunchInstanceShapeConfigDetails", "InstanceShapeConfigDetails"):
        cls = getattr(oci.core.models, name, None)
        if cls is not None:
            return cls
    raise SystemExit(
        "this oci SDK exposes neither LaunchInstanceShapeConfigDetails nor "
        "InstanceShapeConfigDetails - upgrade the oci package"
    )


# --------------------------------------------------------------------------- #
# small env helpers
# --------------------------------------------------------------------------- #
def _env(name: str, default=None):
    raw = os.environ.get(name)
    if raw is None:
        return default
    raw = raw.strip()
    return raw if raw else default


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(float(raw))
    except ValueError:
        raise SystemExit(f"{name} must be a number, got {raw!r}")


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        raise SystemExit(f"{name} must be a number, got {raw!r}")


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes", "on", "y")


def _env_list(name: str):
    raw = _env(name)
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
class Config:
    """Everything comes from environment variables so Coolify owns the config."""

    def __init__(self) -> None:
        # --- OCI API credentials ------------------------------------------- #
        self.user = _env("OCI_USER_OCID")
        self.tenancy = _env("OCI_TENANCY_OCID")
        self.fingerprint = _env("OCI_FINGERPRINT")
        self.region = _env("OCI_REGION")
        self.key_file = _env("OCI_KEY_FILE")
        self.key_content = _env("OCI_KEY_CONTENT")
        self.key_b64 = _env("OCI_KEY_B64")
        self.config_file = _env("OCI_CONFIG_FILE")
        self.config_profile = _env("OCI_CONFIG_PROFILE", "DEFAULT")

        # --- where and what to launch -------------------------------------- #
        self.compartment_id = _env("OCI_COMPARTMENT_ID") or self.tenancy
        self.subnet_id = _env("OCI_SUBNET_ID")
        self.image_id = _env("OCI_IMAGE_ID")
        self.availability_domains = _env_list("OCI_AVAILABILITY_DOMAINS")

        self.shape = _env("OCI_SHAPE", "VM.Standard.A1.Flex")
        self.ocpus = _env_float("OCI_OCPUS", 2.0)
        self.memory_gb = _env_float("OCI_MEMORY_GB", 12.0)
        self.boot_volume_gb = _env_int("OCI_BOOT_VOLUME_GB", 50)
        self.assign_public_ip = _env_bool("OCI_ASSIGN_PUBLIC_IP", True)
        self.instance_name = _env("INSTANCE_NAME", "arm-hunter")

        self.ssh_public_key = _env("SSH_PUBLIC_KEY")
        self.ssh_public_key_file = _env("SSH_PUBLIC_KEY_FILE")

        # --- pacing --------------------------------------------------------- #
        # Floor of 60s: hammering the launch API is what gets a tenancy flagged
        # for abuse. Oracle's own docs ask for backoff on capacity errors.
        self.retry_interval = max(_env_int("RETRY_INTERVAL", 300), 60)
        self.retry_jitter = max(_env_int("RETRY_JITTER", 90), 0)
        self.rounds_before_cooldown = _env_int("ROUNDS_BEFORE_COOLDOWN", 12)
        self.cooldown_seconds = _env_int("COOLDOWN_SECONDS", 1200)
        self.heartbeat_hours = _env_float("HEARTBEAT_HOURS", 12.0)
        self.notify_every_round = _env_bool("NOTIFY_EVERY_ROUND", False)
        self.use_capacity_report = _env_bool("USE_CAPACITY_REPORT", True)
        self.retry_on_limit_exceeded = _env_bool("RETRY_ON_LIMIT_EXCEEDED", False)
        self.dry_run = _env_bool("DRY_RUN", False)

        # --- plumbing ------------------------------------------------------- #
        self.state_dir = Path(_env("STATE_DIR", "/data"))
        self.log_level = _env("LOG_LEVEL", "INFO").upper()
        self.telegram_token = _env("TELEGRAM_BOT_TOKEN")
        self.telegram_chat_id = _env("TELEGRAM_CHAT_ID")

    # -- derived ------------------------------------------------------------ #
    @property
    def state_file(self) -> Path:
        return self.state_dir / "state.json"

    @property
    def heartbeat_file(self) -> Path:
        return self.state_dir / "heartbeat"

    def validate(self) -> None:
        problems = []

        if not self.telegram_token or not self.telegram_chat_id:
            problems.append(
                "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not both set - "
                "the hunt will still run but you will not be alerted"
            )

        has_api_key = bool(self.user and self.tenancy and self.fingerprint)
        if not has_api_key and not self.config_file and not Path(
            os.path.expanduser("~/.oci/config")
        ).exists():
            problems.append(
                "no OCI credentials: set OCI_USER_OCID, OCI_TENANCY_OCID, "
                "OCI_FINGERPRINT and one of OCI_KEY_B64 / OCI_KEY_CONTENT / "
                "OCI_KEY_FILE"
            )

        if not self.compartment_id:
            problems.append("OCI_COMPARTMENT_ID is empty and OCI_TENANCY_OCID is unset")

        if self.ocpus <= 0 or self.memory_gb <= 0:
            problems.append("OCI_OCPUS and OCI_MEMORY_GB must be positive")

        if not self.ssh_public_key and not self.ssh_public_key_file:
            problems.append(
                "set SSH_PUBLIC_KEY (the .pub text) or SSH_PUBLIC_KEY_FILE - "
                "without it you cannot log in to the instance you win"
            )

        hard = [p for p in problems if "not both set" not in p]
        for p in problems:
            LOG.warning("config: %s", p)
        if hard:
            raise SystemExit("configuration problem: " + "; ".join(hard))

    def ssh_key(self) -> str:
        if self.ssh_public_key:
            return self.ssh_public_key
        path = os.path.expanduser(self.ssh_public_key_file or "")
        try:
            return Path(path).read_text().strip()
        except OSError as exc:
            raise SystemExit(f"cannot read SSH_PUBLIC_KEY_FILE {path}: {exc}")


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
class Telegram:
    def __init__(self, token: str | None, chat_id: str | None) -> None:
        self.token = token
        self.chat_id = chat_id

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str) -> bool:
        if not self.enabled:
            LOG.info("telegram disabled, would have sent: %s", text.replace("\n", " | "))
            return False
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        for attempt in (1, 2):
            try:
                resp = requests.post(url, json=payload, timeout=20)
                if resp.status_code == 200:
                    return True
                LOG.warning("telegram rejected the message (%s): %s",
                            resp.status_code, resp.text[:300])
                return False
            except requests.RequestException as exc:
                LOG.warning("telegram send failed (attempt %d): %s", attempt, exc)
                time.sleep(5)
        return False

    @staticmethod
    def esc(value) -> str:
        return html.escape(str(value), quote=False)


# --------------------------------------------------------------------------- #
# the hunter
# --------------------------------------------------------------------------- #
class Hunter:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.tg = Telegram(cfg.telegram_token, cfg.telegram_chat_id)
        self.done = False
        self.stopping = False
        self.attempts = 0
        self.rounds = 0
        self.last_error = "none yet"
        self.last_heartbeat = 0.0

        self.oci_config = self._build_oci_config()
        # No SDK-level retries: DEFAULT_RETRY_STRATEGY would retry the 500
        # "Out of host capacity" eight times per attempt, which is exactly the
        # hammering we want to avoid. We do our own pacing instead.
        no_retry = oci.retry.NoneRetryStrategy()
        self.compute = oci.core.ComputeClient(self.oci_config, retry_strategy=no_retry)
        self.network = oci.core.VirtualNetworkClient(
            self.oci_config, retry_strategy=no_retry
        )

        # filled in by resolve()
        self.subnet_id: str | None = None
        self.image_id: str | None = None
        self.ads: list[str] = []

    # -- setup -------------------------------------------------------------- #
    def _build_oci_config(self) -> dict:
        cfg = self.cfg
        full_creds = bool(cfg.user and cfg.tenancy and cfg.fingerprint)

        if full_creds:
            conf = {
                "user": cfg.user,
                "tenancy": cfg.tenancy,
                "fingerprint": cfg.fingerprint,
                "region": cfg.region or "us-ashburn-1",
            }
            if cfg.key_content:
                conf["key_content"] = cfg.key_content
            elif cfg.key_b64:
                try:
                    conf["key_content"] = base64.b64decode(cfg.key_b64).decode("utf-8")
                except Exception as exc:
                    raise SystemExit(f"OCI_KEY_B64 is not valid base64: {exc}")
            elif cfg.key_file:
                conf["key_file"] = os.path.expanduser(cfg.key_file)
            else:
                default_key = os.path.expanduser("~/.oci/oci_api_key.pem")
                if not Path(default_key).exists():
                    raise SystemExit(
                        "no private key: set OCI_KEY_B64 (or OCI_KEY_CONTENT / "
                        "OCI_KEY_FILE) to the key you downloaded from the OCI "
                        "console"
                    )
                conf["key_file"] = default_key
        else:
            # Fall back to a normal ~/.oci/config so you can test locally.
            path = os.path.expanduser(cfg.config_file or "~/.oci/config")
            LOG.info("using OCI config file %s (profile %s)", path, cfg.config_profile)
            conf = oci.config.from_file(path, cfg.config_profile)

        if not conf.get("region"):
            raise SystemExit("no region: set OCI_REGION")
        try:
            oci.config.validate_config(conf)
        except Exception as exc:
            raise SystemExit(f"OCI credentials look invalid: {exc}")
        return conf

    def resolve(self) -> None:
        """Work out subnet, image and availability domains (auto where possible)."""
        cfg = self.cfg

        self.ads = cfg.availability_domains or [
            ad.name
            for ad in self.compute.list_availability_domains(cfg.compartment_id).data
        ]
        if not self.ads:
            raise SystemExit("no availability domains found for this region")

        self.subnet_id = cfg.subnet_id or self._discover_subnet()
        if not self.subnet_id:
            raise SystemExit(
                "no public subnet found - create a VCN (console: Networking > "
                "Virtual Cloud Networks > Create VCN with Internet Connectivity) "
                "and set OCI_SUBNET_ID"
            )

        self.image_id = cfg.image_id or self._discover_image()
        if not self.image_id:
            raise SystemExit(
                "no ARM image found - set OCI_IMAGE_ID to an aarch64 image OCID"
            )

    def _discover_subnet(self) -> str | None:
        cfg = self.cfg
        LOG.info("OCI_SUBNET_ID not set, looking for a public subnet")
        try:
            vcns = oci.pagination.list_call_get_all_results(
                self.network.list_vcns, cfg.compartment_id
            ).data
        except ServiceError as exc:
            LOG.warning("cannot list VCNs: %s", exc.message)
            return None
        for vcn in vcns:
            subnets = oci.pagination.list_call_get_all_results(
                self.network.list_subnets, cfg.compartment_id, vcn_id=vcn.id
            ).data
            for subnet in subnets:
                if not subnet.prohibit_public_ip_on_vnic:
                    LOG.info(
                        "using subnet %s (%s) in VCN %s",
                        subnet.display_name, subnet.id, vcn.display_name,
                    )
                    return subnet.id
        return None

    def _discover_image(self) -> str | None:
        cfg = self.cfg
        LOG.info("OCI_IMAGE_ID not set, looking for the newest Ubuntu ARM image")
        try:
            images = oci.pagination.list_call_get_all_results(
                self.compute.list_images,
                cfg.compartment_id,
                operating_system="Canonical Ubuntu",
                shape=cfg.shape,
                sort_by="TIMECREATED",
                sort_order="DESC",
            ).data
        except ServiceError as exc:
            LOG.warning("cannot list images: %s", exc.message)
            return None
        for image in images:
            if "aarch64" in (image.display_name or ""):
                LOG.info("using image %s (%s)", image.display_name, image.id)
                return image.id
        if images:
            LOG.info("using image %s (%s)", images[0].display_name, images[0].id)
            return images[0].id
        return None

    # -- state -------------------------------------------------------------- #
    def load_state(self) -> dict:
        try:
            return json.loads(self.cfg.state_file.read_text())
        except (OSError, ValueError):
            return {}

    def save_state(self, state: dict) -> None:
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.cfg.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(self.cfg.state_file)

    def touch_heartbeat(self) -> None:
        try:
            self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
            self.cfg.heartbeat_file.touch()
        except OSError as exc:
            LOG.debug("cannot touch heartbeat: %s", exc)

    # -- OCI calls ---------------------------------------------------------- #
    def find_existing_instance(self):
        try:
            instances = oci.pagination.list_call_get_all_results(
                self.compute.list_instances,
                self.cfg.compartment_id,
                display_name=self.cfg.instance_name,
            ).data
        except ServiceError as exc:
            LOG.warning("cannot list instances: %s", exc.message)
            return None
        for inst in instances:
            if inst.lifecycle_state not in TERMINAL_STATES:
                return inst
        return None

    def capacity_report(self, ad: str) -> str:
        """Advisory only: is the shape nominally available in this AD?

        Returns 'AVAILABLE', 'OUT_OF_HOST_CAPACITY' or 'UNKNOWN'. Never blocks a
        launch - the launch call is the ground truth.
        """
        try:
            details = oci.core.models.CreateComputeCapacityReportDetails(
                # Oracle: "This should always be the root compartment."
                compartment_id=self.oci_config["tenancy"],
                availability_domain=ad,
                shape_availabilities=[
                    oci.core.models.CreateCapacityReportShapeAvailabilityDetails(
                        instance_shape=self.cfg.shape,
                        instance_shape_config=oci.core.models.CapacityReportInstanceShapeConfig(
                            ocpus=self.cfg.ocpus,
                            memory_in_gbs=self.cfg.memory_gb,
                        ),
                    )
                ],
            )
            report = self.compute.create_compute_capacity_report(details).data
            statuses = [
                getattr(item, "availability_status", None)
                for item in (report.shape_availabilities or [])
            ]
            if any(s == "AVAILABLE" for s in statuses):
                return "AVAILABLE"
            if statuses:
                return "OUT_OF_HOST_CAPACITY"
            return "UNKNOWN"
        except Exception as exc:  # noqa: BLE001 - advisory path, never fatal
            LOG.debug("capacity report unavailable for %s: %s", ad, exc)
            return "UNKNOWN"

    def order_ads(self) -> list[str]:
        if not self.cfg.use_capacity_report:
            return list(self.ads)
        scored = []
        for ad in self.ads:
            status = self.capacity_report(ad)
            LOG.info("capacity report %s: %s", ad, status)
            # Prefer ADs that already look free, keep the rest as fallback.
            scored.append((0 if status == "AVAILABLE" else 1, ad))
        scored.sort(key=lambda pair: pair[0])
        return [ad for _, ad in scored]

    def try_launch(self, ad: str) -> str:
        """One launch attempt in one AD.

        Returns 'success', 'continue' (try the next AD / round) or 'abort'.
        """
        cfg = self.cfg
        self.attempts += 1

        if cfg.dry_run:
            LOG.info("[dry-run] would launch %s in %s", cfg.shape, ad)
            self.last_error = "dry-run"
            return "continue"

        details = oci.core.models.LaunchInstanceDetails(
            availability_domain=ad,
            compartment_id=cfg.compartment_id,
            display_name=cfg.instance_name,
            shape=cfg.shape,
            shape_config=launch_shape_config_cls()(
                ocpus=cfg.ocpus,
                memory_in_gbs=cfg.memory_gb,
            ),
            source_details=oci.core.models.InstanceSourceViaImageDetails(
                image_id=self.image_id,
                boot_volume_size_in_gbs=cfg.boot_volume_gb,
            ),
            create_vnic_details=oci.core.models.CreateVnicDetails(
                subnet_id=self.subnet_id,
                assign_public_ip=cfg.assign_public_ip,
            ),
            metadata={"ssh_authorized_keys": cfg.ssh_key()},
        )

        LOG.info("attempt #%d: launching %s (%.0f OCPU / %.0f GB) in %s",
                 self.attempts, cfg.shape, cfg.ocpus, cfg.memory_gb, ad)

        try:
            # opc_retry_token is a request header on the operation, not a field
            # of LaunchInstanceDetails. A fresh token per attempt means we get a
            # genuinely new request rather than a deduplicated one.
            response = self.compute.launch_instance(
                details, opc_retry_token=str(uuid.uuid4())
            )
        except ServiceError as exc:
            kind = self._classify(exc)
            self.last_error = f"{exc.status} {exc.code}: {exc.message}"
            LOG.info("attempt #%d -> %s (%s)", self.attempts, kind, self.last_error)

            if kind == "capacity":
                return "continue"
            if kind == "throttled":
                LOG.warning("rate limited by OCI, backing off 120s")
                self._sleep(120)
                return "continue"
            if kind == "transient":
                return "continue"
            if kind == "quota":
                if cfg.retry_on_limit_exceeded:
                    LOG.warning("LimitExceeded but RETRY_ON_LIMIT_EXCEEDED is on")
                    return "continue"
                self.tg.send(
                    "🛑 <b>Oracle ARM hunt stopped</b>\n"
                    "Oracle returned a quota/limit error, so retrying is pointless.\n\n"
                    f"<code>{self.tg.esc(self.last_error)}</code>\n\n"
                    "Usual causes: you already hold A1 capacity, or the request "
                    "exceeds the Always Free allowance (now 2 OCPU / 12 GB). "
                    "Check Limits &amp; Quotas in the OCI console, then lower "
                    "OCI_OCPUS / OCI_MEMORY_GB or delete the old instance and "
                    "redeploy."
                )
                return "abort"
            # auth / bad OCID / bad parameter -> stop loudly
            self.tg.send(
                "🛑 <b>Oracle ARM hunt stopped</b>\n"
                "Oracle rejected the request in a way that retrying cannot fix "
                "(credentials or a wrong OCID).\n\n"
                f"<code>{self.tg.esc(self.last_error)}</code>\n\n"
                "Re-run the container's <code>--check</code> command to see what "
                "it resolved."
            )
            return "abort"
        except RequestException as exc:
            self.last_error = f"network: {exc}"
            LOG.warning("attempt #%d network problem: %s", self.attempts, exc)
            return "continue"

        instance = response.data
        LOG.info("GOT IT: instance %s in %s", instance.id, ad)
        self.on_success(instance)
        return "success"

    @staticmethod
    def _classify(exc: ServiceError) -> str:
        code = exc.code or ""
        message = (exc.message or "").lower()
        if any(hint in message for hint in CAPACITY_HINTS):
            return "capacity"
        if code == "TooManyRequests" or exc.status == 429:
            return "throttled"
        if code in ("LimitExceeded", "QuotaExceeded"):
            return "quota"
        if exc.status in (401, 403) or code in (
            "NotAuthenticated",
            "NotAuthorized",
            "NotAuthorizedOrNotFound",
            "InvalidParameter",
        ):
            return "fatal"
        if exc.status in (500, 502, 503, 504):
            return "transient"
        return "fatal"

    # -- success ------------------------------------------------------------ #
    def on_success(self, instance, created: bool = True) -> None:
        public_ip = self._wait_and_locate(instance)

        state = {
            "instance_id": instance.id,
            "display_name": instance.display_name,
            "availability_domain": instance.availability_domain,
            "shape": instance.shape,
            "public_ip": public_ip,
            "created": created,
            "found_at": _now(),
            "notified": True,
        }
        self.save_state(state)

        lines = [
            "🎉 <b>Oracle ARM instance is yours</b>",
            f"<b>{self.tg.esc(instance.display_name)}</b> · {self.tg.esc(instance.shape)}",
            f"AD: <code>{self.tg.esc(instance.availability_domain)}</code>",
        ]
        if public_ip:
            lines.append(f"IP: <code>{self.tg.esc(public_ip)}</code>")
            lines.append(
                f"<code>ssh ubuntu@{self.tg.esc(public_ip)}</code>"
                "  (user depends on the image you picked)"
            )
        lines.append(f"OCID: <code>{self.tg.esc(instance.id)}</code>")
        lines.append(
            f"Took {self.attempts} launch attempts over {self.rounds} rounds."
        )
        lines.append("The hunter is now idling and will not launch a second one.")
        self.tg.send("\n".join(lines))

        self.done = True

    def _wait_and_locate(self, instance) -> str | None:
        try:
            oci.wait_until(
                self.compute,
                self.compute.get_instance(instance.id),
                "lifecycle_state",
                "RUNNING",
                max_wait_seconds=420,
                max_interval_seconds=10,
            )
        except Exception as exc:  # noqa: BLE001 - instance exists either way
            LOG.warning("instance did not report RUNNING yet: %s", exc)
        try:
            attachments = oci.pagination.list_call_get_all_results(
                self.network.list_vnic_attachments,
                self.cfg.compartment_id,
                instance_id=instance.id,
            ).data
            for attachment in attachments:
                vnic = self.network.get_vnic(attachment.vnic_id).data
                if vnic.public_ip:
                    return vnic.public_ip
        except ServiceError as exc:
            LOG.warning("cannot read public IP: %s", exc.message)
        return None

    # -- one round ---------------------------------------------------------- #
    def round(self) -> str:
        """Returns 'done', 'abort' or 'retry'."""
        self.rounds += 1
        self.touch_heartbeat()

        existing = self.find_existing_instance()
        if existing:
            state = self.load_state()
            if state.get("instance_id") == existing.id and state.get("notified"):
                LOG.info("instance %s already exists and you were already told",
                         existing.id)
            else:
                LOG.info("found an existing live instance %s, reporting it",
                         existing.id)
                self.on_success(existing, created=False)
            self.done = True
            return "done"

        for ad in self.order_ads():
            if self.stopping:
                return "abort"
            outcome = self.try_launch(ad)
            if outcome == "success":
                return "done"
            if outcome == "abort":
                return "abort"

        if self.cfg.notify_every_round:
            self.tg.send(
                f"⏳ Round {self.rounds}: still no A1 capacity "
                f"({self.attempts} attempts so far). Last: "
                f"<code>{self.tg.esc(self.last_error)}</code>"
            )
        return "retry"

    # -- main loop ---------------------------------------------------------- #
    def loop(self) -> int:
        self.startup()

        while not self.done and not self.stopping:
            outcome = self.round()
            if outcome == "done":
                break
            if outcome == "abort":
                return 2

            delay = self.cfg.retry_interval + random.randint(
                0, self.cfg.retry_jitter
            )
            if (
                self.cfg.rounds_before_cooldown
                and self.rounds % self.cfg.rounds_before_cooldown == 0
            ):
                delay += self.cfg.cooldown_seconds
                LOG.info("long cooldown after %d rounds", self.rounds)
            self._maybe_heartbeat()
            LOG.info("next round in %.0fs", delay)
            self._sleep(delay)

        if self.done:
            return self.idle()
        return 0

    def idle(self) -> int:
        """Hold the container open so Coolify never restarts us into a second
        launch. A no-op loop, not a re-hunt."""
        LOG.info("instance secured - idling. Set the container to stopped, or "
                 "delete it, whenever you like.")
        while not self.stopping:
            self.touch_heartbeat()
            self._sleep(3600)
        return 0

    def startup(self) -> None:
        self.resolve()
        LOG.info("region=%s shape=%s %.0f OCPU/%.0f GB name=%s",
                 self.oci_config["region"], self.cfg.shape,
                 self.cfg.ocpus, self.cfg.memory_gb, self.cfg.instance_name)
        LOG.info("ADs: %s", ", ".join(self.ads))
        LOG.info("subnet=%s", self.subnet_id)
        LOG.info("image=%s", self.image_id)
        LOG.info("pacing: every %ds (+0-%ds jitter), long cooldown every %d rounds",
                 self.cfg.retry_interval, self.cfg.retry_jitter,
                 self.cfg.rounds_before_cooldown)

        state = self.load_state()
        if state.get("instance_id"):
            LOG.info("state file says instance %s was already secured",
                     state["instance_id"])

        self.tg.send(
            "🏹 <b>Oracle ARM hunter started</b>\n"
            f"Region: <code>{self.tg.esc(self.oci_config['region'])}</code>\n"
            f"Target: <code>{self.tg.esc(self.cfg.shape)}</code> "
            f"{self.cfg.ocpus:.0f} OCPU / {self.cfg.memory_gb:.0f} GB\n"
            f"ADs: {self.tg.esc(', '.join(self.ads))}\n"
            f"Retry: every ~{self.cfg.retry_interval}s\n"
            "I'll message you the moment capacity appears."
        )

    def _maybe_heartbeat(self) -> None:
        if self.cfg.heartbeat_hours <= 0:
            return
        now = time.time()
        if now - self.last_heartbeat < self.cfg.heartbeat_hours * 3600:
            return
        self.last_heartbeat = now
        self.tg.send(
            f"💤 Still hunting. {self.rounds} rounds, {self.attempts} attempts, "
            f"no capacity yet.\nLast: <code>{self.tg.esc(self.last_error)}</code>"
        )

    def _sleep(self, seconds: float) -> None:
        """Sleep in slices so SIGTERM is handled promptly."""
        deadline = time.time() + seconds
        while not self.stopping and time.time() < deadline:
            time.sleep(min(5, max(0.1, deadline - time.time())))

    # -- --check ------------------------------------------------------------ #
    def do_check(self) -> int:
        self.resolve()
        print()
        print("oracle-arm-hunter --check")
        print("=" * 58)
        print(f"region            {self.oci_config['region']}")
        print(f"tenancy           {self.oci_config['tenancy']}")
        print(f"user              {self.oci_config.get('user')}")
        print(f"fingerprint       {self.oci_config.get('fingerprint')}")
        print(f"compartment       {self.cfg.compartment_id}")
        print(f"shape             {self.cfg.shape} "
              f"({self.cfg.ocpus:.0f} OCPU / {self.cfg.memory_gb:.0f} GB)")
        print(f"instance name     {self.cfg.instance_name}")
        print(f"boot volume       {self.cfg.boot_volume_gb} GB")
        print(f"availability doms {', '.join(self.ads)}")
        print(f"subnet            {self.subnet_id}")
        print(f"image             {self.image_id}")
        print(f"ssh key           {self.cfg.ssh_key()[:60]}...")
        print(f"retry interval    {self.cfg.retry_interval}s "
              f"(+0-{self.cfg.retry_jitter}s jitter)")
        print(f"telegram          {'configured' if self.tg.enabled else 'NOT configured'}")
        print(f"dry run           {self.cfg.dry_run}")
        print()

        existing = self.find_existing_instance()
        print(f"existing instance {existing.id if existing else 'none'}")
        print()

        if self.cfg.use_capacity_report:
            print("capacity right now:")
            for ad in self.ads:
                print(f"  {ad}: {self.capacity_report(ad)}")
            print()

        if self.tg.enabled:
            ok = self.tg.send(
                "✅ <b>Test from oracle-arm-hunter</b>\n"
                "If you can read this, alerts will reach you."
            )
            print(f"telegram test     {'sent' if ok else 'FAILED - check token/chat id'}")
        else:
            print("telegram test     skipped (not configured)")
        print()
        return 0


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true",
                      help="validate config, print a summary, test Telegram, exit")
    mode.add_argument("--once", action="store_true",
                      help="run a single round across all ADs, then exit")
    args = parser.parse_args(argv)

    cfg = Config()
    setup_logging(cfg.log_level)
    cfg.validate()

    hunter = Hunter(cfg)

    def _stop(signum, _frame):
        LOG.info("signal %s received, shutting down cleanly", signum)
        hunter.stopping = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    if args.check:
        return hunter.do_check()

    if args.once:
        hunter.startup()
        outcome = hunter.round()
        return 0 if outcome in ("done", "retry") else 2

    return hunter.loop()


if __name__ == "__main__":
    sys.exit(main())
