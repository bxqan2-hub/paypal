#!/usr/bin/env bash
set -euo pipefail

TARGET="${1:?pass a target file path}"
REPO_ROOT="$(git rev-parse --show-toplevel)"
git -C "$REPO_ROOT" show a1a76fbd5f85df6840824194181e00ba9bb0885d:paypal_agreement_protocol/web.py > "$TARGET"
printf 'ROLLBACK_RESTORED=%s\n' "$TARGET"
