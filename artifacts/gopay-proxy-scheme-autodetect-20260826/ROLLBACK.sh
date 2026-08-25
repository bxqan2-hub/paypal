#!/usr/bin/env bash
set -euo pipefail
if [ "$#" -ne 2 ]; then echo "usage: ROLLBACK.sh REPO_ROOT TARGET_DIR" >&2; exit 64; fi
repo_root="$1"; target_dir="$2"; commit="3e44110ee1d3a262b731a95464e8162814199c3a"
mkdir -p "$target_dir/payment_link_extractor" "$target_dir/tests"
git -C "$repo_root" show "$commit:payment_link_extractor/mk_gopay.py" > "$target_dir/payment_link_extractor/mk_gopay.py"
git -C "$repo_root" show "$commit:tests/test_gopay_support.py" > "$target_dir/tests/test_gopay_support.py"
printf 'ROLLBACK_OK target=%s commit=%s files=mk_gopay.py,test_gopay_support.py\n' "$target_dir" "$commit"
