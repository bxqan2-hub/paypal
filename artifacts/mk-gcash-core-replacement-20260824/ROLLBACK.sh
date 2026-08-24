#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${1:-$(cd -- "$SCRIPT_DIR/../.." && pwd)}"
PATCH="$SCRIPT_DIR/DIFF_FILE.patch"

git -C "$TARGET" apply --check -R --whitespace=nowarn "$PATCH"
git -C "$TARGET" apply -R --whitespace=nowarn "$PATCH"
printf 'ROLLBACK_OK target=%s restored_commit=7435d6d560fe37172b77e964b823c32dd7629af9\n' "$TARGET"