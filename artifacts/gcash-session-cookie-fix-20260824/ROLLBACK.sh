#!/usr/bin/env bash
set -euo pipefail
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
target_root="${1:-$(git rev-parse --show-toplevel)}"
git -C "$target_root" apply --reverse --whitespace=nowarn "$script_dir/DIFF_FILE.patch"
