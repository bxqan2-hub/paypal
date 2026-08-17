#!/usr/bin/env bash
set -euo pipefail

BASE_COMMIT="92fe8ee"

if [[ "${1:-}" == "--fixture" ]]; then
  target="${2:?fixture target is required}"
  cat > "$target" <<'EOF'
BASE_COMMIT=92fe8ee
STATE=baseline
COUNTRIES=12
DYNAMIC_FIELD_SCHEMAS=12
BUYER_DEFAULT=original
LOCAL_TESTS=3_passed
REFERENCE_PROTOCOL_TESTS=5_passed_12_failed
EOF
  printf 'ROLLED_BACK_FIXTURE %s\n' "$target"
  exit 0
fi

git restore --source="$BASE_COMMIT" -- \
  .env.example \
  iprocket_chain_bridge.py \
  payment_link_extractor/web/app.py \
  payment_link_extractor/web/events.py \
  payment_link_extractor/web/paypal_protocol.py \
  payment_link_extractor/web/routes.py \
  payment_link_extractor/web/tasks.py \
  paypal_agreement_protocol/data/country_discovery/country_field_catalog.json \
  paypal_agreement_protocol/data/paypal_supported_countries.json \
  paypal_agreement_protocol/paypal/flow.py \
  paypal_agreement_protocol/paypal/manual_browser.py \
  paypal_agreement_protocol/paypal/proxy.py \
  paypal_agreement_protocol/web.py \
  paypal_agreement_protocol/web_static/index.html

git rm -f --ignore-unmatch \
  oai_iprocket_chain_bridge.py \
  tests/conftest.py \
  tests/test_optimization_features.py \
  tests/reference_protocol/*.py

printf 'RESTORED_SOURCE %s\n' "$BASE_COMMIT"
