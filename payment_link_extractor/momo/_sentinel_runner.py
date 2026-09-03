from __future__ import annotations

"""Run the browser Sentinel SDK in a short-lived, isolated Chromium context.

The payment extractor itself uses curl-cffi.  The current Sentinel SDK is a
browser bundle and deliberately collects browser/runtime signals, so it is
kept in this small subprocess instead of being reimplemented in Python.
"""

import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit


SDK_URL = "https://chatgpt.com/sentinel/20260810913b/sdk.js"
SDK_BUILD = "20260810913b"
ORIGIN_URL = str(
    os.environ.get("OPLL_SENTINEL_ORIGIN_URL")
    or "https://chatgpt.com/robots.txt"
).strip()
SDK_CACHE_FILE = Path(
    os.environ.get("OPLL_SENTINEL_SDK_CACHE_FILE")
    or str(Path(__file__).with_name(f"sentinel_sdk_{SDK_BUILD}.js"))
)
def _locate_chrome146() -> str:
    """Locate a Chrome/Chromium 146 headless-shell for the Sentinel proof.

    The proof MUST be minted in the same Chrome major version the HTTP request
    presents (146).  Order: explicit ``OPLL_SENTINEL_BROWSER_EXECUTABLE`` first,
    then any 146 build dropped under ``<package>/runtime/`` (Windows or Linux).
    If nothing 146 is found we return "" and Playwright's own bundled Chromium
    is used -- the version guard in ``_apply_browser_identity`` then rejects it
    with an actionable message rather than silently minting a mismatched proof.
    """
    explicit = str(os.environ.get("OPLL_SENTINEL_BROWSER_EXECUTABLE") or "").strip()
    if explicit and Path(explicit).is_file():
        return explicit
    runtime_root = Path(__file__).resolve().parents[1] / "runtime"
    for pattern in (
        "chrome-146*/**/chrome-headless-shell.exe",
        "chrome-146*/**/chrome-headless-shell",
        "chrome-146*/**/chrome.exe",
        "*146*/**/chrome-headless-shell.exe",
        "*146*/**/chrome-headless-shell",
        "*146*/**/chrome.exe",
    ):
        for candidate in sorted(runtime_root.glob(pattern)):
            if candidate.is_file():
                return str(candidate)
    return ""


BROWSER_EXECUTABLE = _locate_chrome146()


def _client_hint_brands(value: str, browser_version: str) -> list[dict[str, str]]:
    pairs = re.findall(r'"([^"]+)"\s*;\s*v="([^"]+)"', str(value or ""))
    if pairs:
        return [{"brand": brand, "version": version} for brand, version in pairs]
    major = str(browser_version or "").split(".", 1)[0] or "146"
    return [
        {"brand": "Chromium", "version": major},
        {"brand": "Not-A.Brand", "version": "24"},
        {"brand": "Google Chrome", "version": major},
    ]


def _apply_browser_identity(
    context: object,
    page: object,
    *,
    user_agent: str,
    sec_ch_ua: str,
    sec_ch_ua_platform: str,
    language: str,
    browser_version: str,
) -> None:
    """Keep Chromium's JS and request metadata on the selected HTTP profile."""
    if not user_agent:
        return
    ua_match = re.search(r"(?:Chrome|Chromium)/(\d+)", user_agent)
    browser_major = str(browser_version or "").split(".", 1)[0]
    if ua_match and browser_major and ua_match.group(1) != browser_major:
        raise RuntimeError(
            f"Sentinel browser is Chrome {browser_version} but the request is "
            f"pinned to Chrome {ua_match.group(1)}. Mint the proof in a matching "
            "146 browser: install one (e.g. `npx @puppeteer/browsers install "
            "chrome-headless-shell@146`) and set OPLL_SENTINEL_BROWSER_EXECUTABLE "
            "to it, or drop it under payment_link_extractor/runtime/chrome-146*/. "
            "Minting the proof in a different version reproduces status=blocked."
        )
    brands = _client_hint_brands(sec_ch_ua, browser_version)
    platform_name = str(sec_ch_ua_platform or "").strip().strip('"') or "macOS"
    platform_token = "MacIntel" if platform_name == "macOS" else platform_name
    grease_names = {brand["brand"] for brand in brands if "brand" in brand["brand"].lower()}
    full_versions = [
        {
            "brand": brand["brand"],
            "version": "24.0.0.0" if brand["brand"] in grease_names else browser_version,
        }
        for brand in brands
    ]
    metadata = {
        "brands": brands,
        "fullVersionList": full_versions,
        "fullVersion": browser_version,
        "platform": platform_name,
        "platformVersion": "10.15.7" if platform_name == "macOS" else "10.0.0",
        "architecture": "x86",
        "model": "",
        "mobile": False,
        "bitness": "64",
        "wow64": False,
    }
    cdp = context.new_cdp_session(page)
    cdp.send(
        "Emulation.setUserAgentOverride",
        {
            "userAgent": user_agent,
            "acceptLanguage": language + ",en;q=0.9",
            "platform": platform_token,
            "userAgentMetadata": metadata,
        },
    )


def _load_sdk_source() -> str:
    try:
        source = SDK_CACHE_FILE.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Sentinel SDK cache unavailable: {type(exc).__name__}") from exc
    if len(source) < 10_000 or "SentinelSDK" not in source:
        raise RuntimeError("Sentinel SDK cache is invalid")
    return source


def _proxy_options(proxy: str) -> dict[str, str] | None:
    value = str(proxy or "").strip()
    if not value:
        return None
    if "://" not in value:
        value = "http://" + value
    parsed = urlsplit(value)
    if not parsed.hostname:
        raise ValueError("invalid proxy")
    server = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port:
        server += f":{parsed.port}"
    result: dict[str, str] = {"server": server}
    if parsed.username is not None:
        result["username"] = unquote(parsed.username)
    if parsed.password is not None:
        result["password"] = unquote(parsed.password)
    return result


def _run(payload: dict[str, object]) -> dict[str, object]:
    from playwright.sync_api import sync_playwright

    flow = str(payload.get("flow") or "").strip()
    if flow not in {"chatgpt_checkout", "checkout_session_approval"}:
        raise ValueError("unsupported Sentinel flow")
    device_id = str(payload.get("device_id") or "").strip()
    session_id = str(payload.get("session_id") or "").strip()
    user_agent = str(payload.get("user_agent") or "").strip()
    sec_ch_ua = str(payload.get("sec_ch_ua") or "").strip()
    sec_ch_ua_platform = str(payload.get("sec_ch_ua_platform") or "").strip()
    language = str(payload.get("language") or "en-US").strip() or "en-US"
    proxy = _proxy_options(str(payload.get("proxy") or ""))
    sdk_source = _load_sdk_source()
    headers = {
        "Accept": "*/*",
        "Accept-Language": language + ",en;q=0.9",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
    }
    if user_agent:
        headers["User-Agent"] = user_agent
    if session_id:
        headers["oai-session-id"] = session_id

    with sync_playwright() as playwright:
        launch_options: dict[str, object] = {
            "headless": True,
            "args": ["--no-sandbox"],
        }
        if BROWSER_EXECUTABLE:
            executable = Path(BROWSER_EXECUTABLE)
            if not executable.is_file():
                raise RuntimeError("configured Sentinel browser is unavailable")
            launch_options["executable_path"] = str(executable)
        if proxy:
            launch_options["proxy"] = proxy
        browser = playwright.chromium.launch(**launch_options)
        try:
            context_options: dict[str, object] = {
                "locale": language,
                "extra_http_headers": headers,
            }
            if user_agent:
                context_options["user_agent"] = user_agent
            context = browser.new_context(**context_options)
            try:
                if device_id:
                    context.add_cookies(
                        [
                            {
                                "name": "oai-did",
                                "value": device_id,
                                "domain": "chatgpt.com",
                                "path": "/",
                            },
                            {
                                "name": "oai-hlib",
                                "value": "true",
                                "domain": "chatgpt.com",
                                "path": "/",
                            },
                        ]
                    )
                page = context.new_page()
                _apply_browser_identity(
                    context,
                    page,
                    user_agent=user_agent,
                    sec_ch_ua=sec_ch_ua,
                    sec_ch_ua_platform=sec_ch_ua_platform,
                    language=language,
                    browser_version=browser.version,
                )
                page.goto(
                    ORIGIN_URL,
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
                # Execute the pinned SDK from the server cache.  The page is
                # still on chatgpt.com so the SDK observes the correct origin.
                # The tiny same-origin robots document avoids downloading the
                # full application shell for every token, and the SDK itself is
                # injected from the pinned local cache below.
                page.add_script_tag(content=sdk_source)
                token = page.evaluate(
                    """
                    async (flow) => {
                      if (!window.SentinelSDK || typeof window.SentinelSDK.token !== "function") {
                        throw new Error("SentinelSDK.token unavailable");
                      }
                      return await window.SentinelSDK.token(flow);
                    }
                    """,
                    flow,
                )
                if not isinstance(token, str) or len(token) < 100:
                    raise RuntimeError("SentinelSDK returned an empty token")
                # The token is intentionally returned only to the parent
                # process.  No token, cookies, or page content is logged.
                return {"ok": True, "token": token}
            finally:
                context.close()
        finally:
            browser.close()


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw or "{}")
        result = _run(payload)
        sys.stdout.write(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        # Keep stderr diagnostic deliberately secret-free; the parent turns
        # this into a task error without exposing proxy or token material.
        sys.stderr.write(f"sentinel runner failed: {type(exc).__name__}: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
