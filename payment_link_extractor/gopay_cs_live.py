from __future__ import annotations

from typing import Callable
from typing import Any

from .gopay_checkout import (
    chatgpt_success_return_url,
    merge_checkout_payload,
    openai_checkout_email,
    update_checkout,
)
import random
import time
import uuid

from .config import (
    DEFAULT_TIMEOUT,
    PROVIDER_POLL_TIMEOUT_SECONDS,
    STRIPE_VERSION_BASE,
    STRIPE_VERSION_FULL,
    normalize_payment_method,
    processor_entity_for_country,
)
from .errors import NetworkError, ProtocolError, ProviderRequiresApproval
from .logging_utils import emit_log, safe_log_text
from .models import CheckoutData, ExtractionConfig, StripeContext
from .providers import provider_redirect_config
from .gopay_stripe_common import (
    STRIPE_CLIENT_BETAS,
    STRIPE_RV_BUILD,
    cs_billing_address,
    cs_elements_client_params,
    cs_stripe_headers,
    ensure_payment_method_offered,
    expected_amount,
    extract_checkout_totals,
    extract_redirect_to_url,
    find_setup_intent,
    find_submission_attempt,
    is_paypal_ba_approval_url,
    resolve_external_redirect,
    stripe_additional_elements_params,
    stripe_context,
    stripe_deferred_intent_params,
    stripe_elements_options_params,
    stripe_key,
    stripe_js_checksum,
    stripe_rv_timestamp,
    stripe_provider_poll,
    stripe_confirm_return_url,
)
from .gopay_transport import (
    EMPTY_PENDING_UPDATES,
    openai_sentinel_headers,
    prepare_openai_browser_flow,
    rotate_openai_approval_context,
    response_json,
    safe_close,
    set_proxy_url,
    stage_http_request,
    synchronize_stripe_browser_ids,
)


GOPAY_STRIPE_RUNTIME_VERSION = STRIPE_RV_BUILD[:10]
GOPAY_FAIL_FAST_MAX_MINOR = 50


def require_gopay_fail_fast_amount(
    value: Any,
    phase: str,
    *,
    enabled: bool,
) -> int | None:
    if not enabled:
        return None
    if value in (None, ""):
        raise ProtocolError(409, f"{phase} amount is missing")
    try:
        amount = int(value)
    except (TypeError, ValueError) as exc:
        raise ProtocolError(409, f"{phase} amount is invalid") from exc
    if amount > GOPAY_FAIL_FAST_MAX_MINOR:
        raise ProtocolError(
            409,
            f"{phase} amount exceeds fail-fast threshold: {amount}",
        )
    return amount


def prefetch_checkout_approval_proof(
    config: ExtractionConfig,
    chatgpt: Any,
    checkout: CheckoutData,
    log: Any | None,
) -> None:
    """Prefetch the approval challenge at the browser-HAR position."""
    rotate_openai_approval_context(
        chatgpt,
        config,
        config.gopay_approve_proxy or config.checkout_proxy,
    )
    processor = processor_entity_for_country(
        str(checkout.get("billing_country") or "GB"),
        str(checkout.get("processor_entity") or ""),
    )
    referer = f"https://chatgpt.com/checkout/{processor}/{checkout['cs_id']}"
    prepare_openai_browser_flow(
        chatgpt,
        flow="checkout_session_approval",
        referer=referer,
        required=True,
    )


def cs_elements_session(
    stripe: Any,
    checkout: CheckoutData,
    init_payload: dict[str, Any],
    ctx: StripeContext,
    log: Any | None,
    *,
    reuse_session: bool = False,
) -> dict[str, Any]:
    methods = payment_method_types(init_payload) or ["card"]
    amount = ctx.get("checkout_amount") or "0"
    try:
        amount = str(int(amount))
    except (TypeError, ValueError):
        amount = "0"
    params: dict[str, str] = {
        "client_betas[0]": STRIPE_CLIENT_BETAS[0],
        "client_betas[1]": STRIPE_CLIENT_BETAS[1],
        "key": stripe_key(checkout),
        "_stripe_version": STRIPE_VERSION_FULL,
        "elements_init_source": "custom_checkout",
        "referrer_host": "chatgpt.com",
        "stripe_js_id": ctx["stripe_js_id"],
        "locale": ctx.get("locale") or "en-GB",
        "browser_timezone": str(ctx.get("browser_timezone") or "Asia/Jakarta"),
        "type": "deferred_intent",
        "checkout_session_id": checkout["cs_id"],
    }
    params.update(stripe_deferred_intent_params(amount, str(ctx.get("currency") or "GBP"), methods))
    if reuse_session and ctx.get("elements_session_id"):
        params["session_id"] = str(ctx["elements_session_id"])
    payment_method_configuration = first_value_by_key(init_payload, "payment_method_configuration")
    configuration_id = (
        payment_method_configuration.get("id")
        if isinstance(payment_method_configuration, dict)
        else payment_method_configuration
    )
    if configuration_id:
        params["deferred_intent[payment_method_configuration][id]"] = str(configuration_id)
    try:
        response = stage_http_request(
            stripe,
            "Stripe Elements session",
            "GET",
            "https://api.stripe.com/v1/elements/sessions",
            log,
            params=params,
            headers=cs_stripe_headers(),
            timeout=DEFAULT_TIMEOUT,
        )
    except Exception as exc:
        emit_log(log, f"Stripe Elements session exception: {type(exc).__name__}")
        return {}
    if response.status_code >= 400:
        emit_log(log, f"Stripe Elements session failed: {response.text[:300]}")
        return {}
    payload = response_json(response, "Stripe Elements session")
    real_session_id = payload.get("session_id") or payload.get("id")
    if real_session_id:
        ctx["elements_session_id"] = str(real_session_id)
    if payload.get("config_id"):
        ctx["elements_session_config_id"] = str(payload["config_id"])
    offered = payment_method_types(payload)
    if offered:
        ctx["payment_method_types"] = offered
    return payload


def stripe_init(config: ExtractionConfig, checkout: CheckoutData, log: Any | None, stripe: Any) -> tuple[dict[str, Any], str]:
    stripe_js_id = str(uuid.uuid4())
    common = {
        "browser_locale": config_locale(config),
        "browser_timezone": config_timezone(config),
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": stripe_js_id,
        "elements_session_client[locale]": config_locale(config),
        "elements_session_client[is_aggregation_expected]": "false",
        "key": stripe_key(checkout),
    }
    common.update(stripe_elements_options_params())
    # Manual approval/server updates must be negotiated on the first /init.
    # Both complete GoPay HARs send the FULL version plus both client betas;
    # BASE can return 2xx while leaving the later approval session incomplete.
    body = dict(common)
    body["_stripe_version"] = STRIPE_VERSION_FULL
    body["elements_session_client[client_betas][0]"] = STRIPE_CLIENT_BETAS[0]
    body["elements_session_client[client_betas][1]"] = STRIPE_CLIENT_BETAS[1]
    response = None
    for attempt in range(3):
        try:
            response = stage_http_request(
                stripe,
                "Stripe payment_pages init",
                "POST",
                f"https://api.stripe.com/v1/payment_pages/{checkout['cs_id']}/init",
                log,
                data=body,
                headers=cs_stripe_headers(),
                timeout=DEFAULT_TIMEOUT,
            )
            break
        except NetworkError:
            if attempt >= 2:
                raise
            emit_log(log, f"Stripe init transport retry {attempt + 2}/3 on same Checkout")
            time.sleep(0.35 * (attempt + 1))
    assert response is not None
    if response.status_code >= 400:
        raise ProtocolError(response.status_code, f"Stripe init failed: {response.text[:500]}")
    return response_json(response, "Stripe init"), stripe_js_id


def _cs_update_tax_region_fields(
    stripe: Any,
    checkout: CheckoutData,
    ctx: StripeContext,
    billing: dict[str, str],
    log: Any | None,
    fields: tuple[str, ...],
    accumulated: dict[str, str] | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    # cccy sends cumulative address stages while retaining one payment-page
    # session. A stage may add several fields atomically (for example the
    # city+postal final stage) before state is committed last.
    common = cs_elements_client_params(ctx)
    common.update(stripe_additional_elements_params("client_attribution_metadata"))
    common.update({"key": stripe_key(checkout), "_stripe_version": STRIPE_VERSION_FULL})
    accumulated = dict(accumulated or {})
    last_payload: dict[str, Any] = {}
    for field in fields:
        value = str(billing.get(field) or "").strip()
        if value:
            accumulated[field] = value
    if not accumulated:
        return last_payload, accumulated
    data = dict(common)
    data.update({f"tax_region[{name}]": item for name, item in accumulated.items()})
    stage_name = "+".join(fields)
    response = stage_http_request(
        stripe,
        f"Stripe tax_region ({stage_name})",
        "POST",
        f"https://api.stripe.com/v1/payment_pages/{checkout['cs_id']}",
        log,
        data=data,
        headers=cs_stripe_headers(),
        timeout=DEFAULT_TIMEOUT,
    )
    if response.status_code >= 400:
        emit_log(log, f"Stripe tax_region failed ({stage_name}): {response.text[:300]}")
        return last_payload, accumulated
    payload = response_json(response, "Stripe tax_region")
    if isinstance(payload, dict):
        last_payload = payload
        checkout_config_id = str(payload.get("config_id") or "").strip()
        if checkout_config_id:
            ctx["checkout_config_id"] = checkout_config_id
        amount = (payload.get("total_summary") or {}).get("total")
        if amount is None:
            amount = (payload.get("total_summary") or {}).get("due")
        if amount is not None:
            ctx["checkout_amount"] = amount
    return last_payload, accumulated


def cs_update_tax_region(
    stripe: Any,
    checkout: CheckoutData,
    ctx: StripeContext,
    billing: dict[str, str],
    log: Any | None,
) -> dict[str, Any]:
    accumulated: dict[str, str] = {}
    payload: dict[str, Any] = {}
    # cccy browser order: country -> partial -> final -> state. Each stage is
    # cumulative and reuses one Stripe Elements/payment-page session.
    for fields in (
        ("country",),
        ("line1",),
        ("city", "postal_code"),
        ("state",),
    ):
        payload, accumulated = _cs_update_tax_region_fields(
            stripe,
            checkout,
            ctx,
            billing,
            log,
            fields,
            accumulated,
        )
    return payload


def cs_snapshot_billing(
    chatgpt: Any,
    checkout: CheckoutData,
    billing: dict[str, str],
    log: Any | None,
) -> None:
    processor = processor_entity_for_country(
        str(checkout.get("billing_country") or "GB"),
        str(checkout.get("processor_entity") or ""),
    )
    try:
        response = stage_http_request(
            chatgpt,
            "ChatGPT checkout/snapshot",
            "POST",
            "https://chatgpt.com/backend-api/payments/checkout/snapshot",
            log,
            json={
                "snapshot": {
                    "billing_address": {
                        "name": billing.get("name", ""),
                        "address": cs_billing_address(billing),
                    }
                }
            },
            headers={
                "Referer": f"https://chatgpt.com/checkout/{processor}/{checkout['cs_id']}",
                "x-openai-target-path": "/backend-api/payments/checkout/snapshot",
                "x-openai-target-route": "/backend-api/payments/checkout/snapshot",
                "x-oai-is-pending-updates": EMPTY_PENDING_UPDATES,
            },
            timeout=DEFAULT_TIMEOUT,
        )
        if response.status_code >= 400:
            emit_log(log, f"ChatGPT checkout/snapshot failed: {response.text[:300]}")
    except Exception as exc:
        emit_log(log, f"ChatGPT checkout/snapshot exception: {type(exc).__name__}")


def cs_checkout_taxes(
    config: ExtractionConfig,
    chatgpt: Any,
    checkout: CheckoutData,
    billing: dict[str, str],
    log: Any | None,
) -> dict[str, Any]:
    path = "/backend-api/payments/checkout/taxes"
    processor = processor_entity_for_country(
        str(checkout.get("billing_country") or "GB"),
        str(checkout.get("processor_entity") or ""),
    )
    body = {
        "checkout_session_id": checkout["cs_id"],
        "checkout_email": openai_checkout_email(checkout) or billing["email"],
        "billing_country": config.country.upper(),
        "billing_name": billing["name"],
        "currency": config_currency(config).lower(),
        "processor_entity": processor,
        "billing_address": cs_billing_address(billing, country=config.country.upper()),
    }
    response = stage_http_request(
        chatgpt,
        "ChatGPT cs_live checkout/taxes",
        "POST",
        "https://chatgpt.com" + path,
        log,
        json=body,
        headers={
            "Referer": f"https://chatgpt.com/checkout/{processor}/{checkout['cs_id']}",
            "x-openai-target-path": path,
            "x-openai-target-route": path,
            "x-oai-is-pending-updates": EMPTY_PENDING_UPDATES,
        },
        timeout=DEFAULT_TIMEOUT,
    )
    if response.status_code >= 400:
        raise ProtocolError(response.status_code, f"cs_live checkout/taxes failed: {response.text[:500]}")
    payload = response_json(response, "cs_live checkout/taxes")
    merge_checkout_payload(checkout, payload)
    return payload


def stripe_create_payment_method(
    stripe: Any,
    checkout: CheckoutData,
    billing: dict[str, str],
    ctx: StripeContext,
    payment_method: str,
    log: Any | None,
) -> str:
    runtime = GOPAY_STRIPE_RUNTIME_VERSION
    body = {
        "billing_details[name]": billing["name"],
        "billing_details[email]": billing["email"],
        "billing_details[phone]": billing["phone"],
        "billing_details[address][country]": billing["country"],
        "billing_details[address][line1]": billing["line1"],
        "billing_details[address][city]": billing["city"],
        "billing_details[address][postal_code]": billing["postal_code"],
        "billing_details[address][state]": billing["state"],
        "type": payment_method,
        "payment_user_agent": f"stripe.js/{runtime}; stripe-js-v3/{runtime}; payment-element; deferred-intent",
        "referrer": "https://chatgpt.com",
        "time_on_page": str(random.randint(25000, 55000)),
        "client_attribution_metadata[checkout_session_id]": checkout["cs_id"],
        "client_attribution_metadata[client_session_id]": ctx["stripe_js_id"],
        "client_attribution_metadata[checkout_config_id]": ctx["config_id"],
        "client_attribution_metadata[elements_session_id]": ctx["elements_session_id"],
        "client_attribution_metadata[elements_session_config_id]": ctx["elements_session_config_id"],
        "client_attribution_metadata[merchant_integration_source]": "elements",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "2021",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
        "key": stripe_key(checkout),
        "_stripe_version": STRIPE_VERSION_BASE,
    }
    response = stage_http_request(
        stripe,
        "Stripe payment_methods",
        "POST",
        "https://api.stripe.com/v1/payment_methods",
        log,
        data=body,
        headers=cs_stripe_headers(),
        timeout=DEFAULT_TIMEOUT,
    )
    if response.status_code >= 400:
        raise ProtocolError(response.status_code, f"Stripe payment_methods failed: {response.text[:500]}")
    payment_method_id = str(response_json(response, "Stripe payment_methods").get("id") or "")
    if not payment_method_id.startswith("pm_"):
        raise ProtocolError(502, "Stripe payment_methods response missing pm_ id")
    return payment_method_id


def stripe_consumer_session_lookup(
    stripe: Any,
    checkout: CheckoutData,
    billing: dict[str, str],
    log: Any | None,
) -> dict[str, Any]:
    """Match Stripe Link consumer initialization present in both complete HARs."""
    body = {
        "request_surface": "web_elements_controller",
        "email_address": openai_checkout_email(checkout) or billing["email"],
        "email_source": "default_value",
        "session_id": checkout["cs_id"],
        "key": stripe_key(checkout),
        "do_not_log_consumer_funnel_event": "true",
    }
    response = stage_http_request(
        stripe,
        "Stripe consumer session lookup",
        "POST",
        "https://api.stripe.com/v1/consumers/sessions/lookup",
        log,
        data=body,
        headers={**cs_stripe_headers(), "Accept-Language": "en"},
        timeout=DEFAULT_TIMEOUT,
    )
    if response.status_code >= 400:
        raise ProtocolError(
            response.status_code,
            f"Stripe consumer session lookup failed: {response.text[:500]}",
        )
    return response_json(response, "Stripe consumer session lookup")


def cs_checkout_page_refresh(
    stripe: Any,
    checkout: CheckoutData,
    ctx: StripeContext,
    log: Any | None,
) -> dict[str, Any]:
    """Refresh the manual-approval payment page after a tax update round."""
    params = cs_elements_client_params(
        ctx,
        locale_fallback=str(checkout.get("payment_locale") or "id"),
    )
    params.update({"key": stripe_key(checkout), "_stripe_version": STRIPE_VERSION_FULL})
    response = stage_http_request(
        stripe,
        "Stripe payment page refresh",
        "GET",
        f"https://api.stripe.com/v1/payment_pages/{checkout['cs_id']}",
        log,
        params=params,
        headers=cs_stripe_headers(),
        timeout=DEFAULT_TIMEOUT,
    )
    if response.status_code >= 400:
        raise ProtocolError(response.status_code, f"Stripe payment page refresh failed: {response.text[:500]}")
    payload = response_json(response, "Stripe payment page refresh")
    checkout_config_id = str(payload.get("config_id") or "").strip()
    if checkout_config_id:
        ctx["checkout_config_id"] = checkout_config_id
    amount = (payload.get("total_summary") or {}).get("total")
    if amount is None:
        amount = (payload.get("total_summary") or {}).get("due")
    if amount is not None:
        ctx["checkout_amount"] = amount
    return payload


def stripe_confirm_cs_live(
    stripe: Any,
    checkout: CheckoutData,
    init_payload: dict[str, Any],
    ctx: StripeContext,
    hosted_url: str,
    payment_method: str,
    billing: dict[str, str],
    log: Any | None,
    *,
    passive_captcha_token: str = "",
    passive_captcha_ekey: str = "",
) -> dict[str, Any]:
    runtime = GOPAY_STRIPE_RUNTIME_VERSION
    amount = ctx.get("checkout_amount") or expected_amount(init_payload)
    elements_session_id = str(ctx.get("elements_session_id") or "")
    elements_session_config_id = str(ctx.get("elements_session_config_id") or "")
    init_checkout_config_id = str(ctx.get("config_id") or "")
    checkout_config_id = str(ctx.get("checkout_config_id") or init_checkout_config_id)
    body = {
        "guid": str(ctx.get("guid") or f"{uuid.uuid4()}"),
        "muid": str(ctx.get("muid") or f"{uuid.uuid4()}"),
        "sid": str(ctx.get("sid") or f"{uuid.uuid4()}"),
        "init_checksum": str(init_payload.get("init_checksum") or ctx.get("init_checksum") or ""),
        "version": runtime,
        "expected_amount": str(amount),
        "expected_payment_method_type": payment_method,
        "js_checksum": str(
            ctx.get("js_checksum")
            or init_payload.get("js_checksum")
            or stripe_js_checksum(init_payload.get("id") or ctx.get("payment_page_id"))
        ),
        "rv_timestamp": str(
            ctx.get("rv_timestamp")
            or init_payload.get("rv_timestamp")
            or stripe_rv_timestamp()
        ),
        "return_url": stripe_confirm_return_url(checkout, hosted_url),
        "_stripe_version": STRIPE_VERSION_FULL,
        "client_attribution_metadata[client_session_id]": ctx["stripe_js_id"],
        "client_attribution_metadata[checkout_session_id]": checkout["cs_id"],
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "custom",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[elements_session_id]": elements_session_id,
        "client_attribution_metadata[elements_session_config_id]": elements_session_config_id,
        "client_attribution_metadata[checkout_config_id]": checkout_config_id,
        "link_brand": "link",
        "key": stripe_key(checkout),
    }
    captcha = str(passive_captcha_token or "").strip()
    if captcha:
        # Riskier sessions may return a passive hCaptcha token.  The browser
        # sends it as a top-level confirm field (not radar_options); omit it
        # only when the caller has no fresh token.
        body["passive_captcha_token"] = captcha
        # Stripe serializes the companion ekey even when it is empty; keep
        # that key so riskier sessions retain the same 62-field contract as
        # the browser HAR.
        body["passive_captcha_ekey"] = str(passive_captcha_ekey or "")
    body.update(cs_elements_client_params(ctx))
    body.update(stripe_additional_elements_params("client_attribution_metadata"))
    body.update(
        {
            "payment_method_data[type]": payment_method,
            "payment_method_data[billing_details][name]": billing["name"],
            "payment_method_data[billing_details][email]": openai_checkout_email(checkout) or billing["email"],
            "payment_method_data[billing_details][address][line1]": billing["line1"],
            "payment_method_data[billing_details][address][city]": billing["city"],
            "payment_method_data[billing_details][address][postal_code]": billing["postal_code"],
            "payment_method_data[billing_details][address][country]": billing["country"],
            "payment_method_data[payment_user_agent]": f"stripe.js/{runtime}; stripe-js-v3/{runtime}; payment-element; deferred-intent",
            "payment_method_data[referrer]": "https://chatgpt.com",
            "payment_method_data[time_on_page]": str(random.randint(45000, 120000)),
            "payment_method_data[client_attribution_metadata][client_session_id]": ctx["stripe_js_id"],
            "payment_method_data[client_attribution_metadata][checkout_session_id]": checkout["cs_id"],
            "payment_method_data[client_attribution_metadata][merchant_integration_source]": "elements",
            "payment_method_data[client_attribution_metadata][merchant_integration_subtype]": "payment-element",
            "payment_method_data[client_attribution_metadata][merchant_integration_version]": "2021",
            "payment_method_data[client_attribution_metadata][payment_intent_creation_flow]": "deferred",
            "payment_method_data[client_attribution_metadata][payment_method_selection_flow]": "automatic",
            "payment_method_data[client_attribution_metadata][elements_session_id]": elements_session_id,
            "payment_method_data[client_attribution_metadata][elements_session_config_id]": elements_session_config_id,
            "payment_method_data[client_attribution_metadata][checkout_config_id]": init_checkout_config_id,
        }
    )
    body.update(stripe_additional_elements_params("payment_method_data[client_attribution_metadata]"))
    if billing.get("state"):
        body["payment_method_data[billing_details][address][state]"] = billing["state"]
    consent_collection = init_payload.get("consent_collection") or {}
    if consent_collection.get("terms_of_service") not in (None, "", "none"):
        body["consent[terms_of_service]"] = "accepted"
    response = stage_http_request(
        stripe,
        "Stripe payment_pages confirm",
        "POST",
        f"https://api.stripe.com/v1/payment_pages/{checkout['cs_id']}/confirm",
        log,
        data=body,
        headers=cs_stripe_headers(),
        timeout=DEFAULT_TIMEOUT,
    )
    if response.status_code >= 400:
        raise ProtocolError(response.status_code, f"Stripe confirm failed: {response.text[:500]}")
    return response_json(response, "Stripe confirm")


def chatgpt_approve(chatgpt: Any, checkout: CheckoutData, log: Any | None) -> None:
    path = "/backend-api/payments/checkout/approve"
    processor = processor_entity_for_country(
        str(checkout.get("billing_country") or "GB"),
        str(checkout.get("processor_entity") or ""),
    )
    referer = f"https://chatgpt.com/checkout/{processor}/{checkout['cs_id']}"
    approve_proxy = str(getattr(chatgpt, "openai_approve_proxy", "") or "").strip()
    if approve_proxy:
        set_proxy_url(chatgpt, approve_proxy)
    # SentinelSDK.token(flow) consumes the challenge prefetched after
    # Elements, performs the final browser ping, and exposes the exact ping
    # telemetry. It does not issue another /sentinel/req on this path.
    generated = openai_sentinel_headers(
        chatgpt,
        flow="checkout_session_approval",
        referer=referer,
        log=log,
        required=True,
    )
    approval_headers = {
        key: value
        for key, value in generated.items()
        if key
        in {
            "OpenAI-Sentinel-Token",
            "oai-web-deployment-attestation",
            "oai-device-id",
        }
        and value
    }
    response = stage_http_request(
        chatgpt,
        "ChatGPT checkout approve",
        "POST",
        "https://chatgpt.com" + path,
        log,
        json={"checkout_session_id": checkout["cs_id"], "processor_entity": processor},
        headers={
            "Referer": referer,
            "x-openai-target-path": path,
            "x-openai-target-route": path,
            "x-oai-is-pending-updates": EMPTY_PENDING_UPDATES,
            **approval_headers,
        },
        timeout=DEFAULT_TIMEOUT,
    )
    if response.status_code >= 400:
        raise ProtocolError(response.status_code, f"ChatGPT approve failed: {response.text[:500]}")
    result = str(response_json(response, "ChatGPT approve").get("result") or "")
    if result == "blocked":
        provider = getattr(chatgpt, "openai_sentinel_provider", None)
        cookie_header = str(getattr(chatgpt, "headers", {}).get("Cookie") or "")
        cookie_names = sorted(
            {
                item.split("=", 1)[0].strip()
                for item in cookie_header.split(";")
                if "=" in item and item.split("=", 1)[0].strip()
            }
        )
        error = ProtocolError(409, "ChatGPT manual approval blocked")
        error.safe_context = {
            "sentinel_provider": type(provider).__name__ if provider is not None else "",
            "browser_channel": str(getattr(provider, "_browser_channel", "") or ""),
            "browser_version": str(getattr(provider, "_browser_version", "") or ""),
            "persistent_runtime": bool(getattr(provider, "_runtime_id", "")),
            "persistent_profile": bool(getattr(provider, "_profile_path", "")),
            "sdk_sha256": str(getattr(provider, "_sdk_sha256", "") or ""),
            "challenge_shapes": list(getattr(provider, "_challenge_shapes", []) or []),
            "prepare_events": list(
                getattr(chatgpt, "openai_sentinel_prepare_events", []) or []
            ),
            "token_events": list(
                getattr(chatgpt, "openai_sentinel_token_events", []) or []
            ),
            "approval_token_length": len(
                str(generated.get("OpenAI-Sentinel-Token") or "")
            ),
            "attestation_length": len(
                str(generated.get("oai-web-deployment-attestation") or "")
            ),
            "cookie_names": cookie_names,
            "pending_updates_length": len(
                str(
                    getattr(chatgpt, "headers", {}).get(
                        "x-oai-is-pending-updates"
                    )
                    or ""
                )
            ),
        }
        raise error
    if result != "approved":
        raise ProtocolError(502, f"ChatGPT approve returned unexpected result: {result or '?'}")


def provider_redirect_after_confirm(
    chatgpt: Any,
    stripe: Any,
    checkout: CheckoutData,
    confirm_payload: dict[str, Any],
    payment_method: str,
    log: Any | None,
    ctx: StripeContext | None = None,
) -> str:
    redirect = extract_redirect_to_url(confirm_payload)
    if redirect:
        return redirect
    setup_intent = find_setup_intent(confirm_payload)
    if setup_intent:
        redirect = extract_redirect_to_url(setup_intent)
        if redirect:
            return redirect
    submission = find_submission_attempt(confirm_payload)
    if str(submission.get("state") or "") == "requires_approval":
        chatgpt_approve(chatgpt, checkout, log)
        return stripe_provider_poll(stripe, checkout, payment_method, PROVIDER_POLL_TIMEOUT_SECONDS, log, ctx)
    try:
        return stripe_provider_poll(stripe, checkout, payment_method, PROVIDER_POLL_TIMEOUT_SECONDS, log, ctx)
    except ProviderRequiresApproval:
        chatgpt_approve(chatgpt, checkout, log)
        return stripe_provider_poll(stripe, checkout, payment_method, PROVIDER_POLL_TIMEOUT_SECONDS, log, ctx)


def extract_cs_live_provider(
    config: ExtractionConfig,
    chatgpt: Any,
    stripe: Any,
    checkout: CheckoutData,
    billing: dict[str, str],
    log: Any | None,
    *,
    stage_callback: Callable[[str], None] | None = None,
) -> dict[str, str]:
    payment_method = normalize_payment_method(config.payment_method)
    init_payload, stripe_js_id = stripe_init(config, checkout, log, stripe)
    totals = extract_checkout_totals(init_payload)
    checkout["payable_amount_minor"] = totals.get("due")
    checkout["currency"] = str(totals.get("currency") or checkout.get("currency") or "GBP").upper()
    ensure_payment_method_offered(init_payload, payment_method, "cs_live Stripe init")
    require_gopay_fail_fast_amount(
        totals.get("due"),
        "custom checkout bootstrap",
        enabled=bool(config.gopay_zero_trial_validation),
    )
    hosted_url = str(init_payload.get("stripe_hosted_url") or "").strip()
    if not hosted_url:
        raise ProtocolError(502, "cs_live Stripe init missing stripe_hosted_url")
    ctx = stripe_context(init_payload, checkout, stripe_js_id)
    ctx["browser_timezone"] = config_timezone(config)
    synchronize_stripe_browser_ids(chatgpt, ctx)
    if stage_callback:
        stage_callback("elements_session")
    elements_payload = cs_elements_session(stripe, checkout, init_payload, ctx, log)
    if elements_payload:
        ensure_payment_method_offered(elements_payload, payment_method, "cs_live Elements session")
    prefetch_checkout_approval_proof(config, chatgpt, checkout, log)
    provider_proxy = str(config.gopay_provider_proxy or config.checkout_proxy).strip()
    if provider_proxy:
        set_proxy_url(chatgpt, provider_proxy)
    stripe_consumer_session_lookup(stripe, checkout, billing, log)
    if stage_callback:
        stage_callback("taxes")
    cs_update_tax_region(stripe, checkout, ctx, billing, log)
    for _round in range(2):
        cs_snapshot_billing(chatgpt, checkout, billing, log)
        cs_checkout_taxes(config, chatgpt, checkout, billing, log)
        cs_checkout_page_refresh(stripe, checkout, ctx, log)
        require_gopay_fail_fast_amount(
            ctx.get("checkout_amount"),
            "taxes refresh",
            enabled=bool(config.gopay_zero_trial_validation),
        )
    checkout["payable_amount_minor"] = ctx.get("checkout_amount")
    refreshed_elements = cs_elements_session(
        stripe,
        checkout,
        init_payload,
        ctx,
        log,
        reuse_session=True,
    )
    if refreshed_elements:
        ensure_payment_method_offered(
            refreshed_elements,
            payment_method,
            "cs_live forced-reuse Elements session",
        )
    if stage_callback:
        stage_callback("payment_confirmation")
    confirm_payload = stripe_confirm_cs_live(
        stripe,
        checkout,
        init_payload,
        ctx,
        hosted_url,
        payment_method,
        billing,
        log,
        passive_captcha_token=config.stripe_hcaptcha_token,
    )
    stripe_redirect = provider_redirect_after_confirm(
        chatgpt, stripe, checkout, confirm_payload, payment_method, log, ctx
    )
    if stage_callback:
        stage_callback("redirect_resolution")
    provider_config = provider_redirect_config(payment_method)
    provider_url = resolve_external_redirect(
        stripe,
        stripe_redirect,
        preferred_hosts=provider_config["preferred_hosts"],
        log=log,
    )
    if payment_method == "paypal" and not is_paypal_ba_approval_url(provider_url):
        raise ProtocolError(502, "PayPal BA 链解析失败：Stripe 中转地址未返回 agreements/approve?ba_token=BA- 链接")
    url = provider_url or stripe_redirect
    return {
        "payment_method_id": "",
        "stripe_redirect_url": stripe_redirect,
        "provider_url": url,
        str(provider_config["result_field"]): url,
    }


def payment_method_types(payload: Any) -> list[str]:
    from .gopay_stripe_common import payment_method_types as _payment_method_types

    return _payment_method_types(payload)


def first_value_by_key(payload: Any, key: str) -> Any:
    from .gopay_checkout import first_value_by_key as _first_value_by_key

    return _first_value_by_key(payload, key)


def config_currency(config: ExtractionConfig) -> str:
    from .config import country_config

    return country_config(config.country)[1]


def config_locale(config: ExtractionConfig) -> str:
    from .config import country_config

    return country_config(config.country)[2]


def config_timezone(config: ExtractionConfig) -> str:
    from .config import country_config

    return country_config(config.country)[3]
