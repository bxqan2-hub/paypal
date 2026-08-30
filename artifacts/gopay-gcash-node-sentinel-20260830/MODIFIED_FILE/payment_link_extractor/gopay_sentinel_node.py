from __future__ import annotations

"""GoPay Sentinel adapter backed by the byte-identical GCash Node/V8 bridge.

The upstream bridge intentionally starts a fresh Node process for every token.
It runs the bundled real SDK inside a browser shim, performs the outer PoW/token
assembly in Node, and does not retain a browser runtime between flows.
"""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
from typing import Any

from .config import DEFAULT_USER_AGENT


_BRIDGE_VERSION = "20260219f9f6"
_ASSET_ROOT = Path(__file__).resolve().parent / "gopay_sentinel_node_assets"
_SDK_PATH = _ASSET_ROOT / "sentinel_assets" / "sentinel_sdk.js"
_BRIDGE_PATH = _ASSET_ROOT / "sentinel_bridge.js"


def _resolve_node_executable() -> str:
    candidates = (os.environ.get("SENTINEL_NODE", "").strip(), "node", "nodejs")
    for candidate in candidates:
        if not candidate:
            continue
        executable = shutil.which(candidate)
        if executable:
            return executable
    raise RuntimeError("GoPay Node Sentinel requires Node.js")


def mint_gopay_sentinel_sync(
    *,
    flow: str,
    device_id: str,
    user_agent: str,
    proxy: str = "",
    cores: int = 16,
    page_url: str = "https://chatgpt.com/",
    language: str = "id-ID",
    timezone: str = "Asia/Jakarta",
    cookie_header: str = "",
    timeout_s: float = 120.0,
    diagnostics: dict[str, object] | None = None,
) -> tuple[str, str]:
    """Run the GoPay-owned copy of the one-process Node/V8 SDK bridge."""
    payload = json.dumps(
        {
            "ua": user_agent,
            "cores": cores,
            "deviceId": device_id,
            "flow": flow,
            "proxy": proxy,
            "version": _BRIDGE_VERSION,
            "pageUrl": page_url,
            "language": language,
            "timezone": timezone,
            "cookieHeader": cookie_header,
            "sentinelOrigin": "https://chatgpt.com",
        },
        separators=(",", ":"),
    ).encode()
    try:
        process = subprocess.run(
            [_resolve_node_executable(), str(_BRIDGE_PATH)],
            input=payload,
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("GoPay Node Sentinel requires Node.js") from exc
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(
            f"GoPay Node Sentinel timed out after {timeout_s:.0f}s"
        ) from exc

    output = (process.stdout or b"").decode("utf-8", "replace").strip()
    if not output:
        raise RuntimeError(
            "GoPay Node Sentinel bridge returned no output "
            f"(exit={process.returncode}, stderr_bytes={len(process.stderr or b'')})"
        )
    try:
        result = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "GoPay Node Sentinel bridge returned invalid JSON "
            f"(exit={process.returncode}, stdout_bytes={len(process.stdout or b'')})"
        ) from exc
    if result.get("error"):
        raise RuntimeError("GoPay Node Sentinel bridge reported an SDK error")
    if process.returncode:
        raise RuntimeError(
            f"GoPay Node Sentinel bridge exited unexpectedly (exit={process.returncode})"
        )
    if diagnostics is not None:
        diagnostics.update(
            ping_status=int(result.get("pingStatus") or 0),
            ping_ms=int(result.get("pingMs") or 0),
            ping_error=str(result.get("pingError") or "")[:200],
            pow_required=bool(result.get("powReq")),
            has_t=bool(result.get("hasT")),
            has_so=bool(result.get("hasSo")),
            so_error_present=bool(result.get("soErr")),
        )
    main = str(result.get("main") or "")
    if not main:
        raise RuntimeError("GoPay Node Sentinel bridge returned no main token")
    try:
        token_payload = json.loads(main)
    except json.JSONDecodeError as exc:
        raise RuntimeError("GoPay Node Sentinel main token is invalid JSON") from exc
    if (
        token_payload.get("id") != device_id
        or token_payload.get("flow") != flow
        or not token_payload.get("c")
        or not token_payload.get("p")
    ):
        raise RuntimeError("GoPay Node Sentinel token binding mismatch")
    observer = str(result.get("so") or "")
    if observer:
        try:
            observer_payload = json.loads(observer)
        except json.JSONDecodeError as exc:
            raise RuntimeError("GoPay Node Sentinel SO token is invalid JSON") from exc
        if (
            observer_payload.get("id") != device_id
            or observer_payload.get("flow") != flow
            or not observer_payload.get("c")
        ):
            raise RuntimeError("GoPay Node Sentinel SO token binding mismatch")
    return main, observer


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _session_cookie_header(session: Any) -> str:
    merged: dict[str, str] = {}
    headers = getattr(session, "headers", None)
    if headers is not None:
        try:
            value = str(headers.get("Cookie") or headers.get("cookie") or "").strip()
        except Exception:
            value = ""
        if value:
            for item in value.split(";"):
                name, separator, selected = item.strip().partition("=")
                if separator and name:
                    merged[name] = selected
    jar = getattr(session, "cookies", None)
    get_dict = getattr(jar, "get_dict", None)
    if callable(get_dict):
        try:
            values = get_dict()
        except Exception:
            values = {}
        if isinstance(values, dict):
            for name, value in values.items():
                if str(name).strip():
                    # The session jar reflects response Set-Cookie updates and
                    # therefore wins over a stale copied Header value.
                    merged[str(name)] = str(value)
    return "; ".join(f"{name}={value}" for name, value in merged.items())


def _set_session_cookie(session: Any, name: str, value: str) -> None:
    selected_name = str(name or "").strip()
    selected_value = str(value or "").strip()
    if not selected_name or not selected_value:
        return
    jar = getattr(session, "cookies", None)
    setter = getattr(jar, "set", None)
    if callable(setter):
        try:
            setter(selected_name, selected_value, domain=".chatgpt.com", path="/")
        except Exception:
            try:
                setter(selected_name, selected_value)
            except Exception:
                pass
    headers = getattr(session, "headers", None)
    if headers is None:
        return
    current = _session_cookie_header(session)
    parts: list[tuple[str, str]] = []
    replaced = False
    for item in current.split(";"):
        key, separator, existing = item.strip().partition("=")
        if not separator or not key:
            continue
        if key == selected_name:
            parts.append((selected_name, selected_value))
            replaced = True
        else:
            parts.append((key, existing))
    if not replaced:
        parts.append((selected_name, selected_value))
    headers["Cookie"] = "; ".join(f"{key}={item}" for key, item in parts)


class GoPayNodeSentinelProvider:
    """Mint each GoPay proof in a new GCash-compatible Node/V8 process."""

    requires_browser_session = False
    send_observer_token = True
    allow_environment_attestation = False
    allow_environment_observer_token = False
    proof_mode = "gopay_node_shim"
    process_model = "one_process_per_token"

    def __init__(
        self,
        *,
        access_token: str,
        device_id: str,
        session_id: str,
        user_agent: str,
        proxy: str,
        transport_session: Any,
        session_token: str = "",
        language: str = "id-ID",
        timezone: str = "Asia/Jakarta",
        log: Any | None = None,
    ) -> None:
        self.access_token = str(access_token or "").strip()
        self.device_id = str(device_id or "").strip()
        self.session_id = str(session_id or "").strip()
        self.user_agent = str(user_agent or DEFAULT_USER_AGENT).strip() or DEFAULT_USER_AGENT
        self.proxy = str(proxy or "").strip()
        self.transport_session = transport_session
        self.session_token = str(session_token or "").strip()
        self.language = str(language or "id-ID").strip() or "id-ID"
        self.timezone = str(timezone or "Asia/Jakarta").strip() or "Asia/Jakarta"
        self.log = log
        self._lock = threading.RLock()
        self._closed = False
        self._prepared_flow = ""
        self._prepared_referer = ""
        self._node_process_count = 0
        self._last_ping_status = 0
        self._last_ping_ms = 0
        self._last_ping_error_present = False
        self._last_token_length = 0
        self._last_so_token_length = 0
        self._challenge_shapes: list[dict[str, Any]] = []
        self._sdk_sha256 = _sha256_file(_SDK_PATH)
        self._bridge_sha256 = _sha256_file(_BRIDGE_PATH)
        self._browser_channel = "node-v8-shim"
        self._browser_version = ""
        self._runtime_id = ""
        self._profile_path = ""
        if self.session_token:
            _set_session_cookie(
                self.transport_session,
                "__Secure-next-auth.session-token",
                self.session_token,
            )
        _set_session_cookie(self.transport_session, "oai-did", self.device_id)

    @property
    def enabled(self) -> bool:
        return _SDK_PATH.is_file() and _BRIDGE_PATH.is_file()

    @staticmethod
    def _normalize_flow(flow: str) -> str:
        selected = str(flow or "").strip()
        if selected.lower() in {"", "default", "__default__"}:
            return "chatgpt_checkout"
        return selected

    def prepare(self) -> None:
        if self._closed:
            raise RuntimeError("GCash Node Sentinel provider is closed")

    def prepare_flow(self, *, flow: str, referer: str) -> None:
        """Record phase metadata without creating a persistent runtime."""
        with self._lock:
            self.prepare()
            self._prepared_flow = self._normalize_flow(flow)
            self._prepared_referer = str(referer or "https://chatgpt.com/")

    def headers(self, flow: str, *, referer: str = "") -> dict[str, str]:
        with self._lock:
            self.prepare()
            _set_session_cookie(self.transport_session, "oai-did", self.device_id)
            cookie_header = _session_cookie_header(self.transport_session)
            cookie_device = ""
            for item in cookie_header.split(";"):
                name, separator, value = item.strip().partition("=")
                if separator and name == "oai-did":
                    cookie_device = value
                    break
            if cookie_device != self.device_id:
                raise RuntimeError("GoPay Node Sentinel oai-did binding mismatch")
            proxies = getattr(self.transport_session, "proxies", None)
            if isinstance(proxies, dict):
                active_proxy = str(
                    proxies.get("https") or proxies.get("http") or ""
                ).strip()
                if active_proxy and active_proxy != self.proxy:
                    raise RuntimeError("GoPay Node Sentinel proxy binding mismatch")
            selected_flow = self._normalize_flow(flow)
            selected_referer = str(
                referer
                or (
                    self._prepared_referer
                    if self._prepared_flow == selected_flow
                    else ""
                )
                or "https://chatgpt.com/"
            )
            diagnostics: dict[str, object] = {}
            main, observer = mint_gopay_sentinel_sync(
                flow=selected_flow,
                device_id=self.device_id,
                user_agent=self.user_agent,
                proxy=self.proxy,
                page_url=selected_referer,
                language=self.language,
                timezone=self.timezone,
                cookie_header=cookie_header,
                timeout_s=120,
                diagnostics=diagnostics,
            )
            # The upstream wrapper already validates id/flow/c. Keep a local
            # assertion so provider failures remain explicit at this boundary.
            try:
                payload = json.loads(main)
            except (TypeError, ValueError) as exc:
                raise RuntimeError("GCash Node Sentinel returned invalid main token") from exc
            if (
                payload.get("id") != self.device_id
                or payload.get("flow") != selected_flow
                or not payload.get("c")
            ):
                raise RuntimeError("GCash Node Sentinel token binding mismatch")
            self._node_process_count += 1
            self._last_ping_status = int(diagnostics.get("ping_status") or 0)
            self._last_ping_ms = int(diagnostics.get("ping_ms") or 0)
            self._last_ping_error_present = bool(diagnostics.get("ping_error"))
            self._last_token_length = len(main)
            self._last_so_token_length = len(observer)
            self._challenge_shapes.append(
                {
                    "flow": selected_flow,
                    "token_length": len(main),
                    "so_token_length": len(observer),
                    "pow_present": bool(payload.get("p")),
                    "ping_status": self._last_ping_status,
                    "ping_error_present": self._last_ping_error_present,
                    "pow_required": bool(diagnostics.get("pow_required")),
                    "has_t": bool(diagnostics.get("has_t")),
                    "has_so": bool(diagnostics.get("has_so")),
                    "so_error_present": bool(
                        diagnostics.get("so_error_present")
                    ),
                }
            )
            telemetry_field = (
                "openai_checkout_telemetry"
                if selected_flow == "chatgpt_checkout"
                else "openai_approve_telemetry"
            )
            setattr(self.transport_session, telemetry_field, "[1,null]")
            result = {
                "OpenAI-Sentinel-Token": main,
                "oai-device-id": self.device_id,
            }
            if observer:
                result["OpenAI-Sentinel-SO-Token"] = observer
            return result

    def validate_checkout_readiness(self, headers: dict[str, str]) -> dict[str, Any]:
        """Validate the explicit Node/process/device/proxy checkout boundary."""
        token = str(headers.get("OpenAI-Sentinel-Token") or "").strip()
        if not token or self._node_process_count <= 0:
            raise RuntimeError("GoPay Node Sentinel checkout proof is incomplete")
        try:
            payload = json.loads(token)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("GoPay Node Sentinel checkout proof is invalid") from exc
        if (
            payload.get("id") != self.device_id
            or payload.get("flow") != "chatgpt_checkout"
            or not payload.get("c")
            or not payload.get("p")
        ):
            raise RuntimeError("GoPay Node Sentinel checkout proof binding mismatch")
        return {
            "proof_mode": self.proof_mode,
            "node_process_count": self._node_process_count,
            "token_length": len(token),
            "observer_token_length": len(
                str(headers.get("OpenAI-Sentinel-SO-Token") or "")
            ),
        }

    def set_cookie(self, name: str, value: str, *, http_only: bool = False) -> None:
        del http_only
        with self._lock:
            self.prepare()
            _set_session_cookie(self.transport_session, name, value)

    def close(self) -> None:
        with self._lock:
            self._closed = True
