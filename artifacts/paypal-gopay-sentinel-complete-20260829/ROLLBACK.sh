#!/usr/bin/env bash
set -euo pipefail

repo_root="${1:?repo root required}"
target_dir="${2:?target directory required}"
mkdir -p "$target_dir"
for file in checkout.py flows/cs_live.py flows/oaics.py transport.py; do
  mkdir -p "$target_dir/$(dirname "$file")"
  git -C "$repo_root" show 36fa3d2:payment_link_extractor/$file > "$target_dir/$file"
done
grep -F 'normalize_payment_method(config.payment_method) in {"paypal", "gopay"}' "$target_dir/transport.py" >/dev/null
printf 'ROLLBACK_OK target=%s restored_commit=36fa3d2 protected_proof_calls=pre-complete status=0\n' "$target_dir"
