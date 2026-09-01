from __future__ import annotations

"""Independent VN/VND MoMo extraction core."""

import base64
import json
import time
from dataclasses import replace
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .auth import account_email, account_name
from .config import billing_for_country, currency_minor_scale
from .errors import ConfigurationError, ExtractionCancelled, ProtocolError
from .models import ExtractionConfig, PaymentLinkResult
from .momo_checkout import (
    MOMO_COUNTRY,
    MOMO_CURRENCY,
    checkout_amount_minor,
    create_checkout,
    momo_checkout_method_state,
    taxes,
    validate_zero_trial_checkout,
)
from .momo_eligibility import probe_momo_trial_eligibility
from .momo_stripe import (
    checkout_confirm,
    confirmation_token,
    elements_session,
    intent_confirm,
    redirect_url,
    resolve_momo_redirect,
    synchronize_momo_stripe_ids,
    validate_momo_url,
)
from .momo_transport import (
    MomoTransportFactory,
    capture_momo_csrf_token,
    close,
    momo_gateway_headers,
    momo_gateway_navigation_headers,
)

MOMO_RESULT_FIELD = "momo_url"


def momo_checkout_payable_amount(checkout: dict[str, Any]) -> int | None:
    return checkout_amount_minor(checkout)


def validate_momo_amount(amount_due_minor: int | None) -> None:
    if amount_due_minor is None:
        raise ProtocolError(409, "Momo expected zero amount, got missing")
    try:
        value = int(amount_due_minor)
    except (TypeError, ValueError):
        try:
            parsed = float(str(amount_due_minor).strip())
        except (TypeError, ValueError):
            raise ProtocolError(409, "Momo expected zero amount, got invalid")
        value = int(parsed) if parsed.is_integer() else -1
    if value != 0:
        raise ProtocolError(409, f"Momo expected zero amount, got {amount_due_minor}")


def pin_momo_attempt_proxy(config: ExtractionConfig) -> ExtractionConfig:
    proxy = str(config.checkout_proxy or "").strip()
    pool = tuple(
        dict.fromkeys(
            str(value).strip()
            for value in (config.proxy_pool or (proxy,))
            if str(value).strip()
        )
    )
    return replace(
        config,
        checkout_proxy=proxy,
        update_proxy=proxy,
        checkout_proxy_attempts=(proxy,),
        update_proxy_attempts=(proxy,),
        proxy_pool=pool,
    )


def _gateway_session_id(url: str) -> str:
    token = parse_qs(urlparse(url).query).get("t", [""])[0]
    try:
        decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode("utf-8", "ignore")
        return decoded.split("|", 1)[0]
    except Exception:
        return ""


def query_gateway(
    session: Any,
    url: str,
    *,
    polls: int = 17,
    require_redirect: bool = False,
    poll_interval: float = 4.0,
    expected_amount: int | None = None,
    cancel_event: Any = None,
    deadline_seconds: float = 120.0,
) -> dict[str, Any]:
    session_id = _gateway_session_id(url)
    if not session_id:
        raise ProtocolError(502, "Momo gateway URL has invalid session token")
    # The browser first opens the gateway page.  That navigation establishes
    # the page origin and may set the CSRF cookie used by querySession.
    started = time.monotonic()
    opener = getattr(session, "request", None)
    if callable(opener):
        try:
            if cancel_event is not None and cancel_event.is_set():
                raise ExtractionCancelled("task cancellation requested")
            page = opener(
                "GET",
                url,
                headers=momo_gateway_navigation_headers(session, url),
                timeout=max(1, min(30, int(max(1, deadline_seconds)))),
            )
            if int(getattr(page, "status_code", 0) or 0) >= 400:
                raise ProtocolError(
                    int(page.status_code), "Momo gateway page failed"
                )
            capture_momo_csrf_token(session, page)
        except (ProtocolError, ExtractionCancelled):
            raise
        except Exception as exc:
            raise ProtocolError(502, "Momo gateway page request failed") from exc
    last: dict[str, Any] = {}
    for index in range(max(1, polls)):
        if cancel_event is not None and cancel_event.is_set():
            raise ExtractionCancelled("task cancellation requested")
        remaining = float(deadline_seconds) - (time.monotonic() - started)
        if remaining <= 0:
            raise ProtocolError(504, "Momo gateway polling deadline exceeded")
        query_headers = momo_gateway_headers(session, url)
        # The browser sends an empty POST (the session id is bound to the
        # gateway cookie/page); retain a JSON fallback for older deployments.
        query_headers["Accept"] = "*/*"
        query_headers.pop("Content-Type", None)
        try:
            response = session.request(
                "POST",
                "https://payment.momo.vn/v2/gateway/querySession",
                data="",
                headers=query_headers,
                timeout=max(1, min(30, int(remaining))),
            )
        except TypeError:
            # Small test/legacy adapters may expose only a JSON keyword.
            response = session.request(
                "POST",
                "https://payment.momo.vn/v2/gateway/querySession",
                json={"sessionId": session_id},
                headers=momo_gateway_headers(session, url),
                timeout=max(1, min(30, int(remaining))),
            )
        if int(getattr(response, "status_code", 0) or 0) >= 400:
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
        status_text = str(last.get("status_code") or "")
        if status_text and status_text not in {"1000", "200", "PENDING"}:
            break
        if index + 1 < polls:
            sleep_for = min(max(0.0, float(poll_interval)), max(0.0, remaining))
            if cancel_event is not None:
                if cancel_event.wait(sleep_for):
                    raise ExtractionCancelled("task cancellation requested")
            else:
                time.sleep(sleep_for)
    if require_redirect and not bool(last.get("redirect")):
        status_code = str(last.get("status_code") or "")
        raise ProtocolError(
            409,
            "Momo gateway session did not reach redirect"
            + (f" (status_code={status_code})" if status_code else ""),
        )
    if require_redirect and str(last.get("status_code") or "") != "9000":
        raise ProtocolError(
            409,
            "Momo gateway session returned unexpected status"
            f" (status_code={last.get('status_code') or '-'})",
        )
    if require_redirect:
        return_url = str(last.get("return_url") or "").strip()
        if not return_url:
            raise ProtocolError(409, "Momo gateway redirect URL is missing")
        return_parts = urlparse(return_url)
        if return_parts.hostname != "payment.momo.vn" or return_parts.path != "/v2/gateway/redirect":
            raise ProtocolError(409, "Momo gateway returned an invalid redirect URL")
        query = parse_qs(return_parts.query)
        amount_values = query.get("amount") or query.get("amount_total")
        if expected_amount is not None:
            if not amount_values:
                raise ProtocolError(409, "Momo gateway redirect amount is missing")
            try:
                if any(float(str(value)) != float(expected_amount) for value in amount_values):
                    raise ProtocolError(409, "Momo gateway returned an unexpected amount")
            except ValueError as exc:
                raise ProtocolError(409, "Momo gateway returned an invalid amount") from exc
    return last


def extract_momo_payment_link(config: ExtractionConfig, *, transport_factory: Any = None, cancel_event: Any = None, stage_callback: Callable[[str], None] | None = None) -> PaymentLinkResult:
    if str(config.payment_method or "").lower() != "momo":
        raise ConfigurationError("Momo core requires payment_method=momo")
    if str(config.country or "").upper() != MOMO_COUNTRY:
        raise ConfigurationError("Momo core requires country=VN")
    factory = transport_factory or MomoTransportFactory(
        str(getattr(config, "momo_fingerprint", "") or "")
    )
    trial_eligible = False
    trial_campaign = ""
    chatgpt = None
    if bool(getattr(config, "momo_trial_eligibility_check", True)):
        if stage_callback:
            stage_callback("eligibility_check")
        eligibility = probe_momo_trial_eligibility(
            config,
            transport_factory=factory,
            cancel_event=cancel_event,
            stage_callback=stage_callback,
        )
        trial_eligible = bool(eligibility.get("eligible"))
        trial_campaign = str(eligibility.get("campaign_id") or "").strip()
        chatgpt = eligibility.pop("_chatgpt_session", None)
        selected_proxy = str(eligibility.get("proxy") or config.checkout_proxy).strip()
        config = replace(
            config,
            checkout_proxy=selected_proxy,
            update_proxy=selected_proxy,
            checkout_proxy_attempts=(selected_proxy,),
            update_proxy_attempts=(selected_proxy,),
            proxy_pool=(selected_proxy,),
        )
    config = pin_momo_attempt_proxy(config)
    def checkpoint(stage: str) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise ExtractionCancelled("task cancellation requested")
        if stage_callback:
            stage_callback(stage)
    billing_profile = billing_for_country(MOMO_COUNTRY)
    email = account_email(config.access_token)
    name = account_name(config.access_token)
    if email or name:
        billing_profile = replace(
            billing_profile,
            email=email or billing_profile.email,
            name=name or billing_profile.name,
        )
    billing = billing_profile.to_dict()
    if chatgpt is None:
        chatgpt = factory.chatgpt(config, config.checkout_proxy)
    stripe = None
    momo = None
    try:
        checkpoint("checkout")
        checkout = create_checkout(
            chatgpt,
            account_email=email,
            trial_eligible=trial_eligible,
            campaign_id=trial_campaign,
        )
        checkpoint("checkout_committed")
        checkpoint("checkout_kind:openai_custom_checkout")
        method_state = momo_checkout_method_state(checkout)
        if method_state is False:
            raise ProtocolError(409, "Momo payment method is not available in Checkout")
        if method_state is True:
            checkpoint("momo_method_confirmed")
        if config.momo_zero_trial_validation:
            validate_zero_trial_checkout(checkout)
        # The successful zero-due HAR initializes Stripe Elements from the
        # Checkout's initial total before taxes are refreshed. This binds the
        # deferred intent to the zero amount instead of letting a later tax
        # request create a fresh paid intent.
        stripe = factory.stripe(config)
        checkpoint("stripe_init")
        synchronize_momo_stripe_ids(chatgpt, stripe, checkout)
        elements_session(stripe, checkout)
        checkpoint("stripe_elements")
        for tax_phase in range(3):
            checkpoint("taxes")
            taxes(chatgpt, checkout, billing, phase=tax_phase)
            synchronize_momo_stripe_ids(chatgpt, stripe, checkout)
        amount_minor = momo_checkout_payable_amount(checkout)
        if config.momo_zero_trial_validation:
            checkpoint("zero_amount_validation")
            validate_zero_trial_checkout(checkout)
            validate_momo_amount(amount_minor)
            checkpoint("zero_amount_confirmed")
        token = confirmation_token(stripe, checkout, billing, config.stripe_hcaptcha_token)
        checkpoint("payment_confirmation")
        confirmed = checkout_confirm(chatgpt, checkout, token)
        intent = intent_confirm(stripe, checkout, token, confirmed)
        raw = redirect_url(intent)
        if raw and not validate_momo_url(raw):
            raw = resolve_momo_redirect(stripe, raw)
        if not validate_momo_url(raw):
            raise ProtocolError(502, "Stripe response did not return a Momo gateway URL")
        checkpoint("redirect_resolution")
        momo = factory.momo(config) if callable(getattr(factory, "momo", None)) else stripe
        gateway_state = query_gateway(
            momo,
            raw,
            polls=17,
            require_redirect=True,
            poll_interval=4.0,
            expected_amount=0 if config.momo_zero_trial_validation else None,
            cancel_event=cancel_event,
        )
        checkout_state = checkout.get("checkout_state") if isinstance(checkout.get("checkout_state"), dict) else {}
        total = checkout_state.get("total") if isinstance(checkout_state.get("total"), dict) else {}
        due = total.get("total") if isinstance(total.get("total"), dict) else {}
        minor = int(due.get("minorUnitsAmount") if due.get("minorUnitsAmount") not in (None, "") else (checkout.get("payable_amount_minor") or 0))
        return PaymentLinkResult(checkout_session_id=checkout["cs_id"], session_kind="openai_custom_checkout", payment_method="momo", billing_country=MOMO_COUNTRY, currency=MOMO_CURRENCY, amount_due=minor / (10 ** currency_minor_scale(MOMO_CURRENCY)), amount_due_minor=minor, billing=billing_profile, account_email=email, payment_method_id="momo", stripe_redirect_url=raw, provider_url=raw, provider_field=MOMO_RESULT_FIELD, provider_value=raw, extra={"payment_route": "momo_oaics_stripe", "momo_gateway_status": "querySession", "momo_gateway_status_code": str(gateway_state.get("status_code") or ""), "momo_checkout_momo_available": method_state, "momo_zero_trial_validation": bool(config.momo_zero_trial_validation)})
    finally:
        close(momo)
        if momo is stripe:
            stripe = None
        close(stripe)
        close(chatgpt)
