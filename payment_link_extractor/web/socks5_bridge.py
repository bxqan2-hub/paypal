from __future__ import annotations

"""Local HTTP CONNECT bridges for authenticated SOCKS5 upstream proxies.

Playwright's proxy configuration used by the upstream GCash project cannot
carry SOCKS5 credentials.  This small bridge exposes an unauthenticated
loopback HTTP proxy while forwarding every CONNECT through the authenticated
SOCKS5 endpoint.  It is deliberately kept in the web/integration layer and
does not modify the vendored MK project.
"""

import atexit
import select
import socket
import threading
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit


def _parse_socks5(value: str) -> tuple[str, int, str, str]:
    normalized = str(value or "").strip().replace("\\@", "@")
    parsed = urlsplit(normalized)
    if parsed.scheme.lower() not in {"socks5", "socks5h"} or not parsed.hostname:
        raise ValueError("SOCKS5 代理格式无效")
    port = parsed.port or 1080
    if not 1 <= int(port) <= 65535:
        raise ValueError("SOCKS5 代理端口无效")
    return (
        parsed.hostname,
        int(port),
        unquote(parsed.username or ""),
        unquote(parsed.password or ""),
    )


def is_authenticated_socks5(value: str) -> bool:
    try:
        _, _, username, password = _parse_socks5(value)
    except (TypeError, ValueError):
        return False
    return bool(username or password)


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        block = sock.recv(size - len(chunks))
        if not block:
            raise OSError("SOCKS5 upstream closed during handshake")
        chunks.extend(block)
    return bytes(chunks)


def _socks5_connect(
    proxy_host: str,
    proxy_port: int,
    username: str,
    password: str,
    target_host: str,
    target_port: int,
    timeout: float,
) -> socket.socket:
    upstream = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    try:
        methods = b"\x05\x02\x00\x02" if username or password else b"\x05\x01\x00"
        upstream.sendall(methods)
        version, method = _recv_exact(upstream, 2)
        if version != 5 or method == 0xFF:
            raise OSError("SOCKS5 upstream rejected authentication methods")
        if method == 0x02:
            user_bytes = username.encode("utf-8")
            pass_bytes = password.encode("utf-8")
            if len(user_bytes) > 255 or len(pass_bytes) > 255:
                raise OSError("SOCKS5 credentials are too long")
            upstream.sendall(b"\x01" + bytes([len(user_bytes)]) + user_bytes + bytes([len(pass_bytes)]) + pass_bytes)
            auth_version, auth_status = _recv_exact(upstream, 2)
            if auth_version != 1 or auth_status != 0:
                raise OSError("SOCKS5 username/password authentication failed")
        elif method != 0x00:
            raise OSError("SOCKS5 upstream selected an unsupported method")

        host_bytes = target_host.encode("idna")
        if len(host_bytes) > 255:
            raise OSError("SOCKS5 target hostname is too long")
        request = b"\x05\x01\x00\x03" + bytes([len(host_bytes)]) + host_bytes + int(target_port).to_bytes(2, "big")
        upstream.sendall(request)
        header = _recv_exact(upstream, 4)
        if header[0] != 5 or header[1] != 0:
            raise OSError(f"SOCKS5 CONNECT failed with code {header[1]}")
        address_type = header[3]
        if address_type == 1:
            _recv_exact(upstream, 4)
        elif address_type == 3:
            length = _recv_exact(upstream, 1)[0]
            _recv_exact(upstream, length)
        elif address_type == 4:
            _recv_exact(upstream, 16)
        else:
            raise OSError("SOCKS5 upstream returned an invalid address type")
        _recv_exact(upstream, 2)
        upstream.settimeout(None)
        return upstream
    except Exception:
        upstream.close()
        raise


def _relay(left: socket.socket, right: socket.socket) -> None:
    sockets = [left, right]
    try:
        while True:
            readable, _, _ = select.select(sockets, [], [], 60.0)
            if not readable:
                continue
            for source in readable:
                data = source.recv(64 * 1024)
                if not data:
                    return
                destination = right if source is left else left
                destination.sendall(data)
    except (OSError, ValueError):
        return


@dataclass
class Socks5HttpBridge:
    source: str
    connect_timeout: float = 8.0

    def __post_init__(self) -> None:
        self._proxy_host, self._proxy_port, self._username, self._password = _parse_socks5(self.source)
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(32)
        self._listener.settimeout(0.5)
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._serve, name="gcash-socks5-bridge", daemon=True)
        self._thread.start()

    @property
    def proxy(self) -> str:
        host, port = self._listener.getsockname()[:2]
        return f"http://127.0.0.1:{port}"

    def stop(self) -> None:
        if self._stopped.is_set():
            return
        self._stopped.set()
        try:
            self._listener.close()
        except OSError:
            pass

    def _serve(self) -> None:
        while not self._stopped.is_set():
            try:
                client, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(client,), name="gcash-socks5-client", daemon=True).start()

    def _handle(self, client: socket.socket) -> None:
        remote = None
        try:
            client.settimeout(self.connect_timeout)
            request = b""
            while b"\r\n\r\n" not in request and len(request) < 16 * 1024:
                block = client.recv(4096)
                if not block:
                    return
                request += block
            first_line = request.split(b"\r\n", 1)[0].decode("latin1", errors="replace")
            method, target, _ = first_line.split(" ", 2)
            if method.upper() != "CONNECT":
                client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\n\r\n")
                return
            if target.startswith("["):
                host_end = target.find("]")
                target_host = target[1:host_end]
                target_port = int(target[host_end + 2:])
            else:
                target_host, port_text = target.rsplit(":", 1)
                target_port = int(port_text)
            remote = _socks5_connect(
                self._proxy_host,
                self._proxy_port,
                self._username,
                self._password,
                target_host,
                target_port,
                self.connect_timeout,
            )
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            client.settimeout(None)
            _relay(client, remote)
        except (OSError, ValueError, UnicodeError):
            try:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
        finally:
            for sock in (client, remote):
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass


_BRIDGES: dict[str, Socks5HttpBridge] = {}
_BRIDGES_LOCK = threading.RLock()


def http_proxy_for(value: str) -> str:
    """Return an upstream-compatible proxy, creating a bridge when needed."""
    if not is_authenticated_socks5(value):
        return str(value).strip()
    normalized = str(value).strip().replace("\\@", "@")
    with _BRIDGES_LOCK:
        bridge = _BRIDGES.get(normalized)
        if bridge is None:
            bridge = Socks5HttpBridge(normalized)
            _BRIDGES[normalized] = bridge
        return bridge.proxy


@atexit.register
def _stop_bridges() -> None:
    with _BRIDGES_LOCK:
        bridges = list(_BRIDGES.values())
        _BRIDGES.clear()
    for bridge in bridges:
        bridge.stop()
