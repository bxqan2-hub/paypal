from __future__ import annotations

"""GoPay Pro adapter: PayPal legacy core plus GCash browser optimization."""

import threading
from typing import Callable

from .models import ExtractionConfig, PaymentLinkResult
from .paypal_channel import extract_legacy_payment_link
from .transport import TransportFactory


GOPAY_PRO_CORE = "paypal_legacy_checkout_stripe"
GOPAY_PRO_OPTIMIZATION = "gcash_browser_sentinel_sdk"


def extract_gopay_pro_payment_link(
    config: ExtractionConfig,
    *,
    transport_factory: TransportFactory | None = None,
    cancel_event: threading.Event | None = None,
    stage_callback: Callable[[str], None] | None = None,
) -> PaymentLinkResult:
    """Run GoPay Pro through PayPal base flow with browser proof optimization."""
    result = extract_legacy_payment_link(
        config,
        transport_factory=transport_factory,
        cancel_event=cancel_event,
        stage_callback=stage_callback,
    )
    result.extra.update(
        {
            "gopay_pro_core": GOPAY_PRO_CORE,
            "gopay_pro_optimization": GOPAY_PRO_OPTIMIZATION,
            "payment_route": "gopay_pro_paypal_base",
        }
    )
    return result
