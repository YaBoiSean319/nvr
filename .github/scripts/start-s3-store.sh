#!/usr/bin/env bash
# Start the S3-compatible store the real-object-store tests run against (#402): moto's server, installed from the hash-locked
# requirements-ci-s3.txt into a virtual environment of its own, with request signing checked. Writes the NVR_TEST_S3_* variables to
# $GITHUB_ENV. Fails loudly if the store does not come up: those tests are not to be skipped quietly.
set -euo pipefail
cd "$(dirname "$0")/../.."
store=/tmp/nvr-s3-store
endpoint=http://127.0.0.1:9000
python -m venv "$store"
"$store/bin/python" -m pip install --quiet --require-hashes -r requirements-ci-s3.txt
INITIAL_NO_AUTH_ACTION_COUNT=3 nohup "$store/bin/moto_server" -H 127.0.0.1 -p 9000 >/tmp/nvr-s3-store.log 2>&1 &
for attempt in $(seq 1 30); do
  if curl -sf "$endpoint/moto-api/" >/dev/null; then break; fi
  sleep 1
done
curl -sf "$endpoint/moto-api/" >/dev/null || { cat /tmp/nvr-s3-store.log; exit 1; }
read -r access_key secret_key < <("$store/bin/python" .github/scripts/s3_store_setup.py "$endpoint" nvr-test)
echo "::add-mask::$secret_key"
{
  echo "NVR_TEST_S3_ENDPOINT=$endpoint"
  echo "NVR_TEST_S3_BUCKET=nvr-test"
  echo "NVR_TEST_S3_ACCESS_KEY=$access_key"
  echo "NVR_TEST_S3_SECRET_KEY=$secret_key"
} >> "${GITHUB_ENV:?}"
