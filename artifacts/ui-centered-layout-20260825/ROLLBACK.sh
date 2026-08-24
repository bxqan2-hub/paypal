#!/usr/bin/env bash
set -euo pipefail
BASELINE=ee23c87ba9641b5f374277901fb884bc2d160e8e
git restore --source="$BASELINE" -- payment_link_extractor/web/static/styles.css tests/test_ui_button_contracts.py
