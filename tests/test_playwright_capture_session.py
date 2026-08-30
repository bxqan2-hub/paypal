from pathlib import Path

from tools.playwright_capture_session import (
    BrowserSession,
    DEFAULT_PROFILE_DIR,
    build_parser,
    default_output,
    read_devtools_port,
    select_session,
)


def session(port: int, profile: Path, url: str) -> BrowserSession:
    return BrowserSession(
        profile.resolve(),
        port,
        "Chromium/test",
        f"ws://127.0.0.1:{port}/devtools/browser/test",
        ({"id": "p", "title": "test", "url": url},),
    )


def test_read_devtools_port(tmp_path: Path) -> None:
    assert read_devtools_port(tmp_path) is None
    (tmp_path / "DevToolsActivePort").write_text("60943\n/devtools/browser/id\n", encoding="utf-8")
    assert read_devtools_port(tmp_path) == 60943


def test_select_session_prefers_url_then_managed_profile(tmp_path: Path) -> None:
    other = session(60001, tmp_path / "other", "https://example.com/")
    managed = session(60002, DEFAULT_PROFILE_DIR, "https://chatgpt.com/")
    assert select_session([other, managed]).port == 60002
    assert select_session([managed, other], "example.com").port == 60001


def test_capture_parser_and_output() -> None:
    args = build_parser().parse_args(["capture", "--channel", "gopay", "--duration", "2"])
    assert args.channel == "gopay"
    assert args.duration == 2
    assert args.return_url == "https://chatgpt.com/"
    output = default_output("gopay")
    assert output.parent.name == "playwright-captures"
    assert output.name.startswith("gopay-cdp-capture-")
    assert output.suffix == ".har"


def test_prepare_parser_accepts_explicit_profile(tmp_path: Path) -> None:
    args = build_parser().parse_args(["prepare", "--profile-dir", str(tmp_path)])
    assert args.profile_dir == tmp_path
    assert args.cdp_port == 0


def test_capture_parser_accepts_explicit_return_url() -> None:
    args = build_parser().parse_args(["capture", "--return-url", "about:blank"])
    assert args.return_url == "about:blank"
