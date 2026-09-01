#!/usr/bin/env sh
set -eu
git apply --reverse "$(dirname "$0")/DIFF_FILE"
