# -*- coding: utf-8 -*-
"""₹0 UPI Autopay 提链协议（8 步，无浏览器）。

    1. ChatGPT 建结账单（带 promo `plus-1-month-free`）  -> cs / pk / processor_entity
    2. Stripe payment page init
    3. 更新印度税区        -> 闸门：total_summary.due 必须为 0 且支持 upi
    4. 建 UPI payment method（不要传 upi[vpa]）
    5. Stripe confirm      -> 从这一步开始消耗该号的零元资格
    6. ChatGPT approve     -> 循环到 result == "approved"（幂等，可原地重放）
    7. 读 payment page     -> setup_intent.next_action…hosted_instructions_url / qr_code
    8. 核验指引页          -> intent_state 必须是 requires_action/processing（见 verify.py）

两个 session 分工：`chatgpt` 走 chatgpt.com（带账号 Bearer），`stripe` 走 api.stripe.com
（只带 publishable key）。
"""
from __future__ import annotations

import re
import time
import uuid

from . import identity, verify
from ._vendor import oaics
from ._vendor.http import chatgpt_session, make_session, request as request_http
from ._vendor.risk import checkout_risk_headers
from .addresses import next_address

PROMO_CAMPAIGN = "plus-1-month-free"

CHECKOUT_URL = "https://chatgpt.com/backend-api/payments/checkout"
APPROVE_URL = "https://chatgpt.com/backend-api/payments/checkout/approve"
INIT_URL = "https://api.stripe.com/v1/payment_pages/%s/init"
CONFIRM_URL = "https://api.stripe.com/v1/payment_pages/%s/confirm"
PAGE_URL = "https://api.stripe.com/v1/payment_pages/%s"
PAYMENT_METHOD_URL = "https://api.stripe.com/v1/payment_methods"

# 必须是 Stripe.js 实际用的那一串（从浏览器 confirm 请求体里抓的）
STRIPE_VERSION = ("2020-08-27;custom_checkout_beta=v1; checkout_server_update_beta=v1; "
                  "checkout_manual_approval_preview=v1")

TRANSIENT_CURL_CODES = (56, 97, 28, 35, 7)
CONFIRM_ATTEMPTS = 2      # 同一会话里 confirm 重试次数
APPROVE_ATTEMPTS = 5
INSTRUCTIONS_ATTEMPTS = 6


class NetworkDown(Exception):
    """传输层断线：没到 confirm 就抛，调用方可以安全地换出口重来。"""


def _curl_code(exc: Exception) -> int:
    match = re.search(r"\bcurl(?:error)?\s*:?\s*\((\d{1,3})\)", str(exc), re.I)
    return int(match.group(1)) if match else 0


def _scan(payload) -> dict:
    """在 Stripe 返回的嵌套 JSON 里找 UPI 交付物。

    只认**看起来是 URL 的字符串值** —— approve 之类的响应里会夹带错误文本，
    里面可能正好出现 `hosted_instructions_url` 字样，不过滤就会把一段报错当成链接去核验。
    """
    found: dict = {}

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if (key in ("hosted_instructions_url", "image_url_png") and isinstance(value, str)
                        and value.startswith("http")):
                    found[key] = value
                elif key == "expires_at" and str(value).isdigit():
                    found[key] = value
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return found


def extract(account: dict, proxy: str, *, tries: int = CONFIRM_ATTEMPTS,
            emit=print) -> dict:
    """对一个账号跑完整提链流程。

    account: {"email": ..., "access_token": ..., "session_token": 可选}
    proxy:   印度出口，如 socks5h://user:pass@host:port
    返回:     成功 -> {"ok": True, "url", "qr_png", "expires_at", "due", ...}
              失败 -> {"ok": False, "error": "..."}（error 取值见 README）
    抛 NetworkDown: 传输层断线，且**没有**走到 confirm，可以安全换出口重来。
    """
    access_token = str(account.get("access_token") or "").strip()
    email = str(account.get("email") or "")
    session_token = str(account.get("session_token") or "")
    if not access_token:
        return {"ok": False, "error": "no_access_token"}
    if tries not in (1, 2):
        raise ValueError("tries 只支持 1 或 2")

    fp = identity.fingerprint(access_token, "IN")
    device_id = fp["device_id"]

    def preconfirm(call):
        """confirm 之前允许重试：这一步失败不消耗任何资格。"""
        for attempt in range(3):
            try:
                return call()
            except Exception as exc:  # noqa: BLE001
                code = _curl_code(exc)
                if attempt == 2 or code not in TRANSIENT_CURL_CODES:
                    raise
                emit("stage reconnect")
                time.sleep(0.2 * (attempt + 1))
        return None

    try:
        chatgpt = chatgpt_session(proxy, access_token, session_token, device_id=device_id,
                                  fingerprint=fp)
        oaics.warmup_chatgpt_page(chatgpt, country="IN", device_id=device_id, timeout=20,
                                  fingerprint=fp)
        sentinel = checkout_risk_headers(chatgpt.get, proxy, device_id, fp["oai_session_id"], fp,
                                         PROMO_CAMPAIGN, "oai-did=" + device_id, 25,
                                         lambda *a: None)
        headers = identity.chatgpt_headers(
            access_token, fp,
            referer="https://chatgpt.com/?promo_campaign=" + PROMO_CAMPAIGN,
            route="/backend-api/payments/checkout")
        headers.update(sentinel or {})
        emit("stage checkout")
        checkout = preconfirm(lambda: request_http(
            chatgpt, "POST", CHECKOUT_URL, headers=headers, timeout=25, json={
                "entry_point": "all_plans_pricing_modal",
                "plan_name": "chatgptplusplan",
                "billing_details": {"country": "IN", "currency": "INR"},
                "promo_campaign": {"promo_campaign_id": PROMO_CAMPAIGN,
                                   "is_coupon_from_query_param": False},
                "checkout_ui_mode": "custom",
            }))
    except Exception as exc:  # noqa: BLE001
        raise NetworkDown("checkout: %s" % str(exc)[:140])

    if checkout.status_code >= 400:
        return {"ok": False, "error": "checkout_http_%s" % checkout.status_code}
    data = checkout.json() or {}
    cs = data.get("checkout_session_id")
    pk = data.get("publishable_key")
    processor = data.get("processor_entity")
    if not cs or not pk:
        return {"ok": False, "error": "checkout_invalid"}

    stripe = make_session(proxy, impersonate=fp["impersonate"], user_agent=fp["ua"],
                          accept_language=fp["accept_language"])
    stripe_headers = identity.stripe_headers(fp)
    phase = "stripe_init"

    def call_stripe(method, url, **kwargs):
        try:
            return request_http(stripe, method, url, headers=stripe_headers, timeout=25, **kwargs)
        except Exception as exc:  # noqa: BLE001
            raise NetworkDown("%s: %s" % (phase, str(exc)[:140]))

    stripe_js_id = str(uuid.uuid4())
    init_body = {
        "browser_locale": "en-US",
        "browser_timezone": "Asia/Kolkata",
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": stripe_js_id,
        "elements_session_client[locale]": "en",
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "never",
        "elements_options_client[saved_payment_method][enable_redisplay]": "never",
        "key": pk,
        "_stripe_version": STRIPE_VERSION,
    }
    emit("stage stripe_init")
    init_response = preconfirm(lambda: call_stripe("POST", INIT_URL % cs, data=init_body))
    if init_response is not None and init_response.status_code >= 400:
        return {"ok": False, "error": "stripe_init_http_%s" % init_response.status_code}
    init = init_response.json() if init_response is not None and init_response.status_code < 400 else {}
    if not isinstance(init.get("total_summary"), dict):
        return {"ok": False, "error": "stripe_init_invalid"}

    address = next_address()
    emit("stage tax_update")
    phase = "tax_update"
    tax_response = preconfirm(lambda: call_stripe("POST", PAGE_URL % cs, data={
        "tax_region[country]": "IN",
        "tax_region[postal_code]": address["postal_code"],
        "tax_region[state]": address["state"],
        "tax_region[city]": address["city"],
        "tax_region[line1]": address["line1"],
        "key": pk,
        "_stripe_version": STRIPE_VERSION,
    }))
    if tax_response is None:
        return {"ok": False, "error": "tax_unverified"}
    if tax_response.status_code >= 400:
        return {"ok": False, "error": "tax_update_http_%s" % tax_response.status_code}
    try:
        tax_page = tax_response.json()
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "tax_unverified"}
    total = tax_page.get("total_summary") if isinstance(tax_page, dict) else None
    if not isinstance(total, dict) or type(total.get("due")) is not int:
        return {"ok": False, "error": "tax_unverified"}
    methods = tax_page.get("payment_method_types") or init.get("payment_method_types") or []
    if "upi" not in {str(item).lower() for item in methods}:
        return {"ok": False, "error": "upi_unavailable"}

    init = {**init, **tax_page}
    due = total["due"]
    emit("  acc=%s due=%s pmc=%s" % (email or "?", due, init.get("payment_method_collection")))
    if due != 0:
        return {"ok": False, "error": "nonzero_due", "due": due}

    hosted = str(init.get("stripe_hosted_url") or "")
    base_url, _, fragment = hosted.partition("#")
    config_id = str(init.get("config_id") or "")
    hits: dict = {}
    declined = False
    billing = address
    result = None
    verify_reason = ""

    for attempt in range(1, tries + 1):
        billing = next_address()
        return_url = "%s?redirect_pm_type=upi&lid=%s&ui_mode=custom" % (base_url, uuid.uuid4())
        if fragment:
            return_url += "#" + fragment

        emit("stage payment_method")
        phase = "payment_method"
        pm_response = call_stripe("POST", PAYMENT_METHOD_URL, data={
            "type": "upi",
            "key": pk,
            "billing_details[name]": billing["name"],
            "billing_details[email]": email,
            "billing_details[address][country]": "IN",
            "billing_details[address][line1]": billing["line1"],
            "billing_details[address][city]": billing["city"],
            "billing_details[address][state]": billing["state"],
            "billing_details[address][postal_code]": billing["postal_code"],
        })
        if pm_response is not None and pm_response.status_code >= 400:
            return {"ok": False, "error": "payment_method_http_%s" % pm_response.status_code}
        pm_id = str(((pm_response.json() or {}) if pm_response is not None else {}).get("id") or "")
        if not pm_id:
            return {"ok": False, "error": "payment_method_unavailable"}

        confirm_body = {
            "eid": "NA",
            "payment_method": pm_id,
            "expected_amount": str(due),
            "tax_id_collection[purchasing_as_business]": "false",
            "expected_payment_method_type": "upi",
            "return_url": return_url,
            "key": pk,
            "_stripe_version": STRIPE_VERSION,
            "version": "a34694b057",
            "client_attribution_metadata[client_session_id]": stripe_js_id,
            "client_attribution_metadata[checkout_session_id]": cs,
            "client_attribution_metadata[merchant_integration_source]": "checkout",
            "client_attribution_metadata[merchant_integration_version]": "custom_checkout",
            "client_attribution_metadata[payment_method_selection_flow]": "merchant_specified",
            "client_attribution_metadata[checkout_config_id]": config_id or str(uuid.uuid4()),
            "link_brand": "link",
        }
        if init.get("init_checksum"):
            confirm_body["init_checksum"] = str(init["init_checksum"])

        emit("stage stripe_confirm")
        emit("  confirm attempted")
        phase = "stripe_confirm"
        confirmed = call_stripe("POST", CONFIRM_URL % cs, data=confirm_body)
        if confirmed is None or confirmed.status_code >= 400:
            status = confirmed.status_code if confirmed is not None else 0
            return {"ok": False, "error": "stripe_confirm_http_%s" % status}

        approve_headers = identity.chatgpt_headers(
            access_token, fp,
            referer="https://chatgpt.com/checkout/%s/%s" % (processor, cs),
            route="/backend-api/payments/checkout/approve")
        approve_headers.update(sentinel or {})
        emit("stage approve")
        for index in range(1, APPROVE_ATTEMPTS + 1):
            try:
                approve = request_http(chatgpt, "POST", APPROVE_URL, timeout=25,
                                       json={"checkout_session_id": cs,
                                             "processor_entity": processor},
                                       headers=approve_headers)
            except Exception:  # noqa: BLE001
                result = "net"
                emit("  approve 断，原地重试 (%d/%d)" % (index, APPROVE_ATTEMPTS))
                time.sleep(0.4 * index)
                continue
            if approve.status_code < 400:
                payload = approve.json() or {}
                result = str(payload.get("result") or "")
                if result.lower() == "approved":
                    break
            elif approve.status_code == 429:
                result = "429"
                break
            else:
                result = "http%s" % approve.status_code
                break

        emit("stage instructions")
        phase = "instructions"
        si_state = ""
        for index in range(1, INSTRUCTIONS_ATTEMPTS + 1):
            try:
                page = call_stripe("GET", "%s?key=%s" % (PAGE_URL % cs, pk))
            except NetworkDown:
                # 只是「读」链接，幂等：原地重试，别换出口把整个号重来（confirm 已经烧了）
                emit("  instructions 断，原地重试 (%d/%d)" % (index, INSTRUCTIONS_ATTEMPTS))
                time.sleep(0.4 * index)
                continue
            if page is not None and page.status_code < 400:
                body = page.json() or {}
                hits.update({k: v for k, v in _scan(body).items() if v})
                setup_intent = body.get("setup_intent") or {}
                si_state = "%s/%s" % (setup_intent.get("status"),
                                      (setup_intent.get("last_setup_error") or {}).get("decline_code") or "-")
                if (setup_intent.get("status") in ("requires_payment_method", "canceled")
                        and setup_intent.get("last_setup_error")):
                    declined = True
            elif page is not None:
                emit("  instructions HTTP %s" % page.status_code)
            if hits.get("hosted_instructions_url"):
                break

        emit("    t%d appr=%-9s SI=%s" % (attempt, result, si_state))
        if hits.get("hosted_instructions_url"):
            ok, label = verify.verify_url(hits["hosted_instructions_url"], proxy=proxy)
            tail = str(hits["hosted_instructions_url"])[-16:]
            emit("  link 核验%s %s url=…%s" % ("通过" if ok else "不过", label, tail))
            if not ok:
                verify_reason = label if label not in verify.INCONCLUSIVE else "inconclusive:" + label
                hits.pop("hosted_instructions_url", None)
        if hits.get("hosted_instructions_url"):
            break
        if declined:
            break
        if result not in (None, "approved"):
            break

    if not hits.get("hosted_instructions_url"):
        error = "declined" if declined else ("link_unverified" if verify_reason else "no_link")
        return {"ok": False, "error": error, "due": due, "si": si_state,
                "approved": result, "verify": verify_reason}
    return {
        "ok": True,
        "email": email,
        "url": hits["hosted_instructions_url"],
        "qr_png": hits.get("image_url_png") or "",
        "expires_at": str(hits.get("expires_at") or ""),
        "due": due,
        "billing": billing,
        "at": int(time.time()),
    }
