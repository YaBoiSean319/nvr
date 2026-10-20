FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NVR_DB_PATH=/data/nvr.sqlite3

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --system --gid 10001 nvr \
    && useradd --system --uid 10001 --gid nvr --home-dir /nonexistent nvr \
    && mkdir -p /data /recordings \
    && chown -R nvr:nvr /app /data /recordings
COPY --chown=nvr:nvr app ./app
USER nvr
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
