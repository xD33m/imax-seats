FROM python:3.15-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    STATE_FILE=/data/state.json \
    HEALTH_FILE=/data/health \
    CHECK_INTERVAL=120

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY notifier.py .

# the state file survives restarts, so nothing is announced twice
RUN useradd --create-home --uid 10001 watcher \
    && mkdir -p /data \
    && chown watcher:watcher /data
VOLUME /data
USER watcher

# unhealthy once no check has succeeded for 5x the polling interval
HEALTHCHECK --interval=2m --timeout=10s --start-period=60s --retries=2 \
    CMD python -c "import os,sys,time; p=os.environ['HEALTH_FILE']; i=int(os.environ['CHECK_INTERVAL']); \
sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p) < 5*i else 1)"

CMD ["python", "notifier.py"]
