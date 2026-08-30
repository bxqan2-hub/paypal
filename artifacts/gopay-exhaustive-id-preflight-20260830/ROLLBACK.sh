#!/usr/bin/env bash
set -euo pipefail

target_root="${1:?target root is required}"
diff_file="${2:?diff file is required}"
files=(
  "payment_link_extractor/gopay_core.py"
  "payment_link_extractor/gopay_checkout.py"
  "payment_link_extractor/web/tasks.py"
  "tests/test_extraction_full_retry.py"
  "tests/test_gopay_isolated_optimization.py"
  "docs/2026-08-30_gopay-missing-parameter-fix.md"
)
cd "$target_root"
patch --batch --silent -p1 -R -i "$diff_file"
# Git blobs in this repository use LF; normalize the independent rollback copy
# so byte hashes match the preserved HEAD baseline exactly on Windows too.
for rel in "${files[@]}"; do
  sed -i 's/\r$//' "$rel"
done
printf 'ROLLBACK_TARGET=%s\n' "$target_root"
printf 'RESTORED_FILES=%s\n' "${#files[@]}"
printf 'RESTORED_STATUS=PASS\n'
