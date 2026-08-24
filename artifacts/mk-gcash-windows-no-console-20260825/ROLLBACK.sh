#!/usr/bin/env bash
set -euo pipefail
# Restore the pre-change commit in a disposable worktree.
BASELINE=92ca47c08858d5af727a4db15b878d38414098fe
git restore --source="$BASELINE" -- \
  MK_GCASH_UPSTREAM.md \
  mk_gcash_project_manifest.json \
  payment_link_extractor/mk_gcash_open_source/sentinel.py \
  payment_link_extractor/mk_gcash_open_source/sentinel_bridge.js \
  tests/test_mk_gcash_replacement.py
