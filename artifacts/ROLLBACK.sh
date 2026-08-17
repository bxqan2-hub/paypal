#!/usr/bin/env bash
set -euo pipefail
# Restores protocol UI, SMS bridge, extractor billing-country UI/routes, formal entry files, and proxy transport files.
TARGET_COMMIT="${1:-HEAD~1}"
FILES=(
  paypal_agreement_protocol/web_static/app.js
  paypal_agreement_protocol/web_static/checkout-preview.css
  paypal_agreement_protocol/web_static/index.html
  payment_link_extractor/web/templates/index.html
  payment_link_extractor/web/paypal_protocol.py
  payment_link_extractor/web/routes.py
  payment_link_extractor/web/static/app.js
  payment_link_extractor/transport.py
  payment_link_extractor/web/proxy_probe.py
  payment_link_extractor/web/routes.py
  payment_link_extractor/web/templates/index.html
  payment_link_extractor/web/static/app.js
  payment_link_extractor/web/static/styles.css
  paypal_agreement_protocol/herosms.py
  payment_link_extractor/web/static/app.js
  paypal_agreement_protocol/web_static/app.js
  .env.example
)
git restore --source "$TARGET_COMMIT" -- "${FILES[@]}"
printf 'restored_to=%s\n' "$TARGET_COMMIT"
