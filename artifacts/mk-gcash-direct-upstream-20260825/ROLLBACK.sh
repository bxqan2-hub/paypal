#!/usr/bin/env bash
set -euo pipefail

TARGET="${1:?usage: ROLLBACK.sh <git-worktree> }"
BASELINE="62996ad2fd01162b4701b82fa371baa4e5d0bad4"
git -C "$TARGET" reset --hard "$BASELINE"
git -C "$TARGET" clean -fd
test "$(git -C "$TARGET" rev-parse HEAD)" = "$BASELINE"
echo "ROLLBACK_OK target=$TARGET restored_commit=$BASELINE"
