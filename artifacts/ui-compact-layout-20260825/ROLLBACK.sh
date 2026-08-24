#!/usr/bin/env bash
set -euo pipefail

TARGET="${1:?usage: ROLLBACK.sh <git-worktree> }"
BASELINE="4265dea9b7fcefcb729ab3cb4c48ac4752e9386a"
git -C "$TARGET" reset --hard "$BASELINE"
git -C "$TARGET" clean -fd
test "$(git -C "$TARGET" rev-parse HEAD)" = "$BASELINE"
echo "ROLLBACK_OK target=$TARGET restored_commit=$BASELINE"
