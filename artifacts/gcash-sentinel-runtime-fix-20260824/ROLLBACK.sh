#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
target_root="${1:?target worktree required}"
git -C "$target_root" apply --reverse --whitespace=nowarn "$script_dir/DIFF_FILE"
printf 'ROLLBACK_APPLY_EXIT=0\n'
printf 'RESTORED_BEHAVIOR=unversioned Sentinel startup, optional fallback, original account-header timing, and original bridge entrypoint\n'
printf 'ROLLBACK_STATUS=restored\n'
