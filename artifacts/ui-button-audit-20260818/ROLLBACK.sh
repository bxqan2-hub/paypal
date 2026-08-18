#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 4 ]]; then
  printf 'usage: %s REPOSITORY TARGET_EXTRACTOR_JS TARGET_EXTRACTOR_HTML TARGET_PAYPAL_HTML\n' "$0" >&2
  exit 64
fi

repository="$1"
target_extractor_js="$2"
target_extractor_html="$3"
target_paypal_html="$4"
base_commit="b3dee28b9832aa6e030f5060530ade98080fc734"

git -C "$repository" show "$base_commit:payment_link_extractor/web/static/app.js" | sed 's/$/\r/' > "$target_extractor_js"
git -C "$repository" show "$base_commit:payment_link_extractor/web/templates/index.html" | sed 's/$/\r/' > "$target_extractor_html"
git -C "$repository" show "$base_commit:paypal_agreement_protocol/web_static/index.html" | sed 's/$/\r/' > "$target_paypal_html"

printf 'ROLLBACK_BASE=%s\n' "$base_commit"
printf 'RESTORED_EXTRACTOR_JS_SHA256=%s\n' "$(sha256sum "$target_extractor_js" | awk '{print toupper($1)}')"
printf 'RESTORED_EXTRACTOR_HTML_SHA256=%s\n' "$(sha256sum "$target_extractor_html" | awk '{print toupper($1)}')"
printf 'RESTORED_PAYPAL_HTML_SHA256=%s\n' "$(sha256sum "$target_paypal_html" | awk '{print toupper($1)}')"
printf 'RESTORED_BEHAVIOR=baseline button labels, implicit types, logout visibility, and empty-batch feedback\n'
printf 'ROLLBACK_STATUS=restored\n'
