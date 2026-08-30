from __future__ import annotations

import json
import hashlib
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from payment_link_extractor import gopay_checkout
from payment_link_extractor import gopay_sentinel_node
from payment_link_extractor import gopay_transport
from payment_link_extractor.gopay_sentinel_node import GoPayNodeSentinelProvider
from payment_link_extractor.models import ExtractionConfig


ROOT = Path(__file__).resolve().parents[1]


def _main_token(device_id: str, flow: str) -> str:
    return json.dumps(
        {
            "p": "pow-fixture",
            "t": "sdk-fixture",
            "c": "challenge-fixture",
            "id": device_id,
            "flow": flow,
        },
        separators=(",", ":"),
    )


def test_node_bridge_is_gopay_owned_and_matches_reference_assets() -> None:
    source = (
        ROOT / "payment_link_extractor/gopay_sentinel_node.py"
    ).read_text(encoding="utf-8")
    assert "mk_gcash_open_source" not in source
    expected = {
        "sentinel_bridge.js": "ea943dd28c0158c289c7aad6446c034290098c8f12d2f2569fce9432019dd357",
        "sentinel_assets/sentinel_bootstrap.js": "9ffa5bac4236da35bf2da0063f56aea500a6a23e78350b3ac085a1572345fced",
        "sentinel_assets/sentinel_sdk.js": "69b60c5f0f6212100ca760d8c0ef478f089f039b2ec489200b35d794243e90a8",
    }
    root = ROOT / "payment_link_extractor/gopay_sentinel_node_assets"
    for relative, digest in expected.items():
        assert hashlib.sha256((root / relative).read_bytes()).hexdigest() == digest


def test_node_wrapper_starts_a_new_subprocess_for_each_call(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_run(command, **kwargs):
        payload = json.loads(kwargs["input"])
        calls.append({"command": command, "payload": payload, **kwargs})
        output = {
            "main": _main_token(payload["deviceId"], payload["flow"]),
            "so": json.dumps(
                {
                    "so": "observer-fixture",
                    "c": "challenge-fixture",
                    "id": payload["deviceId"],
                    "flow": payload["flow"],
                },
                separators=(",", ":"),
            ),
            "pingStatus": 200,
            "pingMs": 19,
            "pingError": "",
            "powReq": True,
            "hasT": True,
            "hasSo": True,
            "soErr": "",
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(output).encode(), b"")

    monkeypatch.setattr(gopay_sentinel_node, "_resolve_node_executable", lambda: "node-fixture")
    monkeypatch.setattr(gopay_sentinel_node.subprocess, "run", fake_run)
    diagnostics: dict[str, object] = {}
    for flow in ("chatgpt_checkout", "checkout_session_approval"):
        main, observer = gopay_sentinel_node.mint_gopay_sentinel_sync(
            flow=flow,
            device_id="device-fixture",
            user_agent="agent-fixture",
            proxy="http://proxy.example:8080",
            page_url="https://chatgpt.com/checkout/fixture",
            language="id-ID",
            timezone="Asia/Jakarta",
            cookie_header="oai-did=device-fixture",
            diagnostics=diagnostics,
        )
        assert json.loads(main)["flow"] == flow
        assert json.loads(observer)["flow"] == flow

    assert len(calls) == 2
    assert all(item["command"][0] == "node-fixture" for item in calls)
    assert all(item["check"] is False for item in calls)
    assert all(item["capture_output"] is True for item in calls)
    assert [item["payload"]["flow"] for item in calls] == [
        "chatgpt_checkout",
        "checkout_session_approval",
    ]
    assert diagnostics == {
        "ping_status": 200,
        "ping_ms": 19,
        "ping_error": "",
        "pow_required": True,
        "has_t": True,
        "has_so": True,
        "so_error_present": False,
    }


def test_node_provider_mints_one_fresh_process_per_token(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_mint(**kwargs):
        calls.append(dict(kwargs))
        kwargs["diagnostics"].update(
            ping_status=200,
            ping_ms=37,
            ping_error="",
        )
        return _main_token(str(kwargs["device_id"]), str(kwargs["flow"])), "so-fixture"

    monkeypatch.setattr(
        gopay_sentinel_node, "mint_gopay_sentinel_sync", fake_mint
    )
    session = SimpleNamespace(
        headers={"Cookie": "oai-did=device-fixture; session-cookie=value"}
    )
    provider = GoPayNodeSentinelProvider(
        access_token="fixture-token",
        device_id="device-fixture",
        session_id="session-fixture",
        user_agent="fixture-agent",
        proxy="http://proxy.example:8080",
        transport_session=session,
        language="id-ID",
        timezone="Asia/Jakarta",
    )
    provider.prepare_flow(
        flow="checkout_session_approval",
        referer="https://chatgpt.com/checkout/openai_ie/cs_fixture",
    )
    checkout = provider.headers(
        "chatgpt_checkout",
        referer="https://chatgpt.com/?promo_campaign=plus-1-month-free",
    )
    approval = provider.headers(
        "checkout_session_approval",
        referer="https://chatgpt.com/checkout/openai_ie/cs_fixture",
    )

    assert len(calls) == 2
    assert [item["flow"] for item in calls] == [
        "chatgpt_checkout",
        "checkout_session_approval",
    ]
    assert all(item["device_id"] == "device-fixture" for item in calls)
    assert all(item["proxy"] == "http://proxy.example:8080" for item in calls)
    assert all(item["language"] == "id-ID" for item in calls)
    assert all(item["timezone"] == "Asia/Jakarta" for item in calls)
    assert all("session-cookie=value" in str(item["cookie_header"]) for item in calls)
    assert checkout["OpenAI-Sentinel-SO-Token"] == "so-fixture"
    assert approval["OpenAI-Sentinel-SO-Token"] == "so-fixture"
    assert provider._node_process_count == 2
    assert provider._runtime_id == ""
    assert provider.process_model == "one_process_per_token"
    assert session.openai_checkout_telemetry == "[1,null]"
    assert session.openai_approve_telemetry == "[1,null]"


def test_node_provider_merges_cookie_header_and_fresher_cookie_jar() -> None:
    class CookieJar:
        def get_dict(self):
            return {
                "oai-did": "jar-device",
                "__Secure-next-auth.session-token.0": "chunk-zero",
                "__Secure-next-auth.session-token.1": "chunk-one",
            }

    session = SimpleNamespace(
        headers={"Cookie": "oai-did=stale-device; extra=header-value"},
        cookies=CookieJar(),
    )
    merged = gopay_sentinel_node._session_cookie_header(session)
    assert "oai-did=jar-device" in merged
    assert "oai-did=stale-device" not in merged
    assert "extra=header-value" in merged
    assert "__Secure-next-auth.session-token.0=chunk-zero" in merged
    assert "__Secure-next-auth.session-token.1=chunk-one" in merged


def test_node_mode_ignores_stale_environment_attestation(monkeypatch) -> None:
    class Provider:
        allow_environment_attestation = False
        allow_environment_observer_token = False

        def headers(self, _flow, **_kwargs):
            return {"OpenAI-Sentinel-Token": "main-fixture"}

    monkeypatch.setenv("OPLL_OAI_WEB_DEPLOYMENT_ATTESTATION", "stale-attestation")
    monkeypatch.setenv("OPLL_OPENAI_SENTINEL_SO_TOKEN", "stale-observer")
    session = SimpleNamespace(
        openai_sentinel_provider=Provider(),
        headers={},
    )
    result = gopay_transport.openai_sentinel_headers(
        session,
        flow="chatgpt_checkout",
        referer="https://chatgpt.com/",
        required=True,
    )
    assert "oai-web-deployment-attestation" not in result
    assert "OpenAI-Sentinel-SO-Token" not in result


def test_node_wrapper_error_does_not_echo_subprocess_output(monkeypatch) -> None:
    secret = "private-token-cookie-proxy-secret"
    process = subprocess.CompletedProcess(
        ["node-fixture"],
        1,
        ("not-json-" + secret).encode(),
        ("stderr-" + secret).encode(),
    )
    monkeypatch.setattr(gopay_sentinel_node, "_resolve_node_executable", lambda: "node-fixture")
    monkeypatch.setattr(
        gopay_sentinel_node.subprocess,
        "run",
        lambda *_args, **_kwargs: process,
    )
    with pytest.raises(RuntimeError) as caught:
        gopay_sentinel_node.mint_gopay_sentinel_sync(
            flow="chatgpt_checkout",
            device_id="device-fixture",
            user_agent="agent-fixture",
        )
    assert secret not in str(caught.value)


def test_node_provider_allows_checkout_without_persistent_browser_material(
    monkeypatch,
) -> None:
    class Response:
        status_code = 200
        text = '{"checkout_session_id":"cs_fixture"}'
        headers: dict[str, str] = {}

        def json(self):
            return {"checkout_session_id": "cs_fixture"}

    captured: dict[str, object] = {}
    session = SimpleNamespace(headers={})
    provider = SimpleNamespace(
        requires_browser_session=False,
        send_observer_token=True,
        proof_mode="gopay_node_shim",
        _node_process_count=1,
        validate_checkout_readiness=lambda _headers: {
            "proof_mode": "gopay_node_shim"
        },
        headers=lambda *_args, **_kwargs: {
            "OpenAI-Sentinel-Token": _main_token(
                "device-fixture", "chatgpt_checkout"
            ),
            "OpenAI-Sentinel-SO-Token": "so-fixture",
            "oai-device-id": "device-fixture",
        },
    )
    session.openai_sentinel_provider = provider

    def fake_request(*_args, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(gopay_checkout, "stage_http_request", fake_request)
    config = ExtractionConfig(
        access_token="fixture-token",
        checkout_proxy="http://proxy.example:8080",
        update_proxy="http://proxy.example:8080",
        country="ID",
        payment_method="gopay",
    )
    committed: list[bool] = []
    checkout = gopay_checkout.create_checkout(
        config,
        session,
        None,
        commit_callback=lambda: committed.append(True),
    )

    assert checkout["session_kind"] == "stripe_checkout"
    assert committed == [True]
    assert captured["headers"]["OpenAI-Sentinel-SO-Token"] == "so-fixture"
    assert "oai-web-deployment-attestation" not in captured["headers"]
