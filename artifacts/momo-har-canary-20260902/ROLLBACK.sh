#!/usr/bin/env bash
set -euo pipefail

TARGET="${1:?rollback target directory is required}"
SOURCE_REPO="${2:?source repository is required}"
BASELINE="${3:-e21e568}"

tracked_files=(
  ".env.example"
  "docs/MOMO_HAR_STATE_MACHINE.md"
  "payment_link_extractor/auth.py"
  "payment_link_extractor/config.py"
  "payment_link_extractor/gopay_transport.py"
  "payment_link_extractor/models.py"
  "payment_link_extractor/momo_checkout.py"
  "payment_link_extractor/momo_core.py"
  "payment_link_extractor/momo_stripe.py"
  "payment_link_extractor/momo_transport.py"
  "payment_link_extractor/transport.py"
  "payment_link_extractor/web/events.py"
  "payment_link_extractor/web/routes.py"
  "payment_link_extractor/web/tasks.py"
  "tests/test_extraction_full_retry.py"
  "tests/test_har_capture_browser_attach.py"
  "tests/test_har_tools.py"
  "tests/test_mitm_capture.py"
  "tests/test_momo_support.py"
  "tests/test_roxy_mitm_control.py"
  "tools/har_capture.py"
  "tools/har_capture_browser_attach.py"
  "tools/har_utils.py"
  "tools/mitm_capture.py"
  "tools/roxy_mitm_control.py"
)
new_files=("payment_link_extractor/momo_eligibility.py")

mkdir -p "$TARGET"
for rel in "${tracked_files[@]}"; do
  mkdir -p "$TARGET/$(dirname "$rel")"
  git -C "$SOURCE_REPO" cat-file -e "$BASELINE:$rel"
  git -C "$SOURCE_REPO" show "$BASELINE:$rel" > "$TARGET/$rel"
done
for rel in "${new_files[@]}"; do
  rm -f "$TARGET/$rel"
done

restored_status=PASS
for rel in "${tracked_files[@]}"; do
  expected="$(git -C "$SOURCE_REPO" show "$BASELINE:$rel" | sha256sum | awk '{print $1}')"
  actual="$(sha256sum "$TARGET/$rel" | awk '{print $1}' | sed 's/^\\//')"
  if [[ "$expected" != "$actual" ]]; then
    restored_status=FAIL
    printf 'HASH_MISMATCH=%s\n' "$rel" >&2
  fi
done
for rel in "${new_files[@]}"; do
  if [[ -e "$TARGET/$rel" ]]; then
    restored_status=FAIL
    printf 'NEW_FILE_REMAINS=%s\n' "$rel" >&2
  fi
done

printf 'ROLLBACK_TARGET=%s\n' "$TARGET"
printf 'BASELINE_COMMIT=%s\n' "$BASELINE"
printf 'RESTORED_TRACKED_FILES=%s\n' "${#tracked_files[@]}"
printf 'REMOVED_NEW_FILES=%s\n' "${#new_files[@]}"
printf 'RESTORED_STATUS=%s\n' "$restored_status"
printf 'HASH_MATCH=%s\n' "$( [[ "$restored_status" == PASS ]] && echo TRUE || echo FALSE )"
[[ "$restored_status" == PASS ]]
