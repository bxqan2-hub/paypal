from __future__ import annotations

"""GoPay channel adapter backed by the imported dedicated Go core slice."""

import threading
from typing import Callable

from .gopay_pro_core.core import extract_gopay_payment_link as _extract_gopay_core
from .models import ExtractionConfig, PaymentLinkResult
from .transport import TransportFactory


def extract_gopay_payment_link(
    config: ExtractionConfig,
    *,
    transport_factory: TransportFactory | None = None,
    cancel_event: threading.Event | None = None,
    stage_callback: Callable[[str], None] | None = None,
) -> PaymentLinkResult:
    """Dispatch only to the imported GoPay core; no PayPal adapter call."""
    return _extract_gopay_core(
        config,
        transport_factory=transport_factory,
        cancel_event=cancel_event,
        stage_callback=stage_callback,
    )
