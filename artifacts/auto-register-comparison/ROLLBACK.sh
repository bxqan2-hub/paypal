#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
TARGET="${1:-$SCRIPT_DIR/MODIFIED_FILE}"
cat > "$TARGET" <<'EOF'
# Comparison report fixture
BASELINE_BRANCH=report-baseline
EOF
printf 'ROLLED_BACK %s\n' "$TARGET"
