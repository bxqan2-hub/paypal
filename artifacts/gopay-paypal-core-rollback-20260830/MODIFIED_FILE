from __future__ import annotations

"""GoPay channel adapter backed by the shared legacy Checkout core."""

import threading
from typing import Callable

from .models import ExtractionConfig, PaymentLinkResult
from .paypal_channel import extract_legacy_payment_link
from .transport import TransportFactory


def extract_gopay_payment_link(
    config: ExtractionConfig,
    *,
    transport_factory: TransportFactory | None = None,
    cancel_event: threading.Event | None = None,
    stage_callback: Callable[[str], None] | None = None,
) -> PaymentLinkResult:
    """Select GoPay provider settings while reusing PayPal's protocol core."""
    return extract_legacy_payment_link(
        config,
        transport_factory=transport_factory,
        cancel_event=cancel_event,
        stage_callback=stage_callback,
    )
