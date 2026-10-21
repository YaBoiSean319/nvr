FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NVR_DB_PATH=/data/nvr.sqlite3

WORKDIR /app
COPY requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt \
    && groupadd --system --gid 10001 nvr \
    && useradd --system --uid 10001 --gid nvr --home-dir /nonexistent nvr \
    && mkdir -p /data /secrets /recordings \
    && chown -R nvr:nvr /app /data /secrets /recordings
COPY --chown=nvr:nvr app ./app
USER nvr
EXPOSE 8280
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('NVR_LISTEN_PORT', '8280') + '/healthz', timeout=3)"
CMD ["python", "-m", "app.entrypoint"]
