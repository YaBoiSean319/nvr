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
    && mkdir -p /opt/nvr/models /opt/nvr/models/plates \
    && curl -fsSL --retry 3 https://storage.openvinotoolkit.org/repositories/open_model_zoo/2021.4/models_bin/2/person-vehicle-bike-detection-2004/FP32/person-vehicle-bike-detection-2004.xml -o /opt/nvr/models/person-vehicle-bike-detection-2004.xml \
    && curl -fsSL --retry 3 https://storage.openvinotoolkit.org/repositories/open_model_zoo/2021.4/models_bin/2/person-vehicle-bike-detection-2004/FP32/person-vehicle-bike-detection-2004.bin -o /opt/nvr/models/person-vehicle-bike-detection-2004.bin \
    && echo '408e2a556cc935a59b765cb72045fb7c5f8071c4b0f2c5433711c2b38ff085d8  /opt/nvr/models/person-vehicle-bike-detection-2004.xml' | sha256sum -c - \
    && echo 'de10beb5a811bd1b38c828b4060898c32502c4919488fd7100269cbdc7595e08  /opt/nvr/models/person-vehicle-bike-detection-2004.bin' | sha256sum -c - \
    # Licence plate models by region (docs/licence-plates.md); the list is app/plate_models.py's and a test keeps the two the same.
    && curl -fsSL --retry 3 https://github.com/ankandrew/open-image-models/releases/download/assets/yolo-v9-t-640-license-plates-end2end.onnx -o /opt/nvr/models/plates/yolo-v9-t-640-license-plates-end2end.onnx \
    && curl -fsSL --retry 3 https://github.com/ankandrew/cnn-ocr-lp/releases/download/arg-plates/global_mobile_vit_v2_ocr.onnx -o /opt/nvr/models/plates/global_mobile_vit_v2_ocr.onnx \
    && curl -fsSL --retry 3 https://github.com/ankandrew/cnn-ocr-lp/releases/download/arg-plates/global_mobile_vit_v2_ocr_config.yaml -o /opt/nvr/models/plates/global_mobile_vit_v2_ocr_config.yaml \
    && curl -fsSL --retry 3 https://github.com/ankandrew/cnn-ocr-lp/releases/download/arg-plates/european_mobile_vit_v2_ocr.onnx -o /opt/nvr/models/plates/european_mobile_vit_v2_ocr.onnx \
    && curl -fsSL --retry 3 https://github.com/ankandrew/cnn-ocr-lp/releases/download/arg-plates/european_mobile_vit_v2_ocr_config.yaml -o /opt/nvr/models/plates/european_mobile_vit_v2_ocr_config.yaml \
    && echo 'c3c1026ca7d0585dd88084d68182dd897113712fa734ae1557ca70174440c076  /opt/nvr/models/plates/yolo-v9-t-640-license-plates-end2end.onnx' | sha256sum -c - \
    && echo 'e2fe5946c02e73901f73d21b3b7223a0c3e3b02059d4964fbf279b01af38d63c  /opt/nvr/models/plates/global_mobile_vit_v2_ocr.onnx' | sha256sum -c - \
    && echo '202b90f2a1f3a0d486b8f1fbd5cdc2f19f4f4881a094f086b983976ed3108aaf  /opt/nvr/models/plates/global_mobile_vit_v2_ocr_config.yaml' | sha256sum -c - \
    && echo '5f388f57ddec318d38d17e420d292f5a049595bec93f111838903f6617f6943f  /opt/nvr/models/plates/european_mobile_vit_v2_ocr.onnx' | sha256sum -c - \
    && echo '202b90f2a1f3a0d486b8f1fbd5cdc2f19f4f4881a094f086b983976ed3108aaf  /opt/nvr/models/plates/european_mobile_vit_v2_ocr_config.yaml' | sha256sum -c - \
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
