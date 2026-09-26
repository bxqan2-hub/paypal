# -*- coding: utf-8 -*-
"""账号 → 稳定的浏览器指纹与请求头。

同一个 access_token 必须每次算出**同一套** device_id / UA / 时区，
否则同一个账号在 OpenAI 眼里会不断换设备，风控直接判死。
"""
from __future__ import annotations

import hashlib
import uuid

from ._vendor import oaics

_CHROME_VERSIONS = ("146", "136", "131", "124")


def fingerprint(access_token: str, country: str = "IN") -> dict:
    """由 access_token 派生稳定指纹（同一 token 永远得到同一套值）。"""
    cc = (country or "US").upper()
    profile = oaics.profile(cc)
    digest = hashlib.sha256(("%s|%s" % (access_token or "", cc)).encode("utf-8")).hexdigest()
    version = _CHROME_VERSIONS[int(digest[:2], 16) % len(_CHROME_VERSIONS)]
    locale = str(profile.get("browser_locale") or "en-US")
    language = str(profile.get("browser_language") or locale)
    return {
        "country": cc,
        "device_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "promo-detect-device|" + digest)),
        "oai_session_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "promo-detect-session|" + digest)),
        "locale": locale,
        "timezone": str(profile.get("browser_timezone") or "America/New_York"),
        "oai_language": language,
        "accept_language": "%s,%s;q=0.9,en;q=0.8" % (language, language.split("-")[0]),
        "impersonate": "chrome" + version,
        "ua": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/%s.0.0.0 Safari/537.36" % version),
        "sec_ch_ua": '"Google Chrome";v="%s", "Not.A/Brand";v="8", "Chromium";v="%s"' % (version, version),
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"Windows"',
        "hardware_concurrency": "16",
        "screen": "1920x1080",
        "platform": "Win32",
        "platform_os": "Windows",
    }


def chatgpt_headers(access_token: str, fp: dict, *, referer: str, route: str) -> dict:
    """ChatGPT 后端接口头（含 Authorization）。"""
    headers = {
        **oaics.common_headers(country=fp.get("country") or "IN", device_id=fp["device_id"],
                               referer=referer, route=route, fingerprint=fp),
        "Authorization": "Bearer " + access_token,
        "Content-Type": "application/json",
    }
    if route:
        headers["x-openai-target-path"] = route
        headers["x-openai-target-route"] = route
    return headers


def stripe_headers(fp: dict) -> dict:
    """Stripe 支付页接口头（只带 publishable key，不带账号 token）。"""
    return {
        "Origin": "https://js.stripe.com",
        "Referer": "https://js.stripe.com/",
        "User-Agent": fp["ua"],
        "Accept-Language": fp["accept_language"],
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }
