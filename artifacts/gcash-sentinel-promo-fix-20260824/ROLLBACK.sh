#!/usr/bin/env bash
set -euo pipefail

WORKTREE="${1:?usage: ROLLBACK.sh WORKTREE [PATCH_FILE]}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PATCH_FILE="${2:-${SCRIPT_DIR}/DIFF_FILE.patch}"

git -C "${WORKTREE}" apply --reverse --whitespace=nowarn "${PATCH_FILE}"
