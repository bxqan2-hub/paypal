#!/usr/bin/env bash
set -euo pipefail

commit="${1:-HEAD}"
git revert --no-edit "$commit"
