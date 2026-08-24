#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${1:-$(cd -- "$SCRIPT_DIR/../.." && pwd)}"
PATCH="$SCRIPT_DIR/DIFF_FILE.patch"

git -C "$TARGET" apply --check -R --whitespace=nowarn "$PATCH"
git -C "$TARGET" apply -R --whitespace=nowarn "$PATCH"
printf 'ROLLBACK_OK target=%s restored_commit=d770a7404a946bcd250cb05406284415055b4c22\n' "$TARGET"