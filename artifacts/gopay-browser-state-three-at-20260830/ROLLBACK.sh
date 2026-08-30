#!/usr/bin/env bash
set -euo pipefail

target="${1:?usage: ROLLBACK.sh TARGET_COPY [SOURCE_REPOSITORY]}"
source_repo="${2:-C:/Users/Administrator/Desktop/提链}"
base_commit="b6e3c463a55b12aa0b2a47e32c6942353e3eda22"

tracked_files=(
  "payment_link_extractor/application.py"
  "payment_link_extractor/auth.py"
  "payment_link_extractor/gopay_checkout.py"
  "payment_link_extractor/gopay_cs_live.py"
  "payment_link_extractor/gopay_sentinel_playwright.py"
  "payment_link_extractor/gopay_transport.py"
  "payment_link_extractor/models.py"
  "payment_link_extractor/web/routes.py"
)

new_files=(
  "tests/test_gopay_browser_session_material.py"
  "docs/2026-08-30_gopay-browser-state-three-account-report.md"
)

for file in "${tracked_files[@]}"; do
  mkdir -p "$(dirname "$target/$file")"
  git -C "$source_repo" show "$base_commit:$file" > "$target/$file"
done

for file in "${new_files[@]}"; do
  rm -f "$target/$file"
done

expected_sha="$(git -C "$source_repo" show "$base_commit:payment_link_extractor/gopay_transport.py" | sha256sum | awk '{print $1}')"
restored_sha="$(sha256sum "$target/payment_link_extractor/gopay_transport.py" | awk '{print $1}')"
restored_sha="${restored_sha#\\}"

echo "ROLLBACK_TARGET=$target"
echo "RESTORED_TRACKED_FILES=${#tracked_files[@]}"
echo "REMOVED_NEW_FILES=${#new_files[@]}"
echo "RESTORED_STATUS=$([[ "$expected_sha" == "$restored_sha" ]] && echo PASS || echo FAIL)"
echo "RESTORED_SHA256=$restored_sha"
echo "EXPECTED_SHA256=$expected_sha"
echo "HASH_MATCH=$([[ "$expected_sha" == "$restored_sha" ]] && echo True || echo False)"

[[ "$expected_sha" == "$restored_sha" ]]
