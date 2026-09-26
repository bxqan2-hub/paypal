# -*- coding: utf-8 -*-
"""交付前核验：一条 hosted_instructions_url 到底是真链还是废链。

**为什么必须核验**
`setup_intent.next_action.upi_handle_redirect_or_display_qr_code.hosted_instructions_url`
在 setup_intent **被拒**（`requires_payment_method`）时**照样会返回**。
只看「有没有 URL」就把链接交出去，用户打开看到的会是 ₹1999 付款页，
而不是 ₹0 的 UPI Autopay 委托。

**判据（两个都要满足）**
1. `intent_state ∈ {requires_action, processing}` —— 委托真的签出来了；
2. UPI URI 里 `fam != 1999.00` —— `fam=1.00` 是「本次扣 ₹0/₹1、授权上限 ₹1999」，
   `fam=1999.00` 是 ₹1999 付款链。

注意 `am=1999.00` 是**授权上限**（`amrule=MAX`），不是本次扣款额，不能拿它判零元。
"""
from __future__ import annotations

import base64
import json
import re
import time
from typing import Any, Optional

PAYLOAD_META = re.compile(
    r'<meta\b[^>]*\bid=["\']payload["\'][^>]*\bdata-message=["\']([^"\']+)', re.I)
FAM_RE = re.compile(r"[?&]fam=([^&]+)")
PASSING_STATES = ("requires_action", "processing")
PAYMENT_CHAIN_FAM = ("1999.00", "1999")

# 这些返回值表示「读不到页面 / 没结论」，不代表链是假的
INCONCLUSIVE = frozenset({"unreachable", "http_5xx", "no_payload", "empty_url"})


def decode_payload(html: str) -> Optional[dict]:
    """把指引页里 `<meta id="payload" data-message="…">` 的 base64url 解成 JSON。"""
    match = PAYLOAD_META.search(html or "")
    if not match:
        return None
    raw = match.group(1).replace("&quot;", '"').replace("-", "+").replace("_", "/")
    raw += "=" * ((4 - len(raw) % 4) % 4)
    try:
        payload = json.loads(base64.b64decode(raw).decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    return payload if isinstance(payload, dict) else None


def judge(payload: dict) -> tuple[bool, str]:
    """按 payload 判定真伪，返回 (是否真链, 说明)。

    `requires_action` / `processing` = 刚签发、可交付；
    `requires_payment_method` = Stripe 拒了，委托没签出来（废链）；
    `canceled` = 窗口关了或被后一次尝试顶掉；
    `succeeded` = 用户已经授权成功（这条链被用掉了，别重复交付）。
    """
    state = str(payload.get("intent_state") or "")
    uri = str(payload.get("mobile_auth_url") or payload.get("upi_uri") or "")
    match = FAM_RE.search(uri)
    fam = match.group(1) if match else ""
    label = "state=%s fam=%s" % (state or "unknown", fam or "?")
    if state not in PASSING_STATES:
        return False, label
    if fam in PAYMENT_CHAIN_FAM:
        return False, label
    return True, label


def _default_get(url, timeout=30.0, headers=None, proxies=None):
    import requests
    return requests.get(url, timeout=timeout, headers=headers, proxies=proxies)


def verify_url(url: str, request_get: Any = None, *, proxy: str = "", timeout: float = 30.0,
               attempts: int = 3,
               accept: str = "text/html,application/xhtml+xml,*/*;q=0.8") -> tuple[bool, str]:
    """抓取指引页并判定，返回 `(是否真链, 说明)`。

    指引页是公网可访问的（直连即可），`proxy` 是备用通道：直连被拦时用当前出口再读一次。
    读取是幂等的，所以失败会重试；仍失败返回 `unreachable` / `http_5xx` 之类，
    属于「没结论」而不是「链是假的」（见 `INCONCLUSIVE`）。
    """
    if not url:
        return False, "empty_url"
    if request_get is None:
        request_get = _default_get

    routes: list[Any] = [None]
    if proxy:
        routes.append({"https": proxy, "http": proxy})

    last = "unreachable"
    for proxies in routes:
        for attempt in range(max(1, attempts)):
            try:
                resp = request_get(url, timeout=timeout, headers={"Accept": accept},
                                   proxies=proxies)
            except Exception:  # noqa: BLE001
                last = "unreachable"
                resp = None
            if resp is not None:
                status = getattr(resp, "status_code", 0)
                if status < 400:
                    payload = decode_payload(getattr(resp, "text", "") or "")
                    if payload is None:
                        return False, "no_payload"
                    return judge(payload)
                last = "http_%s" % status
                if status < 500:
                    return False, last      # 4xx 是明确结论：token 无效/过期
            if attempt + 1 < max(1, attempts):
                time.sleep(0.6 * (attempt + 1))
    return False, last
