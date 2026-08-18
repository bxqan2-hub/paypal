#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 6 ]]; then
  printf 'usage: %s REPOSITORY TARGET_APP_JS TARGET_INDEX_HTML TARGET_CSS TARGET_UI_TEST TARGET_OPT_TEST\n' "$0" >&2
  exit 64
fi

repository="$1"
target_app_js="$2"
target_index_html="$3"
target_css="$4"
target_ui_test="$5"
target_opt_test="$6"
base_commit="11c264610a13b96476aaf74f999f50a6ebe94753"

restore_blob() {
  local source_path="$1"
  local target_path="$2"
  git -C "$repository" show "$base_commit:$source_path" > "$target_path"
}

restore_blob 'paypal_agreement_protocol/web_static/app.js' "$target_app_js"
restore_blob 'paypal_agreement_protocol/web_static/index.html' "$target_index_html"
restore_blob 'paypal_agreement_protocol/web_static/checkout-preview.css' "$target_css"
restore_blob 'tests/test_ui_button_contracts.py' "$target_ui_test"
restore_blob 'tests/test_optimization_features.py' "$target_opt_test"

printf 'ROLLBACK_BASE=%s\n' "$base_commit"
printf 'RESTORED_APP_JS_SHA256=%s\n' "$(sha256sum "$target_app_js" | awk '{print toupper($1)}')"
printf 'RESTORED_INDEX_HTML_SHA256=%s\n' "$(sha256sum "$target_index_html" | awk '{print toupper($1)}')"
printf 'RESTORED_CSS_SHA256=%s\n' "$(sha256sum "$target_css" | awk '{print toupper($1)}')"
printf 'RESTORED_UI_TEST_SHA256=%s\n' "$(sha256sum "$target_ui_test" | awk '{print toupper($1)}')"
printf 'RESTORED_OPT_TEST_SHA256=%s\n' "$(sha256sum "$target_opt_test" | awk '{print toupper($1)}')"
printf 'RESTORED_BEHAVIOR=single current row, positional fallback, stale phone retention, and no manual clear button\n'
printf 'ROLLBACK_STATUS=restored\n'
