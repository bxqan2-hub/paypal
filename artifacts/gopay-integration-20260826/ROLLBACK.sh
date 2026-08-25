#!/usr/bin/env sh
set -eu

REPO="${1:?repository root required}"
TARGET="${2:?target copy required}"
BASELINE_COMMIT="2f2e77ff20b4628f276d4c441c0d46bad18c02af"
SOURCE_PATH="payment_link_extractor/application.py"

git -C "$REPO" show "$BASELINE_COMMIT:$SOURCE_PATH" > "$TARGET"
if git -C "$REPO" show "$BASELINE_COMMIT:$SOURCE_PATH" | cmp -s - "$TARGET"; then
  printf 'ROLLBACK_OK target=%s commit=%s path=%s\n' "$TARGET" "$BASELINE_COMMIT" "$SOURCE_PATH"
else
  printf 'ROLLBACK_FAILED target=%s commit=%s path=%s\n' "$TARGET" "$BASELINE_COMMIT" "$SOURCE_PATH" >&2
  exit 1
fi
