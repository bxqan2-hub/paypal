from __future__ import annotations

"""Lean VN/VND MoMo extraction core (aligned with the known-good 9999 flow).

The whole MoMo path is pinned to a single, coherent **Chrome 146** identity:
the curl_cffi TLS fingerprint (``._transport``), the User-Agent / client hints,
and the browser that mints the Sentinel proof (``._sentinel_runner`` — a real
headless Chrome 146) are all 146.  This coherence is what the checkout/confirm
risk check verifies; mixing a <=150 request fingerprint with a proof minted in
system Chrome 151/152 is exactly what produced ``status=blocked`` before.

The flow itself is intentionally short: Checkout -> Checkout/update -> Stripe
Elements -> taxes -> ConfirmationToken -> confirm -> Intent -> resolve the
``payment.momo.vn`` link and return.  There is no gateway ``querySession``
poll and no proxy-pool eligibility retry storm; both were self-inflicted
failure surfaces, not part of producing the link.
"""

import threading
from dataclasses import replace
from typing import Any, Callable

from ..auth import account_email, normalize_access_token
from ..errors import ConfigurationError, ExtractionCancelled
from ..logging_utils import stage_logger
from ..models import ExtractionConfig, PaymentLinkResult
from ._config import billing_for_country, currency_minor_scale, normalize_payment_method
from ._flow import extract_momo_provider
from ._stripe_common import checkout_payable_amount
from ._transport import DefaultTransportFactory, safe_close

MOMO_RESULT_FIELD = "momo_url"
MOMO_COUNTRY = "VN"
MOMO_CURRENCY = "VND"


def extract_momo_payment_link(
    config: ExtractionConfig,
    *,
    transport_factory: Any = None,
    cancel_event: threading.Event | None = None,
    stage_callback: Callable[[str], None] | None = None,
) -> PaymentLinkResult:
    """Extract one zero-value Vietnamese MoMo payment link."""
    if normalize_payment_method(config.payment_method) != "momo":
        raise ConfigurationError("Momo core requires payment_method=momo")
    if str(config.country or "").upper() != MOMO_COUNTRY:
        raise ConfigurationError("Momo core requires country=VN")
    token = normalize_access_token(config.access_token)
    if not token:
        raise ConfigurationError("AT is required")
    if not str(config.checkout_proxy or "").strip():
        raise ConfigurationError("checkout proxy is required")
    config = replace(
        config,
        access_token=token,
        checkout_proxy=str(config.checkout_proxy).strip(),
        update_proxy=str(config.update_proxy or config.checkout_proxy).strip(),
        stripe_hcaptcha_token=str(config.stripe_hcaptcha_token or "").strip(),
        country="VN",
        payment_method="momo",
    )

    def checkpoint(stage: str) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise ExtractionCancelled("task cancellation requested")
        if stage_callback is not None:
            stage_callback(stage)

    log = stage_logger(config.verbose)
    billing_profile = billing_for_country("VN", "momo")
    billing = billing_profile.to_dict()
    actual_email = account_email(config.access_token)
    if actual_email:
        billing["email"] = actual_email
        billing_profile = replace(billing_profile, email=actual_email)

    factory = transport_factory or DefaultTransportFactory()
    chatgpt = factory.chatgpt(config, config.checkout_proxy)
    stripe = factory.stripe(config)
    try:
        checkout, provider = extract_momo_provider(
            config, chatgpt, stripe, billing, log, stage_callback=checkpoint
        )
        amount_due_minor, amount_currency = checkout_payable_amount(checkout)
        scale = currency_minor_scale(amount_currency)
        provider_value = str(
            provider.get("momo_url") or provider.get("provider_url") or ""
        )
        if not provider_value:
            raise ConfigurationError("MoMo provider URL is missing")
        result = PaymentLinkResult(
            checkout_session_id=str(checkout["cs_id"]),
            session_kind=str(checkout["session_kind"]),
            payment_method="momo",
            billing_country=MOMO_COUNTRY,
            currency=amount_currency,
            amount_due=amount_due_minor / (10**scale),
            amount_due_minor=amount_due_minor,
            billing=billing_profile,
            account_email=actual_email,
            payment_method_id=str(provider.get("payment_method_id") or ""),
            stripe_redirect_url=str(provider.get("stripe_redirect_url") or ""),
            provider_url=str(provider.get("provider_url") or provider_value),
            provider_field=MOMO_RESULT_FIELD,
            provider_value=provider_value,
            extra={
                "payment_route": "momo_oaics_stripe",
                "momo_browser_profile": "chrome146",
                "momo_sentinel_build": "20260810913b",
            },
        )
        checkpoint("completed")
        return result
    finally:
        safe_close(stripe)
        safe_close(chatgpt)
