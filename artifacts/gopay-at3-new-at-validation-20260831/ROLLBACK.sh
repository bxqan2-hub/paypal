#!/usr/bin/env bash
set -euo pipefail
TARGET="${1:?usage: ROLLBACK.sh TARGET_COPY SOURCE_REPO}"
SOURCE_REPO="${2:?usage: ROLLBACK.sh TARGET_COPY SOURCE_REPO}"
BASELINE="9d0a130500481491fe8200aef26af36191cf354c"
new_file="docs/2026-08-31_gopay-at3-and-new-at-validation.md"
before_present="False"; [[ -f "$TARGET/$new_file" ]] && before_present="True"
rm -f "$TARGET/$new_file"
restored_status="PASS"; [[ ! -e "$TARGET/$new_file" ]] || restored_status="FAIL"
printf 'ROLLBACK_TARGET=%s
' "$TARGET"
printf 'REMOVED_NEW_FILES=1
'
printf 'BEFORE_PRESENT=%s
' "$before_present"
printf 'RESTORED_STATUS=%s
' "$restored_status"
[[ "$restored_status" == "PASS" ]]
