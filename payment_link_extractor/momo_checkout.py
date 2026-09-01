from __future__ import annotations

"""Momo OAICS Checkout requests derived from the VN HAR state machine."""

import json
import re
from typing import Any

from .config import billing_for_country, processor_entity_for_country
from .errors import ProtocolError
from .momo_transport import momo_request_headers

MOMO_COUNTRY = "VN"
MOMO_CURRENCY = "VND"
SESSION_RE = re.compile(r"(?:oaics_|cs_)[A-Za-z0-9_]+")


def _walk(value: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        for key in keys:
            if value.get(key) not in (None, "", [], {}):
                return value[key]
        for child in value.values():
            found = _walk(child, keys)
            if found not in (None, "", [], {}):
                return found
    elif isinstance(value, list):
        for child in value:
            found = _walk(child, keys)
            if found not in (None, "", [], {}):
                return found
    return None


def session_id(payload: Any) -> str:
    raw = _walk(payload, ("checkout_session_id", "checkoutSessionId", "session_id"))
    match = SESSION_RE.search(str(raw or "")) or SESSION_RE.search(json.dumps(payload, ensure_ascii=False, default=str))
    return match.group(0) if match else ""


def json_payload(response: Any, stage: str) -> dict[str, Any]:
    try:
        value = response.json()
    except Exception as exc:
        try:
            value = json.loads(str(getattr(response, "text", "")))
        except Exception as parse_exc:
            raise ProtocolError(502, f"{stage} returned invalid JSON") from parse_exc
    if not isinstance(value, dict):
        raise ProtocolError(502, f"{stage} returned a non-object payload")
    return value


def request(
    session: Any,
    method: str,
    url: str,
    stage: str,
    *,
    sentinel_flow: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    kwargs["headers"] = momo_request_headers(
        session,
        method,
        url,
        kwargs.get("headers"),
        flow=sentinel_flow,
        referer=str((kwargs.get("headers") or {}).get("Referer") or ""),
    )
    response = session.request(method, url, timeout=30, **kwargs)
    if int(getattr(response, "status_code", 0) or 0) >= 400:
        status = int(response.status_code)
        raise ProtocolError(status, f"{stage} failed (HTTP {status})")
    return json_payload(response, stage)


def create_checkout(
    session: Any,
    *,
    account_email: str = "",
    trial_eligible: bool = False,
    campaign_id: str = "",
) -> dict[str, Any]:
    path = "/backend-api/payments/checkout"
    body: dict[str, Any] = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": MOMO_COUNTRY, "currency": MOMO_CURRENCY},
        "checkout_ui_mode": "custom",
    }
    payload = request(
        session,
        "POST",
        "https://chatgpt.com" + path,
        "Momo checkout",
        json=body,
        sentinel_flow="chatgpt_checkout",
        headers={"Referer": "https://chatgpt.com/", "x-openai-target-path": path, "x-openai-target-route": path},
    )
    sid = session_id(payload)
    if not sid.startswith("oaics_"):
        raise ProtocolError(502, "Momo checkout did not return an oaics_ session")
    checkout = {
        "cs_id": sid,
        "session_kind": "openai_custom_checkout",
        # The complete VN HAR uses the OpenAI LLC checkout route.  Prefer the
        # server value, while keeping this captured route as the fallback for
        # responses that omit the display-only processor field.
        "processor_entity": str(
            _walk(payload, ("processor_entity", "processorEntity")) or "openai_llc"
        ),
        "billing_country": MOMO_COUNTRY,
        "currency": MOMO_CURRENCY,
        "account_email": account_email,
    }
    aliases = {
        "checkout_session": ("checkout_session", "checkoutSession"),
        "checkout_state": ("checkout_state", "checkoutState"),
        "publishable_key": (
            "publishable_key",
            "stripe_publishable_key",
            "stripePublishableKey",
        ),
        "customer_session_client_secret": (
            "customer_session_client_secret",
            "customerSessionClientSecret",
        ),
        "customer": ("customer", "customer_id", "customerId"),
        "confirm_return_url": ("confirm_return_url", "confirmReturnUrl"),
    }
    for target, keys in aliases.items():
        value = _walk(payload, keys)
        if value not in (None, "", [], {}):
            checkout[target] = value
    return checkout


def apply_trial_promotion(
    session: Any, checkout: dict[str, Any], *, campaign_id: str = ""
) -> dict[str, Any]:
    """Apply the eligible campaign on the existing Momo Checkout session."""
    path = "/backend-api/payments/checkout/update"
    processor = str(checkout.get("processor_entity") or "openai_llc")
    payload = request(
        session,
        "POST",
        "https://chatgpt.com" + path,
        "Momo checkout promotion",
        json={
            "checkout_session_id": checkout["cs_id"],
            "processor_entity": processor,
            "plan_name": "chatgptplusplan",
            "price_interval": "month",
            "seat_quantity": 1,
            "discount_code": None,
            "promo_campaign": {
                "promo_campaign_id": str(campaign_id or "plus-1-month-free"),
                "is_coupon_from_query_param": False,
            },
        },
        headers={
            "Referer": f"https://chatgpt.com/checkout/{processor}/{checkout['cs_id']}",
            "x-openai-target-path": path,
            "x-openai-target-route": path,
        },
    )
    checkout.update(
        {
            key: value
            for key, value in payload.items()
            if key
            in {
                "checkout_session",
                "checkout_state",
                "custom_payment_methods",
                "confirm_return_url",
                "processor_entity",
            }
        }
    )
    return payload


def taxes(session: Any, checkout: dict[str, Any], billing: dict[str, str]) -> dict[str, Any]:
    path = "/backend-api/payments/checkout/taxes"
    processor = processor_entity_for_country(MOMO_COUNTRY, str(checkout.get("processor_entity") or ""))
    payload = request(
        session,
        "POST",
        "https://chatgpt.com" + path,
        "Momo checkout taxes",
        json={
            "checkout_session_id": checkout["cs_id"],
            "checkout_email": checkout.get("account_email") or billing["email"],
            "billing_country": MOMO_COUNTRY,
            "billing_name": billing["name"],
            "currency": MOMO_CURRENCY.lower(),
            "processor_entity": processor,
            "tax_id": None,
            "billing_address": {"line1": billing["line1"], "line2": "", "city": billing["city"], "country": MOMO_COUNTRY, "postal_code": billing["postal_code"], "state": billing["state"]},
        },
        headers={"Referer": f"https://chatgpt.com/checkout/{processor}/{checkout['cs_id']}", "x-openai-target-path": path, "x-openai-target-route": path},
    )
    checkout.update({k: v for k, v in payload.items() if k in {"checkout_session", "checkout_state", "custom_payment_methods", "confirm_return_url"}})
    return payload
