#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Browser-aligned GCash checkout chain with sticky PH routing."""

import copy, html, json, time, urllib.parse, secrets, re, threading, uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from curl_cffi import requests as curl_requests
from payment_monitor import (
    CALLBACK_ACCEPTED_STATUSES,
    CALLBACK_SUCCESS_STATUSES,
    _payload_status,
    _sanitize_continue_context_headers,
    manager as payment_monitor,
)
from sentinel import mint_sentinel_sync

# ─── 常量 ──────────────────────────────────────────────────
OPENAI_BASE = "https://chatgpt.com"
ADYEN_BASE  = "https://checkoutshopper-live.adyen.com"
CHECKOUT_BASE = f"{OPENAI_BASE}/backend-api/payments/checkout"
CONTINUE_PATH = "/backend-api/payments/checkout/custom_payment_method/continue"
ENTITLEMENT_URL = f"{OPENAI_BASE}/backend-api/accounts/check/v4-2023-04-27"
PROCESSOR_ENTITY = "openai_llc"
PLAN_NAME = "chatgptplusplan"
PROMO_CAMPAIGN = {
    "promo_campaign_id": "plus-1-month-free",
    "is_coupon_from_query_param": True,
}
# 单链路尝试次数和执行器的物理上限。
MAX_ATTEMPTS = 5
MAX_WORKERS = 12
MAX_QUEUE_SIZE = 200
# 超时
HTTP_TIMEOUT = 30
QR_TTL_SECONDS = 5 * 60
CALLBACK_MAX_ATTEMPTS = 3
CALLBACK_RETRY_DELAYS = (0.75, 1.5)
CALLBACK_VERIFY_DELAYS = (0.0, 0.75, 1.5)
# Keep every retry on one known curl_cffi profile so a test changes only the
# proxy/checkout state instead of changing the TLS and browser identity too.
BROWSER_PROFILE = ("chrome145", "145", "145.0.0.0")
TLS_IMPERSONATE, CHROME_MAJOR, CHROME_FULL_VERSION = BROWSER_PROFILE
CHROME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    f"Chrome/{CHROME_FULL_VERSION} Safari/537.36"
)


class TokenRevokedError(RuntimeError):
    """The upstream explicitly invalidated the supplied access token."""

# ─── 工具 ──────────────────────────────────────────────────

def _bearer(token):
    return f"Bearer {token}"

def _headers(token=None, account_id=None, content_type="application/json", html=False):
    h = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8" if html else "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"{OPENAI_BASE}/",
        "Sec-Fetch-Site": "cross-site" if html else "same-origin",
        "Sec-Fetch-Mode": "navigate" if html else "cors",
        "Sec-Fetch-Dest": "document" if html else "empty",
    }
    if html:
        h["Upgrade-Insecure-Requests"] = "1"
    else:
        h["Origin"] = OPENAI_BASE
    if content_type:
        h["Content-Type"] = content_type
    if token:
        h["Authorization"] = _bearer(token)
    if account_id:
        h["ChatGPT-Account-Id"] = str(account_id)
    return h



def _parse_proxy(proxy):
    """解析代理字符串，返回 (type, host, port, user, pass)"""
    if not proxy:
        return None, None, None, None, None

    # socks5://host:port
    if proxy.startswith("socks5://"):
        from urllib.parse import urlparse
        p = urlparse(proxy)
        return "socks5", p.hostname, p.port or 1080, p.username, p.password

    # http(s)://user:pass@host:port
    if proxy.startswith("http://") or proxy.startswith("https://"):
        from urllib.parse import urlparse
        p = urlparse(proxy)
        default_port = 443 if p.scheme == "https" else 80
        return p.scheme, p.hostname, p.port or default_port, p.username, p.password

    # host:port:user:pass → HTTP 代理（CONNECT）
    parts = proxy.split(":", 3)
    if len(parts) == 4:
        return "http", parts[0], int(parts[1]), parts[2], parts[3]

    # host:port
    if len(parts) == 2:
        return "http", parts[0], int(parts[1]), None, None

    return "http", None, None, None, None


def _ensure_full_chain_proxy_supported(proxy):
    """Reject proxy modes that the Chromium monitoring stage cannot use."""
    proxy_type, _, _, username, password = _parse_proxy(proxy)
    if proxy_type == "socks5" and (username is not None or password is not None):
        raise RuntimeError(
            "带账号密码的 SOCKS5 不支持完整 GCash 链路；请改用 "
            "host:port:user:pass（HTTP）、http://user:pass@host:port，"
            "或无认证 socks5://host:port"
        )


def _proxy_url(proxy):
    if not proxy:
        return ""
    ptype, host, port, user, password = _parse_proxy(proxy)
    if not host or not port:
        raise RuntimeError("代理格式无效")
    scheme = "socks5h" if ptype == "socks5" else ptype
    auth = ""
    if user is not None:
        encoded_user = urllib.parse.quote(urllib.parse.unquote(str(user)), safe="")
        encoded_password = urllib.parse.quote(urllib.parse.unquote(str(password or "")), safe="")
        auth = f"{encoded_user}:{encoded_password}@"
    rendered_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"{scheme}://{auth}{rendered_host}:{port}"


def _browser_cookies(session):
    """Copy non-auth browser cookies without exposing the bearer token."""
    output = []
    try:
        cookies = session.cookies.jar
    except Exception:
        return output
    for cookie in cookies:
        if not cookie.domain or not cookie.name:
            continue
        item = {
            "name": str(cookie.name),
            "value": str(cookie.value),
            "domain": str(cookie.domain),
            "path": str(cookie.path or "/"),
            "httpOnly": bool(cookie.has_nonstandard_attr("HttpOnly")),
            "secure": bool(cookie.secure),
        }
        if cookie.expires and cookie.expires > time.time():
            item["expires"] = float(cookie.expires)
        output.append(item)
    return output


def _session_cookie_header(session):
    """Return only ChatGPT/OpenAI cookies from the active attempt."""
    pairs = {}
    try:
        for cookie in session.cookies:
            name = str(getattr(cookie, "name", "") or "").strip()
            value = str(getattr(cookie, "value", "") or "").strip()
            domain = str(getattr(cookie, "domain", "") or "").lower()
            if name and value and (
                not domain or "chatgpt.com" in domain or "openai.com" in domain
            ):
                pairs[name] = value
    except Exception:
        try:
            values = session.cookies.get_dict()
        except Exception:
            values = {}
        for name, value in values.items() if isinstance(values, dict) else ():
            if str(name).strip() and str(value).strip():
                pairs[str(name).strip()] = str(value).strip()
    return "; ".join(f"{name}={value}" for name, value in pairs.items())


def _http_error(response, url):
    raw_body = response.text or ""
    body = raw_body
    body = re.sub(r"eyJ[A-Za-z0-9_.-]+", "<token>", body)
    body = re.sub(r"<[^>]+>", " ", body)
    body = re.sub(r"\s+", " ", body).strip()[:240]
    path = urllib.parse.urlparse(url).path or "/"
    trace = response.headers.get("x-request-id") or response.headers.get("cf-ray") or ""
    content_type = (response.headers.get("content-type") or "").lower()
    is_cf_challenge = (
        response.status_code == 403
        and response.headers.get("cf-ray")
        and (
            "text/html" in content_type
            or "challenge-platform" in raw_body.lower()
            or "just a moment" in raw_body.lower()
        )
    )
    if is_cf_challenge:
        body = "Cloudflare 拒绝了请求" + (f"；{body}" if body else "")
    detail = body or response.reason or "请求失败"
    suffix = f" [trace={trace}]" if trace else ""
    message = f"{path} HTTP {response.status_code}：{detail}{suffix}"
    if response.status_code == 401 and (
        "token_revoked" in raw_body.lower()
        or "invalidated oauth token" in raw_body.lower()
    ):
        return TokenRevokedError(message)
    return RuntimeError(message)


def _request(method, url, headers, data=None, timeout=HTTP_TIMEOUT, proxy=None, follow_redirect=False, session=None):
    if data is not None and isinstance(data, (dict, list)):
        data = json.dumps(data).encode("utf-8")

    client = session or curl_requests.Session(impersonate=TLS_IMPERSONATE)
    own_session = session is None
    try:
        proxy_url = _proxy_url(proxy)
        request_headers = dict(headers or {})
        parsed_url = urllib.parse.urlparse(url)
        if parsed_url.hostname == "chatgpt.com" and parsed_url.path.startswith("/backend-api/"):
            request_headers.setdefault("x-openai-target-path", parsed_url.path)
            request_headers.setdefault("x-openai-target-route", parsed_url.path)
        kwargs = {
            "headers": request_headers,
            "data": data,
            "timeout": timeout,
            "allow_redirects": follow_redirect,
            "verify": True,
        }
        if proxy_url:
            kwargs["proxies"] = {"http": proxy_url, "https": proxy_url}
        response = client.request(method, url, **kwargs)
        if response.status_code >= 400:
            raise _http_error(response, url)
        if follow_redirect:
            return str(response.url), response.text, dict(response.headers)
        body = response.text
        if not body:
            return {}, dict(response.headers)
        try:
            return json.loads(body), dict(response.headers)
        except json.JSONDecodeError:
            content_type = response.headers.get("content-type", "")
            raise RuntimeError(f"{urllib.parse.urlparse(url).path} 响应不是 JSON（{content_type}）")
    except RuntimeError:
        raise
    except Exception as e:
        message = re.sub(r"(?i)(https?://)[^/@\s]+@", r"\1<credentials>@", str(e))
        if "timeout" in message.lower() or "timed out" in message.lower():
            raise RuntimeError("连接超时")
        raise RuntimeError(f"连接失败：{message[:200]}")
    finally:
        if own_session:
            client.close()


def _amount_is_zero(value):
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        return value == 0
    if isinstance(value, dict):
        return any(_amount_is_zero(value.get(key)) for key in (
            "value", "amount", "total", "subtotal",
            "minor_units_amount", "minorUnitsAmount", "minor_unit_amount",
        ))
    match = re.search(r"-?\d+(?:\.\d+)?", str(value).strip().replace(",", ""))
    if not match:
        return False
    try:
        return float(match.group(0)) == 0
    except ValueError:
        return False


def _amount_number(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, dict):
        for key in (
            "minor_units_amount", "minorUnitsAmount", "minor_unit_amount",
            "amount", "value", "total",
        ):
            if key in value:
                parsed = _amount_number(value[key])
                if parsed is not None:
                    return parsed
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", str(value).strip().replace(",", ""))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _checkout_amount(resp):
    """Return the checkout payable amount without mistaking a zero tax for total."""
    if not isinstance(resp, (dict, list)):
        return None, ""

    if isinstance(resp, dict):
        checkout_session = resp.get("checkout_session")
        if isinstance(checkout_session, dict):
            for key in ("amount_total", "amount_due", "grand_total"):
                if key in checkout_session:
                    amount = _amount_number(checkout_session[key])
                    if amount is not None:
                        return amount, f"checkout_session.{key}"

    preferred_keys = ("amount_total", "amount_due", "grand_total")
    for node in _walk_json(resp):
        if not isinstance(node, dict):
            continue
        for key in preferred_keys:
            if key in node:
                amount = _amount_number(node[key])
                if amount is not None:
                    return amount, key

    for node in _walk_json(resp):
        if not isinstance(node, dict):
            continue
        state = node.get("checkout_state")
        if isinstance(state, dict) and "total" in state:
            amount = _amount_number(state["total"])
            if amount is not None:
                return amount, "checkout_state.total"
        summary = node.get("total_summary")
        if isinstance(summary, dict):
            for key in ("due", "total"):
                if key in summary:
                    amount = _amount_number(summary[key])
                    if amount is not None:
                        return amount, f"total_summary.{key}"

    if isinstance(resp, dict):
        for key in ("amount", "subtotal"):
            if key in resp:
                amount = _amount_number(resp[key])
                if amount is not None:
                    return amount, key
    return None, ""


def _response_amount_is_zero(resp):
    amount, source = _checkout_amount(resp)
    return bool(source) and amount == 0


def _walk_json(value):
    """Yield every node in a decoded JSON response."""
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _extract_prefixed_string(resp, prefix):
    for value in _walk_json(resp):
        if isinstance(value, str) and value.startswith(prefix):
            return value
    return ""


def _extract_checkout_id(resp):
    return _extract_prefixed_string(resp, "oaics_")


def _extract_processor_entity(resp):
    for value in _walk_json(resp):
        if not isinstance(value, dict):
            continue
        entity = value.get("processor_entity")
        if isinstance(entity, str) and entity:
            return entity
    return ""


def _explicit_payment_status(resp):
    """Read only payment-specific statuses when verifying an ambiguous callback."""
    for value in _walk_json(resp):
        if not isinstance(value, dict):
            continue
        for key in ("payment_status", "paymentStatus", "result_status", "resultStatus"):
            status = value.get(key)
            if isinstance(status, str) and status:
                return re.sub(r"[^a-z0-9_-]", "", status.lower())[:40]
    return ""


def _active_plus_entitlement(resp, account_id=""):
    if not isinstance(resp, dict):
        return False
    accounts = resp.get("accounts")
    if not isinstance(accounts, dict):
        return False
    account_id = str(account_id or "").strip()
    if account_id:
        record = accounts.get(account_id)
    else:
        record = accounts.get("default")
    if not isinstance(record, dict):
        return False
    entitlement = record.get("entitlement")
    account = record.get("account")
    if not isinstance(entitlement, dict) or not isinstance(account, dict):
        return False
    plan_type = str(account.get("plan_type") or "").strip().lower()
    subscription_plan = str(
        entitlement.get("subscription_plan") or ""
    ).strip().lower()
    return bool(
        entitlement.get("has_active_subscription") is True
        and plan_type == "plus"
        and subscription_plan == PLAN_NAME
    )


def _verify_page_context(body):
    source = html.unescape(str(body or ""))
    patterns = {
        "account_id": r'"account"\s*:\s*\{\s*"id"\s*:\s*"([^"]+)"',
        "oai_session_id": r'"sessionId"\s*:\s*"([^"]+)"',
        "web_deployment_attestation": (
            r'"webDeploymentAttestation"\s*:\s*"([^"]+)"'
        ),
        "client_version": r'<html[^>]+data-build="([^"]+)"',
        "client_build_number": r'<html[^>]+data-seq="([^"]+)"',
    }
    output = {}
    for key, pattern in patterns.items():
        match = re.search(pattern, source, flags=re.IGNORECASE)
        if match and match.group(1).strip():
            output[key] = match.group(1).strip()
    return output


def _callback_error_is_retryable(error):
    text = str(error or "").lower()
    return any(marker in text for marker in (
        "连接超时", "连接失败", "timeout", "timed out", "connection reset",
        "connection closed", "eof", "http 408", "http 425", "http 429",
        "http 500", "http 502", "http 503", "http 504",
    ))


def _callback_action_result(redirect_result, native_action_result=None):
    redirect_result = str(redirect_result or "")
    if not redirect_result:
        raise RuntimeError("支付回调参数不完整")
    if isinstance(native_action_result, dict):
        native_redirect = native_action_result.get("redirectResult")
        if native_redirect == redirect_result:
            try:
                serialized = json.dumps(native_action_result, separators=(",", ":"))
            except (TypeError, ValueError):
                serialized = ""
            if serialized and len(serialized) <= 32_768:
                return copy.deepcopy(native_action_result)
    return {"redirectResult": redirect_result}


def _extract_cpmt(resp):
    # A checkout can advertise several custom methods. Prefer the ID explicitly
    # paired with GCash before falling back to the first cpmt token.
    for value in _walk_json(resp):
        if not isinstance(value, dict):
            continue
        strings = [child for child in value.values() if isinstance(child, str)]
        if any(child.lower() == "gcash" for child in strings):
            cpmt = next((child for child in strings if child.startswith("cpmt_")), "")
            if cpmt:
                return cpmt
    candidates = []
    for value in _walk_json(resp):
        if isinstance(value, str) and value.startswith("cpmt_") and value not in candidates:
            candidates.append(value)
    return candidates[0] if len(candidates) == 1 else ""


def _is_gcash_url(url):
    try:
        parsed = urllib.parse.urlparse(str(url))
        host = (parsed.hostname or "").lower()
    except Exception:
        return False
    return parsed.scheme == "https" and (
        host == "gcash.com" or host.endswith(".gcash.com")
    )


def _is_adyen_url(url):
    try:
        parsed = urllib.parse.urlparse(str(url))
        host = (parsed.hostname or "").lower()
    except Exception:
        return False
    return parsed.scheme == "https" and (
        host == "adyen.com" or host.endswith(".adyen.com")
    )


def _is_adyen_checkout_redirect(url):
    """Match the exact GCash redirect action observed in the browser flow."""
    try:
        parsed = urllib.parse.urlparse(str(url))
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    except Exception:
        return False
    return bool(
        parsed.scheme == "https"
        and (parsed.hostname or "").lower() == "checkoutshopper-live.adyen.com"
        and parsed.path == "/checkoutshopper/checkoutPaymentRedirect"
        and query.get("redirectData", [""])[0]
    )


def _retry_decision(result):
    """Return whether a fresh Checkout is safe and useful before payment starts."""
    current_step = str(result.get("current_step") or "")
    err = str(result.get("error_message") or "").lower()
    if any(value in err for value in (
        "token_revoked", "invalidated oauth token", "invalid access token",
        "authentication required", "http 401",
    )):
        return False, ""
    if "checkout_creation_rate_limited" in err or "too many checkout attempts" in err:
        return False, ""
    if current_step == "proxy_test":
        return True, "代理预检失败，换节点重试"
    if current_step == "create_checkout" and any(kw in err for kw in (
        "连接失败", "连接超时", "timeout", "timed out", "refused", "proxy",
        "无法连接", "connection", "reset", "eof", "cloudflare",
        "unusual activity", "http 500", "http 502", "http 503", "http 504",
    )):
        return True, "建单连接失败，换节点重试"
    if current_step == "configure_taxes" and "promo_not_applied" in err:
        return True, "优惠未落地，重建 Checkout"
    if current_step == "configure_taxes" and "gcash_custom_method_missing" in err:
        return True, "GCash 支付方式未生成，重建 Checkout"
    if current_step == "confirm_payment" and (
        "confirm_blocked" in err or "confirm status=blocked" in err
    ):
        return True, "confirm 被阻断，重建 Checkout"
    return False, ""


def _is_retryable_failure(result):
    return _retry_decision(result)[0]


# ─── 核心链路 ──────────────────────────────────────────────

class GCashChain:
    """单账号的 GCash 支付链路处理"""

    def __init__(
        self,
        token,
        client_account_id,
        ph_proxy=None,
        vn_proxy=None,
        account_id=None,
        billing_email="",
        billing_name="",
        on_update=None,
        proxy=None,
        cancel_check=None,
    ):
        self.token = token
        self.client_account_id = client_account_id
        self.account_id = str(account_id or "")
        self.proxy = proxy or ph_proxy or vn_proxy
        # Compatibility aliases for callers created before the single-pool upgrade.
        self.ph_proxy = self.proxy
        self.vn_proxy = self.proxy
        self.cid = None            # checkout ID (oaics_xxx)
        self.cpmt = None           # custom payment method token
        self.processor_entity = PROCESSOR_ENTITY
        self.promo_applied = False
        self.checkout_amount = None
        self.checkout_amount_source = ""
        self.billing_email = str(billing_email or "").strip()
        self.billing_name = str(billing_name or "").strip() or "GCash User"
        self.adyen_url = None      # Adyen 重定向 URL
        self.gcash_url = None      # 最终 GCash 授权 URL
        self.payment_route = ""    # adyen_redirect/direct_gcash
        self.qr_text = None        # 二维码文本
        self.qr_short = None       # 短链接
        self.net_auth_id = None    # GCash netAuthId
        self.qr_expires_at = None  # 二维码过期时间
        self.monitor_id = None
        self.callback_status = "unavailable"
        self.status = "pending"    # pending/running/success/failed
        self.error_message = ""
        self.current_step = "init"
        self.steps = []
        (
            self.tls_impersonate,
            self.chrome_major,
            self.chrome_full_version,
        ) = BROWSER_PROFILE
        self.user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{self.chrome_full_version} Safari/537.36"
        )
        self.device_id = str(uuid.uuid4())
        self.oai_session_id = str(uuid.uuid4())
        self.datadog_trace_id = str(secrets.randbits(63))
        self.datadog_parent_id = str(secrets.randbits(63))
        self.frontend_context = {}
        self._frontend_context_prepared = False
        self.preflight_proxy_trace = {"country": "", "ip": ""}
        self._session = curl_requests.Session(impersonate=self.tls_impersonate)
        try:
            self._session.cookies.set(
                "oai-did", self.device_id, domain=".chatgpt.com", path="/"
            )
        except Exception:
            pass
        self._session_transferred = False
        self._on_update = on_update
        self._cancel_check = cancel_check

    def _fingerprint_headers(self):
        return {
            "User-Agent": self.user_agent,
            "oai-device-id": self.device_id,
            "oai-language": "en-US",
            "sec-ch-ua": (
                f'"Google Chrome";v="{self.chrome_major}", '
                f'"Chromium";v="{self.chrome_major}", "Not.A/Brand";v="24"'
            ),
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "priority": "u=1, i",
        }

    def _frontend_api_context_headers(self):
        headers = {
            "oai-session-id": self.oai_session_id,
            "x-datadog-origin": "rum",
            "x-datadog-sampling-priority": "1",
            "x-datadog-trace-id": self.datadog_trace_id,
            "x-datadog-parent-id": self.datadog_parent_id,
        }
        canonical = {
            "oai-session-id": "oai-session-id",
            "oai-web-deployment-attestation": "oai-web-deployment-attestation",
            "x-oai-is-client-observation": "x-oai-is-client-observation",
            "oai-client-build-number": "oai-client-build-number",
            "oai-client-version": "oai-client-version",
        }
        for source, target in canonical.items():
            value = str(self.frontend_context.get(source) or "").strip()
            if value:
                headers[target] = value
        return headers

    def _prepare_frontend_context(self):
        if self._frontend_context_prepared:
            return
        self._frontend_context_prepared = True
        try:
            context = payment_monitor.collect_frontend_context(
                proxy=self.proxy,
                token=self.token,
                account_id=self.account_id,
                cookies=_browser_cookies(self._session),
                device_id=self.device_id,
                user_agent=self.user_agent,
            )
        except Exception:
            return
        native = _sanitize_continue_context_headers(
            (context or {}).get("headers")
        )
        native_account_id = native.get("chatgpt-account-id", "")
        native_device_id = native.get("oai-device-id", "")
        if self.account_id and native_account_id != self.account_id:
            return
        if native_device_id and native_device_id != self.device_id:
            return
        self.frontend_context.update(native)
        self._merge_browser_cookies(context)

    def _bootstrap(self, proxy, session, page_url=None):
        page_url = page_url or f"{OPENAI_BASE}/"
        headers = _headers(token=None, content_type=None, html=True)
        headers.update(self._fingerprint_headers())
        headers["Sec-Fetch-Site"] = "none"
        _, body, _ = _request(
            "GET", page_url, headers,
            proxy=proxy, follow_redirect=True, session=session,
        )
        page_context = _verify_page_context(body)
        header_map = {
            "oai_session_id": "oai-session-id",
            "web_deployment_attestation": "oai-web-deployment-attestation",
            "client_build_number": "oai-client-build-number",
            "client_version": "oai-client-version",
        }
        for source, target in header_map.items():
            value = str(page_context.get(source) or "").strip()
            if value and not self.frontend_context.get(target):
                self.frontend_context[target] = value

    def _preflight_proxy(self):
        if not self.proxy:
            return {"country": "", "ip": ""}
        _ensure_full_chain_proxy_supported(self.proxy)
        headers = _headers(token=None, content_type=None, html=True)
        headers.update(self._fingerprint_headers())
        headers["Sec-Fetch-Site"] = "none"
        _, body, _ = _request(
            "GET",
            f"{OPENAI_BASE}/cdn-cgi/trace",
            headers,
            proxy=self.proxy,
            follow_redirect=True,
            session=self._session,
        )
        trace = {}
        for line in str(body or "").splitlines():
            key, separator, value = line.partition("=")
            if separator:
                trace[key.strip().lower()] = value.strip()
        country = str(trace.get("loc") or "").upper()
        if country != "PH":
            detected = country or "unknown"
            raise RuntimeError(f"代理预检失败：出口不是 PH（检测到 {detected}）")
        ip = str(trace.get("ip") or "").strip()
        if not ip:
            raise RuntimeError("代理预检失败：ChatGPT trace 未返回出口 IP")
        return {"country": country, "ip": ip}

    def _verify_proxy_stability(self):
        """Reject a rotating route before a Checkout creation request is sent."""
        if not self.proxy:
            return
        baseline_ip = str((self.preflight_proxy_trace or {}).get("ip") or "").strip()
        if not baseline_ip:
            raise RuntimeError("代理预检失败：缺少可复核的初始出口 IP")
        current = self._preflight_proxy()
        if str(current.get("ip") or "").strip() != baseline_ip:
            raise RuntimeError(
                "PROXY_EXIT_DRIFT: 代理出口在建单前发生变化，已阻止 Checkout 请求"
            )

    def _checkout_headers(
        self,
        locale="en-US,en;q=0.9",
        include_account=True,
    ):
        headers = _headers(
            self.token,
            self.account_id if include_account else None,
        )
        headers.update(self._fingerprint_headers())
        headers.update(self._frontend_api_context_headers())
        headers["Accept-Language"] = locale
        if self.cid:
            headers["Referer"] = (
                f"{OPENAI_BASE}/checkout/{self.processor_entity}/{self.cid}"
            )
        return headers

    def _merge_browser_cookies(self, context):
        for cookie in (context or {}).get("cookies") or []:
            try:
                self._session.cookies.set(
                    str(cookie.get("name") or ""),
                    str(cookie.get("value") or ""),
                    domain=str(cookie.get("domain") or ".chatgpt.com"),
                    path=str(cookie.get("path") or "/"),
                )
            except Exception:
                continue

    def _sentinel_headers(self, flow, page_url):
        """Create a fresh Sentinel token for one checkout approval phase."""
        try:
            main, so = mint_sentinel_sync(
                flow=flow,
                device_id=self.device_id,
                user_agent=self.user_agent,
                proxy=_proxy_url(self.proxy) or "",
                page_url=page_url,
                language="en-PH",
                timezone="Asia/Manila",
                cookie_header=_session_cookie_header(self._session),
                timeout_s=120,
            )
        except Exception as exc:
            raise RuntimeError(
                f"Sentinel 生成失败（flow={flow}）：{str(exc)[:160]}"
            ) from exc
        headers = {
            "OpenAI-Sentinel-Token": main,
            "OAI-Telemetry": "[1,null]",
        }
        if so:
            headers["OpenAI-Sentinel-So-Token"] = so
        return headers

    def _merge_checkout_response(self, response):
        cpmt = _extract_cpmt(response)
        if cpmt:
            self.cpmt = cpmt
        amount, source = _checkout_amount(response)
        if source:
            self.checkout_amount = amount
            self.checkout_amount_source = source
            self.promo_applied = amount == 0

    def _notify(self):
        if not self._on_update:
            return
        try:
            self._on_update(self._result())
        except Exception:
            pass

    def _raise_if_cancelled(self):
        if self._cancel_check and self._cancel_check():
            raise RuntimeError("TASK_CANCELLED: 用户已停止任务")

    def _add_step(self, key, label):
        self.current_step = key
        self.steps.append({"key": key, "label": label, "state": "active"})
        self._notify()

    def _set_step_done(self, key):
        for s in self.steps:
            if s["key"] == key:
                s["state"] = "done"
        self._notify()

    def _set_step_error(self, key, msg):
        for s in self.steps:
            if s["key"] == key:
                s["state"] = "error"
        self.error_message = msg
        self._notify()

    def run(self):
        """执行完整 GCash 支付链路"""
        try:
            self.status = "running"
            self._notify()

            # Verify the route before consuming a checkout creation attempt.
            self._raise_if_cancelled()
            self._add_step("proxy_test", "验证 PH 代理出口")
            self.preflight_proxy_trace = self._preflight_proxy()
            self._set_step_done("proxy_test")

            self._raise_if_cancelled()
            self._add_step("create_checkout", "创建 Checkout")
            self._create_checkout()
            self._set_step_done("create_checkout")

            self._raise_if_cancelled()
            self._add_step("configure_taxes", "同步 PH/PHP 税费")
            self._sync_checkout_taxes()
            self._set_step_done("configure_taxes")

            # Step 4: 确认支付方式
            self._raise_if_cancelled()
            self._add_step("confirm_payment", "确认支付方式")
            self._confirm_payment()
            self._set_step_done("confirm_payment")

            # Step 5: 启动支付
            self._raise_if_cancelled()
            self._add_step("start_payment", "启动支付")
            self._start_payment()
            self._set_step_done("start_payment")

            # Step 6: 跟随跳转获取 GCash 链接
            self._raise_if_cancelled()
            self._add_step("follow_redirect", "启动 GCash 扫码监控")
            self._follow_redirect()
            self._set_step_done("follow_redirect")

            self.error_message = ""
            self.status = "success"
            self._notify()
            return self._result()

        except RuntimeError as e:
            self.status = "failed"
            self._set_step_error(self.current_step, str(e))
            return self._result()
        except Exception as e:
            self.status = "failed"
            self._set_step_error(self.current_step, f"未知错误：{str(e)}")
            return self._result()
        finally:
            if not self._session_transferred:
                self._session.close()

    # ─── Step 1: 创建 OpenAI Checkout ──────────────────────

    def _create_checkout(self):
        """创建 OpenAI checkout，获取 cid 和 cpmt"""
        url = CHECKOUT_BASE
        body = {
            "entry_point": "all_plans_pricing_modal",
            "plan_name": PLAN_NAME,
            "billing_details": {
                "country": "PH",
                "currency": "PHP",
            },
            "promo_campaign": copy.deepcopy(PROMO_CAMPAIGN),
            "checkout_ui_mode": "custom",
            "check_card_proxy": True,
        }
        proxy = self.proxy
        checkout_page_url = (
            f"{OPENAI_BASE}/?promo_campaign="
            f"{urllib.parse.quote(PROMO_CAMPAIGN['promo_campaign_id'])}"
        )
        try:
            self._prepare_frontend_context()
            self._bootstrap(proxy, self._session, checkout_page_url)
            self._verify_proxy_stability()
            headers = self._checkout_headers("en-PH")
            headers["Accept"] = "*/*"
            headers["Referer"] = checkout_page_url
            try:
                resp, _ = _request(
                    "POST", url, headers, body,
                    proxy=proxy, session=self._session,
                )
            except RuntimeError as exc:
                if "unusual activity" not in str(exc).lower():
                    raise
                headers.update(
                    self._sentinel_headers("chatgpt_checkout", checkout_page_url)
                )
                resp, _ = _request(
                    "POST", url, headers, body,
                    proxy=proxy, session=self._session,
                )

            cid = _extract_checkout_id(resp)
            cpmt = _extract_cpmt(resp)

            if not cid:
                stripe_cid = _extract_prefixed_string(resp, "cs_")
                if stripe_cid:
                    raise RuntimeError("创建 checkout 失败：账号被分配到 Stripe，无法使用 GCash")
                raise RuntimeError(f"创建 checkout 失败：未获取到 cid（响应：{json.dumps(resp, ensure_ascii=False)[:300]}）")

            self.cid = cid
            self.processor_entity = _extract_processor_entity(resp) or PROCESSOR_ENTITY
            self.cpmt = cpmt
            self._merge_checkout_response(resp)

        except RuntimeError as exc:
            if "unusual activity" in str(exc).lower():
                context = self._frontend_api_context_headers()
                raise RuntimeError(
                    f"{exc} [frontend_context "
                    f"observation={bool(context.get('x-oai-is-client-observation'))} "
                    f"session={bool(context.get('oai-session-id'))} "
                    f"build={bool(context.get('oai-client-build-number'))}]"
                ) from exc
            raise
        except Exception as e:
            raise RuntimeError(f"创建 checkout 异常：{str(e)}")

    def _sync_checkout_taxes(self):
        """Send the browser checkout tax shape on the sticky PH session."""
        if not self.cid:
            raise RuntimeError("缺少 checkout ID，无法同步 taxes")
        body = {
            "checkout_session_id": self.cid,
            "checkout_email": self.billing_email or None,
            "billing_country": "PH",
            "billing_name": self.billing_name or None,
            "currency": "PHP",
            "tax_id": None,
            "processor_entity": self.processor_entity,
            "billing_address": {"country": "PH"},
        }
        taxes_resp, _ = _request(
            "POST",
            f"{CHECKOUT_BASE}/taxes",
            self._checkout_headers("en-PH,en;q=0.9"),
            body,
            proxy=self.proxy,
            session=self._session,
        )
        self._merge_checkout_response(taxes_resp)

        if not self.promo_applied:
            amount = self.checkout_amount if self.checkout_amount is not None else "unknown"
            raise RuntimeError(
                f"promo_not_applied: taxes 后 checkout 金额未变为 0（amount={amount}）"
            )
        if not self.cpmt:
            raise RuntimeError(
                "GCASH_CUSTOM_METHOD_MISSING: taxes 后仍未出现 GCash cpmt_"
            )

    # ─── Step 3: 确认支付方式 ──────────────────────────────

    def _confirm_payment(self):
        """确认 custom payment method"""
        if not self.cid:
            raise RuntimeError("缺少 checkout ID，无法确认支付")
        if not self.cpmt:
            raise RuntimeError("缺少 GCash 支付方式，无法确认支付")

        url = f"{CHECKOUT_BASE}/confirm"
        body = {
            "checkout_session_id": self.cid,
            "selected_payment_method_type": self.cpmt,
        }

        proxy = self.proxy
        checkout_page_url = (
            f"{OPENAI_BASE}/checkout/{self.processor_entity}/{self.cid}"
        )
        self._bootstrap(proxy, self._session, checkout_page_url)
        headers = self._checkout_headers("en-PH")
        headers["Accept"] = "*/*"
        headers.update(
            self._sentinel_headers(
                "checkout_session_approval", checkout_page_url
            )
        )

        try:
            resp, _ = _request("POST", url, headers, body, proxy=proxy, session=self._session)
            status = str(resp.get("status") or "").lower()
            if status == "blocked":
                raise RuntimeError("confirm_blocked: OAICS confirm status=blocked")
            if status != "success" and not resp.get("confirm_return_url"):
                raise RuntimeError(f"确认支付失败：status={status or 'unknown'}")
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"确认支付异常：{str(e)}")

    # ─── Step 4: 启动支付 ──────────────────────────────────

    def _start_payment(self):
        """启动 custom payment method，识别 Adyen 或直接 GCash 路线。"""
        if not self.cid:
            raise RuntimeError("缺少 checkout ID，无法启动支付")

        url = f"{CHECKOUT_BASE}/custom_payment_method/start"
        body = {
            "checkout_session_id": self.cid,
            "custom_payment_method_type_id": self.cpmt,
        }
        proxy = self.proxy
        headers = self._checkout_headers("en-PH,en;q=0.9")

        try:
            resp, _ = _request("POST", url, headers, body, proxy=proxy, session=self._session)
            status = str(resp.get("status") or "").lower()
            next_action = resp.get("next_action") or {}
            navigation_url = (
                next_action.get("url")
                or resp.get("redirect_url")
                or resp.get("url")
                or ""
            )
            payment_method = str(next_action.get("paymentMethodType") or "").lower()
            action_type = str(next_action.get("type") or "").lower()
            action_method = str(next_action.get("method") or "").upper()
            if status != "requires_action":
                raise RuntimeError(f"启动支付返回异常状态：{status or 'unknown'}")
            if payment_method != "gcash" or action_type != "redirect" or action_method != "GET":
                raise RuntimeError("启动支付未返回标准 GCash redirect action")
            if not _is_adyen_checkout_redirect(navigation_url):
                raise RuntimeError("启动支付未返回可信的 Adyen CheckoutShopper 长链")
            self.adyen_url = navigation_url
            self.payment_route = "adyen_redirect"
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"启动支付异常：{str(e)}")

    def _fetch_entitlement_state(self):
        headers = self._checkout_headers()
        headers["Cache-Control"] = "no-cache"
        headers["Pragma"] = "no-cache"
        state, _ = _request(
            "GET",
            ENTITLEMENT_URL,
            headers,
            proxy=self.ph_proxy,
            session=self._session,
        )
        return state

    def _verified_entitlement_state(self, attempts, delays):
        for delay in delays:
            if delay:
                time.sleep(delay)
            try:
                state = self._fetch_entitlement_state()
            except RuntimeError:
                continue
            if _active_plus_entitlement(state, self.account_id):
                return {
                    "status": "completed",
                    "_callback_attempts": attempts,
                    "_callback_verified_by": "plus_entitlement",
                    "_entitlement_verified": True,
                }
        return None

    def _verify_external_callback_state(self):
        """Confirm that the selected ChatGPT account actually received Plus."""
        return self._verified_entitlement_state(0, (0,))

    def _load_checkout_verify_context(self, redirect_result):
        query = urllib.parse.urlencode({
            "stripe_session_id": self.cid,
            "processor_entity": self.processor_entity,
            "plan_type": "plus",
            "currency": "PHP",
            "redirectResult": redirect_result,
        })
        verify_url = f"{OPENAI_BASE}/checkout/verify?{query}"
        headers = _headers(
            self.token, self.account_id, content_type=None, html=True
        )
        headers.update(self._fingerprint_headers())
        headers["Referer"] = "https://m.gcash.com/"
        _, body, _ = _request(
            "GET",
            verify_url,
            headers,
            proxy=self.proxy,
            follow_redirect=True,
            session=self._session,
        )
        context = _verify_page_context(body)
        verify_account_id = context.get("account_id") or ""
        if self.account_id and verify_account_id and verify_account_id != self.account_id:
            raise RuntimeError("checkout verify 账号与提链账号不一致")
        return verify_url, context

    def _continue_headers(self, verify_url, verify_context, native_context_headers=None):
        headers = self._checkout_headers("en-PH,en;q=0.9")
        headers["Referer"] = verify_url
        headers["x-openai-target-path"] = CONTINUE_PATH
        headers["x-openai-target-route"] = CONTINUE_PATH
        header_map = {
            "account_id": "ChatGPT-Account-Id",
            "oai_session_id": "oai-session-id",
            "web_deployment_attestation": "oai-web-deployment-attestation",
            "client_build_number": "oai-client-build-number",
            "client_version": "oai-client-version",
        }
        for key, header in header_map.items():
            value = str((verify_context or {}).get(key) or "").strip()
            if value:
                headers[header] = value

        native = _sanitize_continue_context_headers(native_context_headers)
        native_account_id = native.get("chatgpt-account-id", "")
        expected_account_id = str(
            self.account_id or (verify_context or {}).get("account_id") or ""
        )
        native_device_id = native.get("oai-device-id", "")
        if native and (
            not expected_account_id
            or native_account_id != expected_account_id
            or (self.device_id and native_device_id != self.device_id)
        ):
            native = {}
        canonical_headers = {
            "oai-session-id": "oai-session-id",
            "oai-web-deployment-attestation": "oai-web-deployment-attestation",
            "x-oai-is-client-observation": "x-oai-is-client-observation",
            "oai-client-build-number": "oai-client-build-number",
            "oai-client-version": "oai-client-version",
            "oai-device-id": "oai-device-id",
        }
        for source_name, target_name in canonical_headers.items():
            value = native.get(source_name, "")
            if not value:
                continue
            if source_name == "oai-device-id" and self.device_id:
                if value != self.device_id:
                    continue
            headers[target_name] = value
        return headers

    @staticmethod
    def _accepted_callback_response(status, attempts):
        return {
            "status": "callback_processing",
            "_callback_accepted": True,
            "_continue_status": str(status or "accepted")[:40],
            "_callback_attempts": attempts,
            "_callback_verified_by": "continue_accepted",
        }

    def _continue_payment(
        self,
        redirect_result,
        native_action_result=None,
        native_context_headers=None,
    ):
        """Submit and verify Adyen's result through the original sticky PH session."""
        if not self.cid or not redirect_result:
            raise RuntimeError("支付回调参数不完整")
        body = {
            "checkout_session_id": self.cid,
            "action_result": _callback_action_result(
                redirect_result, native_action_result
            ),
        }
        verified = self._verified_entitlement_state(0, (0,))
        if verified:
            return verified
        last_error = None
        verify_url = ""
        verify_context = {}
        for attempt in range(1, CALLBACK_MAX_ATTEMPTS + 1):
            try:
                if not verify_url:
                    verify_url, verify_context = self._load_checkout_verify_context(
                        redirect_result
                    )
                response, _ = _request(
                    "POST",
                    f"{CHECKOUT_BASE}/custom_payment_method/continue",
                    self._continue_headers(
                        verify_url, verify_context, native_context_headers
                    ),
                    body,
                    proxy=self.proxy,
                    session=self._session,
                )
            except RuntimeError as exc:
                last_error = exc
                retryable = _callback_error_is_retryable(exc)
                delays = (
                    CALLBACK_VERIFY_DELAYS
                    if attempt >= CALLBACK_MAX_ATTEMPTS
                    else CALLBACK_VERIFY_DELAYS[:2]
                )
                verified = self._verified_entitlement_state(attempt, delays)
                if verified:
                    return verified
                if not retryable or attempt >= CALLBACK_MAX_ATTEMPTS:
                    raise
                time.sleep(CALLBACK_RETRY_DELAYS[attempt - 1])
                continue

            status = _payload_status(response)
            payment_status = _explicit_payment_status(response)
            accepted_status = payment_status or status
            if (
                payment_status in CALLBACK_SUCCESS_STATUSES
                or status in CALLBACK_ACCEPTED_STATUSES
            ):
                verified = self._verified_entitlement_state(
                    attempt, CALLBACK_VERIFY_DELAYS
                )
                if verified:
                    return verified
                return self._accepted_callback_response(accepted_status, attempt)
            verified = self._verified_entitlement_state(
                attempt, CALLBACK_VERIFY_DELAYS
            )
            if verified:
                return verified
            raise RuntimeError(
                f"continue 回调未被明确受理：{status or payment_status or 'unknown'}"
            )
        raise last_error or RuntimeError("continue 回调失败")

    # ─── Step 5: 跟随跳转 ──────────────────────────────────

    def _follow_redirect(self):
        """Open the real payment page and keep it alive for the return callback."""
        navigation_url = self.adyen_url or self.gcash_url
        if not navigation_url:
            raise RuntimeError("缺少 Adyen 或 GCash 支付页面 URL")
        try:
            self._session_transferred = True
            monitor_id, gcash_url, expires_at = payment_monitor.start(
                navigation_url=navigation_url,
                proxy=self.proxy,
                token=self.token,
                account_id=self.account_id,
                client_account_id=self.client_account_id,
                device_id=self.device_id,
                user_agent=self.user_agent,
                cookies=_browser_cookies(self._session),
                continue_callback=self._continue_payment,
                verify_callback=self._verify_external_callback_state,
                close_callback=self._session.close,
            )
            self.monitor_id = monitor_id
            self.callback_status = "waiting_scan"
            self.gcash_url = gcash_url
            self._parse_gcash_url(gcash_url)
            self.qr_expires_at = expires_at
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"跟随跳转异常：{str(e)}")

    def _parse_gcash_url(self, url):
        """从 GCash URL 中提取二维码信息"""
        if not _is_gcash_url(url):
            raise RuntimeError("返回链接不是可信的 GCash 域名")
        # 尝试解析参数获取二维码文本
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        net_auth = params.get("netAuthId", [""])[0]
        if net_auth:
            self.net_auth_id = net_auth
        # 没有远端 paywatch 时，二维码编码可直接打开的完整授权 URL。
        self.qr_text = url
        self.qr_expires_at = int(time.time()) + QR_TTL_SECONDS

    def _result(self):
        return {
            "client_account_id": self.client_account_id,
            "status": self.status,
            "current_step": self.current_step,
            "steps": self.steps,
            "error_message": self.error_message,
            "gcash_url": self.gcash_url or "",
            "qr_text": self.qr_text or "",
            "qr_short": self.qr_short or "",
            "net_auth_id": self.net_auth_id or "",
            "qr_expires_at": self.qr_expires_at,
            "monitor_id": self.monitor_id or "",
            "callback_status": self.callback_status,
            "payment_route": self.payment_route,
        }


# ─── 会话管理器 ────────────────────────────────────────────

class QueueFullError(RuntimeError):
    """服务器任务队列已满。"""


class GCashSessionManager:
    """管理多个账号的 GCash 支付任务"""

    def __init__(
        self,
        max_concurrency=6,
        max_queue=50,
        max_session_concurrency=None,
    ):
        self.sessions = {}
        self.lock = threading.Lock()
        self.pending_tasks = deque()
        self.active_count = 0
        self.active_by_session = {}
        self.max_concurrency = max(1, min(int(max_concurrency), MAX_WORKERS))
        self.max_queue = max(1, min(int(max_queue), MAX_QUEUE_SIZE))
        self.max_session_concurrency = (
            self.max_concurrency
            if max_session_concurrency is None
            else max(1, min(int(max_session_concurrency), MAX_WORKERS))
        )
        self.executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="gcash")

    def configure(self, max_concurrency, max_queue):
        """更新全局并发和等待队列上限。"""
        with self.lock:
            self.max_concurrency = max(1, min(int(max_concurrency), MAX_WORKERS))
            self.max_queue = max(1, min(int(max_queue), MAX_QUEUE_SIZE))
            self._dispatch_locked()
            return self._queue_status_locked()

    def create_session(self):
        """创建新会话"""
        session_id = "local_" + secrets.token_hex(8)
        with self.lock:
            self._cleanup_locked()
            self.sessions[session_id] = {
                "tasks": [],
                "created": time.time(),
                "done": False,
            }
        return session_id

    def submit_jobs(self, session_id, payloads):
        """提交任务到会话（每次尝试固定一个 PH 节点）"""
        tasks = []
        with self.lock:
            session = self.sessions.get(session_id)
            if not session:
                raise RuntimeError("会话不存在")

            incoming = len(payloads)
            free_workers = max(0, self.max_concurrency - self.active_count)
            session_free = max(
                0,
                self.max_session_concurrency
                - self.active_by_session.get(session_id, 0),
            )
            immediately_runnable = min(free_workers, session_free)
            waiting_after = max(
                0, len(self.pending_tasks) + incoming - immediately_runnable
            )
            if waiting_after > self.max_queue:
                if not session["tasks"]:
                    self.sessions.pop(session_id, None)
                raise QueueFullError(
                    f"服务器当前繁忙，等待队列已达上限 {self.max_queue}，请稍后重试"
                )

            for p in payloads:
                task = {
                    "client_account_id": p.get("client_account_id", ""),
                    "account_id": p.get("account_id", ""),
                    "billing_email": p.get("billing_email", ""),
                    "billing_name": p.get("billing_name", ""),
                    "token": p.get("token", ""),
                    "proxy": p.get("proxy") or p.get("ph_proxy") or p.get("vn_proxy", ""),
                    "proxy_pool": list(
                        p.get("proxy_pool") or p.get("ph_proxy_pool")
                        or p.get("vn_proxy_pool") or []
                    ),
                    "max_attempts": max(1, min(int(p.get("max_attempts") or MAX_ATTEMPTS), 10)),
                    "status": "queued",
                    "current_step": "parse",
                    "steps": [],
                    "queue_position": None,
                    "error_message": "",
                    "gcash_url": "",
                    "expires_at": None,
                    "monitor_id": "",
                    "callback_status": "unavailable",
                    "payment_route": "",
                    "attempts_used": 0,
                    "attempt_history": [],
                    "cancel_requested": False,
                }
                tasks.append(task)
                session["tasks"].append(task)

            self.pending_tasks.extend((session_id, task) for task in tasks)
            self._dispatch_locked()

        return copy.deepcopy(tasks)

    def _dispatch_locked(self):
        while self.pending_tasks and self.active_count < self.max_concurrency:
            eligible_index = next((
                index
                for index, (pending_session_id, _) in enumerate(self.pending_tasks)
                if self.active_by_session.get(pending_session_id, 0)
                < self.max_session_concurrency
            ), None)
            if eligible_index is None:
                break
            self.pending_tasks.rotate(-eligible_index)
            session_id, task = self.pending_tasks.popleft()
            self.pending_tasks.rotate(eligible_index)
            task["status"] = "running"
            task["queue_position"] = None
            self.active_count += 1
            self.active_by_session[session_id] = (
                self.active_by_session.get(session_id, 0) + 1
            )
            self.executor.submit(self._run_task, session_id, task)
        self._refresh_queue_positions_locked()

    def _refresh_queue_positions_locked(self):
        for position, (_, task) in enumerate(self.pending_tasks, 1):
            task["queue_position"] = position

    def cancel_session(self, session_id):
        """Remove this session's queued work and request cancellation of running work."""
        with self.lock:
            session = self.sessions.get(session_id)
            if not session:
                return {"queued_cancelled": 0, "running_cancel_requested": 0}

            retained = deque()
            queued_cancelled = 0
            for pending_session_id, task in self.pending_tasks:
                if pending_session_id != session_id:
                    retained.append((pending_session_id, task))
                    continue
                task["cancel_requested"] = True
                task["status"] = "failed"
                task["current_step"] = "cancelled"
                task["queue_position"] = None
                task["error_message"] = "TASK_CANCELLED: 用户已停止任务"
                queued_cancelled += 1
            self.pending_tasks = retained

            running_cancel_requested = 0
            for task in session["tasks"]:
                if task.get("status") != "running":
                    continue
                task["cancel_requested"] = True
                task["status"] = "canceling"
                task["current_step"] = "cancelled"
                task["error_message"] = "TASK_CANCELLED: 正在停止任务"
                running_cancel_requested += 1

            session["cancel_requested"] = True
            if all(
                task.get("status") in {"success", "failed"}
                for task in session["tasks"]
            ):
                session["done"] = True
            self._refresh_queue_positions_locked()
            self._dispatch_locked()
            return {
                "queued_cancelled": queued_cancelled,
                "running_cancel_requested": running_cancel_requested,
            }

    def _run_task(self, session_id, task):
        try:
            self._process_task(task)
        except Exception:
            with self.lock:
                task["status"] = "failed"
                task["error_message"] = "任务执行失败"
        finally:
            with self.lock:
                self.active_count = max(0, self.active_count - 1)
                session_active = max(
                    0, self.active_by_session.get(session_id, 0) - 1
                )
                if session_active:
                    self.active_by_session[session_id] = session_active
                else:
                    self.active_by_session.pop(session_id, None)
                session = self.sessions.get(session_id)
                if session and all(
                    item.get("status") in {"success", "failed"}
                    for item in session["tasks"]
                ):
                    session["done"] = True
                self._dispatch_locked()

    def _queue_status_locked(self):
        return {
            "running": self.active_count,
            "queued": len(self.pending_tasks),
            "max_concurrency": self.max_concurrency,
            "max_session_concurrency": self.max_session_concurrency,
            "max_queue": self.max_queue,
        }

    def queue_status(self):
        with self.lock:
            return self._queue_status_locked()

    def discard_session(self, session_id):
        """移除已完成或尚未提交任务的会话。"""
        with self.lock:
            session = self.sessions.get(session_id)
            if not session:
                return False
            if session["tasks"] and not session.get("done"):
                return False
            self.sessions.pop(session_id, None)
            return True

    def _process_task(self, task):
        """执行单个账号，并在支付启动前的可恢复分支上重建 Checkout。"""
        proxy = task["proxy"]
        retry_steps = []

        def sync(result):
            with self.lock:
                if task.get("cancel_requested") and result["status"] == "running":
                    task["status"] = "canceling"
                    task["current_step"] = "cancelled"
                    task["error_message"] = "TASK_CANCELLED: 正在停止任务"
                    return
                task["status"] = result["status"]
                task["current_step"] = result["current_step"]
                task["steps"] = copy.deepcopy(retry_steps + result["steps"])
                task["error_message"] = result["error_message"]
                task["gcash_url"] = result["gcash_url"]
                task["expires_at"] = result["qr_expires_at"]
                task["monitor_id"] = result.get("monitor_id") or ""
                task["callback_status"] = result.get("callback_status") or "unavailable"
                task["payment_route"] = result.get("payment_route") or ""

        max_attempts = task.get("max_attempts", MAX_ATTEMPTS)
        for attempt in range(max_attempts):
            with self.lock:
                if task.get("cancel_requested"):
                    task["status"] = "failed"
                    task["current_step"] = "cancelled"
                    task["error_message"] = "TASK_CANCELLED: 用户已停止任务"
                    return
                task["status"] = "running"
                task["current_step"] = "init"
                task["steps"] = copy.deepcopy(retry_steps)
                task["error_message"] = ""
            chain = GCashChain(
                token=task["token"],
                client_account_id=task["client_account_id"],
                proxy=proxy or None,
                account_id=task["account_id"],
                billing_email=task.get("billing_email", ""),
                billing_name=task.get("billing_name", ""),
                on_update=sync,
                cancel_check=lambda: bool(task.get("cancel_requested")),
            )
            result = chain.run()
            sync(result)
            retryable, retry_label = _retry_decision(result)
            with self.lock:
                task["attempts_used"] = attempt + 1
                task["attempt_history"].append({
                    "status": result.get("status") or "failed",
                    "step": result.get("current_step") or "",
                    "error": result.get("error_message") or "",
                    "proxy": proxy,
                    "payment_route": result.get("payment_route") or "",
                    "retry_reason": retry_label,
                })
            if result["status"] == "success":
                return

            with self.lock:
                cancelled = bool(task.get("cancel_requested"))
                if cancelled:
                    task["status"] = "failed"
                    task["current_step"] = "cancelled"
                    task["error_message"] = "TASK_CANCELLED: 用户已停止任务"
            if cancelled:
                return

            if not retryable or attempt >= max_attempts - 1:
                return

            proxy = self._rotate_proxy(proxy, task.get("proxy_pool")) or proxy
            retry_steps.append({
                "key": f"retry_{attempt + 1}",
                "label": f"{retry_label} #{attempt + 1}",
                "state": "done",
            })
            with self.lock:
                task["proxy"] = proxy
                task["status"] = "running"
                task["current_step"] = retry_steps[-1]["key"]
                task["steps"] = copy.deepcopy(retry_steps)
                task["error_message"] = ""

    def _rotate_proxy(self, failed_proxy, pool=None):
        """从失败项之后开始顺序轮换，确保重试能遍历整个代理池。"""
        pool = list(pool or [])
        if not pool:
            return ""
        try:
            failed_index = pool.index(failed_proxy)
        except ValueError:
            failed_index = -1
        for offset in range(1, len(pool) + 1):
            candidate = pool[(failed_index + offset) % len(pool)]
            if candidate != failed_proxy:
                return candidate
        return ""

    def get_tasks(self, session_id):
        """获取会话中的任务状态"""
        with self.lock:
            session = self.sessions.get(session_id)
            if not session:
                return []
            return copy.deepcopy(session["tasks"])

    def get_session(self, session_id):
        """获取会话信息"""
        with self.lock:
            session = self.sessions.get(session_id)
            if not session:
                return None
            return {
                "done": session["done"],
                "tasks": copy.deepcopy(session["tasks"]),
            }

    @staticmethod
    def monitor_status(monitor_id):
        return payment_monitor.status(monitor_id)

    @staticmethod
    def monitor_qr_image(monitor_id):
        return payment_monitor.qr_image(monitor_id)

    @staticmethod
    def refresh_monitor(monitor_id):
        return payment_monitor.refresh(monitor_id)

    @staticmethod
    def abandon_monitor(monitor_id):
        return payment_monitor.abandon(monitor_id)

    def cleanup(self):
        """清理过期会话（30分钟）"""
        with self.lock:
            return self._cleanup_locked()

    def _cleanup_locked(self):
        now = time.time()
        expired = [
            key for key, session in self.sessions.items()
            if (session.get("done") and now - session["created"] > 1800)
            or (not session.get("tasks") and now - session["created"] > 60)
        ]
        for key in expired:
            self.sessions.pop(key, None)
        return len(expired)


# 全局单例
manager = GCashSessionManager(max_session_concurrency=4)
