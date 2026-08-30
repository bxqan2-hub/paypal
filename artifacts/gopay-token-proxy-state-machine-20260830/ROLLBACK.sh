#!/usr/bin/env bash
set -euo pipefail

target="${1:?target report path is required}"
source_file="${2:?source report path is required}"
mkdir -p "$(dirname "$target")"
cp -- "$source_file" "$target"
printf 'ROLLBACK_TARGET=%s\n' "$target"
printf 'RESTORED_STATUS=PASS\n'
