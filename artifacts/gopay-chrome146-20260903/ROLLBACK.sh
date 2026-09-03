#!/usr/bin/env sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
TARGET=${1:?TARGET_WORKTREE_REQUIRED}
git -C "$TARGET" apply --reverse --whitespace=nowarn "$SCRIPT_DIR/DIFF_FILE"
printf 'ROLLBACK_APPLIED=%s\n' "$TARGET"
