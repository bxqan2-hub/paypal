#!/usr/bin/env bash
set -euo pipefail
TARGET_DIR="${1:?usage: ROLLBACK.sh <modified-worktree> [baseline-commit]}"
BASELINE_COMMIT="${2:-833d182}"
git -C "$TARGET_DIR" reset --hard "$BASELINE_COMMIT"
git -C "$TARGET_DIR" clean -fd
echo "ROLLBACK_RESTORED=$TARGET_DIR@$BASELINE_COMMIT"
