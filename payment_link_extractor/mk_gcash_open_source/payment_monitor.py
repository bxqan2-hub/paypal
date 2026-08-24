#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Headless GCash page monitoring and redirect callback coordination."""

import asyncio
import base64
import binascii
import contextlib
import hashlib
import html
import json
import os
import re
import secrets
import struct
import threading
import time
import urllib.parse
import zlib


MONITOR_TTL_SECONDS = 5 * 60
MONITOR_REFRESH_GRACE_SECONDS = 10 * 60
INITIAL_QR_CAPTURE_TIMEOUT = 55
REFRESH_QR_CAPTURE_TIMEOUT = 45
MONITOR_READY_TIMEOUT = 120
MONITOR_REFRESH_TIMEOUT = 90
CHECKOUT_STATUS_POLL_SECONDS = 3.0
CALLBACK_CONFIRM_TIMEOUT_SECONDS = 5 * 60


def _bounded_env_int(name, default, upper):
    try:
        return max(1, min(int(os.getenv(name, default)), upper))
    except (TypeError, ValueError):
        return default


MAX_ACTIVE_MONITORS = _bounded_env_int("GCASH_MAX_ACTIVE_MONITORS", 12, 24)
TERMINAL_STATUSES = {
    "completed", "callback_failed", "callback_unconfirmed", "expired", "failed",
    "abandoned",
}
CALLBACK_SUCCESS_STATUSES = {"success", "succeeded", "completed", "complete", "paid"}
CALLBACK_ACCEPTED_STATUSES = CALLBACK_SUCCESS_STATUSES | {
    "accepted", "pending", "processing",
}
CALLBACK_FAILURE_STATUSES = {"failed", "error", "cancelled", "canceled"}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
CONTINUE_CONTEXT_HEADER_NAMES = frozenset({
    "chatgpt-account-id",
    "oai-session-id",
    "oai-web-deployment-attestation",
    "x-oai-is-client-observation",
    "oai-client-build-number",
    "oai-client-version",
    "oai-device-id",
    "x-openai-target-path",
    "x-openai-target-route",
})
FRONTEND_CONTEXT_COOKIE_NAMES = frozenset({
    "oai-did", "cf_clearance", "__cf_bm", "__cfseq",
    "cf_chl_rc_i", "cf_chl_rc_ni", "cf_chl_rc_m",
})


def _sanitize_continue_context_headers(headers):
    """Retain only replayable browser context without auth/cookie headers."""
    if not isinstance(headers, dict):
        return {}
    output = {}
    for raw_name, raw_value in headers.items():
        name = str(raw_name or "").strip().lower()
        if name not in CONTINUE_CONTEXT_HEADER_NAMES:
            continue
        value = str(raw_value or "").strip()
        if not value or len(value) > 16_384 or "\r" in value or "\n" in value:
            continue
        output[name] = value
    return output


def _extract_redirect_result(value):
    """Extract Adyen's one-time redirect result without retaining its container."""
    text = str(value or "")
    if not text:
        return ""

    def from_mapping(mapping):
        if not isinstance(mapping, dict):
            return ""
        for key, item in mapping.items():
            normalized = str(key).replace("_", "").lower()
            if normalized == "redirectresult" and isinstance(item, str):
                return item
            if isinstance(item, dict):
                found = from_mapping(item)
                if found:
                    return found
        return ""

    try:
        parsed_url = urllib.parse.urlparse(text)
        if parsed_url.scheme and parsed_url.netloc:
            for section in (parsed_url.query, parsed_url.fragment):
                values = urllib.parse.parse_qs(section, keep_blank_values=True)
                found = from_mapping({key: item[0] for key, item in values.items() if item})
                if found:
                    return found
    except ValueError:
        pass

    try:
        parsed_json = json.loads(text)
        found = from_mapping(parsed_json)
        if found:
            return found
    except (TypeError, ValueError):
        pass

    parsed_form = urllib.parse.parse_qs(text, keep_blank_values=True)
    found = from_mapping({key: item[0] for key, item in parsed_form.items() if item})
    if found:
        return found

    match = re.search(r"(?i)(?:redirectResult|redirect_result)=([^&\s]+)", text)
    if match:
        return urllib.parse.unquote(match.group(1))
    embedded = re.search(
        r'''(?ix)["']?(?:redirectResult|redirect_result)["']?\s*[:=]\s*["']([^"'<>&\s]+)''',
        text,
    )
    return urllib.parse.unquote(embedded.group(1)) if embedded else ""


def _extract_continue_action_result(value):
    """Capture the browser's native action_result without logging its contents."""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return None
    if not isinstance(value, dict):
        return None
    action_result = value.get("action_result")
    if isinstance(action_result, str):
        try:
            action_result = json.loads(action_result)
        except (TypeError, ValueError):
            return None
    if not isinstance(action_result, dict):
        return None
    redirect_result = action_result.get("redirectResult")
    if not isinstance(redirect_result, str) or not redirect_result:
        return None
    try:
        serialized = json.dumps(action_result, separators=(",", ":"))
    except (TypeError, ValueError):
        return None
    if len(serialized) > 32_768:
        return None
    return json.loads(serialized)


def _gcash_url(value):
    text = html.unescape(str(value or ""))
    candidates = [text]
    candidates.extend(re.findall(
        r"https://(?:[a-z0-9-]+\.)*gcash\.com/[^\s\"'<>]+",
        text,
        flags=re.IGNORECASE,
    ))
    for candidate in candidates:
        try:
            parsed = urllib.parse.urlparse(candidate)
        except ValueError:
            continue
        host = (parsed.hostname or "").lower()
        if parsed.scheme == "https" and (host == "gcash.com" or host.endswith(".gcash.com")):
            return candidate
    return ""


def _qr_candidate_score(metadata, width, height):
    """Score a visible page element without inspecting or logging QR contents."""
    width = float(width or 0)
    height = float(height or 0)
    if min(width, height) < 64 or max(width, height) > 900:
        return -1
    ratio = max(width, height) / min(width, height)
    if ratio > 1.45:
        return -1

    metadata = metadata or {}
    marker = " ".join(str(metadata.get(key) or "") for key in (
        "id", "class_name", "alt", "title", "aria_label",
        "parent_id", "parent_class", "grandparent_id", "grandparent_class",
        "parent_text", "test_id", "src",
    )).lower()
    tag = str(metadata.get("tag") or "").lower()
    src = str(metadata.get("src") or "").lower()
    has_qr_marker = bool(re.search(
        r"(?:^|[^a-z0-9])qr(?:code)?(?:[^a-z0-9]|$)", marker
    ))
    has_scan_marker = "scan" in marker and "code" in marker
    if min(width, height) < 96 and not (has_qr_marker or has_scan_marker):
        return -1
    score = int(45 * (1 - (ratio - 1) / 0.45))
    if has_qr_marker:
        score += 140
    elif has_scan_marker:
        score += 90
    if tag == "canvas":
        score += 45
    elif tag in {"img", "svg"}:
        score += 12
    if src.startswith(("data:image/", "blob:")):
        score += 35 if (has_qr_marker or has_scan_marker or tag == "canvas") else 8
    if min(width, height) >= 140:
        score += 15
    return score


def _qr_candidate_is_strong(metadata):
    """Require a QR-specific node, not nearby instructional artwork or text."""
    metadata = metadata or {}
    tag = str(metadata.get("tag") or "").lower()
    structural_values = [
        str(metadata.get(key) or "").lower()
        for key in ("id", "class_name", "parent_id", "parent_class", "test_id")
    ]
    normalized = [re.sub(r"[^a-z0-9]", "", value) for value in structural_values]
    if any("qrcode" in value for value in normalized):
        return True
    if tag == "canvas":
        tokens = set(re.findall(r"[a-z0-9]+", " ".join(structural_values)))
        return "qr" in tokens
    return False


def _decode_png_data_url(value):
    """Return GCash's own PNG bytes without rebuilding the QR payload."""
    text = str(value or "")
    header, separator, payload = text.partition(",")
    if not separator or not header.lower().startswith("data:image/png"):
        return b""
    try:
        if ";base64" in header.lower():
            decoded = base64.b64decode(payload, validate=True)
        else:
            decoded = urllib.parse.unquote_to_bytes(payload)
    except (binascii.Error, ValueError):
        return b""
    if not 300 <= len(decoded) <= 5 * 1024 * 1024:
        return b""
    return decoded if decoded.startswith(PNG_SIGNATURE) else b""


def _png_chunk(kind, payload):
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _fallback_qr_png(gcash_link):
    """Encode only a validated GCash link as the immediate fallback image."""
    trusted_link = _gcash_url(gcash_link)
    if not trusted_link or trusted_link != str(gcash_link or ""):
        raise RuntimeError("备用二维码拒绝了非 GCash 支付链接")

    from qrcode import QRCode
    from qrcode.constants import ERROR_CORRECT_M

    qr = QRCode(error_correction=ERROR_CORRECT_M, border=4)
    qr.add_data(trusted_link)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    if not matrix or len(matrix) != len(matrix[0]):
        raise RuntimeError("备用二维码生成失败")

    scale = max(2, min(8, 384 // len(matrix)))
    width = len(matrix) * scale
    rows = []
    for source_row in matrix:
        pixels = b"".join(
            (b"\x00" if dark else b"\xff") * scale
            for dark in source_row
        )
        encoded_row = b"\x00" + pixels
        rows.extend([encoded_row] * scale)
    header = struct.pack(">IIBBBBB", width, width, 8, 0, 0, 0, 0)
    return (
        PNG_SIGNATURE
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
        + _png_chunk(b"IEND", b"")
    )


async def _capture_real_qr_png(page, timeout_seconds=INITIAL_QR_CAPTURE_TIMEOUT):
    """Capture the QR element rendered by GCash instead of rebuilding its URL."""
    deadline = time.monotonic() + max(1, float(timeout_seconds))
    selector = (
        'canvas, img, svg, [role="img"], '
        '[class*="qr" i], [id*="qr" i], [data-testid*="qr" i]'
    )
    last_stats = {
        "frames": 0,
        "candidates": 0,
        "visible": 0,
        "strong": 0,
        "best_score": -1,
    }
    while time.monotonic() < deadline:
        ranked = []
        scan_stats = {
            "frames": len(page.frames),
            "candidates": 0,
            "visible": 0,
            "strong": 0,
            "best_score": -1,
        }
        for frame in page.frames:
            candidates = frame.locator(selector)
            try:
                count = min(await candidates.count(), 80)
            except Exception:
                count = 0
            scan_stats["candidates"] += count
            for index in range(count):
                candidate = candidates.nth(index)
                try:
                    if not await candidate.is_visible():
                        continue
                    scan_stats["visible"] += 1
                    box = await candidate.bounding_box()
                    if not box:
                        continue
                    metadata = await candidate.evaluate("""element => {
                        const parent = element.parentElement || {};
                        const grandparent = parent.parentElement || {};
                        return {
                          tag: (element.tagName || '').toLowerCase(),
                          id: element.id || '',
                          class_name: typeof element.className === 'string' ? element.className : '',
                          alt: element.getAttribute?.('alt') || '',
                          title: element.getAttribute?.('title') || '',
                          aria_label: element.getAttribute?.('aria-label') || '',
                          test_id: element.getAttribute?.('data-testid') || '',
                          src: String(element.currentSrc || element.getAttribute?.('src') || '').slice(0, 256),
                          parent_id: parent.id || '',
                          parent_class: typeof parent.className === 'string' ? parent.className : '',
                          grandparent_id: grandparent.id || '',
                          grandparent_class: typeof grandparent.className === 'string' ? grandparent.className : '',
                          parent_text: String(parent.innerText || '').slice(0, 160)
                        };
                    }""")
                    score = _qr_candidate_score(metadata, box["width"], box["height"])
                    scan_stats["best_score"] = max(scan_stats["best_score"], score)
                    is_strong = _qr_candidate_is_strong(metadata)
                    if is_strong:
                        scan_stats["strong"] += 1
                    if score >= 80 and is_strong:
                        ranked.append((score, candidate, metadata))
                except Exception:
                    continue
        last_stats = scan_stats
        for _, candidate, metadata in sorted(ranked, key=lambda item: item[0], reverse=True):
            try:
                source_png = await candidate.evaluate("""element => {
                    if ((element.tagName || '').toLowerCase() === 'canvas') {
                      try { return element.toDataURL('image/png'); } catch (_) { return ''; }
                    }
                    return String(element.currentSrc || element.getAttribute?.('src') || '');
                }""")
                png = _decode_png_data_url(source_png)
                if png:
                    return png
            except Exception:
                pass
            try:
                png = await candidate.screenshot(
                    type="png", animations="disabled", timeout=5_000,
                )
            except Exception:
                continue
            if isinstance(png, bytes) and png.startswith(PNG_SIGNATURE) and len(png) >= 300:
                return png
        await page.wait_for_timeout(350)
    raise RuntimeError(
        "GCash 页面未抓取到真实二维码"
        f"（frames={last_stats['frames']} candidates={last_stats['candidates']} "
        f"visible={last_stats['visible']} strong={last_stats['strong']} "
        f"best_score={last_stats['best_score']}）"
    )


def _playwright_proxy(proxy):
    if not proxy:
        return None
    from gcash_chain import _parse_proxy

    proxy_type, host, port, username, password = _parse_proxy(proxy)
    if not host or not port:
        raise RuntimeError("回调监控节点格式无效")
    if proxy_type == "socks5" and (username is not None or password is not None):
        raise RuntimeError(
            "带账号密码的 SOCKS5 不支持 GCash 二维码与回调监控；"
            "请改用认证 HTTP 代理或无认证 SOCKS5"
        )
    scheme = proxy_type if proxy_type in {"http", "https", "socks5"} else "http"
    rendered_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    config = {"server": f"{scheme}://{rendered_host}:{port}"}
    if username is not None:
        config["username"] = str(username)
        config["password"] = str(password or "")
    return config


def _short_hash(value):
    text = str(value or "")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12] if text else ""


def _route_label(value):
    try:
        parsed = urllib.parse.urlparse(str(value or ""))
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not host:
        return ""
    segments = []
    for raw_segment in (parsed.path or "/").split("/"):
        segment = urllib.parse.unquote(raw_segment)
        is_identifier = (
            len(segment) > 40
            or bool(re.fullmatch(r"[A-Za-z0-9_-]{24,}", segment))
            or bool(re.fullmatch(
                r"(?i)(?:oaics|cpmt|pay|pi|pm|sess|session|redirect)_[A-Za-z0-9_-]+",
                segment,
            ))
            or bool(re.fullmatch(
                r"(?i)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                segment,
            ))
        )
        segments.append("<id>" if is_identifier else re.sub(r"[^A-Za-z0-9._~-]", "_", segment))
    path = re.sub(r"/{2,}", "/", "/".join(segments) or "/")
    return f"{host}{path}"[:180]


def _safe_log_error(value):
    text = str(value or "")
    text = re.sub(r"(?i)\b(?:https?|socks4a?|socks5h?)://[^\s\"'<>]+", "<url>", text)
    text = re.sub(
        r"(?i)authorization\s*[:=]\s*bearer\s+[^\s,;]+",
        "Authorization: Bearer <token>",
        text,
    )
    text = re.sub(r"eyJ[A-Za-z0-9_.-]+", "<token>", text)
    text = re.sub(
        r"(?i)(redirectResult|redirect_result)\s*[:=]\s*[^\s&;,]+",
        r"\1=<redirect>",
        text,
    )
    text = re.sub(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", "<email>", text, flags=re.I)
    text = re.sub(
        r"(?<![\w.-])[\w.-]+:\d{2,5}:[^:\s]+:[^\s]+",
        "<proxy>",
        text,
    )
    text = re.sub(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?", "<ip>", text)
    return re.sub(r"\s+", " ", text).strip()[:300]


def _payload_status(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return ""

    def walk(item):
        if isinstance(item, dict):
            for key in ("status", "payment_status", "result_status"):
                status = item.get(key)
                if isinstance(status, str) and status:
                    return re.sub(r"[^a-z0-9_-]", "", status.lower())[:40]
            for child in item.values():
                found = walk(child)
                if found:
                    return found
        elif isinstance(item, list):
            for child in item:
                found = walk(child)
                if found:
                    return found
        return ""

    return walk(value)


class PaymentMonitorManager:
    """Runs several proxy-isolated pages inside one headless Chromium process."""

    def __init__(self):
        self._states = {}
        self._states_lock = threading.Lock()
        self._thread = None
        self._loop = None
        self._loop_ready = threading.Event()
        self._browser = None
        self._playwright = None
        self._browser_lock = None
        self._semaphore = None
        self._frontend_context_semaphore = None

    def _ensure_loop(self):
        with self._states_lock:
            if self._thread and self._thread.is_alive():
                return
            self._loop_ready.clear()
            self._thread = threading.Thread(
                target=self._loop_main,
                name="gcash-payment-monitor",
                daemon=True,
            )
            self._thread.start()
        if not self._loop_ready.wait(10):
            raise RuntimeError("支付监控服务启动超时")

    def _loop_main(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._browser_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(MAX_ACTIVE_MONITORS)
        # Context capture is short lived and must keep working while all
        # long-lived payment monitor slots are waiting for scans.
        self._frontend_context_semaphore = asyncio.Semaphore(
            min(4, MAX_ACTIVE_MONITORS)
        )
        self._loop_ready.set()
        loop.run_forever()

    async def _get_browser(self):
        async with self._browser_lock:
            if self._browser and self._browser.is_connected():
                return self._browser
            from playwright.async_api import async_playwright

            if self._playwright is None:
                self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=[
                    "--disable-background-networking",
                    "--disable-component-update",
                    "--disable-dev-shm-usage",
                    "--disable-extensions",
                ],
            )
            return self._browser

    def collect_frontend_context(
        self,
        *,
        proxy,
        token,
        account_id,
        cookies,
        device_id="",
        user_agent="",
        timeout=8,
    ):
        """Capture browser-generated ChatGPT context before creating Checkout."""
        self._ensure_loop()
        timeout = max(3, min(int(timeout or 8), 15))
        future = asyncio.run_coroutine_threadsafe(
            self._collect_frontend_context(
                proxy=proxy,
                token=token,
                account_id=account_id,
                cookies=cookies,
                device_id=device_id,
                user_agent=user_agent,
                timeout=timeout,
            ),
            self._loop,
        )
        try:
            return future.result(timeout=timeout + 12)
        except Exception:
            future.cancel()
            raise

    async def _collect_frontend_context(
        self,
        *,
        proxy,
        token,
        account_id,
        cookies,
        device_id,
        user_agent,
        timeout,
    ):
        context = None
        # Account and device identifiers are caller-owned isolation metadata.
        # Browser-generated risk context is accepted only when it agrees with
        # them, and the bearer token is never injected into bootstrap traffic.
        captured = {}
        if account_id:
            captured["chatgpt-account-id"] = str(account_id)
        if device_id:
            captured["oai-device-id"] = str(device_id)
        ready = asyncio.Event()
        try:
            async with self._frontend_context_semaphore:
                browser = await self._get_browser()
                options = {
                    "proxy": _playwright_proxy(proxy),
                    "locale": "en-PH",
                    "timezone_id": "Asia/Manila",
                    "viewport": {"width": 1280, "height": 900},
                }
                if user_agent:
                    options["user_agent"] = str(user_agent)
                context = await browser.new_context(**options)
                if cookies:
                    safe_cookies = [
                        cookie for cookie in cookies
                        if cookie.get("name") in FRONTEND_CONTEXT_COOKIE_NAMES
                    ]
                    if safe_cookies:
                        await context.add_cookies(safe_cookies)

                async def capture_request(route, request):
                    parsed = urllib.parse.urlparse(request.url)
                    is_backend = (
                        parsed.hostname == "chatgpt.com"
                        and parsed.path.startswith("/backend-api/")
                    )
                    if not is_backend:
                        await route.continue_()
                        return
                    headers = await request.all_headers()
                    context_headers = _sanitize_continue_context_headers(headers)
                    native_account_id = context_headers.get("chatgpt-account-id", "")
                    native_device_id = context_headers.get("oai-device-id", "")
                    account_matches = not (
                        account_id
                        and native_account_id
                        and native_account_id != str(account_id)
                    )
                    device_matches = not (
                        device_id
                        and native_device_id
                        and native_device_id != str(device_id)
                    )
                    if account_matches and device_matches:
                        context_headers.pop("chatgpt-account-id", None)
                        context_headers.pop("oai-device-id", None)
                        captured.update(context_headers)
                    required = (
                        "x-oai-is-client-observation",
                        "oai-session-id",
                        "oai-client-build-number",
                        "oai-client-version",
                    )
                    if all(captured.get(name) for name in required):
                        ready.set()
                    # Let read-only, unauthenticated bootstrap calls complete so
                    # the frontend can initialize its later observation header.
                    # Mutating backend calls are unnecessary for this page.
                    if str(request.method or "GET").upper() in {
                        "GET", "HEAD", "OPTIONS",
                    }:
                        await route.continue_()
                    else:
                        await route.abort("blockedbyclient")

                await context.route("**/*", capture_request)
                page = await context.new_page()
                try:
                    await page.goto(
                        "https://chatgpt.com/",
                        wait_until="commit",
                        timeout=timeout * 1000,
                    )
                except Exception:
                    pass
                loop = asyncio.get_running_loop()
                capture_deadline = loop.time() + timeout
                try:
                    await asyncio.wait_for(
                        ready.wait(), timeout=max(0.01, timeout * 0.55)
                    )
                except asyncio.TimeoutError:
                    pass
                if not captured.get("x-oai-is-client-observation"):
                    remaining = capture_deadline - loop.time()
                    if remaining > 0:
                        try:
                            await page.reload(
                                wait_until="commit",
                                timeout=max(250, int(min(3, remaining) * 1000)),
                            )
                        except Exception:
                            pass
                remaining = capture_deadline - loop.time()
                if not ready.is_set() and remaining > 0:
                    try:
                        await asyncio.wait_for(ready.wait(), timeout=remaining)
                    except asyncio.TimeoutError:
                        pass
                browser_cookies = []
                for cookie in await context.cookies("https://chatgpt.com/"):
                    if cookie.get("name") in FRONTEND_CONTEXT_COOKIE_NAMES:
                        browser_cookies.append(cookie)
                return {
                    "headers": dict(captured),
                    "cookies": browser_cookies,
                }
        finally:
            if context is not None:
                with contextlib.suppress(Exception):
                    await context.close()

    def _update(self, monitor_id, **values):
        transition = None
        with self._states_lock:
            state = self._states.get(monitor_id)
            if not state:
                return
            previous_status = state.get("status")
            state.update(values)
            state["updated_at"] = int(time.time())
            next_status = state.get("status")
            if next_status and next_status != previous_status:
                transition = next_status
        if transition:
            self._audit(monitor_id, "status_change", status=transition)

    def _audit(self, monitor_id, event, **fields):
        with self._states_lock:
            state = dict(self._states.get(str(monitor_id or "")) or {})
        parts = [f"[payment-monitor] {str(monitor_id or '')[:16]}"]
        if fields.get("status"):
            parts.append(f"status={str(fields.pop('status'))[:40]}")
        parts.append(f"event={re.sub(r'[^a-z0-9_-]', '', str(event or '').lower())[:48]}")
        client_account_id = state.get("client_account_id")
        if client_account_id:
            safe_client = re.sub(r"[^A-Za-z0-9_.:-]", "", str(client_account_id))[:64]
            if safe_client:
                parts.append(f"client={safe_client}")
        for key, value in fields.items():
            if value is None or value == "":
                continue
            safe_key = re.sub(r"[^a-z0-9_]", "", str(key).lower())[:40]
            if not safe_key:
                continue
            if isinstance(value, bool):
                safe_value = "true" if value else "false"
            elif safe_key == "error":
                safe_value = _safe_log_error(value)
            else:
                safe_value = re.sub(r"\s+", "_", str(value)).strip("_")[:180]
            if safe_value:
                parts.append(f"{safe_key}={safe_value}")
        print(" ".join(parts), flush=True)

    def _complete_verified_payment(self, monitor_id, verified, source):
        if not isinstance(verified, dict) or verified.get("_entitlement_verified") is not True:
            return False
        business_status = _payload_status(verified)
        if business_status not in CALLBACK_SUCCESS_STATUSES:
            return False
        verified_by = str(
            verified.get("_callback_verified_by") or "plus_entitlement"
        )
        self._audit(
            monitor_id,
            "callback_completed",
            source=source,
            business_status=business_status,
            verified_by=verified_by,
        )
        self._update(monitor_id, status="completed", refresh_available=False, error="")
        return True

    async def _verify_payment_completion(self, monitor_id, verify_callback):
        try:
            verified = await asyncio.to_thread(verify_callback)
        except Exception as exc:
            self._audit(monitor_id, "entitlement_poll_failed", error=exc)
            return False
        return self._complete_verified_payment(
            monitor_id, verified, "plus_entitlement_poll"
        )

    def _mark_callback_unconfirmed(self, monitor_id):
        self._audit(
            monitor_id,
            "callback_unconfirmed",
            seconds=CALLBACK_CONFIRM_TIMEOUT_SECONDS,
            reason="plus_entitlement_not_active",
        )
        self._update(
            monitor_id,
            status="callback_unconfirmed",
            refresh_available=False,
            error="回调已受理，但未确认 Plus 权益到账",
        )

    async def _upgrade_real_qr(
        self,
        monitor_id,
        page,
        timeout_seconds,
        ready_event,
        fallback_event,
    ):
        self._audit(
            monitor_id,
            "real_qr_capture_started",
            seconds=timeout_seconds,
        )
        source = "gcash_page"
        event = ready_event
        try:
            qr_png = await _capture_real_qr_png(page, timeout_seconds=timeout_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._audit(monitor_id, "real_qr_unavailable", error=exc)
            with self._states_lock:
                current = dict(self._states.get(monitor_id) or {})
            if current.get("status") in TERMINAL_STATUSES:
                return
            try:
                qr_png = _fallback_qr_png(current.get("gcash_url"))
            except Exception as fallback_exc:
                self._audit(monitor_id, "qr_fallback_failed", error=fallback_exc)
                return
            source = "gcash_link_fallback"
            event = fallback_event

        with self._states_lock:
            current = dict(self._states.get(monitor_id) or {})
        if current.get("status") in TERMINAL_STATUSES:
            return
        qr_version = int(current.get("qr_version") or 0) + 1
        self._audit(
            monitor_id,
            event,
            source=source,
            image_bytes=len(qr_png),
            image_ref=_short_hash(qr_png),
            qr_version=qr_version,
        )
        self._update(
            monitor_id,
            qr_png=qr_png,
            qr_source=source,
            qr_version=qr_version,
            refresh_available=True,
        )

    def _schedule_real_qr_upgrade(
        self,
        monitor_id,
        page,
        timeout_seconds,
        ready_event,
        fallback_event,
    ):
        with self._states_lock:
            state = dict(self._states.get(monitor_id) or {})
        previous = state.get("_qr_upgrade_task")
        if previous is not None and not previous.done():
            previous.cancel()
        task = asyncio.create_task(
            self._upgrade_real_qr(
                monitor_id,
                page,
                timeout_seconds,
                ready_event,
                fallback_event,
            )
        )
        self._update(monitor_id, _qr_upgrade_task=task)
        return task

    def start(
        self,
        *,
        navigation_url,
        proxy,
        token,
        account_id,
        cookies,
        continue_callback,
        verify_callback,
        close_callback,
        client_account_id="",
        device_id="",
        user_agent="",
    ):
        self._ensure_loop()
        monitor_id = "monitor_" + secrets.token_urlsafe(18)
        ready = threading.Event()
        closed = threading.Event()
        with self._states_lock:
            self._cleanup_locked()
            self._states[monitor_id] = {
                "status": "starting",
                "gcash_url": "",
                "qr_png": b"",
                "qr_source": "",
                "qr_version": 0,
                "expires_at": None,
                "refresh_available": False,
                "refresh_until": None,
                "error": "",
                "client_account_id": str(client_account_id or ""),
                "proxy_ref": _short_hash(proxy),
                "_closed": closed,
                "updated_at": int(time.time()),
            }
        args = {
            "monitor_id": monitor_id,
            "navigation_url": navigation_url,
            "proxy": proxy,
            "token": token,
            "account_id": account_id,
            "cookies": list(cookies or []),
            "continue_callback": continue_callback,
            "verify_callback": verify_callback,
            "close_callback": close_callback,
            "device_id": str(device_id or ""),
            "user_agent": str(user_agent or ""),
            "ready": ready,
            "closed": closed,
        }
        self._audit(
            monitor_id,
            "monitor_created",
            route=_route_label(navigation_url),
            proxy_ref=_short_hash(proxy),
            cookie_count=len(args["cookies"]),
        )
        future = asyncio.run_coroutine_threadsafe(self._run_monitor(args), self._loop)
        self._update(monitor_id, _monitor_future=future)
        if not ready.wait(MONITOR_READY_TIMEOUT):
            self._audit(monitor_id, "monitor_ready_timeout", seconds=MONITOR_READY_TIMEOUT)
            self._update(monitor_id, status="failed", error="支付页面启动超时")
            raise RuntimeError("后台打开 GCash 页面超时")
        with self._states_lock:
            state = dict(self._states.get(monitor_id) or {})
        if not state.get("gcash_url"):
            raise RuntimeError(state.get("error") or "后台未获取到 GCash 页面")
        return monitor_id, state["gcash_url"], state["expires_at"]

    async def _run_monitor(self, args):
        monitor_id = args["monitor_id"]
        context = None
        ready = args["ready"]
        capture = {
            "redirect_result": "",
            "native_action_result": None,
            "browser_context_headers": {},
            "callback_completed": False,
            "continue_requested_at": 0.0,
            "continue_failed": False,
            "continue_accepted": False,
            "fallback_submitted": False,
            "callback_confirm_started": 0.0,
        }
        phase = "browser_start"
        try:
            async with self._semaphore:
                browser = await self._get_browser()
                context_options = {
                    "proxy": _playwright_proxy(args["proxy"]),
                    "locale": "en-PH",
                    "timezone_id": "Asia/Manila",
                    # The supplied browser profile is Windows desktop. A desktop
                    # viewport keeps GCash's 20vw QR at a scannable resolution.
                    "viewport": {"width": 1280, "height": 900},
                }
                if args.get("user_agent"):
                    context_options["user_agent"] = args["user_agent"]
                context = await browser.new_context(
                    **context_options,
                )
                self._audit(monitor_id, "browser_context_ready")
                if args["cookies"]:
                    await context.add_cookies(args["cookies"])

                async def route_request(route, request):
                    parsed = urllib.parse.urlparse(request.url)
                    if parsed.hostname == "chatgpt.com" and parsed.path.startswith("/backend-api/"):
                        headers = await request.all_headers()
                        headers["authorization"] = f"Bearer {args['token']}"
                        if args["account_id"]:
                            headers["chatgpt-account-id"] = str(args["account_id"])
                        if args.get("device_id"):
                            headers["oai-device-id"] = args["device_id"]
                        headers["x-openai-target-path"] = parsed.path
                        headers["x-openai-target-route"] = parsed.path
                        context_headers = _sanitize_continue_context_headers(headers)
                        previous_context = capture["browser_context_headers"]
                        merged_context = dict(previous_context)
                        merged_context.update(context_headers)
                        capture["browser_context_headers"] = merged_context
                        is_continue = parsed.path.endswith(
                            "/custom_payment_method/continue"
                        )
                        gained_observation = bool(
                            context_headers.get("x-oai-is-client-observation")
                            and not previous_context.get(
                                "x-oai-is-client-observation"
                            )
                        )
                        gained_attestation = bool(
                            context_headers.get("oai-web-deployment-attestation")
                            and not previous_context.get(
                                "oai-web-deployment-attestation"
                            )
                        )
                        if is_continue or gained_observation or gained_attestation:
                            self._audit(
                                monitor_id,
                                "browser_callback_context_captured",
                                source="native_continue" if is_continue else "backend_request",
                                context_header_count=len(merged_context),
                                observation_captured=bool(
                                    merged_context.get("x-oai-is-client-observation")
                                ),
                                attestation_captured=bool(
                                    merged_context.get("oai-web-deployment-attestation")
                                ),
                            )
                        await route.continue_(headers=headers)
                    else:
                        await route.continue_()

                await context.route("**/*", route_request)
                page = await context.new_page()
                self._update(
                    monitor_id,
                    _page=page,
                    _refresh_lock=asyncio.Lock(),
                )

                def record_redirect(found, source):
                    if not found or capture["redirect_result"]:
                        return False
                    capture["redirect_result"] = found
                    self._audit(
                        monitor_id,
                        "redirect_captured",
                        source=source,
                        redirect_ref=_short_hash(found),
                        redirect_length=len(found),
                    )
                    self._update(monitor_id, status="redirect_captured")
                    return True

                def inspect_request(request):
                    post_data = request.post_data or ""
                    found = _extract_redirect_result(request.url)
                    source = "request_url"
                    if not found:
                        found = _extract_redirect_result(post_data)
                        source = "request_body"
                    record_redirect(found, source)
                    path = urllib.parse.urlparse(request.url).path
                    if path.endswith("/custom_payment_method/continue"):
                        native_action_result = _extract_continue_action_result(post_data)
                        if native_action_result:
                            native_redirect = native_action_result["redirectResult"]
                            if not capture["redirect_result"]:
                                record_redirect(native_redirect, "native_continue_body")
                            if capture["redirect_result"] == native_redirect:
                                capture["native_action_result"] = native_action_result
                        capture["continue_requested_at"] = time.monotonic()
                        capture["continue_failed"] = False
                        self._audit(
                            monitor_id,
                            "native_continue_requested",
                            method=request.method,
                            route=_route_label(request.url),
                            action_result_captured=bool(capture["native_action_result"]),
                        )
                        self._update(monitor_id, status="callback_processing")

                async def inspect_response(response):
                    path = urllib.parse.urlparse(response.url).path
                    if path.endswith("/custom_payment_method/continue"):
                        content_type = str(response.headers.get("content-type") or "").lower()
                        business_status = ""
                        if "json" in content_type:
                            try:
                                business_status = _payload_status(await response.text())
                            except Exception:
                                pass
                        self._audit(
                            monitor_id,
                            "native_continue_response",
                            http_status=response.status,
                            business_status=business_status or "unknown",
                            content_type=content_type.split(";", 1)[0],
                        )
                        callback_accepted = business_status in CALLBACK_ACCEPTED_STATUSES
                        if 200 <= response.status < 300 and callback_accepted:
                            capture["continue_accepted"] = True
                            if not capture["callback_confirm_started"]:
                                capture["callback_confirm_started"] = time.monotonic()
                            self._audit(
                                monitor_id,
                                "callback_accepted",
                                source="native_browser",
                                http_status=response.status,
                                business_status=business_status or "unknown",
                            )
                            self._update(monitor_id, status="callback_processing")
                        else:
                            capture["continue_failed"] = True
                            self._audit(
                                monitor_id,
                                "native_continue_unconfirmed",
                                http_status=response.status,
                                business_status=business_status or "unknown",
                            )
                            self._update(monitor_id, status="callback_processing")

                    if capture["redirect_result"]:
                        return
                    headers = response.headers
                    found = _extract_redirect_result(headers.get("location", ""))
                    source = "response_location"
                    content_type = str(headers.get("content-type") or "").lower()
                    if not found and response.request.resource_type in {"document", "xhr", "fetch"}:
                        if any(kind in content_type for kind in ("json", "text", "html", "form")):
                            try:
                                found = _extract_redirect_result(await response.text())
                                source = "response_body"
                            except Exception:
                                pass
                    record_redirect(found, source)

                last_navigation = {"route": ""}

                def inspect_frame(frame):
                    if frame.parent_frame is not None:
                        return
                    route = _route_label(frame.url)
                    if route and route != last_navigation["route"]:
                        last_navigation["route"] = route
                        self._audit(monitor_id, "page_navigated", route=route)

                def inspect_request_failed(request):
                    route = _route_label(request.url)
                    is_continue = route.endswith("/custom_payment_method/continue")
                    if is_continue:
                        capture["continue_failed"] = True
                    if is_continue or request.resource_type == "document":
                        self._audit(
                            monitor_id,
                            "network_request_failed",
                            resource=request.resource_type,
                            route=route,
                            error=request.failure or "unknown",
                        )

                page.on("request", inspect_request)
                page.on("response", inspect_response)
                page.on("framenavigated", inspect_frame)
                page.on("requestfailed", inspect_request_failed)

                phase = "initial_navigation"
                self._audit(
                    monitor_id,
                    "navigation_started",
                    route=_route_label(args["navigation_url"]),
                )
                try:
                    await page.goto(
                        args["navigation_url"],
                        wait_until="domcontentloaded",
                        timeout=45_000,
                    )
                except Exception as exc:
                    # Redirect pages frequently keep navigation pending while their
                    # JavaScript polls. The URL/body checks below decide readiness.
                    self._audit(monitor_id, "navigation_incomplete", error=exc)

                phase = "gcash_discovery"
                gcash_link = ""
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline and not gcash_link:
                    gcash_link = _gcash_url(page.url)
                    if not gcash_link:
                        try:
                            gcash_link = _gcash_url(await page.content())
                        except Exception:
                            pass
                    if not gcash_link:
                        await page.wait_for_timeout(400)
                if not gcash_link:
                    raise RuntimeError("真实支付页未进入 GCash")

                expires_at = int(time.time()) + MONITOR_TTL_SECONDS
                self._audit(
                    monitor_id,
                    "gcash_link_ready",
                    route=_route_label(gcash_link),
                    expires_at=expires_at,
                )
                self._update(
                    monitor_id,
                    status="waiting_scan",
                    gcash_url=gcash_link,
                    qr_png=b"",
                    qr_source="real_qr_pending",
                    qr_version=0,
                    expires_at=expires_at,
                    refresh_available=False,
                    refresh_until=expires_at + MONITOR_REFRESH_GRACE_SECONDS,
                )
                ready.set()
                self._schedule_real_qr_upgrade(
                    monitor_id,
                    page,
                    INITIAL_QR_CAPTURE_TIMEOUT,
                    "qr_ready",
                    "qr_fallback_ready",
                )

                phase = "waiting_scan"
                callback_started = 0.0
                last_page_scan = 0.0
                last_checkout_poll = 0.0
                while True:
                    if capture["callback_completed"]:
                        return
                    if not capture["redirect_result"] and time.monotonic() - last_page_scan >= 1:
                        last_page_scan = time.monotonic()
                        found = _extract_redirect_result(page.url)
                        if not found:
                            try:
                                found = _extract_redirect_result(await page.content())
                            except Exception:
                                pass
                        if found:
                            source = "page_url" if _extract_redirect_result(page.url) else "page_html"
                            record_redirect(found, source)
                    redirect_result = capture["redirect_result"]
                    if redirect_result:
                        phase = "callback"
                        if not callback_started:
                            callback_started = time.monotonic()
                        # Give the actual ChatGPT return page a short chance to
                        # submit continue itself with the injected auth headers.
                        native_started = capture["continue_requested_at"]
                        native_timed_out = native_started and time.monotonic() - native_started >= 20
                        should_fallback = (
                            not capture["continue_accepted"]
                            and not capture["fallback_submitted"]
                            and (
                                capture["continue_failed"]
                                or native_timed_out
                                or (not native_started and time.monotonic() - callback_started >= 4)
                            )
                        )
                        if should_fallback:
                            capture["fallback_submitted"] = True
                            fallback_reason = (
                                "native_failed" if capture["continue_failed"]
                                else "native_timeout" if native_timed_out
                                else "native_not_started"
                            )
                            self._audit(
                                monitor_id,
                                "fallback_continue_started",
                                reason=fallback_reason,
                                redirect_ref=_short_hash(redirect_result),
                                redirect_length=len(redirect_result),
                                action_result_keys="redirectResult",
                                context_header_count=len(
                                    capture["browser_context_headers"]
                                ),
                                observation_captured=bool(
                                    capture["browser_context_headers"].get(
                                        "x-oai-is-client-observation"
                                    )
                                ),
                                action_result_source=(
                                    "native_browser"
                                    if capture["native_action_result"]
                                    else "server_normalized"
                                ),
                            )
                            self._update(monitor_id, status="callback_processing")
                            fallback_response = await asyncio.to_thread(
                                args["continue_callback"],
                                redirect_result,
                                capture["native_action_result"],
                                capture["browser_context_headers"],
                            )
                            business_status = _payload_status(fallback_response) or "unknown"
                            callback_attempt_value = (
                                fallback_response.get("_callback_attempts")
                                if isinstance(fallback_response, dict)
                                else None
                            )
                            callback_attempts = int(
                                1 if callback_attempt_value is None
                                else callback_attempt_value
                            )
                            verified_by = str(
                                fallback_response.get("_callback_verified_by")
                                or "continue_response"
                            ) if isinstance(fallback_response, dict) else "continue_response"
                            self._audit(
                                monitor_id,
                                "fallback_continue_response",
                                business_status=business_status,
                                attempts=callback_attempts,
                                verified_by=verified_by,
                            )
                            if self._complete_verified_payment(
                                monitor_id,
                                fallback_response,
                                "server_fallback_entitlement",
                            ):
                                capture["callback_completed"] = True
                                return
                            callback_accepted = bool(
                                isinstance(fallback_response, dict)
                                and fallback_response.get("_callback_accepted") is True
                            )
                            if not callback_accepted:
                                raise RuntimeError(
                                    "continue 回调未被明确受理"
                                )
                            capture["continue_accepted"] = True
                            if not capture["callback_confirm_started"]:
                                capture["callback_confirm_started"] = time.monotonic()
                            self._audit(
                                monitor_id,
                                "callback_accepted",
                                source="server_fallback",
                                business_status=business_status,
                                attempts=callback_attempts,
                                verified_by=verified_by,
                            )
                            self._update(monitor_id, status="callback_processing")

                    if (
                        time.monotonic() - last_checkout_poll >= CHECKOUT_STATUS_POLL_SECONDS
                    ):
                        last_checkout_poll = time.monotonic()
                        if await self._verify_payment_completion(
                            monitor_id, args["verify_callback"]
                        ):
                            capture["callback_completed"] = True
                            return

                    confirm_started = capture["callback_confirm_started"]
                    if (
                        confirm_started
                        and time.monotonic() - confirm_started
                        >= CALLBACK_CONFIRM_TIMEOUT_SECONDS
                    ):
                        self._mark_callback_unconfirmed(monitor_id)
                        return

                    with self._states_lock:
                        current = dict(self._states.get(monitor_id) or {})
                    stamp = time.time()
                    current_status = current.get("status")
                    current_expiry = int(current.get("expires_at") or 0)
                    if current_status == "waiting_scan" and current_expiry and stamp >= current_expiry:
                        self._audit(monitor_id, "qr_expired", expires_at=current_expiry)
                        self._update(
                            monitor_id,
                            status="expired",
                            refresh_available=True,
                            refresh_until=int(stamp) + MONITOR_REFRESH_GRACE_SECONDS,
                        )
                    elif current_status == "expired":
                        refresh_until = int(current.get("refresh_until") or 0)
                        if not refresh_until or stamp >= refresh_until:
                            self._audit(
                                monitor_id,
                                "monitor_released",
                                reason="refresh_grace_expired",
                            )
                            self._update(monitor_id, refresh_available=False)
                            return
                    await page.wait_for_timeout(250)
        except Exception as exc:
            current = self.status(monitor_id).get("status")
            status = "callback_failed" if current not in {"starting", "failed"} else "failed"
            self._audit(
                monitor_id,
                "monitor_error",
                phase=phase,
                previous_status=current,
                next_status=status,
                error=exc,
            )
            self._update(
                monitor_id,
                status=status,
                refresh_available=False,
                error=str(exc)[:300],
            )
        finally:
            ready.set()
            with self._states_lock:
                qr_upgrade_task = (
                    self._states.get(monitor_id) or {}
                ).get("_qr_upgrade_task")
            if qr_upgrade_task is not None and not qr_upgrade_task.done():
                qr_upgrade_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await qr_upgrade_task
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass
            try:
                args["close_callback"]()
            except Exception:
                pass
            self._audit(
                monitor_id,
                "monitor_closed",
                final_status=self.status(monitor_id).get("status"),
            )
            args["closed"].set()

    async def _refresh_page(self, monitor_id):
        with self._states_lock:
            state = dict(self._states.get(monitor_id) or {})
        page = state.get("_page")
        refresh_lock = state.get("_refresh_lock")
        if page is None or refresh_lock is None or page.is_closed():
            raise RuntimeError("原 GCash 页面已释放，需要重新提链")

        async with refresh_lock:
            clicked = False
            labels = re.compile(
                r"refresh(?: qr(?: code)?)?|try again|retry|generate(?: new)? qr|new qr",
                re.IGNORECASE,
            )
            candidates = page.locator("button, [role=button], a").filter(has_text=labels)
            for index in range(min(await candidates.count(), 12)):
                candidate = candidates.nth(index)
                try:
                    if await candidate.is_visible():
                        await candidate.click(timeout=5_000)
                        clicked = True
                        break
                except Exception:
                    continue

            if clicked:
                self._audit(monitor_id, "refresh_navigation", strategy="page_control")
                await page.wait_for_timeout(1_200)
            else:
                self._audit(monitor_id, "refresh_navigation", strategy="page_reload")
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=30_000)
                except Exception:
                    # Some GCash pages keep polling and never become fully idle.
                    pass

            gcash_link = ""
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline and not gcash_link:
                gcash_link = _gcash_url(page.url)
                if not gcash_link:
                    try:
                        gcash_link = _gcash_url(await page.content())
                    except Exception:
                        pass
                if not gcash_link:
                    await page.wait_for_timeout(300)
            if not gcash_link:
                raise RuntimeError("刷新后未回到可信的 GCash 页面")

            current = self.status(monitor_id)
            if current.get("status") == "completed":
                return current
            expires_at = int(time.time()) + MONITOR_TTL_SECONDS
            self._audit(
                monitor_id,
                "refresh_gcash_link_ready",
                route=_route_label(gcash_link),
                expires_at=expires_at,
            )
            self._update(
                monitor_id,
                status="waiting_scan",
                gcash_url=gcash_link,
                qr_png=b"",
                qr_source="real_qr_pending",
                expires_at=expires_at,
                refresh_available=False,
                refresh_until=expires_at + MONITOR_REFRESH_GRACE_SECONDS,
                error="",
            )
            self._schedule_real_qr_upgrade(
                monitor_id,
                page,
                REFRESH_QR_CAPTURE_TIMEOUT,
                "refresh_qr_ready",
                "refresh_qr_fallback_ready",
            )
            return self.status(monitor_id)

    def refresh(self, monitor_id):
        monitor_id = str(monitor_id or "")
        with self._states_lock:
            state = dict(self._states.get(monitor_id) or {})
        status = state.get("status") or "unavailable"
        self._audit(monitor_id, "refresh_requested", current_status=status)
        if status not in {"waiting_scan", "expired"}:
            if status == "completed":
                raise RuntimeError("支付已完成，无需刷新二维码")
            raise RuntimeError("当前支付页面不能刷新，需要重新提链")
        if status == "expired" and time.time() >= int(state.get("refresh_until") or 0):
            self._update(monitor_id, refresh_available=False)
            raise RuntimeError("二维码刷新保留时间已结束，需要重新提链")
        if not state.get("refresh_available") or self._loop is None:
            raise RuntimeError("原 GCash 页面已释放，需要重新提链")

        self._update(
            monitor_id,
            status="refreshing",
            qr_png=b"",
            qr_source="real_qr_pending",
            refresh_available=False,
            error="",
        )
        future = asyncio.run_coroutine_threadsafe(self._refresh_page(monitor_id), self._loop)
        try:
            return future.result(timeout=MONITOR_REFRESH_TIMEOUT)
        except Exception as exc:
            future.cancel()
            self._audit(monitor_id, "refresh_failed", previous_status=status, error=exc)
            with self._states_lock:
                current = dict(self._states.get(monitor_id) or {})
            if current.get("status") != "completed":
                stamp = int(time.time())
                expires_at = int(current.get("expires_at") or 0)
                refresh_until = int(current.get("refresh_until") or 0)
                fallback_status = "waiting_scan" if expires_at > stamp else "expired"
                previous_png = state.get("qr_png")
                previous_qr_valid = (
                    isinstance(previous_png, bytes)
                    and previous_png.startswith(PNG_SIGNATURE)
                    and int(state.get("expires_at") or 0) > stamp
                )
                self._update(
                    monitor_id,
                    status=fallback_status,
                    qr_png=previous_png if previous_qr_valid else b"",
                    qr_source=(state.get("qr_source") or "") if previous_qr_valid else "",
                    qr_version=int(state.get("qr_version") or 0),
                    refresh_available=refresh_until > stamp,
                    error=str(exc)[:300],
                )
            message = str(exc).strip() or "刷新二维码超时"
            raise RuntimeError(f"刷新二维码失败：{message}") from exc

    def abandon(self, monitor_id, timeout=5):
        """Stop a live payment monitor and release browser/session resources."""
        monitor_id = str(monitor_id or "")
        with self._states_lock:
            state = dict(self._states.get(monitor_id) or {})
        if not state:
            return self.status(monitor_id)

        current_status = state.get("status") or "unavailable"
        if current_status == "completed":
            return self.status(monitor_id)
        if current_status != "abandoned":
            self._audit(
                monitor_id,
                "monitor_abandon_requested",
                previous_status=current_status,
            )
            self._update(
                monitor_id,
                status="abandoned",
                refresh_available=False,
                qr_png=b"",
                qr_source="",
                error="",
            )

        future = state.get("_monitor_future")
        if future is not None and not future.done():
            future.cancel()
        closed = state.get("_closed")
        if closed is not None:
            closed.wait(max(0.1, min(float(timeout), 10.0)))
        return self.status(monitor_id)

    def status(self, monitor_id):
        with self._states_lock:
            state = dict(self._states.get(str(monitor_id or "")) or {})
        stamp = int(time.time())
        refresh_available = bool(
            state.get("refresh_available")
            and state.get("status") in {"waiting_scan", "expired"}
            and int(state.get("refresh_until") or 0) > stamp
        )
        return {
            "status": state.get("status") or "unavailable",
            "expires_at": state.get("expires_at"),
            "gcash_url": state.get("gcash_url") or "",
            "qr_source": state.get("qr_source") or "",
            "qr_version": int(state.get("qr_version") or 0),
            "qr_ready": bool(
                isinstance(state.get("qr_png"), bytes)
                and state.get("qr_png", b"").startswith(PNG_SIGNATURE)
            ),
            "refresh_available": refresh_available,
        }

    def qr_image(self, monitor_id):
        with self._states_lock:
            png = (self._states.get(str(monitor_id or "")) or {}).get("qr_png")
        if isinstance(png, bytes) and png.startswith(PNG_SIGNATURE):
            return png
        return b""

    def _cleanup_locked(self):
        stamp = time.time()
        expired = [
            monitor_id
            for monitor_id, state in self._states.items()
            if state.get("status") in TERMINAL_STATUSES
            and stamp - int(state.get("updated_at") or stamp) > 3600
        ]
        for monitor_id in expired:
            self._states.pop(monitor_id, None)


manager = PaymentMonitorManager()
