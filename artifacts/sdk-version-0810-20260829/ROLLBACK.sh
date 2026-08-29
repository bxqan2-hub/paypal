#!/usr/bin/env bash
set -euo pipefail

repo_root="${1:?repo root required}"
target_file="${2:?target fixture required}"
mkdir -p "$(dirname "$target_file")"
git -C "$repo_root" show d655101:payment_link_extractor/config.py > "$target_file"
grep -F 'STRIPE_RUNTIME_VERSION = "692f102a8f"' "$target_file" >/dev/null
printf 'ROLLBACK_OK target=%s restored_commit=d655101 sdk_version=692f102a8f status=0\n' "$target_file"
