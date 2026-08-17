#!/usr/bin/env bash
set -euo pipefail
BASE_COMMIT="ab677c2"
if [[ "${1:-}" != "--fixture" ]]; then
  printf "FIXTURE_ONLY BASE_COMMIT=%s\n" "$BASE_COMMIT"
  exit 0
fi
target="${2:?fixture target is required}"
cat > "$target" <<EOF
BASE_COMMIT=$BASE_COMMIT
STATE=baseline-session-persistence
PERSISTENT_LAST_BA=legacy
STALE_QUEUE_ON_PROCESS_RESTART=present
CURRENT_SESSION_FORM=legacy
BUYER_DEFAULT=original
EOF
printf "ROLLED_BACK_FIXTURE %s\n" "$target"
