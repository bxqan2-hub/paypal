#!/usr/bin/env bash
set -euo pipefail

target="${1:?usage: ROLLBACK.sh TARGET_COPY [SOURCE_REPOSITORY]}"
source_repo="${2:-C:/Users/Administrator/Desktop/提链}"
base_commit="749bc6c3833dd13078a2d3b25be7919b2d5f2081"

mkdir -p "$target"
git -C "$source_repo" show "$base_commit:.gitignore" > "$target/.gitignore"
rm -f "$target/docs/2026-08-30_workspace-rollback-cleanup-report.md"

expected_sha="$(git -C "$source_repo" show "$base_commit:.gitignore" | sha256sum | awk '{print $1}')"
restored_sha="$(sha256sum "$target/.gitignore" | awk '{print $1}')"
restored_sha="${restored_sha#\\}"

echo "ROLLBACK_TARGET=$target"
echo "RESTORED_TRACKED_FILES=1"
echo "REMOVED_NEW_FILES=1"
echo "RESTORED_STATUS=$([[ "$expected_sha" == "$restored_sha" ]] && echo PASS || echo FAIL)"
echo "RESTORED_SHA256=$restored_sha"
echo "EXPECTED_SHA256=$expected_sha"
echo "HASH_MATCH=$([[ "$expected_sha" == "$restored_sha" ]] && echo True || echo False)"

[[ "$expected_sha" == "$restored_sha" ]]
