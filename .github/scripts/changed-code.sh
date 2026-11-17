#!/usr/bin/env bash
# Says whether a pull request changes anything but documentation: writes code=true or code=false to $GITHUB_OUTPUT.
# Anything that is not a pull request, any file that is not documentation, and any doubt (no files found, a fetch that fails) is code=true,
# so the browser journeys are skipped only when it is certain they cannot be affected.
set -u
output="${GITHUB_OUTPUT:-/dev/stdout}"

if [ "${EVENT:-}" != "pull_request" ] || [ -z "${BASE:-}" ] || [ -z "${HEAD:-}" ]; then
  echo "code=true" >> "$output"
  exit 0
fi

git fetch --no-tags --depth=1 origin "$BASE" "$HEAD" >/dev/null 2>&1 || { echo "code=true" >> "$output"; exit 0; }
files=$(git diff --name-only "$BASE" "$HEAD" 2>/dev/null) || { echo "code=true" >> "$output"; exit 0; }
if [ -z "$files" ]; then
  echo "code=true" >> "$output"
  exit 0
fi

code=false
while IFS= read -r file; do
  case "$file" in
    docs/*|*.md|LICENSE|.github/ISSUE_TEMPLATE/*) ;;
    *) code=true ;;
  esac
done <<< "$files"
echo "code=$code" >> "$output"
