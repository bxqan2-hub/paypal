#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TARGET_ROOT="${1:-$ROOT}"
BASE_COMMIT="c009e667dd7192e6cb0bca72456cc4c5aad8c972"

FILES=(
  "payment_link_extractor/web/paypal_protocol.py"
  "paypal_agreement_protocol/web_static/app.js"
  "tests/test_protocol_regressions.py"
)
MODIFIED_SHA256=(
  "dd53923b14e4206a777a5bf5468b6df6a4ac327dca431934a639683e02e67f98"
  "f2334c97ff9b766f845b4ea8c111cb89a8a19a245d6127d8c6be8903557ea836"
  "cf36459b379937d3883bb671f05ca2acd4dfea9615bae7a4c74b9fab737a60ac"
)
BASE_SHA256=(
  "d4c8338cb123ac16a95d3d0aa235f8155ea242a4754a40a87b57ee72fa19668e"
  "93fee10c70ee112f8afcf6b021f68876c055747d3678722d34716cf6f2c17ecf"
  "1ea93def795d5bcb524400b12459483fb5b4e2b057648af1238e602b48171797"
)

for index in "${!FILES[@]}"; do
  relative="${FILES[$index]}"
  target="$TARGET_ROOT/$relative"
  actual="$(sha256sum "$target" | awk '{print $1}')"
  if [[ "$actual" != "${MODIFIED_SHA256[$index]}" ]]; then
    printf 'ROLLBACK_ERROR input=%s result=unexpected_sha256 actual=%s expected=%s status=2\n' \
      "$target" "$actual" "${MODIFIED_SHA256[$index]}" >&2
    exit 2
  fi
done

for index in "${!FILES[@]}"; do
  relative="${FILES[$index]}"
  target="$TARGET_ROOT/$relative"
  git -C "$ROOT" show "$BASE_COMMIT:$relative" > "$target"
  restored="$(sha256sum "$target" | awk '{print $1}')"
  if [[ "$restored" != "${BASE_SHA256[$index]}" ]]; then
    printf 'ROLLBACK_ERROR input=%s result=restore_sha256_mismatch actual=%s expected=%s status=3\n' \
      "$target" "$restored" "${BASE_SHA256[$index]}" >&2
    exit 3
  fi
done

printf 'ROLLBACK_OK command=git-show input=%s result=restored base_commit=%s files=%s status=0\n' \
  "$TARGET_ROOT" "$BASE_COMMIT" "${#FILES[@]}"
