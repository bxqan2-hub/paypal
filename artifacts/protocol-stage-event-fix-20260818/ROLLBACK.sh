#!/usr/bin/env bash
set -euo pipefail

TARGET="${1:?pass a target file path}"
REPO_ROOT="$(git rev-parse --show-toplevel)"
git -C "$REPO_ROOT" show 0c72c96f3e33d91782beae05cc3dcc46b7b6f285:paypal_agreement_protocol/web.py > "$TARGET"
printf 'ROLLBACK_RESTORED=%s\n' "$TARGET"
