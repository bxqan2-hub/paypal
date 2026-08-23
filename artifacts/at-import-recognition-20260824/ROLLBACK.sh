#!/usr/bin/env bash
set -euo pipefail

repo_root="${1:?repository root is required}"
target_file="${2:?rollback target file is required}"
source_path="${3:-payment_link_extractor/web/static/app.js}"
mkdir -p "$(dirname "$target_file")"
git -C "$repo_root" show "HEAD:${source_path}" > "$target_file"
printf 'ROLLBACK_APPLIED: %s restored from HEAD\n' "$target_file"
