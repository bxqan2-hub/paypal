#!/usr/bin/env bash
set -euo pipefail
TARGET_COMMIT="${1:-HEAD~1}"
FILES=(
  paypal_agreement_protocol/web_static/app.js
  paypal_agreement_protocol/web_static/checkout-preview.css
  paypal_agreement_protocol/web_static/index.html
)
git restore --source "$TARGET_COMMIT" -- "${FILES[@]}"
printf 'restored_to=%s\n' "$TARGET_COMMIT"
