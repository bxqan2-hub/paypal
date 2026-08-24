#!/usr/bin/env bash
set -euo pipefail

TARGET="${1:?usage: ROLLBACK.sh <git-worktree> }"
BASELINE="6f8b8856b63c937c2a310feb38cca9112794dca2"
git -C "$TARGET" reset --hard "$BASELINE"
git -C "$TARGET" clean -fd
test "$(git -C "$TARGET" rev-parse HEAD)" = "$BASELINE"
echo "ROLLBACK_OK target=$TARGET restored_commit=$BASELINE"
