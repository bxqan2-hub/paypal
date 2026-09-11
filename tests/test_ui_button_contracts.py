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
        self.inputs: list[dict[str, object]] = []
        self.forms: list[str | None] = []
        self.nested_form = False
        self._current: dict[str, object] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "form":
            self.nested_form = self.nested_form or bool(self.forms)
            self.forms.append(dict(attrs).get("id"))
        form = self.forms[-1] if self.forms else None
        if tag == "input":
            self.inputs.append({"attrs": dict(attrs), "form": form})
        if tag == "button":
            self._current = {"attrs": dict(attrs), "text": [], "form": form}

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._current["text"].append(data)  # type: ignore[union-attr]

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self.forms:
            self.forms.pop()
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
    assert len(extractor_buttons) == 24

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
        "concurrency-apply": 'elements.concurrencyForm.addEventListener("submit", applyConcurrency)',
        "concurrency-refresh": 'elements.concurrencyRefresh.addEventListener("click", refreshConcurrency)',
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


def test_global_concurrency_controls_use_an_independent_form_and_server_truth() -> None:
    html = EXTRACTOR_HTML.read_text(encoding="utf-8")
    source = EXTRACTOR_JS.read_text(encoding="utf-8")
    parser = _ButtonParser()
    parser.feed(html)
    assert not parser.nested_form
    buttons = {button["attrs"]["id"]: button for button in parser.buttons if "id" in button["attrs"]}
    assert buttons["concurrency-apply"]["form"] == "concurrency-form"
    assert buttons["concurrency-apply"]["attrs"]["type"] == "submit"
    assert buttons["concurrency-refresh"]["form"] == "concurrency-form"
    assert buttons["concurrency-refresh"]["attrs"]["type"] == "button"
    assert buttons["submit-button"]["form"] == "task-form"
    control = next(item for item in parser.inputs if item["attrs"].get("id") == "task-concurrency")
    assert control["form"] == "concurrency-form"
    assert control["attrs"]["type"] == "number"
    assert control["attrs"]["min"] == "1"
    assert control["attrs"]["step"] == "1"
    assert "value" not in control["attrs"]  # No browser default overrides persisted server settings.
    assert "真正同时运行的任务数，不是批量提交请求数" in html
    assert "所有支付渠道共用全局名额" in html
    assert "调低不会终止运行中任务" in html

    implementation = source[source.index("  function renderConcurrency()"):source.index("  function readSavedPassword()")]
    assert 'apiFetch("/api/tasks/concurrency")' in implementation
    assert 'apiFetch("/api/tasks/concurrency", {' in implementation
    assert 'body: JSON.stringify({ concurrency: value })' in implementation
    assert "event.preventDefault();" in implementation
    assert "Number.isInteger(value)" in implementation
    assert "value > control.snapshot.max_concurrency" in implementation
    assert "snapshot.active_slots" in implementation
    assert "snapshot.queued_tasks" in implementation
    assert "snapshot.active_slots > snapshot.concurrency" in implementation
    assert "!control.dirty && document.activeElement !== elements.concurrencyInput" in implementation
    assert "revision === control.revision" in implementation
    assert "localStorage" not in implementation
    assert "submitTaskRequest" not in implementation


def test_global_concurrency_sync_runs_on_login_and_reconnect_before_task_id_filter() -> None:
    source = EXTRACTOR_JS.read_text(encoding="utf-8")
    auth = source[source.index("  async function authenticate("):source.index("  function escapeHtml(")]
    assert "await refreshConcurrency();" in auth
    reducer = source[source.index("  function reduceTaskEvent("):source.index("  function matchesFilter(")]
    assert reducer.index('event.type === "task.concurrency"') < reducer.index("!event.task_id")
    assert "receiveConcurrency(event.data);" in reducer
    socket = source[source.index("  function connectTaskSocket()"):source.index("  function scheduleReconnect()")]
    assert socket.index('message.type === "auth.ok"') < socket.index("refreshConcurrency();")
    assert 'elements.concurrencyInput.addEventListener("input"' in source
    assert 'window.addEventListener("focus", () => { if (authReady) refreshConcurrency(); });' in source


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
    concurrency_methods = {
        method for rule in app.url_map.iter_rules() if rule.rule == "/api/tasks/concurrency"
        for method in rule.methods or ()
    }
    assert {"GET", "POST"}.issubset(concurrency_methods)

    protocol_source = (ROOT / "paypal_agreement_protocol" / "web.py").read_text(encoding="utf-8")
    for route_fragment in {
        'path == "/api/jobs"',
        'path.endswith("/browser/action")',
        'path.endswith("/captcha")',
        'path.endswith("/cancel")',
        'path.endswith("/otp")',
    }:
        assert route_fragment in protocol_source
