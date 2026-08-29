#!/usr/bin/env bash
set -euo pipefail

repo_root="${1:?repo root required}"
target_file="${2:?target fixture required}"
mkdir -p "$(dirname "$target_file")"
git -C "$repo_root" show bca6458:payment_link_extractor/config.py > "$target_file"
grep -F 'STRIPE_RUNTIME_VERSION = "0810"' "$target_file" >/dev/null
printf 'ROLLBACK_OK target=%s restored_commit=bca6458 sdk_version=0810 status=0\n' "$target_file"
