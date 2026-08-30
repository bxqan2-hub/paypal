#!/usr/bin/env bash
set -euo pipefail

target_root="${1:?target root is required}"
source_dir="${2:?source snapshot directory is required}"
files=(
  "payment_link_extractor/gopay_core.py"
  "payment_link_extractor/web/tasks.py"
  "payment_link_extractor/web/static/app.js"
  "tests/test_gopay_isolated_optimization.py"
  "docs/2026-08-30_gopay-missing-parameter-fix.md"
)
for rel in "${files[@]}"; do
  mkdir -p "$target_root/$(dirname "$rel")"
  cp -- "$source_dir/$rel" "$target_root/$rel"
done
printf 'ROLLBACK_TARGET=%s\n' "$target_root"
printf 'RESTORED_FILES=%s\n' "${#files[@]}"
printf 'RESTORED_STATUS=PASS\n'
