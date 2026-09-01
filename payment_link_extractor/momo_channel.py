from __future__ import annotations

import threading
from typing import Callable, Any

from .models import ExtractionConfig, PaymentLinkResult
from .momo_core import extract_momo_payment_link as _extract_momo_payment_link


def extract_momo_payment_link(config: ExtractionConfig, *, transport_factory: Any = None, cancel_event: threading.Event | None = None, stage_callback: Callable[[str], None] | None = None) -> PaymentLinkResult:
    return _extract_momo_payment_link(config, transport_factory=transport_factory, cancel_event=cancel_event, stage_callback=stage_callback)
