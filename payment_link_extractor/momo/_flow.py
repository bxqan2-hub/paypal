from __future__ import annotations

import random
import time
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import uuid

from ._checkout import (
    checkout_session_kind,
    extract_checkout_session_id,
    extract_processor_entity,
    extract_publishable_key,
    merge_checkout_payload,
)
from ._config import (
    DEFAULT_TIMEOUT,
    STRIPE_VERSION_FULL,
    processor_entity_for_country,
)
from ..errors import ProtocolError
from ..logging_utils import emit_log, safe_log_text
from ..models import CheckoutData, ExtractionConfig, StripeContext
from ..providers import provider_redirect_config
from ._sentinel_client import payment_sentinel_headers
from ._stripe_common import (
    STRIPE_CLIENT_BETAS,
    cs_elements_client_params,
    cs_stripe_headers,
    ensure_payment_method_offered,
    expected_amount,
    extract_checkout_totals,
    extract_redirect_to_url,
    find_submission_attempt,
    resolve_external_redirect,
    stripe_key,
)
from ._transport import response_json, set_proxy_url, stage_http_request
from ._oaics import extract_oaics_provider


MOMO_RUNTIME_VERSION = "6f8494a281"
MOMO_TRIAL_DAYS = 30
MOMO_MAX_MINOR_AMOUNT = 50


def _checkpoint(callback: Callable[[str], None] | None, stage: str) -> None:
    if callback is not None:
        callback(stage)


def _browser_id() -> str:
    return f"{uuid.uuid4()}{uuid.uuid4().hex[:8]}"


def _momo_confirm_return_url(checkout: CheckoutData, hosted_url: str) -> str:
    """Match the MoMo package's pay.openai.com success-return URL shape."""
    url = str(hosted_url or "").strip()
    if not url:
        url = f"https://checkout.stripe.com/c/pay/{checkout['cs_id']}"
    parsed = urlsplit(url)
    netloc = parsed.netloc
    if netloc.lower() == "checkout.stripe.com":
        netloc = "pay.openai.com"
    processor = processor_entity_for_country(
        "VN", str(checkout.get("processor_entity") or "")
    )
    success_url = (
        "https://chatgpt.com/checkout/verify"
        f"?stripe_session_id={checkout['cs_id']}"
        f"&processor_entity={processor}&plan_type=plus"
    )
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.setdefault("success_return_url", success_url)
    return urlunsplit((parsed.scheme or "https", netloc, parsed.path, urlencode(query), parsed.fragment))


def _momo_checkout(
    config: ExtractionConfig,
    chatgpt: Any,
    log: Any | None,
) -> CheckoutData:
    # Match the supplied MoMo extractor: warm the origin, then create the
    # promotional custom checkout with the monthly/trial fields required by
    # the VN flow. The checkout is refreshed once through checkout/update
    # before Stripe init.
    try:
        stage_http_request(
            chatgpt,
            "MoMo ChatGPT warmup",
            "GET",
            "https://chatgpt.com/",
            log,
            headers={"Referer": "https://chatgpt.com/"},
            timeout=DEFAULT_TIMEOUT,
        )
    except Exception as exc:
        emit_log(log, f"MoMo ChatGPT warmup skipped: {type(exc).__name__}")
    path = "/backend-api/payments/checkout"
    body = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": "VN", "currency": "VND"},
        "promo_campaign": {
            "promo_campaign_id": "plus-1-month-free",
            "is_coupon_from_query_param": False,
        },
        "checkout_ui_mode": "custom",
        "price_interval": "month",
        "seat_quantity": 1,
        "subscription_data": {"trial_period_days": MOMO_TRIAL_DAYS},
    }
    request_headers = {
        "Referer": "https://chatgpt.com/",
        "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8",
        "oai-language": "vi-VN",
        "x-openai-target-path": path,
        "x-openai-target-route": path,
    }
    request_headers.update(
        payment_sentinel_headers(chatgpt, "chatgpt_checkout", log)
    )
    response = stage_http_request(
        chatgpt,
        "MoMo ChatGPT checkout",
        "POST",
        "https://chatgpt.com" + path,
        log,
        json=body,
        headers=request_headers,
        timeout=DEFAULT_TIMEOUT,
    )
    if response.status_code >= 400:
        raise ProtocolError(response.status_code, f"MoMo checkout create failed: {response.text[:500]}")
    payload = response_json(response, "MoMo checkout create")
    session_id = extract_checkout_session_id(payload)
    session_kind = checkout_session_kind(session_id)
    if session_kind not in {"stripe_checkout", "openai_custom_checkout"}:
        detected = session_kind or "missing"
        raise ProtocolError(
            409,
            f"MoMo checkout returned unsupported session kind: {detected}",
        )
    checkout: CheckoutData = {
        "cs_id": session_id,
        "session_kind": session_kind,
        "processor_entity": extract_processor_entity(payload),
        "publishable_key": extract_publishable_key(payload),
        "billing_country": "VN",
        "currency": "VND",
        "payment_locale": "vi-VN",
    }
    merge_checkout_payload(checkout, payload)
    return checkout


def _momo_checkout_update(
    config: ExtractionConfig,
    chatgpt: Any,
    checkout: CheckoutData,
    log: Any | None,
) -> dict[str, Any]:
    path = "/backend-api/payments/checkout/update"
    processor = processor_entity_for_country("VN", str(checkout.get("processor_entity") or ""))
    body = {
        "checkout_session_id": checkout["cs_id"],
        "processor_entity": processor,
        "plan_name": "chatgptplusplan",
        "price_interval": "month",
        "seat_quantity": 1,
        "billing_details": {"country": "VN", "currency": "VND"},
        "checkout_ui_mode": "custom",
        "promo_campaign": {
            "promo_campaign_id": "plus-1-month-free",
            "is_coupon_from_query_param": False,
        },
        "subscription_data": {"trial_period_days": MOMO_TRIAL_DAYS},
    }
    update_proxy = str(config.update_proxy or config.checkout_proxy).strip()
    set_proxy_url(chatgpt, update_proxy)
    try:
        request_headers = {
            "Referer": f"https://chatgpt.com/checkout/{processor}/{checkout['cs_id']}",
            "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8",
            "oai-language": "vi-VN",
            "x-openai-target-path": path,
            "x-openai-target-route": path,
        }
        request_headers.update(
            payment_sentinel_headers(chatgpt, "chatgpt_checkout", log)
        )
        response = stage_http_request(
            chatgpt,
            "MoMo ChatGPT checkout/update",
            "POST",
            "https://chatgpt.com" + path,
            log,
            json=body,
            headers=request_headers,
            timeout=DEFAULT_TIMEOUT,
        )
    finally:
        set_proxy_url(chatgpt, config.checkout_proxy)
    if response.status_code >= 400:
        raise ProtocolError(response.status_code, f"MoMo checkout/update failed: {response.text[:500]}")
    payload = response_json(response, "MoMo checkout/update")
    if payload.get("success") is False:
        raise ProtocolError(409, f"MoMo checkout/update rejected: {safe_log_text(payload)}")
    merge_checkout_payload(checkout, payload)
    return payload


def _momo_stripe_init(
    stripe: Any,
    checkout: CheckoutData,
    log: Any | None,
) -> tuple[dict[str, Any], StripeContext]:
    stripe_js_id = str(uuid.uuid4())
    body = {
        "browser_locale": "vi-VN",
        "browser_timezone": "Asia/Ho_Chi_Minh",
        "elements_session_client[client_betas][0]": STRIPE_CLIENT_BETAS[0],
        "elements_session_client[client_betas][1]": STRIPE_CLIENT_BETAS[1],
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": stripe_js_id,
        "elements_session_client[locale]": "vi",
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "never",
        "elements_options_client[saved_payment_method][enable_redisplay]": "never",
        "key": stripe_key(checkout),
        "_stripe_version": STRIPE_VERSION_FULL,
    }
    response = stage_http_request(
        stripe,
        "MoMo Stripe payment_pages init",
        "POST",
        f"https://api.stripe.com/v1/payment_pages/{checkout['cs_id']}/init",
        log,
        data=body,
        headers=cs_stripe_headers(),
        timeout=DEFAULT_TIMEOUT,
    )
    if response.status_code >= 400:
        raise ProtocolError(response.status_code, f"MoMo Stripe init failed: {response.text[:500]}")
    payload = response_json(response, "MoMo Stripe init")
    ensure_payment_method_offered(payload, "momo", "MoMo Stripe init")
    totals = extract_checkout_totals(payload)
    checkout["payable_amount_minor"] = totals.get("due")
    checkout["currency"] = str(totals.get("currency") or "vnd").upper()
    ctx: StripeContext = {
        "stripe_js_id": stripe_js_id,
        "client_session_id": str(uuid.uuid4()),
        "guid": _browser_id(),
        "muid": _browser_id(),
        "sid": _browser_id(),
        "elements_session_id": f"elements_session_{uuid.uuid4().hex[:11]}",
        "elements_session_config_id": str(payload.get("config_id") or uuid.uuid4()),
        "config_id": str(payload.get("config_id") or ""),
        "init_checksum": str(payload.get("init_checksum") or ""),
        "currency": "vnd",
        "checkout_amount": expected_amount(payload),
        "locale": "vi",
        "runtime_version": MOMO_RUNTIME_VERSION,
    }
    try:
        amount = int(float(str(ctx["checkout_amount"])))
    except (TypeError, ValueError):
        amount = -1
    if amount < 0 or amount > MOMO_MAX_MINOR_AMOUNT:
        raise ProtocolError(409, f"MoMo amount must be <= {MOMO_MAX_MINOR_AMOUNT} minor units; got {ctx['checkout_amount']}")
    return payload, ctx


def _momo_payment_method(
    stripe: Any,
    checkout: CheckoutData,
    billing: dict[str, str],
    ctx: StripeContext,
    log: Any | None,
) -> str:
    body = {
        "billing_details[name]": billing.get("name") or "Nguyen Van An",
        "billing_details[email]": billing.get("email") or "buyer@example.com",
        "billing_details[phone]": billing.get("phone") or "+84901234567",
        "billing_details[address][country]": "VN",
        "billing_details[address][line1]": billing.get("line1") or "12 Nguyen Hue",
        "billing_details[address][line2]": "",
        "billing_details[address][city]": billing.get("city") or "Ho Chi Minh City",
        "billing_details[address][postal_code]": billing.get("postal_code") or "700000",
        "billing_details[address][state]": billing.get("state") or "SG",
        "type": "momo",
        "payment_user_agent": f"stripe.js/{MOMO_RUNTIME_VERSION}; stripe-js-v3/{MOMO_RUNTIME_VERSION}; payment-element; deferred-intent",
        "referrer": "https://chatgpt.com",
        "time_on_page": str(random.randint(30000, 90000)),
        "client_attribution_metadata[checkout_session_id]": checkout["cs_id"],
        "client_attribution_metadata[client_session_id]": str(ctx["client_session_id"]),
        "client_attribution_metadata[checkout_config_id]": str(ctx["config_id"]),
        "client_attribution_metadata[elements_session_id]": str(ctx["elements_session_id"]),
        "client_attribution_metadata[elements_session_config_id]": str(ctx["elements_session_config_id"]),
        "client_attribution_metadata[merchant_integration_source]": "elements",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "2021",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
        "key": stripe_key(checkout),
        "_stripe_version": STRIPE_VERSION_FULL,
    }
    response = stage_http_request(
        stripe,
        "MoMo Stripe payment_methods",
        "POST",
        "https://api.stripe.com/v1/payment_methods",
        log,
        data=body,
        headers=cs_stripe_headers(),
        timeout=DEFAULT_TIMEOUT,
    )
    if response.status_code >= 400:
        raise ProtocolError(response.status_code, f"MoMo Stripe payment_methods failed: {response.text[:500]}")
    payment_method_id = str(response_json(response, "MoMo Stripe payment_methods").get("id") or "")
    if not payment_method_id.startswith("pm_"):
        raise ProtocolError(502, "MoMo Stripe payment_methods response missing pm_ id")
    return payment_method_id


def _momo_confirm(
    stripe: Any,
    checkout: CheckoutData,
    init_payload: dict[str, Any],
    ctx: StripeContext,
    payment_method_id: str,
    log: Any | None,
) -> dict[str, Any]:
    hosted_url = str(init_payload.get("stripe_hosted_url") or "")
    body = {
        "guid": str(ctx["guid"]),
        "muid": str(ctx["muid"]),
        "sid": str(ctx["sid"]),
        "payment_method": payment_method_id,
        "init_checksum": str(ctx["init_checksum"]),
        "version": MOMO_RUNTIME_VERSION,
        "expected_amount": str(ctx["checkout_amount"]),
        "expected_payment_method_type": "momo",
        "return_url": _momo_confirm_return_url(checkout, hosted_url),
        "elements_session_client[session_id]": str(ctx["elements_session_id"]),
        "elements_session_client[locale]": "vi",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[stripe_js_id]": str(ctx["stripe_js_id"]),
        "elements_session_client[client_betas][0]": STRIPE_CLIENT_BETAS[0],
        "elements_session_client[client_betas][1]": STRIPE_CLIENT_BETAS[1],
        "elements_options_client[saved_payment_method][enable_save]": "never",
        "elements_options_client[saved_payment_method][enable_redisplay]": "never",
        "client_attribution_metadata[client_session_id]": str(ctx["client_session_id"]),
        "client_attribution_metadata[checkout_session_id]": checkout["cs_id"],
        "client_attribution_metadata[checkout_config_id]": str(ctx["config_id"]),
        "client_attribution_metadata[elements_session_id]": str(ctx["elements_session_id"]),
        "client_attribution_metadata[elements_session_config_id]": str(ctx["elements_session_config_id"]),
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "custom",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
        "consent[terms_of_service]": "accepted",
        "key": stripe_key(checkout),
        "_stripe_version": STRIPE_VERSION_FULL,
    }
    response = stage_http_request(
        stripe,
        "MoMo Stripe payment_pages confirm",
        "POST",
        f"https://api.stripe.com/v1/payment_pages/{checkout['cs_id']}/confirm",
        log,
        data=body,
        headers=cs_stripe_headers(),
        timeout=DEFAULT_TIMEOUT,
    )
    if response.status_code >= 400:
        raise ProtocolError(response.status_code, f"MoMo Stripe confirm failed: {response.text[:500]}")
    return response_json(response, "MoMo Stripe confirm")


def _momo_approve(
    chatgpt: Any,
    checkout: CheckoutData,
    log: Any | None,
) -> None:
    try:
        stage_http_request(
            chatgpt,
            "MoMo Sentinel ping",
            "POST",
            "https://chatgpt.com/backend-api/sentinel/ping",
            log,
            json={},
            headers={
                "Referer": "https://chatgpt.com/",
                "x-openai-target-path": "/backend-api/sentinel/ping",
                "x-openai-target-route": "/backend-api/sentinel/ping",
            },
            timeout=DEFAULT_TIMEOUT,
        )
    except Exception as exc:
        emit_log(log, f"MoMo Sentinel ping skipped: {type(exc).__name__}")
    processor = processor_entity_for_country("VN", str(checkout.get("processor_entity") or ""))
    path = "/backend-api/payments/checkout/approve"
    request_headers = {
        "Referer": f"https://chatgpt.com/checkout/{processor}/{checkout['cs_id']}",
        "x-openai-target-path": path,
        "x-openai-target-route": path,
    }
    request_headers.update(
        payment_sentinel_headers(chatgpt, "checkout_session_approval", log)
    )
    response = stage_http_request(
        chatgpt,
        "MoMo ChatGPT checkout approve",
        "POST",
        "https://chatgpt.com" + path,
        log,
        json={"checkout_session_id": checkout["cs_id"], "processor_entity": processor},
        headers=request_headers,
        timeout=DEFAULT_TIMEOUT,
    )
    if response.status_code >= 400:
        raise ProtocolError(response.status_code, f"MoMo ChatGPT approve failed: {response.text[:500]}")
    result = str(response_json(response, "MoMo ChatGPT approve").get("result") or "")
    if result != "approved":
        raise ProtocolError(409 if result == "blocked" else 502, f"MoMo ChatGPT approve returned {result or '?'}")


def _momo_redirect_poll(
    stripe: Any,
    checkout: CheckoutData,
    ctx: StripeContext,
    log: Any | None,
    timeout_seconds: float = 45,
) -> str:
    params = cs_elements_client_params(ctx, locale_fallback="vi")
    params.update({"key": stripe_key(checkout), "_stripe_version": STRIPE_VERSION_FULL})
    deadline = time.monotonic() + max(1.0, timeout_seconds)
    last_state = ""
    while time.monotonic() < deadline:
        response = stage_http_request(
            stripe,
            "MoMo Stripe redirect poll",
            "GET",
            f"https://api.stripe.com/v1/payment_pages/{checkout['cs_id']}",
            log,
            params=params,
            headers=cs_stripe_headers(),
            timeout=DEFAULT_TIMEOUT,
        )
        if response.status_code == 200:
            payload = response_json(response, "MoMo Stripe redirect poll")
            redirect = extract_redirect_to_url(payload)
            if redirect:
                return redirect
            submission = find_submission_attempt(payload)
            last_state = str(submission.get("state") or "")
            if last_state == "failed":
                raise ProtocolError(502, f"MoMo Stripe submission failed: {safe_log_text(submission)}")
        else:
            last_state = f"http {response.status_code}"
        time.sleep(1)
    raise ProtocolError(504, f"MoMo redirect poll timeout: state={last_state or '?'}")


def extract_momo_provider(
    config: ExtractionConfig,
    chatgpt: Any,
    stripe: Any,
    billing: dict[str, str],
    log: Any | None,
    *,
    stage_callback: Callable[[str], None] | None = None,
) -> tuple[CheckoutData, dict[str, str]]:
    if config.country.upper() != "VN":
        raise ProtocolError(400, "MoMo extraction requires country VN")
    _checkpoint(stage_callback, "checkout")
    checkout = _momo_checkout(config, chatgpt, log)
    _checkpoint(stage_callback, f"checkout_kind:{checkout['session_kind']}")
    _checkpoint(stage_callback, "checkout_update")
    _momo_checkout_update(config, chatgpt, checkout, log)
    if checkout["session_kind"] == "openai_custom_checkout":
        _checkpoint(stage_callback, "stripe_init")
        provider = extract_oaics_provider(
            config,
            chatgpt,
            stripe,
            checkout,
            billing,
            log,
            stage_callback=stage_callback,
        )
        return checkout, provider
    _checkpoint(stage_callback, "stripe_init")
    init_payload, ctx = _momo_stripe_init(stripe, checkout, log)
    _checkpoint(stage_callback, "payment_confirmation")
    payment_method_id = _momo_payment_method(stripe, checkout, billing, ctx, log)
    confirm_payload = _momo_confirm(
        stripe,
        checkout,
        init_payload,
        ctx,
        payment_method_id,
        log,
    )
    stripe_redirect = extract_redirect_to_url(confirm_payload)
    if not stripe_redirect:
        submission = find_submission_attempt(confirm_payload)
        if str(submission.get("state") or "") == "requires_approval":
            _checkpoint(stage_callback, "approve")
            _momo_approve(chatgpt, checkout, log)
        stripe_redirect = _momo_redirect_poll(stripe, checkout, ctx, log)
    _checkpoint(stage_callback, "redirect_resolution")
    provider_config = provider_redirect_config("momo")
    provider_url = resolve_external_redirect(
        stripe,
        stripe_redirect,
        preferred_hosts=provider_config["preferred_hosts"],
        log=log,
    )
    url = provider_url or stripe_redirect
    return checkout, {
        "payment_method_id": payment_method_id,
        "stripe_redirect_url": stripe_redirect,
        "provider_url": url,
        "momo_url": url,
    }
