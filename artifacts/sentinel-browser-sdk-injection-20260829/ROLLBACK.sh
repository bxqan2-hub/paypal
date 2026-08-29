#!/usr/bin/env bash
set -euo pipefail

repo_root="${1:?repo root required}"
target_file="${2:?target fixture required}"
mkdir -p "$(dirname "$target_file")"
git -C "$repo_root" show 9ea3528:payment_link_extractor/transport.py > "$target_file"
expected=$(git -C "$repo_root" rev-parse 9ea3528:payment_link_extractor/transport.py)
actual=$(git -C "$repo_root" hash-object "$target_file")
test "$expected" = "$actual"
printf 'ROLLBACK_OK target=%s restored_commit=9ea3528 sha_match=true status=0\n' "$target_file"
