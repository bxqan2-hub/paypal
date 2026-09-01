#!/usr/bin/env bash
set -euo pipefail

target="${1:?rollback target directory is required}"
repo="${2:?repository root is required}"
baseline="${3:-e21e568}"
mkdir -p "$target"

files=(
  "payment_link_extractor/momo_transport.py"
  "payment_link_extractor/momo_checkout.py"
  "payment_link_extractor/momo_stripe.py"
  "payment_link_extractor/transport.py"
  "payment_link_extractor/web/tasks.py"
  "tests/test_momo_support.py"
  "tests/test_extraction_full_retry.py"
  "docs/MOMO_HAR_STATE_MACHINE.md"
)
for rel in "${files[@]}"; do
  mkdir -p "$target/$(dirname "$rel")"
  git -C "$repo" show "${baseline}:${rel}" > "$target/$rel"
done

printf 'ROLLBACK_TARGET=%s\n' "$target"
printf 'RESTORED_STATUS=PASS\n'
printf 'BASELINE_COMMIT=%s\n' "$baseline"
for rel in "${files[@]}"; do
  printf '%s_SHA256=' "$rel"
  sha256sum "$target/$rel" | awk '{print $1}'
done
