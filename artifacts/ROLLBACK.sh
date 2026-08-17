#!/usr/bin/env bash
set -euo pipefail
TARGET_COMMIT="${1:-HEAD^}"
git restore --source "$TARGET_COMMIT" -- START.bat STOP.bat payment_link_extractor/web/app.py payment_link_extractor/web/static/app.js payment_link_extractor/web/static/styles.css payment_link_extractor/web/templates/index.html requirements.txt payment_link_extractor/web/paypal_protocol.py paypal_agreement_protocol .env.example
printf 'restored_to=%s\n' "$TARGET_COMMIT"
