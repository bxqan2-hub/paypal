#!/usr/bin/env bash
set -euo pipefail

repo_root="${1:?repo root required}"
target_file="${2:?target fixture required}"
mkdir -p "$(dirname "$target_file")"
printf '%s\n' \
  'BRANCH: payment_link_extractor/channels.py' \
  'FIELD: gopay.adapter_module' \
  'VALUE: payment_link_extractor.mk_gopay' \
  'FIELD: gopay.adapter_callable' \
  'VALUE: extract_mk_gopay_payment_link' \
  'FIELD: gopay.uses_legacy_transport' \
  'VALUE: false' \
  'FIELD: gopay.uses_checkout_update' \
  'VALUE: false' > "$target_file"
printf 'ROLLBACK_OK target=%s restored=pre_shared-core-gopay-registration status=0\n' "$target_file"
