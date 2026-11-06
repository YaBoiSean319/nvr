#!/usr/bin/env bash
# Install FFmpeg on a CI runner even when the Ubuntu mirror is slow.
#
# `apt-get install` fetches its ~90 packages one at a time, and on a slow mirror (a 9 MB package took over 100 s) three attempts did not fit the
# step's time limit, so the job died before any test ran. This asks apt for the list of what it needs, downloads the packages eight at a time into apt's
# own cache (the first attempt from the runner's mirror, later ones from archive.ubuntu.com), and then lets apt install from the cache. apt checks every
# package's size and checksum against the signed package index, so where a file came from does not matter and a bad one is fetched again.
#
# PACKAGE and PROBE can be set to test this with something small.
set -uo pipefail

package="${PACKAGE:-ffmpeg}"
probe="${PROBE:-ffmpeg}"
if command -v "$probe" >/dev/null 2>&1; then
  "$probe" -version >/dev/null 2>&1 || true
  echo "$probe is already installed"
  exit 0
fi

apt_opts=(-o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 -o DPkg::Lock::Timeout=60)
archives=/var/cache/apt/archives
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

for attempt in 1 2 3; do
  echo "::group::Install $package, attempt $attempt"
  if sudo timeout -k 10s 120s apt-get "${apt_opts[@]}" update \
     && sudo apt-get "${apt_opts[@]}" install --no-install-recommends -y --print-uris "$package" 2>/dev/null \
        | sed -n "s/^'\([^']*\)' \([^ ]*\) .*/\1 \2/p" > "$work/uris"; then
    if [ "$attempt" -gt 1 ]; then
      # The runner's mirror was too slow: use the main archive for this attempt (and the security archive for what it serves).
      sed -i -E 's#^http://azure\.archive\.ubuntu\.com/ubuntu/#http://archive.ubuntu.com/ubuntu/#' "$work/uris"
    fi
    echo "$(wc -l < "$work/uris") packages to fetch"
    # Eight at a time, each with its own time limit; a package already in the cache is skipped.
    sudo xargs -P 8 -L 1 bash -c '
      url="$0"; name="$1"
      [ -s "'"$archives"'/$name" ] && exit 0
      curl -fsSL --retry 3 --connect-timeout 20 --max-time 240 -o "'"$archives"'/$name.part" "$url" && mv "'"$archives"'/$name.part" "'"$archives"'/$name"
    ' < "$work/uris" || echo "some downloads failed; apt will fetch what is missing"
    if sudo timeout -k 10s 300s apt-get "${apt_opts[@]}" install --no-install-recommends -y "$package"; then
      echo "::endgroup::"
      "$probe" -version >/dev/null 2>&1 || command -v "$probe" >/dev/null
      exit $?
    fi
  fi
  echo "::endgroup::"
  echo "$package install attempt ${attempt} failed or timed out" >&2
  sleep 5
done
echo "$package could not be installed after 3 attempts" >&2
exit 1
