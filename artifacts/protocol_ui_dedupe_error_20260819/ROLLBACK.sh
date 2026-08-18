#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BASE_COMMIT="9cfeda28ed3aeb7bddb94429ee591134e62dc437"
SOURCE_REL="paypal_agreement_protocol/web_static/app.js"
ORIGINAL_WORKTREE_SHA256="3acf82bcb321450313d54ac66137f1536164cbbcccbdab67c021171d75ea7076"
BASE_BLOB_SHA256="5041faff9cdbf40de15c7d3fb92e045c0bf900442e0b0e61990ffc4134f58fcf"
MODIFIED_SHA256="30c01844765c4115d0939d7fd79542f2c233f61c8766b143c36041be8e0f66e3"
TARGET="${1:-$ROOT/$SOURCE_REL}"
ACTUAL_SHA256="$(sha256sum "$TARGET" | awk '{print $1}')"
if [[ "$ACTUAL_SHA256" != "$MODIFIED_SHA256" ]]; then
  printf 'ROLLBACK_ERROR input=%s result=unexpected_sha256 actual=%s expected=%s status=2\n' "$TARGET" "$ACTUAL_SHA256" "$MODIFIED_SHA256" >&2
  exit 2
fi
TMP_FILE="$(mktemp)"
trap 'rm -f "$TMP_FILE"' EXIT
git -C "$ROOT" show "${BASE_COMMIT}:${SOURCE_REL}" > "$TMP_FILE"
BASE_SHA256="$(sha256sum "$TMP_FILE" | awk '{print $1}')"
if [[ "$BASE_SHA256" != "$BASE_BLOB_SHA256" ]]; then
  printf 'ROLLBACK_ERROR input=%s result=base_sha256_mismatch actual=%s expected=%s status=3\n' "$BASE_COMMIT" "$BASE_SHA256" "$BASE_BLOB_SHA256" >&2
  exit 3
fi
cat "$TMP_FILE" > "$TARGET"
RESTORED_SHA256="$(sha256sum "$TARGET" | awk '{print $1}')"
if [[ "$RESTORED_SHA256" != "$BASE_BLOB_SHA256" ]]; then
  printf 'ROLLBACK_ERROR input=%s result=restore_sha256_mismatch actual=%s expected=%s status=4\n' "$TARGET" "$RESTORED_SHA256" "$BASE_BLOB_SHA256" >&2
  exit 4
fi
printf 'ROLLBACK_OK command=git-show input=%s result=restored baseline_worktree_sha256=%s restored_git_blob_sha256=%s status=0\n' "$TARGET" "$ORIGINAL_WORKTREE_SHA256" "$RESTORED_SHA256"
