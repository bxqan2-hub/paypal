import base64
import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

import app
import payment_monitor


def jwt(payload):
    def encode(value):
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{encode({'alg': 'none', 'typ': 'JWT'})}.{encode(payload)}.signature"


class StandaloneAppTests(unittest.TestCase):
    def test_account_parser_accepts_text_and_obeys_deployment_limit(self):
        token = jwt({
            "exp": int(time.time()) + 3600,
            "https://api.openai.com/profile": {"email": "claim@example.com"},
            "https://api.openai.com/auth": {"chatgpt_account_id": "account-private"},
        })
        second_token = jwt({
            "exp": int(time.time()) + 3600,
            "email": "second@example.com",
        })
        accounts, warnings = app.parse_accounts({
            "tokens": f"display@example.com|{token}\n{second_token}",
        })

        self.assertEqual(2, len(accounts))
        self.assertEqual("display@example.com", accounts[0]["email"])
        self.assertEqual("account-private", accounts[0]["account_id"])
        self.assertEqual(token, accounts[0]["token"])
        self.assertEqual([], warnings)

        with patch.object(app, "MAX_ACCOUNTS", 1):
            with self.assertRaisesRegex(ValueError, "最多提交 1 个账号"):
                app.parse_accounts({"tokens": f"{token}\n{second_token}"})

    def test_account_parser_rejects_expired_and_invalid_inputs(self):
        expired = jwt({"exp": int(time.time()) - 1})
        with self.assertRaisesRegex(ValueError, "AT 已过期"):
            app.parse_accounts({"tokens": expired})
        with self.assertRaisesRegex(ValueError, "未找到 JWT AT"):
            app.parse_accounts({"tokens": "not-a-token"})

    def test_proxy_pool_is_custom_only_and_supports_documented_formats(self):
        pool = app.parse_proxy_pool([
            "proxy.example:8080",
            "proxy.example:8080",
            "http://user:pass@proxy.example:8081",
            "socks5://proxy.example:1080",
        ])
        self.assertEqual(3, len(pool))
        with self.assertRaisesRegex(ValueError, "SOCKS5"):
            app.parse_proxy_pool(["socks5://user:pass@proxy.example:1080"])
        with self.assertRaisesRegex(ValueError, "自备代理池"):
            app.create_job({"proxy_mode": "platform"})

        manager = app.GCashSessionManager(max_concurrency=1, max_queue=2)
        try:
            self.assertEqual(
                "proxy-2", manager._rotate_proxy("proxy-1", ["proxy-1", "proxy-2"])
            )
            self.assertEqual("", manager._rotate_proxy("proxy-1", []))
        finally:
            manager.executor.shutdown(wait=True)

    def test_fallback_qr_contains_a_valid_png_for_a_gcash_link(self):
        link = (
            "https://m.gcash.com/gcash-login-web/index.html"
            "?netAuthId=synthetic-test-value"
        )
        png = payment_monitor._fallback_qr_png(link)
        self.assertTrue(png.startswith(payment_monitor.PNG_SIGNATURE))
        self.assertGreater(len(png), 300)
        with self.assertRaisesRegex(RuntimeError, "非 GCash"):
            payment_monitor._fallback_qr_png("https://example.com/not-gcash")

    def test_public_task_omits_access_token_proxy_and_internal_ids(self):
        task = {
            "client_account_id": "acct_0123456789abcdef",
            "account_id": "account-private",
            "token": "server-bearer-private",
            "proxy": "proxy.example:8080:user:private",
            "proxy_pool": ["proxy.example:8080:user:private"],
            "monitor_id": "monitor-private",
            "status": "success",
            "current_step": "follow_redirect",
            "steps": [],
            "gcash_url": "https://m.gcash.com/gcash-login-web/index.html?netAuthId=public-link",
            "expires_at": int(time.time()) + 300,
            "callback_status": "waiting_scan",
            "attempt_history": [{"proxy": "proxy.example:8080:user:private"}],
        }
        meta = {
            "accounts": {
                "acct_0123456789abcdef": {
                    "email": "holder@example.com",
                    "name": "Holder",
                }
            }
        }
        with patch.object(app.CHAIN_MANAGER, "monitor_status", return_value={
            "status": "waiting_scan",
            "expires_at": int(time.time()) + 300,
            "gcash_url": task["gcash_url"],
            "qr_ready": True,
            "qr_source": "gcash_page",
            "qr_version": 2,
            "refresh_available": True,
        }):
            public = app._public_task("local_0123456789abcdef", task, meta)

        rendered = json.dumps(public)
        for private in (
            "server-bearer-private",
            "proxy.example",
            "user:private",
            "account-private",
            "monitor-private",
            "attempt_history",
        ):
            self.assertNotIn(private, rendered)
        self.assertTrue(public["link_ready"])
        self.assertTrue(public["qr_ready"])

    def test_abandon_account_releases_monitor_and_returns_public_state(self):
        client_id = "acct_0123456789abcdef"
        task = {
            "client_account_id": client_id,
            "monitor_id": "monitor-private",
            "status": "success",
            "current_step": "follow_redirect",
            "steps": [],
            "gcash_url": "https://m.gcash.com/gcash-login-web/index.html?netAuthId=test",
            "expires_at": int(time.time()) + 300,
        }
        meta = {"accounts": {client_id: {"email": "holder@example.com", "name": ""}}}
        with (
            patch.object(app, "_task", return_value=task),
            patch.object(app, "_job_meta", return_value=meta),
            patch.object(app.CHAIN_MANAGER, "monitor_status", side_effect=[
                {"status": "waiting_scan", "refresh_available": True},
                {"status": "abandoned", "refresh_available": False},
            ]),
            patch.object(app.CHAIN_MANAGER, "abandon_monitor") as abandon,
        ):
            public = app.abandon_account("local_0123456789abcdef", client_id)

        abandon.assert_called_once_with("monitor-private")
        self.assertEqual("abandoned", public["payment_status"])
        self.assertFalse(public["payment_success"])
        self.assertNotIn("monitor-private", json.dumps(public))

    def test_payment_monitor_abandon_cancels_future_and_clears_qr(self):
        manager = payment_monitor.PaymentMonitorManager()
        future = MagicMock()
        future.done.return_value = False
        closed = threading.Event()
        closed.set()
        manager._states["monitor-test"] = {
            "status": "waiting_scan",
            "qr_png": payment_monitor.PNG_SIGNATURE + b"test",
            "qr_source": "gcash_page",
            "refresh_available": True,
            "refresh_until": int(time.time()) + 60,
            "_monitor_future": future,
            "_closed": closed,
            "updated_at": int(time.time()),
        }

        result = manager.abandon("monitor-test", timeout=.1)

        future.cancel.assert_called_once_with()
        self.assertEqual("abandoned", result["status"])
        self.assertFalse(result["qr_ready"])
        self.assertFalse(result["refresh_available"])

    def test_local_http_surface_has_health_static_asset_and_security_headers(self):
        server = app.Server(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=5) as response:
                body = json.loads(response.read())
                self.assertTrue(body["ok"])
                self.assertEqual("DENY", response.headers["X-Frame-Options"])
                self.assertEqual("no-store", response.headers["Cache-Control"])
            with urllib.request.urlopen(base + "/assets/logo.svg", timeout=5) as response:
                self.assertEqual("image/svg+xml", response.headers.get_content_type())
            with urllib.request.urlopen(base + "/assets/mikael-mail-logo.webp", timeout=5) as response:
                self.assertEqual("image/webp", response.headers.get_content_type())

            with patch.object(app, "abandon_job", return_value={
                "ok": True, "abandoned": 1, "job": {"job_id": "local_0123456789abcdef"},
            }) as abandon:
                request = urllib.request.Request(
                    base + "/api/jobs/local_0123456789abcdef/abandon",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    body = json.loads(response.read())
                self.assertEqual(1, body["abandoned"])
                abandon.assert_called_once_with("local_0123456789abcdef")

            request = urllib.request.Request(
                base + "/api/jobs",
                data=json.dumps({"proxy_mode": "platform"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(400, caught.exception.code)
            self.assertIn("自备代理池", caught.exception.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
