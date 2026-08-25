(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const elements = {
    form: $("taskForm"), tokens: $("tokensInput"), proxies: $("proxyInput"),
    proxyScheme: $("proxySchemeInput"),
    concurrency: $("concurrencyInput"), retry: $("retryInput"), timeout: $("timeoutInput"),
    startInterval: $("startIntervalInput"), interval: $("intervalInput"), diagnostics: $("diagnosticsInput"),
    start: $("startButton"), stop: $("stopButton"), tokenCount: $("tokenCount"), proxyCount: $("proxyCount"),
    formError: $("formError"), statusChip: $("statusChip"), statusText: $("statusText"), taskId: $("taskId"),
    statTotal: $("statTotal"), statCompleted: $("statCompleted"), statSuccess: $("statSuccess"), statFailed: $("statFailed"),
    progressFill: $("progressFill"), resultBody: $("resultBody"), copySuccess: $("copySuccessButton"),
    clear: $("clearButton"), logList: $("logList"), accountLogLabel: $("accountLogLabel"), toast: $("toast"),
  };

  const statusLabels = { idle: "待机", running: "运行中", stopping: "停止中", stopped: "已停止", completed: "已完成", failed: "失败" };
  const resultLabels = {
    success: "成功", oaics_rejected: "拒绝 OAICS", invalid_gopay_redirect: "无效链接",
    stopped: "已停止", checkout_403: "HTTP 403", token_invalid: "Token 无效",
    proxy_failed: "代理失败", approve_blocked: "确认拒绝", running: "运行中", failed: "失败",
  };
  const stageLabels = {
    task: "任务", network: "网络", checkout: "Checkout", gopay: "GoPay",
    submit: "提交", approve: "确认", result: "结果", retry: "重试",
  };
  let currentTaskId = "";
  let currentResults = [];
  let selectedAccountIndex = 0;
  let accountLogLastId = 0;
  let pollTimer = 0;
  let toastTimer = 0;

  function showToast(message) {
    elements.toast.textContent = message;
    elements.toast.hidden = false;
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => { elements.toast.hidden = true; }, 2200);
  }

  function setError(message = "") {
    elements.formError.textContent = message;
    elements.formError.hidden = !message;
  }

  function countTokenInput() {
    const text = elements.tokens.value.trim();
    if (!text) return 0;
    const jwt = text.match(/eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}/g);
    if (jwt && jwt.length) return new Set(jwt).size;
    try {
      const parsed = JSON.parse(text);
      if (Array.isArray(parsed)) return parsed.length;
      if (parsed && Array.isArray(parsed.accounts)) return parsed.accounts.length;
    } catch (_error) {}
    return new Set(text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean)).size;
  }

  function proxyLines() {
    return elements.proxies.value.split(/\r?\n/).map((line) => line.trim()).filter((line) => line && !line.startsWith("#"));
  }

  function updateCounts() {
    elements.tokenCount.textContent = `${countTokenInput()} 个账号`;
    elements.proxyCount.textContent = `${new Set(proxyLines()).size} 条`;
  }

  function setRunning(running) {
    elements.start.disabled = running;
    elements.stop.disabled = !running;
    elements.form.querySelectorAll("input, textarea, select").forEach((field) => { field.disabled = running; });
  }

  function setStatus(state) {
    const normalized = statusLabels[state] ? state : "idle";
    elements.statusChip.dataset.state = normalized;
    elements.statusText.textContent = statusLabels[normalized];
    setRunning(normalized === "running" || normalized === "stopping");
  }

  function formatTime(timestamp) {
    if (!timestamp) return "";
    return new Date(timestamp).toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  function appendAccountLogs(logs) {
    if (!Array.isArray(logs) || !logs.length) return;
    elements.logList.querySelectorAll(".empty-log").forEach((item) => item.remove());
    const fragment = document.createDocumentFragment();
    for (const item of logs) {
      if (!item || Number(item.id) <= accountLogLastId) continue;
      const row = document.createElement("div");
      row.className = "log-line";
      row.dataset.level = item.level || "info";
      const stamp = document.createElement("span");
      stamp.className = "log-time";
      stamp.textContent = formatTime(Number(item.time));
      const stage = document.createElement("span");
      stage.className = "log-stage";
      stage.textContent = stageLabels[item.stage] || String(item.stage || "任务");
      const message = document.createElement("span");
      message.className = "log-message";
      message.textContent = String(item.message || "");
      row.append(stamp, stage, message);
      fragment.append(row);
      accountLogLastId = Math.max(accountLogLastId, Number(item.id) || 0);
    }
    elements.logList.append(fragment);
    elements.logList.scrollTop = elements.logList.scrollHeight;
  }

  function renderSummary(summary = {}) {
    const total = Number(summary.total) || 0;
    const completed = Number(summary.completed) || 0;
    elements.statTotal.textContent = String(total);
    elements.statCompleted.textContent = String(completed);
    elements.statSuccess.textContent = String(Number(summary.success) || 0);
    elements.statFailed.textContent = String(Number(summary.failed) || 0);
    elements.progressFill.style.width = `${total ? Math.min(100, completed * 100 / total) : 0}%`;
  }

  function resultStatusLabel(status) {
    return resultLabels[status] || String(status || "失败");
  }

  function renderResults(results) {
    currentResults = Array.isArray(results) ? results.slice() : [];
    elements.copySuccess.disabled = !currentResults.some((item) => item.status === "success" && item.url);
    if (!currentResults.length) {
      elements.resultBody.innerHTML = '<tr><td colspan="5" class="empty-cell">等待任务结果</td></tr>';
      return;
    }
    elements.resultBody.replaceChildren();
    const fragment = document.createDocumentFragment();
    for (const item of currentResults) {
      const row = document.createElement("tr");
      if (Number(item.index) === selectedAccountIndex) row.classList.add("selected");
      const index = document.createElement("td");
      index.textContent = String(item.index || "");
      const account = document.createElement("td");
      account.className = "account-id";
      account.textContent = String(item.account || "unknown");
      account.title = account.textContent;
      const state = document.createElement("td");
      const badge = document.createElement("span");
      badge.className = "status-label";
      badge.dataset.status = item.status || "failed";
      badge.textContent = resultStatusLabel(item.status);
      state.append(badge);
      const result = document.createElement("td");
      result.className = "result-detail";
      if (item.status === "success" && item.url) {
        const link = document.createElement("a");
        link.className = "result-link";
        link.href = item.url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = item.url;
        result.append(link);
      } else {
        result.textContent = String(item.detail || "失败");
        result.title = result.textContent;
      }
      const action = document.createElement("td");
      const logs = document.createElement("button");
      logs.type = "button";
      logs.className = "row-button";
      logs.dataset.logIndex = String(item.index || "");
      logs.dataset.logAccount = String(item.account || "unknown");
      logs.textContent = "日志";
      action.append(logs);
      if (item.status === "success" && item.url) {
        const copy = document.createElement("button");
        copy.type = "button";
        copy.className = "row-button";
        copy.dataset.copyUrl = item.url;
        copy.textContent = "复制";
        action.append(copy);
      }
      row.append(index, account, state, result, action);
      fragment.append(row);
    }
    elements.resultBody.append(fragment);
    if (!selectedAccountIndex && currentResults.length) {
      selectAccount(Number(currentResults[0].index), String(currentResults[0].account || "unknown"));
    }
  }

  function resetOutput() {
    currentTaskId = "";
    currentResults = [];
    selectedAccountIndex = 0;
    accountLogLastId = 0;
    elements.taskId.textContent = "";
    elements.accountLogLabel.textContent = "选择一个账号";
    elements.logList.innerHTML = '<div class="empty-log">选择账号后查看日志</div>';
    renderSummary({});
    renderResults([]);
    setError();
  }

  function applySnapshot(snapshot) {
    if (!snapshot || typeof snapshot !== "object") return;
    if (snapshot.task_id && snapshot.task_id !== currentTaskId) {
      if (currentTaskId) {
        selectedAccountIndex = 0;
        accountLogLastId = 0;
        elements.accountLogLabel.textContent = "选择一个账号";
        elements.logList.innerHTML = '<div class="empty-log">选择账号后查看日志</div>';
      }
      currentTaskId = snapshot.task_id;
      elements.taskId.textContent = currentTaskId;
    }
    renderSummary(snapshot.summary);
    renderResults(snapshot.results);
    setStatus(snapshot.state || "idle");
    if (snapshot.state === "failed" || snapshot.state === "stopped") setError(snapshot.error || "批量任务未完成");
  }

  function selectAccount(index, account) {
    if (!index) return;
    selectedAccountIndex = index;
    accountLogLastId = 0;
    elements.accountLogLabel.textContent = `#${index} · ${account}`;
    elements.logList.innerHTML = '<div class="empty-log">等待账号日志</div>';
    renderResults(currentResults);
    pollAccountLogs();
  }

  async function pollAccountLogs() {
    if (!selectedAccountIndex) return;
    const requestedIndex = selectedAccountIndex;
    try {
      const data = await request(`/api/account-logs?index=${requestedIndex}&after=${accountLogLastId}`);
      if (requestedIndex === selectedAccountIndex) appendAccountLogs(data.logs);
    } catch (_error) {}
  }

  async function request(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      cache: "no-store",
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
    return body;
  }

  async function poll() {
    try {
      applySnapshot(await request("/api/task?after=0"));
      await pollAccountLogs();
    } catch (_error) {
      elements.statusText.textContent = "连接中断";
    } finally {
      pollTimer = window.setTimeout(poll, 1000);
    }
  }

  elements.form.addEventListener("submit", async (event) => {
    event.preventDefault();
    setError();
    if (!elements.tokens.value.trim()) return setError("请填写批量 Access Token");
    if (!proxyLines().length) return setError("请填写至少一条代理");
    resetOutput();
    setStatus("running");
    try {
      applySnapshot(await request("/api/task/start", {
        method: "POST",
        body: JSON.stringify({
          tokens: elements.tokens.value,
          proxies: proxyLines().join("\n"),
          proxy_scheme: elements.proxyScheme.value,
          concurrency: Number(elements.concurrency.value),
          max_retry: Number(elements.retry.value),
          poll_timeout: Number(elements.timeout.value),
          start_interval: Number(elements.startInterval.value),
          poll_interval_ms: Number(elements.interval.value),
          diagnostics: elements.diagnostics.checked,
        }),
      }));
    } catch (error) {
      setStatus("failed");
      setError(error.message || "启动失败");
    }
  });

  elements.stop.addEventListener("click", async () => {
    elements.stop.disabled = true;
    try {
      applySnapshot(await request("/api/task/stop", { method: "POST", body: "{}" }));
    } catch (error) {
      setError(error.message || "停止失败");
    }
  });

  elements.resultBody.addEventListener("click", async (event) => {
    const logButton = event.target.closest("[data-log-index]");
    if (logButton) {
      selectAccount(Number(logButton.dataset.logIndex), logButton.dataset.logAccount || "unknown");
      return;
    }
    const button = event.target.closest("[data-copy-url]");
    if (!button) return;
    try {
      await navigator.clipboard.writeText(button.dataset.copyUrl || "");
      showToast("链接已复制");
    } catch (_error) {
      showToast("复制失败");
    }
  });

  elements.copySuccess.addEventListener("click", async () => {
    const links = currentResults.filter((item) => item.status === "success" && item.url).map((item) => item.url);
    if (!links.length) return;
    try {
      await navigator.clipboard.writeText(links.join("\n"));
      showToast(`已复制 ${links.length} 条链接`);
    } catch (_error) {
      showToast("复制失败");
    }
  });

  elements.clear.addEventListener("click", () => {
    elements.logList.innerHTML = '<div class="empty-log">暂无日志</div>';
  });
  elements.tokens.addEventListener("input", updateCounts);
  elements.proxies.addEventListener("input", updateCounts);
  window.addEventListener("beforeunload", () => window.clearTimeout(pollTimer));

  updateCounts();
  poll();
})();
