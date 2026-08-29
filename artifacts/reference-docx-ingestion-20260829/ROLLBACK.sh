#!/usr/bin/env bash
set -euo pipefail
if [ "$#" -ne 2 ]; then
  echo "usage: ROLLBACK.sh REPO_ROOT TARGET_DIR" >&2
  exit 64
fi
repo_root="$1"
target_dir="$2"
commit="d7ddee5645b0f803aee7379c2aa8dcddcc4c3d84"
rm -rf "$target_dir/docs/reference/payment-orchestration"
rm -f "$target_dir/tools/extract_reference_docx.py" "$target_dir/tests/test_reference_docx.py"
printf 'ROLLBACK_OK target=%s commit=%s removed=docs/reference/payment-orchestration,tools/extract_reference_docx.py,tests/test_reference_docx.py\n' "$target_dir" "$commit"
