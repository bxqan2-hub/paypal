#!/usr/bin/env bash
set -euo pipefail
BASE_COMMIT="261a81b3e4a8dfb2cb5069c8cd70dba4b37e8e12"
if [[ "${1:-}" != "--fixture" ]]; then
  printf 'FIXTURE_ONLY BASE_COMMIT=%s\n' "$BASE_COMMIT"
  exit 0
fi
target="${2:?fixture target is required}"
cat > "$target" <<EOF
BASE_COMMIT=$BASE_COMMIT
STATE=baseline-no-auto-retry
MAX_AUTO_RETRIES=0
ATTEMPTS_ON_FAILURE=1
EOF
printf 'ROLLED_BACK_FIXTURE %s\n' "$target"
