#!/usr/bin/env bash
set -euo pipefail
TARGET="${1:?usage: ROLLBACK.sh TARGET_COPY SOURCE_REPO}"
SOURCE_REPO="${2:?usage: ROLLBACK.sh TARGET_COPY SOURCE_REPO}"
BASELINE="edbaba151a58a95d36017b1491f85470de1c7aa3"
tracked_files=(
  "payment_link_extractor/gopay_cs_live.py"
  "payment_link_extractor/gopay_sentinel_playwright.py"
  "payment_link_extractor/gopay_transport.py"
  "tests/test_gopay_isolated_optimization.py"
  "tests/test_gopay_sentinel_playwright.py"
)
new_files=("docs/2026-08-31_gopay-three-at-retest-navigation-fix.md")
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
  [[ "$actual" == "$expected" ]] || restored_status="FAIL"
done
for path in "${new_files[@]}"; do [[ ! -e "$TARGET/$path" ]] || restored_status="FAIL"; done
restored_sha256="$(sha256sum "$TARGET/$key_file" | awk '{print $1}')"
expected_sha256="$(git -C "$SOURCE_REPO" show "$BASELINE:$key_file" | sha256sum | awk '{print $1}')"
hash_match="False"; [[ "$restored_sha256" == "$expected_sha256" ]] && hash_match="True"
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
