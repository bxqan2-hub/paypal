#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 4 ]]; then
  printf 'usage: %s REPOSITORY TARGET_APP_JS TARGET_UI_TEST TARGET_REGRESSION_TEST\n' "$0" >&2
  exit 64
fi

repository="$1"
target_app_js="$2"
target_ui_test="$3"
target_regression_test="$4"
base_commit="635892f25e817fd93443e9c848245f02cbe44dcb"

git -C "$repository" show "$base_commit:paypal_agreement_protocol/web_static/app.js" > "$target_app_js"
git -C "$repository" show "$base_commit:tests/test_ui_button_contracts.py" > "$target_ui_test"
git -C "$repository" show "$base_commit:tests/test_protocol_regressions.py" > "$target_regression_test"

printf 'ROLLBACK_BASE=%s\n' "$base_commit"
printf 'RESTORED_APP_JS_SHA256=%s\n' "$(sha256sum "$target_app_js" | awk '{print toupper($1)}')"
printf 'RESTORED_UI_TEST_SHA256=%s\n' "$(sha256sum "$target_ui_test" | awk '{print toupper($1)}')"
printf 'RESTORED_REGRESSION_TEST_SHA256=%s\n' "$(sha256sum "$target_regression_test" | awk '{print toupper($1)}')"
printf 'RESTORED_BEHAVIOR=completed rows can enter terminal-number replacement and expose get-new-number action\n'
printf 'ROLLBACK_STATUS=restored\n'
