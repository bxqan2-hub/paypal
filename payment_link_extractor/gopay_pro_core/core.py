from __future__ import annotations

"""Dedicated GoPay extraction core backed by the imported Go source slice.

The Go files under this package are the protocol reference supplied in
``gojek-gopay-cs-link-sanitized``.  This Python boundary keeps the site
runtime contract while ensuring GoPay has its own orchestration path and
result mapping instead of entering the PayPal adapter.
"""

import threading
from pathlib import Path
from typing import Callable

from ..auth import account_email
from ..checkout import check_coupon_eligibility, create_checkout, require_country_currency, update_checkout
from ..config import billing_for_country, currency_minor_scale
from ..errors import ConfigurationError, ExtractionCancelled, ProtocolError
from ..flows.cs_live import extract_cs_live_provider
from ..flows.oaics import extract_oaics_provider
from ..logging_utils import stage_logger
from ..models import ExtractionConfig, PaymentLinkResult
from ..stripe_common import checkout_payable_amount
from ..transport import DefaultTransportFactory, TransportFactory, safe_close

GOPAY_COUNTRY = "ID"
GOPAY_CURRENCY = "IDR"
GOPAY_RESULT_FIELD = "gopay_url"
GOPAY_CORE_SOURCE_DIR = Path(__file__).resolve().parent
GOPAY_CORE_SOURCE_MANIFEST = GOPAY_CORE_SOURCE_DIR / "SOURCE_MANIFEST.json"


def validate_gopay_amount(
    amount_due_minor: int,
    *,
    promotion_applied: bool,
) -> None:
    """Fail closed unless an applied GoPay promotion produces zero due."""
    if not promotion_applied:
        return
    try:
        amount = int(amount_due_minor)
    except (TypeError, ValueError) as exc:
        raise ProtocolError(409, "expected zero amount, got invalid") from exc
    if amount != 0:
        raise ProtocolError(409, f"expected zero amount, got {amount}")


def extract_gopay_payment_link(
    config: ExtractionConfig,
    *,
    transport_factory: TransportFactory | None = None,
    cancel_event: threading.Event | None = None,
    stage_callback: Callable[[str], None] | None = None,
) -> PaymentLinkResult:
    """Run the GoPay-only CS/checkout orchestration and map ``gopay_url``."""
    if str(config.payment_method or "").strip().lower() != "gopay":
        raise ConfigurationError("GoPay core requires payment_method=gopay")
    if str(config.country or "").strip().upper() != GOPAY_COUNTRY:
        raise ConfigurationError("GoPay core requires country=ID")

    def checkpoint(stage: str) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise ExtractionCancelled("task cancellation requested")
        if stage_callback is not None:
            stage_callback(stage)

    apply_checkout_update = bool(config.apply_checkout_update)
    log = stage_logger(config.verbose)
    billing = billing_for_country(GOPAY_COUNTRY).to_dict()
    factory = transport_factory or DefaultTransportFactory()
    chatgpt = factory.chatgpt(config, config.checkout_proxy)
    stripe = None
    try:
        if apply_checkout_update:
            checkpoint("eligibility_check")
            check_coupon_eligibility(config, chatgpt, log)
        checkpoint("checkout")
        checkout = create_checkout(config, chatgpt, log)
        checkpoint(f"checkout_kind:{checkout['session_kind']}")
        if config.oaics_only and checkout["session_kind"] == "stripe_checkout":
            raise ConfigurationError("仅 OAICS 模式下检测到 CS Checkout，任务已失败")
        require_country_currency(checkout, config)
        if apply_checkout_update:
            checkpoint("checkout_update")
            update_checkout(config, chatgpt, checkout, log)
            require_country_currency(checkout, config)
        stripe = factory.stripe(config)
        checkpoint("stripe_init")
        if checkout["session_kind"] == "stripe_checkout":
            provider = extract_cs_live_provider(config, chatgpt, stripe, checkout, billing, log, stage_callback=checkpoint)
        elif checkout["session_kind"] == "openai_custom_checkout":
            provider = extract_oaics_provider(config, chatgpt, stripe, checkout, billing, log, stage_callback=checkpoint)
        else:
            raise ConfigurationError(f"unsupported checkout session: {checkout.get('cs_id')}")
        amount_due_minor, amount_currency = checkout_payable_amount(checkout)
        amount_currency = amount_currency or GOPAY_CURRENCY
        validate_gopay_amount(
            amount_due_minor,
            promotion_applied=apply_checkout_update,
        )
        amount_due = amount_due_minor / (10 ** currency_minor_scale(amount_currency))
        provider_value = str(provider.get(GOPAY_RESULT_FIELD) or provider.get("provider_url") or "")
        if not provider_value:
            raise ConfigurationError("GoPay core did not return gopay_url")
        result = PaymentLinkResult(
            checkout_session_id=str(checkout["cs_id"]),
            session_kind=str(checkout["session_kind"]),
            payment_method="gopay",
            billing_country=GOPAY_COUNTRY,
            currency=amount_currency,
            amount_due=amount_due,
            amount_due_minor=amount_due_minor,
            billing=billing_for_country(GOPAY_COUNTRY),
            account_email=account_email(config.access_token),
            payment_method_id=str(provider.get("payment_method_id") or ""),
            stripe_redirect_url=str(provider.get("stripe_redirect_url") or ""),
            provider_url=str(provider.get("provider_url") or provider_value),
            provider_field=GOPAY_RESULT_FIELD,
            provider_value=provider_value,
            extra={
                "gopay_core_source": str(GOPAY_CORE_SOURCE_DIR),
                "gopay_core_manifest": str(GOPAY_CORE_SOURCE_MANIFEST),
                "payment_route": "gopay_cs_dedicated",
            },
        )
        checkpoint("completed")
        return result
    finally:
        safe_close(stripe)
        safe_close(chatgpt)
