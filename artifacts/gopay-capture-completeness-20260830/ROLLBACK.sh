#!/usr/bin/env bash
set -euo pipefail
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
SOURCE="${1:-$ROOT/MODIFIED_FILE}"
COPY="${2:-$ROOT/rollback-test-copy}"
cp -- "$SOURCE" "$COPY"
before="$(sha256sum "$COPY" | awk '{print toupper($1)}')"
printf '\n# rollback probe\n' >> "$COPY"
changed="$(sha256sum "$COPY" | awk '{print toupper($1)}')"
cp -- "$SOURCE" "$COPY"
after="$(sha256sum "$COPY" | awk '{print toupper($1)}')"
rm -f -- "$COPY"
[ "$before" = "$after" ]
[ "$before" != "$changed" ]
printf 'ROLLBACK_BEFORE=%s\nROLLBACK_CHANGED=%s\nROLLBACK_RESTORED=%s\n' "$before" "$changed" "$after"