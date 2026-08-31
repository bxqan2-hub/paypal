from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "gopay_live_canary", ROOT / "tools/gopay_live_canary.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_canary_loaders_keep_token_and_proxy_slots_private(tmp_path: Path) -> None:
    tokens = tmp_path / "tokens.txt"
    tokens.write_text("eyJone.payload.signature\neyJtwo.payload.signature\n", encoding="utf-8")
    assert MODULE.load_tokens(tokens) == [
        "eyJone.payload.signature",
        "eyJtwo.payload.signature",
    ]

    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(
        json.dumps(
            {
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "id.example.com:8080:user:password",
                        }
                    ],
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    proxies = MODULE.load_rollout_proxies(rollout)
    assert proxies == ["http://user:password@id.example.com:8080"]


def test_canary_provider_shape_outputs_no_order_identifier() -> None:
    shape = MODULE._provider_shape(
        "https://app.midtrans.com/snap/v4/redirection/private-order-id"
    )
    assert shape == {
        "present": True,
        "host": "app.midtrans.com",
        "path_prefix": "/snap/v4",
    }
    assert "private-order-id" not in json.dumps(shape)


def test_canary_loader_removes_repeated_markdown_escapes(tmp_path: Path) -> None:
    tokens = tmp_path / "escaped.txt"
    tokens.write_text(
        r"eyJheader.payload.sig\\\_part\\\_tail",
        encoding="utf-8",
    )
    assert MODULE.load_tokens(tokens) == ["eyJheader.payload.sig_part_tail"]


def test_canary_loader_parses_jsonl_before_matching_token(tmp_path: Path) -> None:
    rollout = tmp_path / "runtime.jsonl"
    rollout.write_text(
        json.dumps(
            {
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": r"eyJheader.payload.signature\\\_tail"
                            + "\nnext line",
                        }
                    ],
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert MODULE.load_tokens(rollout) == [
        "eyJheader.payload.signature_tail"
    ]


def test_canary_loads_runtime_browser_state_without_exposing_values(
    tmp_path: Path,
) -> None:
    state = tmp_path / "browser-state.json"
    state.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "__Secure-next-auth.session-token.0",
                        "value": "chunk-zero",
                    },
                    {
                        "name": "__Secure-next-auth.session-token.1",
                        "value": "chunk-one",
                    },
                    {"name": "oai-did", "value": "device-fixture"},
                    {"name": "_account", "value": "account-fixture"},
                    {"name": "__stripe_mid", "value": "mid-fixture"},
                    {"name": "__stripe_sid", "value": "sid-fixture"},
                    {
                        "name": "__Secure-next-auth.session-token.bad",
                        "value": "must-be-ignored",
                    },
                    {"name": "__cf_bm", "value": "must-be-ignored"},
                ],
                "oai-web-deployment-attestation": "a" * 291,
            }
        ),
        encoding="utf-8",
    )
    cookies, attestation = MODULE.load_browser_state(state)
    assert cookies == (
        ("__Secure-next-auth.session-token.0", "chunk-zero"),
        ("__Secure-next-auth.session-token.1", "chunk-one"),
        ("__stripe_mid", "mid-fixture"),
        ("__stripe_sid", "sid-fixture"),
        ("_account", "account-fixture"),
        ("oai-did", "device-fixture"),
    )
    assert attestation == "a" * 291


def test_canary_accepts_attestation_only_runtime_state(tmp_path: Path) -> None:
    state = tmp_path / "attestation-only.json"
    state.write_text(
        json.dumps(
            {
                "cookies": [],
                "oai-web-deployment-attestation": "a" * 291,
            }
        ),
        encoding="utf-8",
    )
    cookies, attestation = MODULE.load_browser_state(state)
    assert cookies == ()
    assert attestation == "a" * 291


def test_canary_classifies_mismatched_browser_account() -> None:
    error = RuntimeError(
        "GoPay browser session account does not match the access token"
    )
    error.failure_mode = "browser_session_account_mismatch"
    result = MODULE._safe_error(error)
    assert result["category"] == "browser_session_account_mismatch"
    assert result["failure_mode"] == "browser_session_account_mismatch"


def test_canary_classifies_browser_binding_outage() -> None:
    error = RuntimeError(
        "GoPay browser session account verification is unavailable"
    )
    error.failure_mode = "browser_session_binding_unavailable"
    result = MODULE._safe_error(error)
    assert result["category"] == "browser_session_binding_unavailable"
