#!/usr/bin/env bash
set -euo pipefail
TARGET_COMMIT="${1:-HEAD^}"
FILES=(
  payment_link_extractor/web/paypal_protocol.py
  payment_link_extractor/web/static/app.js
  payment_link_extractor/web/templates/index.html
  paypal_agreement_protocol/herosms.py
  paypal_agreement_protocol/web.py
  paypal_agreement_protocol/web_static/app.js
  paypal_agreement_protocol/web_static/index.html
  tests/test_protocol_regressions.py
)
for file in "${FILES[@]}"; do
  if git cat-file -e "${TARGET_COMMIT}:${file}" 2>/dev/null; then
    git checkout "${TARGET_COMMIT}" -- "$file"
  else
    rm -f -- "$file"
  fi
done
printf 'rollback_target=%s\n' "$TARGET_COMMIT"
printf 'restored_behavior=terminal-log guard removed; bounded current-phone retry removed; failed-job phone replacement removed; bulk BA push removed\n'
git status --short -- "${FILES[@]}"
