#!/usr/bin/env bash
set -euo pipefail

TARGET="${1:?usage: ROLLBACK.sh TARGET [BASELINE]}"
BASELINE="${2:-}"
if [[ -n "$BASELINE" ]]; then
  cp -- "$BASELINE" "$TARGET"
  RESULT="copied baseline"
else
  git restore --source=HEAD -- "$TARGET"
  RESULT="restored from HEAD"
fi
printf 'ROLLBACK_TARGET=%s\nROLLBACK_RESULT=%s\n' "$TARGET" "$RESULT"
