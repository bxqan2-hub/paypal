from __future__ import annotations

import json
import re
from typing import Any

from .config import DEFAULT_TIMEOUT, normalize_payment_method, processor_entity_for_country
from .errors import ConfigurationError, ProtocolError
from .logging_utils import safe_log_text
from .models import CheckoutData, ExtractionConfig
from .gopay_transport import (
    openai_sentinel_headers,
    prepare_openai_browser_session,
    response_json,
    set_proxy_url,
    stage_http_request,
)

CHECKOUT_SESSION_ID_RE = re.compile(r"(?:oaics_|cs_)[A-Za-z0-9_]+")
PUBLISHABLE_KEY_RE = re.compile(r"pk_live_[A-Za-z0-9]+")


class CheckoutCreateError(ProtocolError):
    """Structured GoPay Checkout failure used by retry orchestration."""

    def __init__(
        self,
        status_code: int,
        detail: str,
        *,
        failure_mode: str,
        retryable: bool,
    ) -> None:
        super().__init__(status_code, detail)
        self.failure_mode = str(failure_mode or "checkout_create_failed")
        self.retryable = bool(retryable)


class PromoEligibilityError(ProtocolError):
    """Structured GoPay promotion result used by retry orchestration."""

    def __init__(
        self,
        status_code: int,
        detail: str,
        *,
        failure_mode: str,
        retryable: bool,
    ) -> None:
        super().__init__(status_code, detail)
        self.failure_mode = str(failure_mode or "promo_eligibility_failed")
        self.retryable = bool(retryable)


def classify_checkout_create_failure(status_code: int, body: Any) -> tuple[str, bool]:
    """Classify GoPay Checkout creation failures for precise retries."""
    text = str(body or "").lower()
    # A 401 is the one terminal condition for an AT. Every other checkout
    # failure is treated as retryable so the caller can rotate the proxy and
    # rebuild the browser/Checkout state.
    if status_code == 401:
        return "access_token_invalid", False
    if "unusual activity" in text:
        return "unusual_activity", True
    if status_code == 429 or "rate limit" in text or "too many requests" in text:
        return "rate_limited", True
    if status_code in {408, 425} or status_code >= 500:
        return "upstream_transient", True
    if "payment method" in text and any(
        marker in text for marker in ("not available", "unavailable", "unsupported")
    ):
        return "payment_method_unavailable", True
    if status_code == 403:
        return "access_denied", True
    return "checkout_create_rejected", True


def extract_processor_entity(data: Any) -> str:
    if isinstance(data, dict):
        direct = data.get("processor_entity") or data.get("processorEntity")
        if direct:
            return str(direct).strip()
        for value in data.values():
            found = extract_processor_entity(value)
            if found:
                return found
    elif isinstance(data, list):
        for value in data:
            found = extract_processor_entity(value)
            if found:
                return found
    return ""


def extract_publishable_key(data: Any) -> str:
    if isinstance(data, dict):
        for key in ("publishable_key", "publishableKey", "stripe_publishable_key"):
            if data.get(key):
                return str(data[key]).strip()
        for value in data.values():
            found = extract_publishable_key(value)
            if found:
                return found
    elif isinstance(data, list):
        for value in data:
            found = extract_publishable_key(value)
            if found:
                return found
    text = json.dumps(data, ensure_ascii=False) if isinstance(data, (dict, list)) else str(data or "")
    match = PUBLISHABLE_KEY_RE.search(text)
    return match.group(0) if match else ""


def checkout_session_kind(session_id: str) -> str:
    value = str(session_id or "").strip()
    if value.startswith("oaics_"):
        return "openai_custom_checkout"
    if value.startswith("cs_"):
        return "stripe_checkout"
    return ""


def extract_checkout_session_id(value: Any) -> str:
    if isinstance(value, str):
        text = value.strip()
        if checkout_session_kind(text):
            return text
        match = CHECKOUT_SESSION_ID_RE.search(text)
        return match.group(0) if match else ""
    if isinstance(value, dict):
        for key in ("checkout_session_id", "session_id", "id"):
            found = extract_checkout_session_id(value.get(key))
            if found:
                return found
        for nested in value.values():
            found = extract_checkout_session_id(nested)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = extract_checkout_session_id(nested)
            if found:
                return found
    return ""


def first_value_by_key(payload: Any, key: str) -> Any:
    if isinstance(payload, dict):
        if key in payload:
            return payload[key]
        for value in payload.values():
            found = first_value_by_key(value, key)
            if found not in (None, "", [], {}):
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = first_value_by_key(value, key)
            if found not in (None, "", [], {}):
                return found
    return None


def all_values_by_key(payload: Any, key: str) -> list[Any]:
    """Collect every non-empty value for a key from nested responses."""
    found: list[Any] = []
    if isinstance(payload, dict):
        if key in payload and payload[key] not in (None, "", [], {}):
            found.append(payload[key])
        for value in payload.values():
            found.extend(all_values_by_key(value, key))
    elif isinstance(payload, list):
        for value in payload:
            found.extend(all_values_by_key(value, key))
    return found


def _payment_method_key(value: Any) -> str:
    if isinstance(value, dict):
        for key in (
            "id",
            "type",
            "name",
            "payment_method_type",
            "custom_payment_method_type_id",
        ):
            marker = str(value.get(key) or "").strip().lower()
            if marker:
                return marker
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return str(value or "").strip().lower()


def merge_payment_method_values(*collections: Any) -> list[Any]:
    """Merge nested method lists while preserving order and enriching duplicates."""
    result: list[Any] = []
    positions: dict[str, int] = {}
    for collection in collections:
        values = collection if isinstance(collection, list) else [collection]
        for value in values:
            if value in (None, "", [], {}):
                continue
            marker = _payment_method_key(value)
            if not marker:
                continue
            if marker in positions:
                index = positions[marker]
                if isinstance(result[index], dict) and isinstance(value, dict):
                    result[index] = {**result[index], **value}
                continue
            positions[marker] = len(result)
            result.append(value)
    return result


def merge_checkout_payload(checkout: CheckoutData, payload: dict[str, Any]) -> None:
    processor = extract_processor_entity(payload)
    if processor:
        checkout["processor_entity"] = processor
    publishable_key = extract_publishable_key(payload)
    if publishable_key:
        checkout["publishable_key"] = publishable_key
    method_types = merge_payment_method_values(
        checkout.get("payment_method_types") or [],
        *all_values_by_key(payload, "payment_method_types"),
    )
    custom_methods = merge_payment_method_values(
        checkout.get("custom_payment_methods") or [],
        *all_values_by_key(payload, "custom_payment_methods"),
    )
    if method_types:
        checkout["payment_method_types"] = method_types
    if custom_methods:
        checkout["custom_payment_methods"] = custom_methods
    checkout["payment_methods"] = merge_payment_method_values(
        checkout.get("payment_methods") or [], method_types, custom_methods
    )
    for key in (
        "checkout_state",
        "checkout_ui_mode",
        "confirm_return_url",
        "customer_session_client_secret",
        "checkout_session",
        "customer_details",
    ):
        value = first_value_by_key(payload, key)
        if value not in (None, "", [], {}):
            checkout[key] = value


def create_checkout(
    config: ExtractionConfig,
    chatgpt: Any,
    log: Any | None,
) -> CheckoutData:
    path = "/backend-api/payments/checkout"
    payment_method = normalize_payment_method(config.payment_method)
    if payment_method != "gopay":
        raise ConfigurationError("GoPay Checkout core requires payment_method=gopay")
    body = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "billing_details": {
            "country": config.country.upper(),
            "currency": config_currency(config),
        },
        "checkout_ui_mode": "custom",
    }
    referer = "https://chatgpt.com/"
    headers = {
        "Referer": referer,
        "x-openai-target-path": path,
        "x-openai-target-route": path,
    }
    # The browser can attach a fresh Sentinel token on the initial checkout
    # request.  Keep it optional so older deployments remain compatible.
    headers.update(
        openai_sentinel_headers(
            chatgpt,
            flow="chatgpt_checkout",
            referer=referer,
            log=log,
            required=True,
        )
    )
    response = stage_http_request(
        chatgpt,
        "ChatGPT checkout",
        "POST",
        "https://chatgpt.com" + path,
        log,
        json=body,
        headers=headers,
        timeout=DEFAULT_TIMEOUT,
    )
    if response.status_code >= 400:
        mode, retryable = classify_checkout_create_failure(response.status_code, response.text)
        raise CheckoutCreateError(
            response.status_code,
            f"checkout create failed [{mode}]: {response.text[:500]}",
            failure_mode=mode,
            retryable=retryable,
        )
    payload = response_json(response, "checkout create")
    session_id = extract_checkout_session_id(payload)
    kind = checkout_session_kind(session_id)
    if not session_id or not kind:
        raise ProtocolError(502, "checkout response missing cs_/oaics_ session id")
    checkout: CheckoutData = {
        "cs_id": session_id,
        "session_kind": kind,
        "processor_entity": extract_processor_entity(payload),
        "publishable_key": extract_publishable_key(payload),
        "billing_country": config.country.upper(),
        "currency": config_currency(config),
        "payment_locale": config_locale(config),
    }
    merge_checkout_payload(checkout, payload)
    return checkout


def check_coupon_eligibility(
    config: ExtractionConfig,
    chatgpt: Any,
    log: Any | None,
) -> dict[str, Any]:
    if not str(config.update_proxy or "").strip():
        raise ConfigurationError("update proxy is required for eligibility check")
    path = "/backend-api/promo_campaign/check_coupon"
    url = f"https://chatgpt.com{path}?coupon=plus-1-month-free&is_coupon_from_query_param=true"
    set_proxy_url(chatgpt, config.update_proxy)
    try:
        # Bootstrap the same browser origin before probing eligibility so the
        # request inherits the current `x-oai-is-pending-updates` receipt seen
        # in the PayPal/GCash HAR captures.
        prepare_openai_browser_session(chatgpt)
        response = stage_http_request(
            chatgpt,
            "Promo eligibility check",
            "GET",
            url,
            log,
            headers={
                "Referer": "https://chatgpt.com/?promo_campaign=plus-1-month-free",
                "x-openai-target-path": path,
                "x-openai-target-route": path,
            },
            timeout=DEFAULT_TIMEOUT,
        )
        if response.status_code >= 400:
            raise PromoEligibilityError(
                response.status_code,
                f"promo eligibility check failed: {safe_log_text(response.text)}",
                failure_mode=(
                    "access_token_invalid" if response.status_code == 401 else "promo_eligibility_failed"
                ),
                retryable=response.status_code != 401,
            )
        payload = response_json(response, "promo eligibility check")
        state = payload.get("state")
        if state != "eligible":
            raise PromoEligibilityError(
                409,
                f"promo eligibility rejected: state={state or '?'}",
                failure_mode="promo_not_eligible",
                retryable=True,
            )
        return payload
    finally:
        set_proxy_url(chatgpt, config.checkout_proxy)


def update_checkout(
    config: ExtractionConfig,
    chatgpt: Any,
    checkout: CheckoutData,
    log: Any | None,
) -> dict[str, Any]:
    path = "/backend-api/payments/checkout/update"
    processor = processor_entity_for_country(
        str(checkout.get("billing_country") or config.country),
        str(checkout.get("processor_entity") or ""),
    )
    body = {
        "checkout_session_id": checkout["cs_id"],
        "processor_entity": processor,
        "plan_name": "chatgptplusplan",
        "price_interval": "month",
        "seat_quantity": 1,
        "promo_campaign": {
            "promo_campaign_id": "plus-1-month-free",
            "is_coupon_from_query_param": False,
        },
    }
    set_proxy_url(chatgpt, config.update_proxy)
    try:
        response = stage_http_request(
            chatgpt,
            "ChatGPT checkout/update",
            "POST",
            "https://chatgpt.com" + path,
            log,
            json=body,
            headers={
                "Referer": f"https://chatgpt.com/checkout/{processor}/{checkout['cs_id']}",
                "x-openai-target-path": path,
                "x-openai-target-route": path,
            },
            timeout=DEFAULT_TIMEOUT,
        )
    finally:
        set_proxy_url(chatgpt, config.checkout_proxy)
    if response.status_code >= 400:
        raise ProtocolError(response.status_code, f"checkout/update failed: {response.text[:500]}")
    payload = response_json(response, "checkout/update")
    if payload.get("success") is False:
        raise ProtocolError(409, f"checkout/update rejected: {safe_log_text(payload)}")
    merge_checkout_payload(checkout, payload)
    return payload


def require_country_currency(checkout: CheckoutData, config: ExtractionConfig) -> None:
    expected_country, expected_currency, *_ = country_values(config)
    if str(checkout.get("billing_country") or "").upper() != expected_country:
        raise ProtocolError(502, f"checkout billing country is not {expected_country}")
    if str(checkout.get("currency") or "").upper() != expected_currency:
        raise ProtocolError(
            502,
            f"checkout currency is not {expected_currency}: {checkout.get('currency') or '?'}",
        )


def chatgpt_success_return_url(checkout: CheckoutData) -> str:
    entity = processor_entity_for_country(
        str(checkout.get("billing_country") or "GB"),
        str(checkout.get("processor_entity") or ""),
    )
    return (
        "https://chatgpt.com/checkout/verify?"
        f"stripe_session_id={checkout['cs_id']}&processor_entity={entity}&plan_type=plus"
    )


def openai_checkout_email(checkout: CheckoutData) -> str:
    state = checkout.get("checkout_state")
    if isinstance(state, dict) and state.get("email"):
        return str(state["email"]).strip()
    session = checkout.get("checkout_session")
    if isinstance(session, dict):
        nested = session.get("checkout_state")
        if isinstance(nested, dict) and nested.get("email"):
            return str(nested["email"]).strip()
        details = session.get("customer_details")
        if isinstance(details, dict) and details.get("email"):
            return str(details["email"]).strip()
    return ""


def config_values(config: ExtractionConfig) -> tuple[str, str, str, str]:
    from .config import country_config

    return country_config(config.country)


def country_values(config: ExtractionConfig) -> tuple[str, str, str, str]:
    return config_values(config)


def config_currency(config: ExtractionConfig) -> str:
    return config_values(config)[1]


def config_locale(config: ExtractionConfig) -> str:
    return config_values(config)[2]
