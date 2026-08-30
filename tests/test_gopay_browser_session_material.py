from __future__ import annotations

import base64
import json
from types import SimpleNamespace

from payment_link_extractor import auth
from payment_link_extractor import gopay_checkout
from payment_link_extractor import gopay_sentinel_playwright
from payment_link_extractor import gopay_transport
from payment_link_extractor.models import ExtractionConfig


def _token(account_id: str, user_id: str, marker: str) -> str:
    def part(value: object) -> str:
        return base64.urlsafe_b64encode(
            json.dumps(value, separators=(",", ":")).encode()
        ).decode().rstrip("=")

    return ".".join(
        [
            part({"alg": "RS256"}),
            part(
                {
                    "https://api.openai.com/auth": {
                        "chatgpt_account_id": account_id,
                        "chatgpt_user_id": user_id,
                        "user_id": user_id,
                    }
                }
            ),
            marker,
        ]
    )


def test_extracts_exact_nextauth_chunks_and_attestation() -> None:
    payload = {
        "access_token": "fixture",
        "cookies": [
            {
                "name": "__Secure-next-auth.session-token.1",
                "value": "chunk-one",
            },
            {
                "name": "__Secure-next-auth.session-token.0",
                "value": "chunk-zero",
            },
            {"name": "unrelated", "value": "ignored"},
        ],
        "oai-web-deployment-attestation": "a" * 291,
    }
    assert auth.extract_nextauth_session_cookies(payload) == (
        ("__Secure-next-auth.session-token.0", "chunk-zero"),
        ("__Secure-next-auth.session-token.1", "chunk-one"),
    )
    assert auth.extract_deployment_attestation(payload) == "a" * 291


def test_playwright_cookie_import_preserves_chunks_and_overrides_device() -> None:
    cookies = gopay_sentinel_playwright._cookies_from_header(
        "oai-did=stale; __cf_bm=cloudflare",
        "device-fixture",
        (
            ("__Secure-next-auth.session-token.0", "chunk-zero"),
            ("__Secure-next-auth.session-token.1", "chunk-one"),
        ),
    )
    by_name = {item["name"]: item for item in cookies}
    assert by_name["oai-did"]["value"] == "device-fixture"
    assert by_name["__cf_bm"]["value"] == "cloudflare"
    assert by_name["__Secure-next-auth.session-token.0"]["httpOnly"] is True
    assert by_name["__Secure-next-auth.session-token.1"]["value"] == "chunk-one"


def test_device_profile_is_stable_across_refreshed_at_for_same_account(
    monkeypatch,
) -> None:
    class Session:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}
            self.proxies: dict[str, str] = {}

    monkeypatch.setattr(gopay_transport, "new_session", Session)

    def device(token: str) -> str:
        config = ExtractionConfig(
            access_token=token,
            checkout_proxy="http://proxy.example:8080",
            update_proxy="http://proxy.example:8080",
            country="ID",
            payment_method="gopay",
        )
        return gopay_transport.GoPayTransportFactory().chatgpt(
            config, config.checkout_proxy
        ).headers["oai-device-id"]

    first = _token("account-same", "user-same", "signature-one")
    refreshed = _token("account-same", "user-same", "signature-two")
    other = _token("account-other", "user-other", "signature-three")
    assert auth.account_user_id(first) == "user-same"
    assert device(first) == device(refreshed)
    assert device(first) != device(other)


def test_checkout_failure_context_contains_only_shape() -> None:
    secret = "private-checkout-identifier"
    response = SimpleNamespace(
        text=json.dumps({"detail": secret}),
        json=lambda: {"detail": secret},
    )
    context = gopay_checkout.checkout_failure_safe_context(response)
    assert context["response_keys"] == ["detail"]
    assert context["response_length"] > 0
    assert len(context["response_sha256"]) == 64
    assert secret not in json.dumps(context)
