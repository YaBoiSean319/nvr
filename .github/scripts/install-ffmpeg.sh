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

mirrors=()
if [ -r /etc/apt/apt-mirrors.txt ]; then
  while read -r first _; do [ -n "$first" ] && mirrors+=("${first%/}"); done < /etc/apt/apt-mirrors.txt
fi
mirrors+=(http://archive.ubuntu.com/ubuntu)

for attempt in 1 2 3; do
  echo "::group::Install $package, attempt $attempt"
  if sudo timeout -k 10s 120s apt-get "${apt_opts[@]}" update \
     && sudo apt-get "${apt_opts[@]}" install --no-install-recommends -y --print-uris "$package" 2>/dev/null \
        | sed -n "s/^'\([^']*\)' \([^ ]*\) .*/\1 \2/p" > "$work/uris"; then
    # On the runners apt addresses packages as mirror+file:/etc/apt/apt-mirrors.txt/..., which only apt understands: turn that into a real URL on one of the
    # mirrors the runner lists (the first on attempt 1, the next on each retry, the main Ubuntu archive last).
    mirror="${mirrors[$(( (attempt - 1) % ${#mirrors[@]} ))]}"
    sed -i -E "s#^mirror\+file:/etc/apt/apt-mirrors\.txt/#${mirror}/#" "$work/uris"
    echo "fetching from $mirror"
    echo "$(wc -l < "$work/uris") packages to fetch"
    # Eight at a time, each with its own time limit; a package already in the cache is skipped.
    sudo xargs -P 8 -L 1 bash -c '
      url="$0"; name="$1"
      [ -s "'"$archives"'/$name" ] && exit 0
      curl -fsSL --retry 3 --connect-timeout 20 --max-time 240 -o "'"$archives"'/$name.part" "$url" && mv "'"$archives"'/$name.part" "'"$archives"'/$name"
    ' < "$work/uris" || echo "some downloads failed; apt will fetch what is missing"
    echo "$(while read -r _ name; do [ -s "$archives/$name" ] && echo ok; done < "$work/uris" | wc -l) of $(wc -l < "$work/uris") packages fetched in parallel"
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
