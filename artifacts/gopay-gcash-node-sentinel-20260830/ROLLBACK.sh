#!/usr/bin/env bash
set -euo pipefail

target="${1:?usage: ROLLBACK.sh TARGET_COPY [SOURCE_REPOSITORY]}"
source_repo="${2:-C:/Users/Administrator/Desktop/提链}"
base_commit="b6e4663a9c11f6cbec6147ea30a7188a68a89688"

tracked_files=(
  ".env.example"
  ".gitignore"
  "payment_link_extractor/gopay_checkout.py"
  "payment_link_extractor/gopay_cs_live.py"
  "payment_link_extractor/gopay_transport.py"
  "tests/test_gopay_isolated_optimization.py"
  "tests/test_gopay_live_canary.py"
  "tools/gopay_live_canary.py"
)

new_files=(
  "payment_link_extractor/gopay_sentinel_node.py"
  "payment_link_extractor/gopay_sentinel_node_assets/sentinel_bridge.js"
  "payment_link_extractor/gopay_sentinel_node_assets/sentinel_assets/sentinel_bootstrap.js"
  "payment_link_extractor/gopay_sentinel_node_assets/sentinel_assets/sentinel_sdk.js"
  "tests/test_gopay_sentinel_node.py"
  "docs/2026-08-30_gopay-gcash-node-sentinel-live-report.md"
)

for file in "${tracked_files[@]}"; do
  mkdir -p "$(dirname "$target/$file")"
  git -C "$source_repo" show "$base_commit:$file" > "$target/$file"
done

for file in "${new_files[@]}"; do
  rm -f "$target/$file"
done

find "$target/payment_link_extractor/gopay_sentinel_node_assets" -depth -type d -empty -delete 2>/dev/null || true

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
