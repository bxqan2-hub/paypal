from __future__ import annotations

import importlib.util
import time
from pathlib import Path

import pytest


APP_PATH = Path(__file__).resolve().parents[1] / "app.py"
SPEC = importlib.util.spec_from_file_location("gopay_link_app", APP_PATH)
assert SPEC and SPEC.loader
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


def fake_token(label: str) -> str:
    return f"eyJ{label}{'a' * 110}.payload.signature"


def wait_for_terminal(runner: app.BatchRunner, timeout: float = 5) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        snapshot = runner.snapshot()
        if snapshot["state"] in {"completed", "failed", "stopped"}:
            return snapshot
        time.sleep(0.02)
    raise AssertionError(f"batch did not finish: {runner.snapshot()}")


def make_runner(tmp_path: Path, script_body: str) -> app.BatchRunner:
    script = tmp_path / "fake_batch.py"
    script.write_text(script_body, encoding="utf-8")
    return app.BatchRunner(
        data_dir=tmp_path / "data",
        project_root=tmp_path,
        batch_script=script,
    )


def valid_payload() -> dict:
    return {
        "tokens": f"{fake_token('one')}\n{fake_token('two')}\n{fake_token('three')}",
        "proxies": "http://user:password@127.0.0.1:8080\n",
        "proxy_scheme": "http",
        "concurrency": 2,
        "max_retry": 3,
        "poll_timeout": 20,
        "poll_interval_ms": 500,
        "start_interval": 0,
    }


def test_redirect_validation_is_strict() -> None:
    assert app.validate_redirect_url(
        "https://pm-redirects.stripe.com/authorize/acct_123/sa_nonce_456"
    )
    assert not app.validate_redirect_url(
        "https://app.midtrans.com/snap/v4/redirection/example"
    )
    assert not app.validate_redirect_url(
        "https://chatgpt.com/checkout/openai_llc/oaics_example"
    )


def test_normalize_result_rejects_oaics_and_non_stripe_links() -> None:
    oaics = app.normalize_batch_result(
        {
            "index": 1,
            "account": "account-one",
            "status": "success",
            "url": "https://chatgpt.com/checkout/openai_llc/oaics_example",
        }
    )
    midtrans = app.normalize_batch_result(
        {
            "index": 2,
            "account": "account-two",
            "status": "success",
            "url": "https://app.midtrans.com/snap/v4/redirection/example",
        }
    )
    assert oaics["status"] == "oaics_rejected"
    assert oaics["url"] == ""
    assert midtrans["status"] == "invalid_gopay_redirect"
    assert midtrans["url"] == ""


def test_normalize_result_preserves_account_email() -> None:
    result = app.normalize_batch_result(
        {
            "index": 1,
            "account": "user.name+gopay@example.com",
            "status": "failed",
            "detail": "failed",
        }
    )

    assert result["account"] == "user.name+gopay@example.com"


def test_redact_log_hides_tokens_and_proxy_credentials() -> None:
    jwt = "eyJ" + "a" * 30 + "." + "b" * 30 + "." + "c" * 30
    line = f"token={jwt} proxy=http://alice:secret@example.com:8080"
    redacted = app.redact_log(line)
    assert jwt not in redacted
    assert "alice" not in redacted
    assert "secret" not in redacted


def test_account_protocol_log_is_reduced_to_short_milestone() -> None:
    item = app.simplify_account_log(
        '[2026-08-25 10:31:17] stripe.init: '
        '{"amount":0,"currency":"idr","methods":["card","gopay"]}'
    )
    assert item == {
        "stage": "gopay",
        "level": "success",
        "message": "0 元 GoPay 已确认",
    }
    assert app.simplify_account_log(
        '[2026-08-25 10:31:14] bootstrap.context: {"status":200}'
    ) is None


def test_batch_parses_results_and_removes_inputs(tmp_path: Path) -> None:
    runner = make_runner(
        tmp_path,
        """
import json
print('[ACCOUNT_START] ' + json.dumps({"index": 1, "account": "user.name+gopay@example.com"}), flush=True)
print('[ACCOUNT_LOG] ' + json.dumps({"index": 1, "account": "user.name+gopay@example.com", "line": '[2026-08-25 10:31:17] stripe.init: {"amount":0,"currency":"idr","methods":["card","gopay"]}'}), flush=True)
rows = [
    {"index": 1, "account": "user.name+gopay@example.com", "status": "success", "url": "https://pm-redirects.stripe.com/authorize/acct_test/sa_nonce_test", "detail": "ok", "exit_code": 0},
    {"index": 2, "account": "acct-two", "status": "success", "url": "https://chatgpt.com/checkout/openai_llc/oaics_test", "detail": "bad", "exit_code": 0},
    {"index": 3, "account": "acct-three", "status": "success", "url": "https://app.midtrans.com/snap/v4/redirection/test", "detail": "bad", "exit_code": 0},
]
for row in rows:
    print("[BATCH_RESULT] " + json.dumps(row), flush=True)
""",
    )
    started = runner.start(valid_payload())
    assert started["state"] == "running"
    assert started["config"]["effective_concurrency"] == 2
    assert "tokens" not in started
    assert "proxies" not in started

    finished = wait_for_terminal(runner)
    assert finished["state"] == "completed"
    assert finished["summary"] == {
        "total": 3,
        "completed": 3,
        "success": 1,
        "failed": 2,
        "stopped": 0,
    }
    assert [item["status"] for item in finished["results"]] == [
        "success",
        "oaics_rejected",
        "invalid_gopay_redirect",
    ]
    assert finished["results"][1]["url"] == ""
    assert finished["results"][2]["url"] == ""
    assert finished["results"][0]["account"] == "user.name+gopay@example.com"
    account_snapshot = runner.account_log_snapshot(1)
    assert account_snapshot["account"] == "user.name+gopay@example.com"
    account_logs = account_snapshot["logs"]
    assert [item["message"] for item in account_logs] == [
        "账号任务已启动",
        "0 元 GoPay 已确认",
        "GoPay 授权链接已生成",
    ]
    assert all("{" not in item["message"] for item in account_logs)
    messages = "\n".join(item["message"] for item in finished["logs"])
    assert "oaics_test" not in messages
    assert "app.midtrans.com" not in messages

    runtime_dir = tmp_path / "data" / "batches" / finished["task_id"]
    assert not list(runtime_dir.rglob("batch_tokens.txt"))
    assert not list(runtime_dir.rglob("proxy_seeds.txt"))

    csv_text = runner.csv_bytes().decode("utf-8-sig")
    assert "user.name+gopay@example.com" in csv_text
    assert "pm-redirects.stripe.com/authorize" in csv_text
    assert "oaics_test" not in csv_text
    assert "app.midtrans.com" not in csv_text


def test_stop_terminates_entire_batch(tmp_path: Path) -> None:
    runner = make_runner(
        tmp_path,
        """
import time
print('batch waiting', flush=True)
time.sleep(30)
""",
    )
    runner.start(valid_payload())
    runner.stop()
    finished = wait_for_terminal(runner)
    assert finished["state"] == "stopped"
    assert finished["error"] == "批量任务已停止"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("tokens", "short", "未识别到有效 Access Token"),
        ("proxies", "", "请填写至少一条代理"),
        ("concurrency", 501, "concurrency 必须在 0-500 之间"),
        ("max_retry", 0, "max_retry 必须在 1-100 之间"),
        ("poll_timeout", 601, "poll_timeout 必须在 5-600 之间"),
        ("start_interval", 61, "start_interval 必须在 0-60 之间"),
        ("proxy_scheme", "ftp", "proxy_scheme 必须是"),
    ],
)
def test_invalid_payload_is_rejected(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    runner = make_runner(tmp_path, "print('unused')")
    payload = valid_payload()
    payload[field] = value
    with pytest.raises(app.RequestError, match=message):
        runner.start(payload)
