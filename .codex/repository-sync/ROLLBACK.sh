#!/usr/bin/env bash
set -euo pipefail

root="${1:-.}"
cat > "${root%/}/.gitignore" <<'EOF'
__pycache__/
.env
.env.*
!.env.example
EOF
rm -f -- "${root%/}/AGENTS.md"
printf '%s\n' 'ROLLBACK_APPLIED: .gitignore restored and AGENTS.md removed'
