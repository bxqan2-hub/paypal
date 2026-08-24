#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${1:-$(cd -- "$SCRIPT_DIR/../.." && pwd)}"
PATCH="$SCRIPT_DIR/DIFF_FILE.patch"

git -C "$TARGET" apply --check -R --whitespace=nowarn "$PATCH"
git -C "$TARGET" apply -R --whitespace=nowarn "$PATCH"
printf 'ROLLBACK_OK target=%s restored_commit=f0d8388c9b39eb97fec76a0ec7475975bcc06b9b\n' "$TARGET"