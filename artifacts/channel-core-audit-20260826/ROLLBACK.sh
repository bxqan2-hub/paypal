#!/usr/bin/env bash
set -euo pipefail
if [ "$#" -ne 2 ]; then
  echo "usage: ROLLBACK.sh REPO_ROOT TARGET_DIR" >&2
  exit 64
fi
repo_root="$1"
target_dir="$2"
commit="ffd6db87678f6a21b566fd254d4cfed20d730e9c"
mkdir -p "$target_dir"
git -C "$repo_root" show "$commit:payment_link_extractor/mk_gopay.py" > "$target_dir/mk_gopay.py"
git -C "$repo_root" show "$commit:payment_link_extractor/mk_gcash.py" > "$target_dir/mk_gcash.py"
rm -f "$target_dir/upstream_contract.py"
echo "ROLLBACK_OK target=$target_dir commit=$commit files=mk_gopay.py,mk_gcash.py removed=upstream_contract.py"
