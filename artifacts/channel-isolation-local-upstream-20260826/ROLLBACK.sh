#!/usr/bin/env bash
set -euo pipefail
if [ "$#" -ne 2 ]; then echo "usage: ROLLBACK.sh REPO_ROOT TARGET_DIR" >&2; exit 64; fi
repo_root="$1"; target_dir="$2"; commit="afdce3656999518205e9edab825dce49626317b4"
mkdir -p "$target_dir"
files=(
  AGENTS.md
  MK_GCASH_UPSTREAM.md
  mk_gcash_project_manifest.json
  payment_link_extractor/application.py
  payment_link_extractor/channels.py
  payment_link_extractor/paypal_channel.py
  payment_link_extractor/cli.py
  payment_link_extractor/config.py
  payment_link_extractor/mk_gcash.py
  payment_link_extractor/mk_gcash_open_source/sentinel.py
  payment_link_extractor/mk_gcash_open_source/sentinel_bridge.js
  payment_link_extractor/web/routes.py
  tests/test_mk_gcash_replacement.py
  tests/test_channel_isolation.py
)
for path in "${files[@]}"; do
  if git -C "$repo_root" cat-file -e "$commit:$path" 2>/dev/null; then
    mkdir -p "$target_dir/$(dirname "$path")"
    git -C "$repo_root" show "$commit:$path" > "$target_dir/$path"
  else
    rm -f "$target_dir/$path"
  fi
done
printf 'ROLLBACK_OK target=%s commit=%s files=%s\n' "$target_dir" "$commit" "${#files[@]}"
