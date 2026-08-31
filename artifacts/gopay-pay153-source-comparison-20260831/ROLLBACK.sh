#!/usr/bin/env bash
set -euo pipefail

TARGET="${1:?usage: ROLLBACK.sh TARGET_COPY SOURCE_REPO}"
SOURCE_REPO="${2:?usage: ROLLBACK.sh TARGET_COPY SOURCE_REPO}"
BASELINE="450994c99840ee039cf20b563a9e2e4661547ed4"

tracked_files=(
  "payment_link_extractor/auth.py"
  "payment_link_extractor/gopay_checkout.py"
  "payment_link_extractor/gopay_cs_live.py"
  "payment_link_extractor/gopay_sentinel_playwright.py"
  "payment_link_extractor/gopay_transport.py"
  "tests/test_gopay_browser_session_material.py"
  "tests/test_gopay_isolated_optimization.py"
  "tests/test_gopay_live_canary.py"
  "tests/test_gopay_sentinel_playwright.py"
  "tools/gopay_live_canary.py"
)
new_files=(
  "docs/2026-08-31_gopay-pay153-source-comparison.md"
)

key_file="payment_link_extractor/gopay_sentinel_playwright.py"
before_sha256="$(sha256sum "$TARGET/$key_file" | awk '{print $1}')"
for path in "${tracked_files[@]}"; do
  mkdir -p "$(dirname "$TARGET/$path")"
  git -C "$SOURCE_REPO" show "$BASELINE:$path" > "$TARGET/$path"
done
for path in "${new_files[@]}"; do rm -f "$TARGET/$path"; done

restored_status="PASS"
for path in "${tracked_files[@]}"; do
  expected="$(git -C "$SOURCE_REPO" show "$BASELINE:$path" | sha256sum | awk '{print $1}')"
  actual="$(sha256sum "$TARGET/$path" | awk '{print $1}')"
  if [[ "$actual" != "$expected" ]]; then restored_status="FAIL"; fi
done
for path in "${new_files[@]}"; do
  if [[ -e "$TARGET/$path" ]]; then restored_status="FAIL"; fi
done

restored_sha256="$(sha256sum "$TARGET/$key_file" | awk '{print $1}')"
expected_sha256="$(git -C "$SOURCE_REPO" show "$BASELINE:$key_file" | sha256sum | awk '{print $1}')"
hash_match="False"
if [[ "$restored_sha256" == "$expected_sha256" ]]; then hash_match="True"; fi

printf 'ROLLBACK_TARGET=%s
' "$TARGET"
printf 'RESTORED_TRACKED_FILES=%s
' "${#tracked_files[@]}"
printf 'REMOVED_NEW_FILES=%s
' "${#new_files[@]}"
printf 'RESTORED_STATUS=%s
' "$restored_status"
printf 'BEFORE_SHA256=%s
' "$before_sha256"
printf 'RESTORED_SHA256=%s
' "$restored_sha256"
printf 'EXPECTED_SHA256=%s
' "$expected_sha256"
printf 'HASH_MATCH=%s
' "$hash_match"

[[ "$restored_status" == "PASS" && "$hash_match" == "True" ]]
