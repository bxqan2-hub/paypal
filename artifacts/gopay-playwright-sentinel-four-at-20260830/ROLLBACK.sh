#!/usr/bin/env bash
set -euo pipefail

target="${1:?usage: ROLLBACK.sh TARGET_COPY [SOURCE_REPOSITORY]}"
source_repo="${2:-C:/Users/Administrator/Desktop/提链}"
base_commit="2f6b95d121ba540ba1b3c87363312c8a8f054b4c"

tracked_files=(
  ".env.example"
  ".gitignore"
  "payment_link_extractor/gopay_checkout.py"
  "payment_link_extractor/gopay_core.py"
  "payment_link_extractor/gopay_cs_live.py"
  "payment_link_extractor/gopay_stripe_common.py"
  "payment_link_extractor/gopay_transport.py"
  "payment_link_extractor/web/tasks.py"
  "tests/test_extraction_full_retry.py"
  "tests/test_gopay_isolated_optimization.py"
)

new_files=(
  "payment_link_extractor/gopay_sentinel_playwright.py"
  "tools/gopay_live_canary.py"
  "tests/test_gopay_live_canary.py"
  "tests/test_gopay_sentinel_playwright.py"
  "docs/2026-08-30_gopay-playwright-sentinel-four-account-report.md"
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
