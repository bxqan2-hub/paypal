from __future__ import annotations

import importlib.util
import io
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


def test_canary_runtime_input_keeps_secrets_in_memory() -> None:
    tokens, proxies = MODULE.load_runtime_input(
        io.StringIO(
            "eyJfixture.payload.signature\n"
            "2\n"
            "id.example.com:8080:user-a:password-a\n"
            "id.example.com:8080:user-b:password-b\n"
        )
    )
    assert tokens == ["eyJfixture.payload.signature"]
    assert proxies == [
        "http://user-a:password-a@id.example.com:8080",
        "http://user-b:password-b@id.example.com:8080",
    ]
