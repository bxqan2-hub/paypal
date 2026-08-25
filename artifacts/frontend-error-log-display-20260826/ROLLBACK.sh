#!/usr/bin/env bash
set -euo pipefail
if [ "$#" -ne 2 ]; then echo "usage: ROLLBACK.sh REPO_ROOT TARGET_DIR" >&2; exit 64; fi
repo_root="$1"; target_dir="$2"; commit="c0da4682b6c445d6be417bfa903d4c0d8d27a0da"
mkdir -p "$target_dir/payment_link_extractor/web/static" "$target_dir/tests"
git -C "$repo_root" show "$commit:payment_link_extractor/web/static/app.js" > "$target_dir/payment_link_extractor/web/static/app.js"
git -C "$repo_root" show "$commit:payment_link_extractor/web/static/styles.css" > "$target_dir/payment_link_extractor/web/static/styles.css"
rm -f "$target_dir/tests/test_frontend_error_display.py"
printf 'ROLLBACK_OK target=%s commit=%s files=app.js,styles.css removed=test_frontend_error_display.py\n' "$target_dir" "$commit"
