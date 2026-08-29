#!/usr/bin/env bash
set -euo pipefail

repo_root="${1:?repo root required}"
target_file="${2:?target fixture required}"
mkdir -p "$(dirname "$target_file")"
git -C "$repo_root" show 6d85a45:payment_link_extractor/flows/cs_live.py > "$target_file"
expected=$(git -C "$repo_root" rev-parse 6d85a45:payment_link_extractor/flows/cs_live.py)
actual=$(git -C "$repo_root" hash-object "$target_file")
test "$expected" = "$actual"
printf 'ROLLBACK_OK target=%s restored_commit=6d85a45 sha_match=true status=0\n' "$target_file"
