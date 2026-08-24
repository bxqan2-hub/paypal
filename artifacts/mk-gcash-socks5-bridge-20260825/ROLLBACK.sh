#!/usr/bin/env bash
set -euo pipefail

TARGET="${1:?usage: ROLLBACK.sh <git-worktree> }"
BASELINE="965cb29a6d5f6e8c1918e7fe479ee928e90f0266"
git -C "$TARGET" reset --hard "$BASELINE"
git -C "$TARGET" clean -fd
test "$(git -C "$TARGET" rev-parse HEAD)" = "$BASELINE"
echo "ROLLBACK_OK target=$TARGET restored_commit=$BASELINE"
