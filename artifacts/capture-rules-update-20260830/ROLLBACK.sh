#!/usr/bin/env bash
set -euo pipefail
target="${1:?target path is required}"
source_file="${2:?source snapshot path is required}"
before=$(sha256sum "$target" | awk '{print $1}')
expected=$(sha256sum "$source_file" | awk '{print $1}')
cp -- "$source_file" "$target"
restored=$(sha256sum "$target" | awk '{print $1}')
printf 'ROLLBACK_TARGET=%s\nBEFORE_SHA256=%s\nRESTORED_SHA256=%s\nEXPECTED_SHA256=%s\n' "$target" "$before" "$restored" "$expected"
[[ "$restored" == "$expected" ]]
printf 'RESTORED_STATUS=PASS\n'