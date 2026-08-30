#!/usr/bin/env bash
set -euo pipefail

TARGET="${1:?usage: ROLLBACK.sh TARGET_COPY SOURCE_REPO}"
SOURCE_REPO="${2:?usage: ROLLBACK.sh TARGET_COPY SOURCE_REPO}"
BASELINE="102437a"
tracked_files=(
  "payment_link_extractor/gopay_cs_live.py"
  "tests/test_gopay_isolated_optimization.py"
  "docs/2026-08-30_gopay-approve-har-alignment-report.md"
)
key_file="payment_link_extractor/gopay_cs_live.py"
before_sha256="$(sha256sum "$TARGET/$key_file" | awk '{print $1}' | sed 's/^\\//')"
for path in "${tracked_files[@]}"; do
  mkdir -p "$(dirname "$TARGET/$path")"
  git -C "$SOURCE_REPO" show "$BASELINE:$path" > "$TARGET/$path"
done
restored_status="PASS"
for path in "${tracked_files[@]}"; do
  expected="$(git -C "$SOURCE_REPO" show "$BASELINE:$path" | sha256sum | awk '{print $1}')"
  actual="$(sha256sum "$TARGET/$path" | awk '{print $1}' | sed 's/^\\//')"
  if [[ "$actual" != "$expected" ]]; then restored_status="FAIL"; fi
done
restored_sha256="$(sha256sum "$TARGET/$key_file" | awk '{print $1}' | sed 's/^\\//')"
expected_sha256="$(git -C "$SOURCE_REPO" show "$BASELINE:$key_file" | sha256sum | awk '{print $1}')"
hash_match="False"
if [[ "$restored_sha256" == "$expected_sha256" ]]; then hash_match="True"; fi
printf 'ROLLBACK_TARGET=%s\n' "$TARGET"
printf 'RESTORED_TRACKED_FILES=%s\n' "${#tracked_files[@]}"
printf 'REMOVED_NEW_FILES=0\n'
printf 'RESTORED_STATUS=%s\n' "$restored_status"
printf 'BEFORE_SHA256=%s\n' "$before_sha256"
printf 'RESTORED_SHA256=%s\n' "$restored_sha256"
printf 'EXPECTED_SHA256=%s\n' "$expected_sha256"
printf 'HASH_MATCH=%s\n' "$hash_match"
[[ "$restored_status" == "PASS" && "$hash_match" == "True" ]]
