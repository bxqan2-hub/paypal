from __future__ import annotations

"""Independent VN/VND MoMo extraction core."""

import base64
import json
import time
from dataclasses import replace
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .auth import account_email
from .config import billing_for_country, currency_minor_scale
from .errors import ConfigurationError, ExtractionCancelled, ProtocolError
from .models import ExtractionConfig, PaymentLinkResult
from .momo_checkout import MOMO_COUNTRY, MOMO_CURRENCY, create_checkout, taxes
from .momo_stripe import checkout_confirm, confirmation_token, elements_session, intent_confirm, redirect_url, validate_momo_url
from .momo_transport import (
    MomoTransportFactory,
    capture_momo_csrf_token,
    close,
    momo_gateway_headers,
)

MOMO_RESULT_FIELD = "momo_url"


def momo_checkout_payable_amount(checkout: dict[str, Any]) -> int | None:
    state = checkout.get("checkout_state") if isinstance(checkout.get("checkout_state"), dict) else {}
    total = state.get("total") if isinstance(state.get("total"), dict) else {}
    due = total.get("total") if isinstance(total.get("total"), dict) else {}
    raw = due.get("minorUnitsAmount")
    if raw in (None, ""):
        raw = checkout.get("payable_amount_minor")
    try:
        return None if raw in (None, "") else int(raw)
    except (TypeError, ValueError):
        return None


def validate_momo_amount(amount_due_minor: int | None) -> None:
    if amount_due_minor is None:
        raise ProtocolError(409, "Momo expected zero amount, got missing")
    if int(amount_due_minor) != 0:
        raise ProtocolError(409, f"Momo expected zero amount, got {amount_due_minor}")


def pin_momo_attempt_proxy(config: ExtractionConfig) -> ExtractionConfig:
    proxy = str(config.checkout_proxy or "").strip()
    return replace(config, checkout_proxy=proxy, update_proxy=proxy, checkout_proxy_attempts=(proxy,), update_proxy_attempts=(proxy,), proxy_pool=(proxy,))


def _gateway_session_id(url: str) -> str:
    token = parse_qs(urlparse(url).query).get("t", [""])[0]
    try:
        decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode("utf-8", "ignore")
        return decoded.split("|", 1)[0]
    except Exception:
        return ""


def query_gateway(session: Any, url: str, *, polls: int = 1) -> dict[str, Any]:
    session_id = _gateway_session_id(url)
    if not session_id:
        raise ProtocolError(502, "Momo gateway URL has invalid session token")
    # The browser first opens the gateway page.  That navigation establishes
    # the page origin and may set the CSRF cookie used by querySession.
    opener = getattr(session, "request", None)
    if callable(opener):
        try:
            page = opener(
                "GET",
                url,
                headers=momo_gateway_headers(session, url),
                timeout=30,
            )
            if int(getattr(page, "status_code", 0) or 0) >= 400:
                raise ProtocolError(
                    int(page.status_code), "Momo gateway page failed"
                )
            capture_momo_csrf_token(session, page)
        except ProtocolError:
            raise
        except Exception as exc:
            raise ProtocolError(502, "Momo gateway page request failed") from exc
    last: dict[str, Any] = {}
    for index in range(max(1, polls)):
        response = session.request(
            "POST",
            "https://payment.momo.vn/v2/gateway/querySession",
            json={"sessionId": session_id},
            headers=momo_gateway_headers(session, url),
            timeout=30,
        )
        if int(getattr(response, "status_code", 0) or 0) >= 400:
            raise ProtocolError(int(response.status_code), "Momo querySession failed")
        try:
            value = response.json()
        except Exception:
            value = json.loads(str(getattr(response, "text", "{}")))
        last = value if isinstance(value, dict) else {}
        if bool(last.get("redirect")):
            break
        if index + 1 < polls:
            time.sleep(0.25)
    return last


def extract_momo_payment_link(config: ExtractionConfig, *, transport_factory: Any = None, cancel_event: Any = None, stage_callback: Callable[[str], None] | None = None) -> PaymentLinkResult:
    if str(config.payment_method or "").lower() != "momo":
        raise ConfigurationError("Momo core requires payment_method=momo")
    if str(config.country or "").upper() != MOMO_COUNTRY:
        raise ConfigurationError("Momo core requires country=VN")
    config = pin_momo_attempt_proxy(config)
    def checkpoint(stage: str) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise ExtractionCancelled("task cancellation requested")
        if stage_callback:
            stage_callback(stage)
    billing_profile = billing_for_country(MOMO_COUNTRY)
    email = account_email(config.access_token)
    if email:
        billing_profile = replace(billing_profile, email=email)
    billing = billing_profile.to_dict()
    factory = transport_factory or MomoTransportFactory()
    chatgpt = factory.chatgpt(config, config.checkout_proxy)
    stripe = None
    momo = None
    try:
        checkpoint("checkout")
        checkout = create_checkout(chatgpt, account_email=email)
        checkpoint("checkout_committed")
        checkpoint("checkout_kind:openai_custom_checkout")
        for _ in range(3):
            checkpoint("taxes")
            taxes(chatgpt, checkout, billing)
        amount_minor = momo_checkout_payable_amount(checkout)
        if config.momo_zero_trial_validation:
            checkpoint("zero_amount_validation")
            validate_momo_amount(amount_minor)
            checkpoint("zero_amount_confirmed")
        stripe = factory.stripe(config)
        checkpoint("stripe_init")
        elements_session(stripe, checkout)
        checkpoint("stripe_elements")
        token = confirmation_token(stripe, checkout, billing, config.stripe_hcaptcha_token)
        checkpoint("payment_confirmation")
        confirmed = checkout_confirm(chatgpt, checkout, token)
        intent = intent_confirm(stripe, checkout, token, confirmed)
        raw = redirect_url(intent)
        if not validate_momo_url(raw):
            raise ProtocolError(502, "Stripe response did not return a Momo gateway URL")
        checkpoint("redirect_resolution")
        momo = factory.momo(config) if callable(getattr(factory, "momo", None)) else stripe
        query_gateway(momo, raw, polls=1)
        checkout_state = checkout.get("checkout_state") if isinstance(checkout.get("checkout_state"), dict) else {}
        total = checkout_state.get("total") if isinstance(checkout_state.get("total"), dict) else {}
        due = total.get("total") if isinstance(total.get("total"), dict) else {}
        minor = int(due.get("minorUnitsAmount") if due.get("minorUnitsAmount") not in (None, "") else (checkout.get("payable_amount_minor") or 0))
        return PaymentLinkResult(checkout_session_id=checkout["cs_id"], session_kind="openai_custom_checkout", payment_method="momo", billing_country=MOMO_COUNTRY, currency=MOMO_CURRENCY, amount_due=minor / (10 ** currency_minor_scale(MOMO_CURRENCY)), amount_due_minor=minor, billing=billing_profile, account_email=email, payment_method_id="momo", stripe_redirect_url=raw, provider_url=raw, provider_field=MOMO_RESULT_FIELD, provider_value=raw, extra={"payment_route": "momo_oaics_stripe", "momo_gateway_status": "querySession", "momo_zero_trial_validation": bool(config.momo_zero_trial_validation)})
    finally:
        close(momo)
        if momo is stripe:
            stripe = None
        close(stripe)
        close(chatgpt)
