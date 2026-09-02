from __future__ import annotations

"""Independent VN/VND MoMo extraction core."""

import base64
import json
import os
import time
from dataclasses import replace
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .auth import account_email, account_id, decode_jwt_payload
from .config import billing_for_country, currency_minor_scale
from .errors import ConfigurationError, ExtractionCancelled, ProtocolError
from .models import ExtractionConfig, PaymentLinkResult
from .momo_checkout import (
    MOMO_COUNTRY,
    MOMO_CURRENCY,
    create_checkout,
    hydrate_checkout_route,
    refresh_momo_customer_balance,
    taxes,
)
from .momo_eligibility import probe_momo_trial_eligibility
from .momo_stripe import (
    checkout_confirm,
    confirmation_token,
    elements_session,
    intent_confirm,
    redirect_url,
    resolve_momo_redirect,
    prepare_momo_link_context,
    synchronize_momo_stripe_browser_ids,
    validate_momo_url,
)
from .momo_transport import (
    MomoTransportFactory,
    capture_momo_csrf_token,
    close,
    clear_momo_pending_updates,
    momo_gateway_page_headers,
    momo_gateway_headers,
    seed_momo_account_cookie,
)

MOMO_RESULT_FIELD = "momo_url"


def _momo_account_name(access_token: str) -> str:
    payload = decode_jwt_payload(access_token)
    profile = payload.get("https://api.openai.com/profile")
    if isinstance(profile, dict):
        for key in ("name", "given_name", "nickname"):
            value = str(profile.get(key) or "").strip()
            if value:
                return value
    for key in ("name", "given_name", "nickname"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


def _momo_runtime_billing(profile: Any, config: ExtractionConfig) -> Any:
    """Overlay AT/profile or runtime billing fields without changing channel defaults."""
    values = {
        "name": str(
            getattr(config, "account_name", "")
            or _momo_account_name(str(getattr(config, "access_token", "") or ""))
            or ""
        ).strip(),
        "email": str(getattr(config, "account_email", "") or "").strip(),
        "phone": os.getenv("OPLL_MOMO_BILLING_PHONE", "").strip(),
        "line1": os.getenv("OPLL_MOMO_BILLING_LINE1", "").strip(),
        "city": os.getenv("OPLL_MOMO_BILLING_CITY", "").strip(),
        "state": os.getenv("OPLL_MOMO_BILLING_STATE", "").strip(),
        "postal_code": os.getenv("OPLL_MOMO_BILLING_POSTAL_CODE", "").strip(),
    }
    updates = {
        key: value
        for key, value in values.items()
        if value
    }
    return replace(profile, **updates) if updates else profile


def _merge_momo_checkout_billing(
    billing: dict[str, str], checkout: dict[str, Any]
) -> dict[str, str]:
    """Adopt non-empty billing fields returned by the live tax snapshot."""
    state = checkout.get("checkout_state")
    state = state if isinstance(state, dict) else {}
    address_state = state.get("billingAddress")
    address_state = address_state if isinstance(address_state, dict) else {}
    address = address_state.get("address")
    address = address if isinstance(address, dict) else address_state
    merged = dict(billing)
    name = str(address_state.get("name") or "").strip()
    if name:
        merged["name"] = name
    for key in ("line1", "city", "country", "postal_code", "state"):
        value = str(address.get(key) or "").strip()
        if value:
            merged[key] = value
    return merged


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


def _gateway_session_token(url: str) -> str:
    """Return the exact opaque ``t`` value used by MoMo's JSON contract."""
    return str(parse_qs(urlparse(url).query).get("t", [""])[0] or "").strip()


def query_gateway(
    session: Any,
    url: str,
    *,
    polls: int = 1,
    poll_interval: float | None = None,
    require_redirect: bool = False,
) -> dict[str, Any]:
    session_token = _gateway_session_token(url)
    session_id = _gateway_session_id(url)
    if not session_token or not session_id:
        raise ProtocolError(502, "Momo gateway URL has invalid session token")
    # The browser first opens the gateway page.  That navigation establishes
    # the page origin and may set the CSRF cookie used by querySession.
    opener = getattr(session, "request", None)
    if callable(opener):
        try:
            page = opener(
                "GET",
                url,
                headers=momo_gateway_page_headers(session, url),
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
    try:
        interval = (
            float(poll_interval)
            if poll_interval is not None
            else float(os.getenv("OPLL_MOMO_GATEWAY_POLL_INTERVAL", "4.25") or "4.25")
        )
    except (TypeError, ValueError):
        interval = 4.25
    interval = max(0.0, min(30.0, interval))
    bodyless = bool(getattr(session, "momo_query_session_bodyless", False))
    last: dict[str, Any] = {}
    for index in range(max(1, polls)):
        request_kwargs: dict[str, Any] = {
            "headers": momo_gateway_headers(session, url, bodyless=bodyless),
            "timeout": 30,
        }
        # Browser captures carry no postData for querySession: the gateway
        # cookie is authoritative.  Keep the historical JSON form for custom
        # sessions and retry it only when a browser-style request is rejected.
        if not bodyless:
            # HAR responses echo the full opaque t token as sessionId; some
            # older fixtures used the decoded prefix, so the latter remains a
            # fallback only after the exact form is rejected.
            request_kwargs["json"] = {"sessionId": session_id}
        response = session.request(
            "POST",
            "https://payment.momo.vn/v2/gateway/querySession",
            **request_kwargs,
        )
        if int(getattr(response, "status_code", 0) or 0) >= 400 and bodyless:
            request_kwargs["json"] = {"sessionId": session_token}
            request_kwargs["headers"] = momo_gateway_headers(session, url)
            response = session.request(
                "POST",
                "https://payment.momo.vn/v2/gateway/querySession",
                **request_kwargs,
            )
        if int(getattr(response, "status_code", 0) or 0) >= 400 and not bodyless:
            request_kwargs["json"] = {"sessionId": session_token}
            response = session.request(
                "POST",
                "https://payment.momo.vn/v2/gateway/querySession",
                **request_kwargs,
            )
        if int(getattr(response, "status_code", 0) or 0) >= 400:
            raise ProtocolError(int(response.status_code), "Momo querySession failed")
        try:
            value = response.json()
        except Exception:
            value = json.loads(str(getattr(response, "text", "{}")))
        last = value if isinstance(value, dict) else {}
        last["_poll_count"] = index + 1
        if bool(last.get("redirect")):
            break
        if index + 1 < polls:
            time.sleep(interval)
    if require_redirect:
        status_code = last.get("status_code")
        try:
            status_code = int(status_code)
        except (TypeError, ValueError):
            status_code = 0
        if status_code != 9000 or not bool(last.get("redirect")):
            raise ProtocolError(
                504,
                "Momo gateway authorization did not reach redirect "
                f"(status_code={status_code or '?'})",
            )
    return last


def extract_momo_payment_link(config: ExtractionConfig, *, transport_factory: Any = None, cancel_event: Any = None, stage_callback: Callable[[str], None] | None = None) -> PaymentLinkResult:
    if str(config.payment_method or "").lower() != "momo":
        raise ConfigurationError("Momo core requires payment_method=momo")
    if str(config.country or "").upper() != MOMO_COUNTRY:
        raise ConfigurationError("Momo core requires country=VN")
    factory = transport_factory or MomoTransportFactory(
        str(getattr(config, "momo_fingerprint", "") or "")
    )
    eligibility: dict[str, Any] = {}
    if bool(getattr(config, "momo_trial_eligibility_check", True)):
        if stage_callback:
            stage_callback("eligibility_check")
        eligibility = probe_momo_trial_eligibility(
            config,
            transport_factory=factory,
            cancel_event=cancel_event,
            stage_callback=stage_callback,
            retain_session=True,
        )
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
    billing_profile = _momo_runtime_billing(
        billing_for_country(MOMO_COUNTRY), config
    )
    email = str(getattr(config, "account_email", "") or "").strip() or account_email(
        config.access_token
    )
    if email:
        billing_profile = replace(billing_profile, email=email)
    billing = billing_profile.to_dict()
    # When eligibility ran, keep its ChatGPT session alive.  The current HAR
    # uses one device/session/cookie context from account probe through
    # Checkout Confirm; creating a second session changes the runtime receipt
    # and Sentinel context.  A later blocked response is diagnosed from the
    # dynamic approval parameters, not preclassified as a login failure.
    chatgpt = eligibility.pop("_chatgpt_session", None)
    session_reused = chatgpt is not None
    if chatgpt is None:
        chatgpt = factory.chatgpt(config, config.checkout_proxy)
    # Full extraction uses the strict HAR contract; standalone transport
    # helpers and test doubles may still exercise optional proof behavior.
    try:
        setattr(chatgpt, "momo_sentinel_required", True)
    except Exception:
        pass
    stripe = None
    momo = None
    try:
        checkpoint("checkout")
        checkout = create_checkout(
            chatgpt,
            account_email=email,
        )
        checkpoint("checkout_committed")
        checkpoint("checkout_kind:openai_custom_checkout")
        clear_momo_pending_updates(chatgpt)
        browser_context = getattr(chatgpt, "openai_sentinel_provider", None)
        enable_browser_receipts = getattr(
            browser_context, "enable_browser_receipts", None
        )
        if callable(enable_browser_receipts):
            enable_browser_receipts()
        checkpoint("checkout_hydration")
        seed_momo_account_cookie(chatgpt, account_id(config.access_token))
        hydrate_checkout_route(chatgpt, checkout)
        checkpoint("customer_balance")
        refresh_momo_customer_balance(chatgpt, checkout)
        browser_checkout_prepare = getattr(
            browser_context, "prepare_checkout_page", None
        )
        browser_context_result: dict[str, Any] = {}
        if callable(browser_checkout_prepare) and os.getenv(
            "OPLL_MOMO_BROWSER_CHECKOUT_WARMUP", "1"
        ).strip().lower() not in {"0", "false", "off", "no"}:
            checkpoint("browser_checkout_warmup")
            browser_context_result = browser_checkout_prepare(
                f"https://chatgpt.com/checkout/"
                f"{checkout.get('processor_entity') or 'openai_llc'}/"
                f"{checkout['cs_id']}",
                referer="https://chatgpt.com/?promo_campaign=plus-1-month-free",
            ) or {}
            checkout["momo_browser_stripe_context"] = browser_context_result
        # The browser prefetches the approval-flow proof as soon as the
        # Custom checkout page opens.  Stripe/Elements and the three tax
        # refreshes then run while that short-lived proof remains cached; the
        # later checkout/confirm request consumes the same proof.  Prefetch it
        # in the retained ChatGPT/Sentinel context instead of minting a new
        # approval token only at confirm time.
        approval_provider = getattr(chatgpt, "openai_sentinel_provider", None)
        prepare_flow = getattr(approval_provider, "prepare_flow", None)
        if callable(prepare_flow):
            processor_for_referer = str(
                checkout.get("processor_entity") or "openai_llc"
            )
            approval_referer = (
                f"https://chatgpt.com/checkout/{processor_for_referer}/"
                f"{checkout['cs_id']}"
            )
            checkpoint("sentinel_refresh_checkout")
            prepare_flow(
                flow="chatgpt_checkout",
                referer=approval_referer,
            )
            checkpoint("sentinel_prepare_approval")
            prepare_flow(
                flow="checkout_session_approval",
                referer=approval_referer,
            )
        # The captured VN Custom flow initializes Stripe Elements immediately
        # after Checkout, before the three tax refreshes.  Keep the same
        # session context and amount snapshot for the later confirmation
        # token rather than moving Elements behind the tax calls.
        stripe = factory.stripe(config)
        checkpoint("stripe_init")
        elements_session(stripe, checkout)
        synchronize_momo_stripe_browser_ids(chatgpt, stripe, checkout)
        checkpoint("stripe_link_context")
        prepare_momo_link_context(stripe, checkout, email)
        checkpoint("stripe_elements")
        for tax_iteration in range(1, 4):
            checkpoint("taxes")
            taxes(
                chatgpt,
                checkout,
                billing,
                tax_iteration=tax_iteration,
            )
        amount_minor = momo_checkout_payable_amount(checkout)
        if config.momo_zero_trial_validation:
            checkpoint("zero_amount_validation")
            validate_momo_amount(amount_minor)
            checkpoint("zero_amount_confirmed")
        billing = _merge_momo_checkout_billing(billing, checkout)
        billing_profile = replace(
            billing_profile,
            name=billing.get("name", billing_profile.name),
            email=billing.get("email", billing_profile.email),
            phone=billing.get("phone", billing_profile.phone),
            line1=billing.get("line1", billing_profile.line1),
            city=billing.get("city", billing_profile.city),
            state=billing.get("state", billing_profile.state),
            postal_code=billing.get("postal_code", billing_profile.postal_code),
        )
        runtime_hcaptcha = str(
            getattr(chatgpt, "momo_stripe_hcaptcha_token", "") or ""
        ).strip()
        token = confirmation_token(
            stripe,
            checkout,
            billing,
            config.stripe_hcaptcha_token or runtime_hcaptcha,
        )
        checkpoint("payment_confirmation")
        confirmed = checkout_confirm(chatgpt, checkout, token)
        intent = intent_confirm(stripe, checkout, token, confirmed)
        # The browser refreshes the approval proof once more after the Stripe
        # intent response.  Keep this as a runtime context refresh; the proof
        # is not persisted or reused by a later attempt.
        post_intent_provider = getattr(chatgpt, "openai_sentinel_provider", None)
        post_intent_prepare = getattr(post_intent_provider, "prepare_flow", None)
        if callable(post_intent_prepare):
            checkpoint("sentinel_refresh_post_intent")
            post_intent_prepare(
                flow="checkout_session_approval",
                referer=(
                    f"https://chatgpt.com/checkout/"
                    f"{checkout.get('processor_entity') or 'openai_llc'}/"
                    f"{checkout['cs_id']}"
                ),
            )
        raw = redirect_url(intent)
        if raw and not validate_momo_url(raw):
            raw = resolve_momo_redirect(stripe, raw)
        if not validate_momo_url(raw):
            raise ProtocolError(502, "Stripe response did not return a Momo gateway URL")
        checkpoint("redirect_resolution")
        momo = factory.momo(config) if callable(getattr(factory, "momo", None)) else stripe
        try:
            gateway_polls = max(
                1,
                min(
                    30,
                    int(os.getenv("OPLL_MOMO_GATEWAY_POLLS", "15") or "15"),
                ),
            )
        except ValueError:
            gateway_polls = 15
        gateway_state = query_gateway(
            momo,
            raw,
            polls=gateway_polls,
            require_redirect=True,
        )
        checkout_state = checkout.get("checkout_state") if isinstance(checkout.get("checkout_state"), dict) else {}
        total = checkout_state.get("total") if isinstance(checkout_state.get("total"), dict) else {}
        due = total.get("total") if isinstance(total.get("total"), dict) else {}
        minor = int(due.get("minorUnitsAmount") if due.get("minorUnitsAmount") not in (None, "") else (checkout.get("payable_amount_minor") or 0))
        return PaymentLinkResult(checkout_session_id=checkout["cs_id"], session_kind="openai_custom_checkout", payment_method="momo", billing_country=MOMO_COUNTRY, currency=MOMO_CURRENCY, amount_due=minor / (10 ** currency_minor_scale(MOMO_CURRENCY)), amount_due_minor=minor, billing=billing_profile, account_email=email, payment_method_id="momo", stripe_redirect_url=raw, provider_url=raw, provider_field=MOMO_RESULT_FIELD, provider_value=raw, extra={"payment_route": "momo_oaics_stripe", "momo_gateway_status": "querySession", "momo_gateway_status_code": gateway_state.get("status_code"), "momo_gateway_redirect": bool(gateway_state.get("redirect")), "momo_gateway_polls": gateway_state.get("_poll_count"), "momo_browser_profile": str(getattr(factory, "profile", {}).get("name", "")), "momo_session_reused": session_reused, "momo_pricing_http_status": eligibility.get("pricing_http_status"), "momo_preflight_http_statuses": eligibility.get("preflight_http_statuses", {}), "momo_anon_preflight_http_statuses": eligibility.get("anon_preflight_http_statuses", {}), "momo_hcaptcha_source": checkout.get("momo_hcaptcha_source", "absent"), "momo_hcaptcha_site_key": bool(checkout.get("momo_hcaptcha_site_key")), "momo_hcaptcha_rqdata": bool(checkout.get("momo_hcaptcha_rqdata") or checkout.get("momo_hcaptcha_link_rqdata")), "momo_pending_updates": len(getattr(chatgpt, "momo_pending_updates", []) or []), "momo_account_cookie": bool(getattr(chatgpt, "momo_account_cookie_present", False)), "momo_backend_auth_bridge": bool(getattr(chatgpt, "momo_backend_auth_bridge_enabled", False)), "momo_browser_receipts_enabled": bool(getattr(chatgpt, "momo_browser_receipts_enabled", False)), "momo_sentinel_provider_mode": str(getattr(chatgpt, "momo_sentinel_provider_mode", "browser") or "browser"), "momo_timezone_applied": bool(getattr(chatgpt, "momo_timezone_applied", False)), "momo_checkout_hydration": checkout.get("momo_checkout_hydration", {}), "momo_customer_balance": checkout.get("momo_customer_balance", {}), "momo_link_context": checkout.get("momo_link_context", {}), "momo_browser_stripe_context": checkout.get("momo_browser_stripe_context", browser_context_result), "momo_stripe_session_id_consistent": str(checkout.get("stripe_js_id") or "") == str(checkout.get("stripe_client_session_id") or ""), "momo_zero_trial_validation": bool(config.momo_zero_trial_validation)})
    finally:
        close(momo)
        if momo is stripe:
            stripe = None
        close(stripe)
        close(chatgpt)
