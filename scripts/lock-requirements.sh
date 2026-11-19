#!/usr/bin/env bash
# Regenerate the locked, hashed Python requirements (#285). Edit requirements.in (the packages the app uses directly, pinned) or requirements-dev.in
# (adds the development tools), then run this and commit all four files (and requirements-ci-s3.*, the S3 store CI tests against). The image installs requirements.txt with --require-hashes, so what is built is
# exactly what was locked; tests/test_requirements_lock.py fails when the lock and the .in files disagree.
set -euo pipefail
cd "$(dirname "$0")/.."
command -v uv >/dev/null || { echo "Install uv first: python -m pip install uv" >&2; exit 1; }
for name in requirements requirements-dev requirements-ci-s3; do
  uv pip compile "$name.in" --generate-hashes --universal --python-version 3.12 -o "$name.txt"
done
