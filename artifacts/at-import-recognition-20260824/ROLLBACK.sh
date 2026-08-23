#!/usr/bin/env bash
set -euo pipefail

repo_root="${1:?repository root is required}"
target_file="${2:?rollback target file is required}"
source_path="${3:-payment_link_extractor/web/static/app.js}"
baseline_commit="${4:-740004e758bb70e66ffa323a07b0c66c70735137}"
mkdir -p "$(dirname "$target_file")"
git -C "$repo_root" show "${baseline_commit}:${source_path}" > "$target_file"
printf 'ROLLBACK_APPLIED: %s restored from %s\n' "$target_file" "$baseline_commit"
