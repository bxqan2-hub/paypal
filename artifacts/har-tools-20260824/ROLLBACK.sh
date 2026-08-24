#!/usr/bin/env bash
set -euo pipefail
TARGET="${1:-.}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
git -C "$TARGET" apply --reverse --whitespace=nowarn "$SCRIPT_DIR/DIFF_FILE.patch"
