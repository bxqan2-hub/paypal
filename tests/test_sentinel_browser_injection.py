from __future__ import annotations

import threading
from types import SimpleNamespace

from payment_link_extractor.transport import BrowserSentinelProvider


def test_sentinel_init_script_publishes_window_sdk_without_bootstrap_shim() -> None:
    script = BrowserSentinelProvider._build_sentinel_init_script()
    assert "window.SentinelSDK = SentinelSDK;" in script
    assert "window.__opllSentinelInjected = true;" in script
    # The Node-only bootstrap replaces browser read-only globals and must not
    # be injected into Chromium's real window.
    assert "g.crypto = g.crypto || {};" not in script


def test_sentinel_provider_defaults_empty_flow_to_chatgpt_checkout() -> None:
    provider = BrowserSentinelProvider.__new__(BrowserSentinelProvider)
    provider._failed = False
    provider._started = True
    provider._lock = threading.RLock()
    provider._cookies = ""
    provider._attestation = ""
    calls: list[str] = []
    provider._ping = lambda _referer: None

    def fake_eval(expression: str, timeout: float = 75.0):
        calls.append(expression)
        return "proof-fixture"

    provider._eval = fake_eval
    headers = provider.headers("")
    assert headers["OpenAI-Sentinel-Token"] == "proof-fixture"
    assert '"chatgpt_checkout"' in calls[-1]


def test_sentinel_provider_promotes_http_only_oai_did_to_device_header() -> None:
    provider = BrowserSentinelProvider.__new__(BrowserSentinelProvider)
    provider.device_id = "generated-device"
    provider.transport_session = SimpleNamespace(headers={})
    provider._run = lambda _args: {
        "data": {
            "cookies": [
                {"name": "oai-did", "value": "browser-cookie-device", "httpOnly": True},
                {"name": "session", "value": "session-fixture"},
            ]
        }
    }
    provider._sync_cookies()
    assert provider.device_id == "browser-cookie-device"
    assert provider.transport_session.headers["oai-device-id"] == "browser-cookie-device"
    assert "oai-did=browser-cookie-device" in provider.transport_session.headers["Cookie"]
