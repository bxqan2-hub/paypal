#!/usr/bin/env bash
set -euo pipefail

TARGET="${1:?usage: ROLLBACK.sh <git-worktree> }"
BASELINE="a77b90c5c96d5a6432281de2595da9b94a5c40b9"
git -C "$TARGET" reset --hard "$BASELINE"
git -C "$TARGET" clean -fd
test "$(git -C "$TARGET" rev-parse HEAD)" = "$BASELINE"
echo "ROLLBACK_OK target=$TARGET restored_commit=$BASELINE"
