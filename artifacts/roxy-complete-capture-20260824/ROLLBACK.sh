#!/usr/bin/env bash
set -euo pipefail
TARGET_DIR="${1:?usage: ROLLBACK.sh <worktree> [baseline-commit]}"
BASELINE_COMMIT="${2:-8295d96d40df610c8ef3e6ceb624e7c3a789b9f3}"
FILES=(
  HAR_TOOLS.md
  ROXY_CAPTURE_START.bat
  tests/test_har_tools.py
  tests/test_roxy_har_capture.py
  tools/har_capture.py
  tools/roxy_har_capture.py
)
git -C "$TARGET_DIR" restore --source "$BASELINE_COMMIT" --staged --worktree -- "${FILES[@]}"
echo "ROLLBACK_RESTORED=$TARGET_DIR@$BASELINE_COMMIT"
