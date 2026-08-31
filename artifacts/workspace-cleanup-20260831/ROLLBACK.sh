#!/usr/bin/env bash
set -euo pipefail
TARGET="$1"
SOURCE_REPO="$2"
BASELINE="6b1a8cc80f01d632c2da47fb259f813a95e6ab6a"
tracked_files=".env.example payment_link_extractor/gopay_sentinel_playwright.py payment_link_extractor/sentinel_assets/sentinel_sdk.js tests/test_gopay_sentinel_playwright.py"
new_files="docs/2026-08-31_workspace-cleanup-report.md"
key_file="payment_link_extractor/gopay_sentinel_playwright.py"
before_sha256="$(sha256sum "$TARGET/$key_file" | awk '{print $1}')"
for path in $tracked_files; do
  mkdir -p "$(dirname "$TARGET/$path")"
  git -C "$SOURCE_REPO" show "$BASELINE:$path" > "$TARGET/$path"
done
for path in $new_files; do rm -f "$TARGET/$path"; done
restored_status="PASS"
for path in $tracked_files; do
  expected="$(git -C "$SOURCE_REPO" show "$BASELINE:$path" | sha256sum | awk '{print $1}')"
  actual="$(sha256sum "$TARGET/$path" | awk '{print $1}')"
  [ "$actual" = "$expected" ] || restored_status="FAIL"
done
for path in $new_files; do [ ! -e "$TARGET/$path" ] || restored_status="FAIL"; done
restored_sha256="$(sha256sum "$TARGET/$key_file" | awk '{print $1}')"
expected_sha256="$(git -C "$SOURCE_REPO" show "$BASELINE:$key_file" | sha256sum | awk '{print $1}')"
hash_match="False"
[ "$restored_sha256" = "$expected_sha256" ] && hash_match="True"
printf 'ROLLBACK_TARGET=%s\n' "$TARGET"
printf 'RESTORED_TRACKED_FILES=%s\n' "$(echo "$tracked_files" | wc -w)"
printf 'REMOVED_NEW_FILES=%s\n' "$(echo "$new_files" | wc -w)"
printf 'RESTORED_STATUS=%s\n' "$restored_status"
printf 'BEFORE_SHA256=%s\n' "$before_sha256"
printf 'RESTORED_SHA256=%s\n' "$restored_sha256"
printf 'EXPECTED_SHA256=%s\n' "$expected_sha256"
printf 'HASH_MATCH=%s\n' "$hash_match"
[ "$restored_status" = "PASS" ] && [ "$hash_match" = "True" ]
