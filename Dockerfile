FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY hunter.py ./

# Non-root, and a writable state dir it owns.
RUN useradd --create-home --uid 10001 hunter \
 && mkdir -p /data \
 && chown -R hunter:hunter /data /app
USER hunter

# Heartbeat file is touched every round; if it goes stale the hunt is wedged.
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
  CMD python -c "import os,sys,time;p='/data/heartbeat';sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p)<3600 else 1)"

CMD ["python", "hunter.py"]
