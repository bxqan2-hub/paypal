#!/usr/bin/env bash
set -euo pipefail
TARGET="${1:?pass a target file path}"
REPO_ROOT="$(git rev-parse --show-toplevel)"
git -C "$REPO_ROOT" show 5a721151e7217c529292e8768c675dfb620f4f30:payment_link_extractor/web/events.py > "$TARGET"
printf 'ROLLBACK_RESTORED=%s\n' "$TARGET"
