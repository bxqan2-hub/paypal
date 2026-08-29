#!/usr/bin/env bash
set -euo pipefail

repo_root="${1:?repo root required}"
target_dir="${2:?target directory required}"
mkdir -p "$target_dir"
git -C "$repo_root" show c92b434:payment_link_extractor/channels.py > "$target_dir/channels.py"
git -C "$repo_root" show c92b434:payment_link_extractor/web/templates/index.html > "$target_dir/index.html"
git -C "$repo_root" show c92b434:payment_link_extractor/web/static/app.js > "$target_dir/app.js"
rm -rf "$target_dir/gopay_pro_core" "$target_dir/gopay_pro.py" "$target_dir/gopay_pro_project_manifest.json"
grep -F '"gopay_pro"' "$target_dir/channels.py" >/dev/null && exit 1 || true
printf 'ROLLBACK_OK target=%s restored_commit=c92b434 gopay_pro_removed=true status=0\n' "$target_dir"
