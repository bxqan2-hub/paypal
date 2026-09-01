from __future__ import annotations

"""Stripe MoMo confirmation chain; no PayPal or GoPay protocol imports."""

import random
import uuid
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from .config import DEFAULT_STRIPE_PK, STRIPE_VERSION_BASE, STRIPE_VERSION_FULL
from .errors import ProtocolError
from .momo_checkout import json_payload
from .momo_transport import momo_request_headers


def _post(session: Any, url: str, stage: str, data: dict[str, Any]) -> dict[str, Any]:
    response = session.request("POST", url, data=data, timeout=30, headers={"Content-Type": "application/x-www-form-urlencoded"})
    if int(getattr(response, "status_code", 0) or 0) >= 400:
        raise ProtocolError(int(response.status_code), f"{stage} failed")
    return json_payload(response, stage)


def elements_session(session: Any, checkout: dict[str, Any]) -> dict[str, Any]:
    response = session.request("GET", "https://api.stripe.com/v1/elements/sessions", params={"locale": "vi-VN", "currency": "vnd", "mode": "subscription", "key": checkout.get("publishable_key") or DEFAULT_STRIPE_PK}, timeout=30)
    if int(getattr(response, "status_code", 0) or 0) >= 400:
        raise ProtocolError(int(response.status_code), "Momo Stripe Elements init failed")
    payload = json_payload(response, "Momo Stripe Elements init")
    checkout["elements_session"] = payload
    return payload


def confirmation_token(session: Any, checkout: dict[str, Any], billing: dict[str, str], captcha: str = "") -> str:
    key = str(checkout.get("publishable_key") or DEFAULT_STRIPE_PK)
    body: dict[str, Any] = {
        "payment_method_data[type]": "momo",
        "payment_method_data[billing_details][name]": billing["name"],
        "payment_method_data[billing_details][address][line1]": billing["line1"],
        "payment_method_data[billing_details][address][city]": billing["city"],
        "payment_method_data[billing_details][address][country]": "VN",
        "payment_method_data[billing_details][address][postal_code]": billing["postal_code"],
        "payment_method_data[billing_details][address][state]": billing["state"],
        "payment_method_data[payment_user_agent]": "stripe.js/faa58182a6; stripe-js-v3/faa58182a6; payment-element; deferred-intent",
        "payment_method_data[referrer]": "https://chatgpt.com",
        "payment_method_data[time_on_page]": str(random.randint(45000, 120000)),
        "payment_method_data[guid]": uuid.uuid4().hex,
        "payment_method_data[muid]": uuid.uuid4().hex,
        "payment_method_data[sid]": uuid.uuid4().hex,
        "setup_future_usage": "off_session",
        "mandate_data[customer_acceptance][type]": "online",
        "mandate_data[customer_acceptance][online][infer_from_client]": "true",
        "client_context[currency]": "vnd",
        "client_context[mode]": "subscription",
        "client_context[payment_method_types][0]": "momo",
        "client_context[payment_method_types][1]": "link",
        "client_context[payment_method_types][2]": "card",
        "set_as_default_payment_method": "false",
        "key": key,
        "_stripe_version": STRIPE_VERSION_BASE,
    }
    if captcha:
        body["payment_method_data[radar_options][hcaptcha_token]"] = captcha
    ctx = checkout.get("elements_session") if isinstance(checkout.get("elements_session"), dict) else {}
    for source, target in (("session_id", "elements_session_id"), ("config_id", "elements_session_config_id")):
        if ctx.get(source):
            body[f"payment_method_data[client_attribution_metadata][{target}]"] = str(ctx[source])
    payload = _post(session, "https://api.stripe.com/v1/confirmation_tokens", "Momo Stripe confirmation token", body)
    token = str(payload.get("id") or "")
    if not token.startswith("ctoken_"):
        raise ProtocolError(502, "Momo Stripe confirmation token missing ctoken_ id")
    return token


def checkout_confirm(session: Any, checkout: dict[str, Any], token: str) -> dict[str, Any]:
    path = "/backend-api/payments/checkout/confirm"
    url = "https://chatgpt.com" + path
    response = session.request(
        "POST",
        url,
        json={
            "checkout_session_id": checkout["cs_id"],
            "confirm_token": token,
            "selected_payment_method_type": "momo",
        },
        timeout=30,
        headers=momo_request_headers(
            session,
            "POST",
            url,
            {"Referer": f"https://chatgpt.com/checkout/{checkout['cs_id']}"},
            flow="checkout_session_approval",
            referer=f"https://chatgpt.com/checkout/{checkout['cs_id']}",
        ),
    )
    if int(getattr(response, "status_code", 0) or 0) >= 400:
        raise ProtocolError(int(response.status_code), "Momo checkout confirm failed")
    payload = json_payload(response, "Momo checkout confirm")
    if str(payload.get("status") or "").lower() != "success" or "client_secret" not in payload:
        raise ProtocolError(409, "Momo checkout confirm did not return a client secret")
    return payload


def intent_confirm(session: Any, checkout: dict[str, Any], token: str, confirmed: dict[str, Any]) -> dict[str, Any]:
    secret = str(confirmed.get("client_secret") or "")
    intent_id = secret.split("_secret_", 1)[0]
    if not intent_id.startswith(("pi_", "seti_")):
        raise ProtocolError(502, "Momo Stripe client secret has unsupported intent type")
    endpoint = f"https://api.stripe.com/v1/{'payment_intents' if intent_id.startswith('pi_') else 'setup_intents'}/{intent_id}/confirm"
    return _post(session, endpoint, "Momo Stripe intent confirm", {"return_url": str(confirmed.get("confirm_return_url") or f"https://chatgpt.com/checkout/verify?stripe_session_id={checkout['cs_id']}"), "confirmation_token": token, "key": str(checkout.get("publishable_key") or DEFAULT_STRIPE_PK), "_stripe_version": STRIPE_VERSION_FULL, "client_secret": secret})


def redirect_url(payload: dict[str, Any]) -> str:
    for key in ("redirect_to_url", "url", "next_action", "payment_method_options"):
        value = payload.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
        if isinstance(value, dict):
            found = redirect_url(value)
            if found:
                return found
    return ""


def validate_momo_url(value: str) -> bool:
    parsed = urlparse(str(value or ""))
    query = parse_qs(parsed.query)
    return parsed.scheme == "https" and parsed.hostname == "payment.momo.vn" and parsed.path == "/v2/gateway/pay" and bool(query.get("t")) and bool(query.get("s"))
