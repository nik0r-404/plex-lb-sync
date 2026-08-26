FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    STATE_FILE=/data/state.json

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY plex_lb_sync.py verify_plex_history.py ./

# Run unprivileged; /data is mounted as a volume and must belong to this user.
RUN useradd --create-home --uid 1000 scrobbler \
    && mkdir -p /data \
    && chown -R scrobbler:scrobbler /data /app
USER scrobbler

VOLUME ["/data"]

CMD ["python", "-u", "/app/plex_lb_sync.py"]
