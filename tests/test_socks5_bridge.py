from __future__ import annotations

import socket
import threading
from urllib.parse import urlsplit

from payment_link_extractor.web.socks5_bridge import (
    Socks5HttpBridge,
    _parse_socks5,
    is_authenticated_socks5,
)


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        data += sock.recv(size - len(data))
    return data


def test_authenticated_socks5_parser_accepts_escaped_at():
    host, port, username, password = _parse_socks5(
        r"socks5://user:pass\@127.0.0.1:3000"
    )
    assert (host, port, username, password) == ("127.0.0.1", 3000, "user", "pass")
    assert is_authenticated_socks5("socks5://user:pass@127.0.0.1:3000")
    assert not is_authenticated_socks5("socks5://127.0.0.1:3000")


def test_http_bridge_tunnels_authenticated_socks5_connect():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    upstream_port = listener.getsockname()[1]
    ready = threading.Event()

    def fake_socks5() -> None:
        ready.set()
        server, _ = listener.accept()
        try:
            assert _recv_exact(server, 4) == b"\x05\x02\x00\x02"
            server.sendall(b"\x05\x02")
            version = _recv_exact(server, 1)
            user_len = _recv_exact(server, 1)[0]
            user = _recv_exact(server, user_len)
            pass_len = _recv_exact(server, 1)[0]
            password = _recv_exact(server, pass_len)
            assert (version, user, password) == (b"\x01", b"user", b"pass")
            server.sendall(b"\x01\x00")
            assert _recv_exact(server, 4) == b"\x05\x01\x00\x03"
            target_len = _recv_exact(server, 1)[0]
            target = _recv_exact(server, target_len)
            target_port = _recv_exact(server, 2)
            assert (target, target_port) == (b"example.com", (443).to_bytes(2, "big"))
            server.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            payload = server.recv(4)
            server.sendall(payload)
        finally:
            server.close()

    thread = threading.Thread(target=fake_socks5, daemon=True)
    thread.start()
    ready.wait(1)
    bridge = Socks5HttpBridge(f"socks5://user:pass@127.0.0.1:{upstream_port}")
    try:
        bridge_host = urlsplit(bridge.proxy).hostname
        bridge_port = urlsplit(bridge.proxy).port
        client = socket.create_connection((bridge_host, bridge_port), timeout=2)
        try:
            client.sendall(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
            response = client.recv(1024)
            assert response.startswith(b"HTTP/1.1 200 Connection Established")
            client.sendall(b"ping")
            assert _recv_exact(client, 4) == b"ping"
        finally:
            client.close()
    finally:
        bridge.stop()
        listener.close()
        thread.join(timeout=1)
