from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_ROOT = ROOT / "paypal_agreement_protocol"
if str(PROTOCOL_ROOT) not in sys.path:
    sys.path.insert(0, str(PROTOCOL_ROOT))

# The production switch remains opt-in.  The regression suite enables it so
# every catalog-backed country path is exercised rather than only the verified
# compatibility subset.
os.environ.setdefault("PAYPAL_WEB_ENABLE_DYNAMIC_COUNTRIES", "1")
