from __future__ import annotations

import importlib.util
import base64
import json
from pathlib import Path
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "gopay_live_canary", ROOT / "tools/gopay_live_canary.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _live_token(*, exp_offset: int = 3600, nbf_offset: int = -60) -> str:
    def part(value) -> str:
        return base64.urlsafe_b64encode(
            json.dumps(value, separators=(",", ":")).encode()
        ).decode().rstrip("=")

    now = int(time.time())
    header = part({"alg": "RS256", "typ": "JWT"})
    payload = part(
        {
            "exp": now + exp_offset,
            "nbf": now + nbf_offset,
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "account-fixture"
            },
        }
    )
    signature = base64.urlsafe_b64encode(b"s" * 256).decode().rstrip("=")
    return f"{header}.{payload}.{signature}"


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


def test_canary_loader_repairs_markdown_star_without_truncating_signature(
    tmp_path: Path,
) -> None:
    tokens = tmp_path / "star.txt"
    tokens.write_text(
        "eyJheader.payload.sig*part*tail",
        encoding="utf-8",
    )
    assert MODULE.load_tokens(tokens) == [
        "eyJheader.payload.sig_part_tail"
    ]


def test_canary_proxy_loader_accepts_runtime_json(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime.json"
    runtime.write_text(
        json.dumps({"proxies": ["id.example.com:8080:user:password"]}),
        encoding="utf-8",
    )
    assert MODULE.load_rollout_proxies(runtime) == [
        "http://user:password@id.example.com:8080"
    ]


def test_canary_strict_runtime_token_validation() -> None:
    metadata = MODULE.validate_runtime_token(_live_token())
    assert metadata["signature_bytes"] == 256
    with pytest.raises(ValueError):
        MODULE.validate_runtime_token(_live_token(exp_offset=-1))
    with pytest.raises(ValueError):
        MODULE.validate_runtime_token(_live_token(nbf_offset=3600))
    with pytest.raises(ValueError):
        MODULE.validate_runtime_token("eyJtruncated.payload.signature")
