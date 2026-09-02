#!/usr/bin/env bash
set -euo pipefail

TARGET_DIR="${1:?rollback target directory is required}"
SOURCE_REPO="${2:?source repository is required}"
TARGET_COMMIT="${3:-8c11fdd}"

files=(
  ".env.example"
  "docs/MOMO_HAR_STATE_MACHINE.md"
  "payment_link_extractor/auth.py"
  "payment_link_extractor/momo_checkout.py"
  "payment_link_extractor/momo_core.py"
  "payment_link_extractor/momo_eligibility.py"
  "payment_link_extractor/momo_stripe.py"
  "payment_link_extractor/momo_transport.py"
  "payment_link_extractor/transport.py"
  "payment_link_extractor/web/events.py"
  "payment_link_extractor/web/routes.py"
  "payment_link_extractor/web/tasks.py"
  "tests/test_har_tools.py"
  "tests/test_momo_support.py"
  "tests/test_roxy_mitm_control.py"
  "tools/har_capture.py"
  "tools/har_utils.py"
  "tools/roxy_mitm_control.py"
)

mkdir -p "$TARGET_DIR"
for rel in "${files[@]}"; do
  mkdir -p "$TARGET_DIR/$(dirname "$rel")"
  git -C "$SOURCE_REPO" cat-file -e "$TARGET_COMMIT:$rel"
  git -C "$SOURCE_REPO" show "$TARGET_COMMIT:$rel" > "$TARGET_DIR/$rel"
done

restored_status=PASS
for rel in "${files[@]}"; do
  expected="$(git -C "$SOURCE_REPO" show "$TARGET_COMMIT:$rel" | sha256sum | awk '{print $1}')"
  actual="$(sha256sum "$TARGET_DIR/$rel" | awk '{print $1}' | sed 's/^\\//')"
  if [[ "$expected" != "$actual" ]]; then
    restored_status=FAIL
    printf 'HASH_MISMATCH=%s\n' "$rel" >&2
  fi
done

printf 'ROLLBACK_TARGET=%s\n' "$TARGET_DIR"
printf 'RESTORED_COMMIT=%s\n' "$TARGET_COMMIT"
printf 'RESTORED_FILES=%s\n' "${#files[@]}"
printf 'RESTORED_STATUS=%s\n' "$restored_status"
printf 'HASH_MATCH=%s\n' "$( [[ "$restored_status" == PASS ]] && echo TRUE || echo FALSE )"
[[ "$restored_status" == PASS ]]
