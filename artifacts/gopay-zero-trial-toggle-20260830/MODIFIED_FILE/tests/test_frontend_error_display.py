from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "payment_link_extractor" / "web" / "static" / "app.js"
STYLES = ROOT / "payment_link_extractor" / "web" / "static" / "styles.css"
INDEX_HTML = ROOT / "payment_link_extractor" / "web" / "templates" / "index.html"


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


def test_frontend_exposes_gopay_zero_trial_validation_switch() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    source = APP_JS.read_text(encoding="utf-8")
    styles = STYLES.read_text(encoding="utf-8")

    assert 'id="gopay-zero-trial-validation" type="checkbox" checked' in html
    assert "开启 0 元试用与 0 元链接校验" in html
    assert "跳过第 1、6 步" in html
    assert 'paymentMethod === "gopay"' in source
    assert "result.gopay_zero_trial_validation" in source
    assert "zero_amount_validation" in source
    assert ".gopay-zero-trial-field" in styles
