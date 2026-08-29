#!/usr/bin/env bash
set -euo pipefail

repo_root="${1:?repo root required}"
target_dir="${2:?target directory required}"
mkdir -p "$target_dir"
git -C "$repo_root" show 660fae9:payment_link_extractor/gopay_pro.py > "$target_dir/gopay_pro.py"
git -C "$repo_root" show 660fae9:payment_link_extractor/channels.py > "$target_dir/channels.py"
git -C "$repo_root" show 660fae9:payment_link_extractor/transport.py > "$target_dir/transport.py"
grep -F 'GOPAY_PRO_PROJECT_DIR' "$target_dir/gopay_pro.py" >/dev/null
printf 'ROLLBACK_OK target=%s restored_commit=660fae9 previous_direct-copy-state=true status=0\n' "$target_dir"
