#!/usr/bin/env bash
set -euo pipefail
target="${1:?usage: ROLLBACK.sh <copy> [repo-root]}"
root="${2:-$(cd "$(dirname "$0")/../.." && pwd)}"
git -C "$root" show f203a50cb1640b218cff9f249dcdf4b09dc51c03:payment_link_extractor/channels.py > "$target"
sha256sum "$target"
