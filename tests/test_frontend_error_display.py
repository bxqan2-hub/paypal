from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "payment_link_extractor" / "web" / "static" / "app.js"
STYLES = ROOT / "payment_link_extractor" / "web" / "static" / "styles.css"


def test_frontend_explains_gopay_zero_amount_failure_without_changing_core() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    assert "零元优惠未生效" in source
    assert "expected zero amount" in source
    assert "单纯更换代理通常不会改变账号优惠资格" in source
    assert "查看原始日志" in source


def test_frontend_has_distinct_error_display_styles() -> None:
    source = STYLES.read_text(encoding="utf-8")
    for selector in (
        ".task-error-panel",
        ".task-error-promo-ineligible",
        ".task-error-network",
        ".task-error-proxy-input",
        ".task-error-raw",
    ):
        assert selector in source
