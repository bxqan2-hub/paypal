#!/usr/bin/env bash
set -euo pipefail

repo_root="${1:?repo root required}"
target_file="${2:?target fixture required}"
mkdir -p "$(dirname "$target_file")"
git -C "$repo_root" show 5a63e90:payment_link_extractor/transport.py > "$target_file"
grep -F 'normalize_payment_method(config.payment_method) == "gcash"' "$target_file" >/dev/null
printf 'ROLLBACK_OK target=%s restored_commit=5a63e90 routing=gcash-provider status=0\n' "$target_file"
