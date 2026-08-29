#!/usr/bin/env bash
set -euo pipefail

target="${1:?target path is required}"
source_file="${2:?source snapshot path is required}"

before=$(sha256sum "$target" | awk '{print $1}')
expected=$(sha256sum "$source_file" | awk '{print $1}')
cp -- "$source_file" "$target"
restored=$(sha256sum "$target" | awk '{print $1}')

printf 'ROLLBACK_TARGET=%s\n' "$target"
printf 'BEFORE_SHA256=%s\n' "$before"
printf 'RESTORED_SHA256=%s\n' "$restored"
printf 'EXPECTED_SHA256=%s\n' "$expected"
if [[ "$restored" != "$expected" ]]; then
  printf 'RESTORED_STATUS=FAIL\n' >&2
  exit 1
fi
printf 'RESTORED_STATUS=PASS\n'
