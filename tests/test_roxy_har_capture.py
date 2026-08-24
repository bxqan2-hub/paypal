from __future__ import annotations

import json
from pathlib import Path

from tools.roxy_har_capture import discover_roxy_targets, read_devtools_port


def test_read_devtools_port_and_discover_page(tmp_path: Path, monkeypatch) -> None:
    profile = tmp_path / "profile-a"
    profile.mkdir()
    (profile / "DevToolsActivePort").write_text("45678\n/devtools/browser/test\n", encoding="utf-8")

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def read(self, *_args) -> bytes:
            return json.dumps(
                [
                    {
                        "type": "page",
                        "id": "page-1",
                        "title": "ChatGPT",
                        "url": "https://chatgpt.com/",
                        "webSocketDebuggerUrl": "ws://127.0.0.1:45678/devtools/page/page-1",
                    },
                    {"type": "browser", "id": "browser-1"},
                ]
            ).encode()

    monkeypatch.setattr("tools.roxy_har_capture.urlopen", lambda *_args, **_kwargs: FakeResponse())
    assert read_devtools_port(profile) == 45678
    targets = discover_roxy_targets(tmp_path)
    assert len(targets) == 1
    assert targets[0].profile_id == "profile-a"
    assert targets[0].page_id == "page-1"
