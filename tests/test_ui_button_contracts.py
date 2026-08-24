from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

from payment_link_extractor.web.app import create_app


ROOT = Path(__file__).resolve().parents[1]
PAYPAL_HTML = ROOT / "paypal_agreement_protocol" / "web_static" / "index.html"
PAYPAL_JS = ROOT / "paypal_agreement_protocol" / "web_static" / "app.js"
PAYPAL_CSS = ROOT / "paypal_agreement_protocol" / "web_static" / "checkout-preview.css"
EXTRACTOR_HTML = ROOT / "payment_link_extractor" / "web" / "templates" / "index.html"
EXTRACTOR_JS = ROOT / "payment_link_extractor" / "web" / "static" / "app.js"
EXTRACTOR_CSS = ROOT / "payment_link_extractor" / "web" / "static" / "styles.css"


class _ButtonParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.buttons: list[dict[str, object]] = []
        self._current: dict[str, object] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "button":
            self._current = {"attrs": dict(attrs), "text": []}

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._current["text"].append(data)  # type: ignore[union-attr]

    def handle_endtag(self, tag: str) -> None:
        if tag == "button" and self._current is not None:
            self.buttons.append(self._current)
            self._current = None


def _buttons(path: Path) -> list[dict[str, object]]:
    parser = _ButtonParser()
    parser.feed(path.read_text(encoding="utf-8"))
    return parser.buttons


def test_both_interfaces_have_no_empty_or_implicit_static_buttons() -> None:
    paypal_buttons = _buttons(PAYPAL_HTML)
    extractor_buttons = _buttons(EXTRACTOR_HTML)
    assert len(paypal_buttons) == 18
    assert len(extractor_buttons) == 22

    for button in paypal_buttons + extractor_buttons:
        attrs = button["attrs"]
        text = " ".join("".join(button["text"]).split())  # type: ignore[arg-type]
        assert text or attrs.get("aria-label"), attrs  # type: ignore[union-attr]
        assert attrs.get("type") in {"button", "submit"}, attrs  # type: ignore[union-attr]


def test_paypal_interface_static_and_dynamic_buttons_are_wired() -> None:
    source = PAYPAL_JS.read_text(encoding="utf-8")
    direct_click_ids = {
        "themeToggle",
        "clearInterfaceButton",
        "countryPickerToggle",
        "acquirePhonesButton",
        "refreshPhonesButton",
        "cancelBatchButton",
        "toggleConfigButton",
        "loadVaultButton",
        "completeVaultButton",
        "cancelButton",
        "refreshJobs",
        "otpSubmit",
        "browserType",
        "browserFinish",
        "captchaSubmit",
        "copyResult",
        "jobLogModalClose",
    }
    for button_id in direct_click_ids:
        assert f"$('{button_id}').addEventListener('click'" in source
    assert "$('protocolForm').addEventListener('submit'" in source

    for attribute in {
        "data-queue-cancel",
        "data-queue-copy",
        "data-queue-log",
        "data-queue-otp",
        "data-queue-phone",
        "data-queue-replace-phone",
        "data-batch-code",
        "data-batch-cancel",
    }:
        assert source.count(attribute) >= 2, attribute


def test_paypal_account_display_history_is_token_scoped_and_manually_cleared() -> None:
    source = PAYPAL_JS.read_text(encoding="utf-8")
    html = PAYPAL_HTML.read_text(encoding="utf-8")
    css = PAYPAL_CSS.read_text(encoding="utf-8")

    assert 'id="clearInterfaceButton"' in html
    assert 'type="button"' in html
    assert "interface-refresh-button" in css
    assert "const OPENED_ACCOUNT_HISTORY_KEY = 'paypal.protocol.opened-accounts.v1';" in source
    assert "if (isNewPush && existingSignature) archiveCurrentAccountRows();" in source
    assert "document.querySelector('#phone') && (phone || isNewPush)" in source
    assert "const tokenMatchedJob = state.batchJobs.find(item => jobToken(item) === token);" in source
    assert "indexedJobToken === token" in source
    assert "return [...historyRows, ...currentRows]" in source
    assert "const existingRows = currentQueueRows();" in source
    assert "rememberBatchJobs(state.batchJobs);" in source
    assert "clearOpenedAccountHistory();" in source
    assert "window.history.replaceState({}, '', window.location.pathname);" in source
    assert "MAX_OPENED_ACCOUNT_HISTORY" not in source
    assert "entries.length === 1 ? state.batchJobs[0]" not in source


def test_completed_paypal_accounts_are_excluded_from_phone_actions() -> None:
    source = PAYPAL_JS.read_text(encoding="utf-8")

    assert "function isCompletedJob(job)" in source
    assert "job.status === 'completed'" in source
    assert "!isCompletedJob(item.row?.job)" in source
    assert "!isCompletedJob(existingRows[index]?.job)" in source
    assert "if (isCompletedJob(existingRows[index]?.job)) continue;" in source
    assert "isCompletedJob(job)) action" in source
    assert "replaceTerminalNumber" not in source


def test_masked_batch_jobs_stay_bound_and_signup_error_is_attributed_to_registration() -> None:
    source = PAYPAL_JS.read_text(encoding="utf-8")

    assert "const BATCH_ACCOUNT_MAP_KEY = 'paypal.protocol.batch-account-map.v1';" in source
    assert "function registerBatchAccountMap(jobs = [], entries = sortedBaPoolEntries())" in source
    assert "const mappedJob = state.batchJobs.find(item => batchAccountForJob(item)?.token === token);" in source
    assert "const job = mappedJob || tokenMatchedJob" in source
    assert "if (list[0] && typeof list[0] === 'object') registerBatchAccountMap(list);" in source
    assert "function isAccountAlreadyExistsWithoutToken(job)" in source
    assert "账号注册失败（短信验证码已接收并提交）" in source
    assert "短信验证码已正常接收并提交" in source


def test_extractor_interface_static_and_dynamic_buttons_are_wired() -> None:
    source = EXTRACTOR_JS.read_text(encoding="utf-8")
    bindings = {
        "auth-submit": 'elements.authForm.addEventListener("submit"',
        "logout-button": 'elements.logoutButton.addEventListener("click", logout)',
        "batch-import-button": 'elements.batchImportButton.addEventListener("click", openBatchImport)',
        "extract-token-button": 'elements.extractTokenButton.addEventListener("click", extractTokenToInput)',
        "copy-token-button": 'elements.copyTokenButton.addEventListener("click", copyAccessToken)',
        "refresh-proxy-source": 'byId("refresh-proxy-source").addEventListener("click", refreshProxySource)',
        "submit-button": 'elements.taskForm.addEventListener("submit", submitTask)',
        "export-csv-button": 'elements.exportCsvButton.addEventListener("click", downloadSelectedCsv)',
        "push-selected-paypal": 'elements.pushSelectedPaypalButton.addEventListener("click", pushPaypalTasks)',
        "retry-network-failed-tasks": 'elements.retryNetworkFailedTasksButton.addEventListener("click", retryAllNetworkFailedTasks)',
        "clear-failed-tasks": 'elements.clearFailedTasksButton.addEventListener("click"',
        "clear-succeeded-tasks": 'elements.clearSucceededTasksButton.addEventListener("click"',
        "task-details-close": 'elements.taskDetailsClose.addEventListener("click", closeTaskDetails)',
        "batch-import-close": 'elements.batchImportCloseButton.addEventListener("click", closeBatchImport)',
        "batch-import-validate": 'elements.batchValidateButton.addEventListener("click", validateBatchImport)',
        "batch-import-submit": 'elements.batchSubmitButton.addEventListener("click", submitBatchImport)',
    }
    for button_id, binding in bindings.items():
        assert button_id in EXTRACTOR_HTML.read_text(encoding="utf-8")
        assert binding in source

    assert 'elements.taskFilters.addEventListener("click"' in source
    assert 'elements.viewToggle.addEventListener("click"' in source
    assert "请至少粘贴一条账号 Token 或 JSON" in source
    assert "elements.logoutButton.hidden = !password;" in source
    assert "function hasAccessTokenShape(value)" in source
    assert "parts.length === 5" in source
    assert "token:${inspection.accessToken}" in source
    for attribute in {
        "data-details",
        "data-cancel",
        "data-retry",
        "data-delete",
        "data-test-proxy",
        "data-toggle-proxy",
        "data-copy-proxy",
        "data-copy",
    }:
        assert source.count(attribute) >= 2, attribute


def test_extractor_workspace_is_centered_with_breathing_room() -> None:
    css = EXTRACTOR_CSS.read_text(encoding="utf-8")
    assert ".shell { width: min(calc(100% - 56px), 1680px);" in css
    assert "max-width: 1680px; margin: 0 auto;" in css


def test_button_backend_routes_exist() -> None:
    app = create_app({"TESTING": True})
    rules = {rule.rule: set(rule.methods or ()) for rule in app.url_map.iter_rules()}
    expected = {
        "/api/health": "GET",
        "/api/defaults": "GET",
        "/api/proxy/source": "GET",
        "/api/tasks": "POST",
        "/api/proxy/test": "POST",
        "/api/tasks/<task_id>/cancel": "POST",
        "/api/tasks/<task_id>/retry": "POST",
        "/api/tasks/<task_id>": "DELETE",
        "/api/tasks/bulk-delete": "POST",
        "/paypal-pay/<path:protocol_path>": "POST",
    }
    for route, method in expected.items():
        assert route in rules
        assert method in rules[route]

    protocol_source = (ROOT / "paypal_agreement_protocol" / "web.py").read_text(encoding="utf-8")
    for route_fragment in {
        'path == "/api/jobs"',
        'path.endswith("/browser/action")',
        'path.endswith("/captcha")',
        'path.endswith("/cancel")',
        'path.endswith("/otp")',
    }:
        assert route_fragment in protocol_source
