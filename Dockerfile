# The browser interface is built in its own stage; only the static output ships.
FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NVR_DB_PATH=/data/nvr.sqlite3

WORKDIR /app
COPY requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates smartmontools \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt \
    && mkdir -p /opt/nvr/models \
    && curl -fsSL --retry 3 https://storage.openvinotoolkit.org/repositories/open_model_zoo/2021.4/models_bin/2/person-vehicle-bike-detection-2004/FP32/person-vehicle-bike-detection-2004.xml -o /opt/nvr/models/person-vehicle-bike-detection-2004.xml \
    && curl -fsSL --retry 3 https://storage.openvinotoolkit.org/repositories/open_model_zoo/2021.4/models_bin/2/person-vehicle-bike-detection-2004/FP32/person-vehicle-bike-detection-2004.bin -o /opt/nvr/models/person-vehicle-bike-detection-2004.bin \
    && echo '408e2a556cc935a59b765cb72045fb7c5f8071c4b0f2c5433711c2b38ff085d8  /opt/nvr/models/person-vehicle-bike-detection-2004.xml' | sha256sum -c - \
    && echo 'de10beb5a811bd1b38c828b4060898c32502c4919488fd7100269cbdc7595e08  /opt/nvr/models/person-vehicle-bike-detection-2004.bin' | sha256sum -c - \
    && groupadd --system --gid 10001 nvr \
    && useradd --system --uid 10001 --gid nvr --home-dir /nonexistent nvr \
    && mkdir -p /data /secrets /recordings /backups \
    && chown -R nvr:nvr /app /data /secrets /recordings /backups /opt/nvr/models
COPY --chown=nvr:nvr app ./app
COPY --from=web --chown=nvr:nvr /app/web_dist ./app/web_dist
COPY --chown=nvr:nvr scripts ./scripts
USER nvr
EXPOSE 8280
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('NVR_LISTEN_PORT', '8280') + '/healthz', timeout=3)"
CMD ["python", "-m", "app.entrypoint"]
