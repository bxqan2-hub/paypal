const state = {
  accounts: [],
  selected: new Set(),
  job: null,
  pollTimer: null,
  modalAccountId: "",
  stopping: false,
  abandoning: false,
  maxAccounts: 50,
  maxProxies: 100,
};

const ACTIVE_JOB_STATES = new Set(["queued", "running", "canceling"]);
const ACTIVE_PAYMENT_STATES = new Set([
  "starting", "waiting_scan", "refreshing", "redirect_captured", "callback_processing",
]);
const JWT_RE = /(eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+)/;
const $ = id => document.getElementById(id);
const esc = value => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;");

function toast(message) {
  const node = $("toast");
  node.textContent = message;
  node.classList.add("show");
  clearTimeout(window.__toastTimer);
  window.__toastTimer = setTimeout(() => node.classList.remove("show"), 2600);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    method: options.method || "GET",
    headers: {"Content-Type": "application/json", ...(options.headers || {})},
    body: options.body,
    cache: "no-store",
  });
  const body = await response.json().catch(() => ({error: "响应格式无效"}));
  if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
  return body;
}

function lines(value) {
  return String(value || "").split(/\r?\n/).map(line => line.trim()).filter(Boolean);
}

function proxyPool() {
  return [...new Set(lines($("proxyInput").value))];
}

function decodeJwtPayload(token) {
  try {
    const encoded = token.split(".")[1].replaceAll("-", "+").replaceAll("_", "/");
    const padded = encoded + "=".repeat((4 - encoded.length % 4) % 4);
    const bytes = Uint8Array.from(atob(padded), character => character.charCodeAt(0));
    return JSON.parse(new TextDecoder().decode(bytes));
  } catch {
    return {};
  }
}

function lineFields(line) {
  const parts = String(line || "").split("|").map(part => part.trim());
  let email = "";
  let name = "";
  if (parts.length >= 3) {
    name = parts.slice(0, -2).join("|").trim();
    if (parts.at(-2).includes("@")) email = parts.at(-2);
  } else if (parts.length === 2) {
    if (parts[0].includes("@")) email = parts[0];
    else name = parts[0];
  }
  return {email, name};
}

function accountFromLine(line, index) {
  const match = String(line || "").match(JWT_RE);
  if (!match) throw new Error("未找到 JWT AT");
  const token = match[1];
  const payload = decodeJwtPayload(token);
  const profile = typeof payload["https://api.openai.com/profile"] === "object"
    ? payload["https://api.openai.com/profile"] || {} : {};
  const fields = lineFields(line);
  const expiresAt = Number(payload.exp) || 0;
  const email = fields.email || profile.email || payload.email || payload.preferred_username || "";
  const name = fields.name || profile.name || payload.name || payload.given_name || "";
  return {
    id: `draft_${Date.now().toString(36)}_${index}_${Math.random().toString(36).slice(2, 8)}`,
    token,
    email: String(email).trim(),
    name: String(name).trim(),
    expiresAt,
    expired: Boolean(expiresAt && expiresAt <= Math.floor(Date.now() / 1000)),
  };
}

function parseAccounts() {
  const rawLines = lines($("tokenInput").value);
  if (!rawLines.length) return toast("请先粘贴账号 AT");
  if (rawLines.length > state.maxAccounts) {
    return toast(`当前部署每次最多导入 ${state.maxAccounts} 个账号`);
  }

  const parsed = [];
  const warnings = [];
  const seen = new Set();
  rawLines.forEach((line, index) => {
    try {
      const account = accountFromLine(line, index + 1);
      if (seen.has(account.token)) throw new Error("重复 AT，已跳过");
      seen.add(account.token);
      parsed.push(account);
    } catch (error) {
      warnings.push(`第 ${index + 1} 行：${error.message}`);
    }
  });
  if (!parsed.length) return toast(warnings[0] || "没有可用账号");

  state.accounts = parsed;
  state.selected = new Set(parsed.filter(account => !account.expired).map(account => account.id));
  $("importStatus").textContent = warnings.length
    ? `已解析 ${parsed.length} 个，跳过 ${warnings.length} 行`
    : `已解析 ${parsed.length} 个账号`;
  $("search").value = "";
  $("stateFilter").value = "";
  renderAccounts();
  goStep(2);
  if (warnings.length) toast(warnings.join(" · "));
}

function formatExpiry(timestamp) {
  if (!timestamp) return "未提供到期时间";
  return new Date(Number(timestamp) * 1000).toLocaleString("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  });
}

function visibleAccounts() {
  const query = $("search").value.trim().toLowerCase();
  const filter = $("stateFilter").value;
  return state.accounts.filter(account => {
    if (filter === "valid" && account.expired) return false;
    if (filter === "expired" && !account.expired) return false;
    const text = `${account.email} ${account.name}`.toLowerCase();
    return !query || text.includes(query);
  });
}

function renderAccounts() {
  const visible = visibleAccounts();
  $("accountRows").innerHTML = visible.map(account => {
    const selected = state.selected.has(account.id);
    const identity = account.email || account.name || "未识别邮箱";
    return `
      <label class="accountCard ${selected ? "selected" : ""} ${account.expired ? "disabled" : ""}">
        <input type="checkbox" data-account-id="${esc(account.id)}" ${selected ? "checked" : ""} ${account.expired ? "disabled" : ""}>
        <span class="accountCheck" aria-hidden="true">✓</span>
        <span class="accountIdentity">
          <b>${esc(identity)}</b>
          <small>${esc(account.name && account.email ? account.name : `AT 到期：${formatExpiry(account.expiresAt)}`)}</small>
          <em class="accountState ${account.expired ? "expired" : "valid"}">${account.expired ? "已过期" : "有效"}</em>
        </span>
      </label>`;
  }).join("");
  $("accountEmpty").classList.toggle("hidden", visible.length > 0);

  document.querySelectorAll("[data-account-id]").forEach(input => {
    input.onchange = () => {
      if (input.checked) state.selected.add(input.dataset.accountId);
      else state.selected.delete(input.dataset.accountId);
      renderAccounts();
    };
  });
  updateFlow();
}

function updateInputCounts() {
  const accountCount = lines($("tokenInput").value).length;
  const proxies = proxyPool();
  $("inputCounter").textContent = `${accountCount} 个账号 · ${proxies.length} 个节点`;
  $("inputCounter").classList.toggle("bad", accountCount > state.maxAccounts || proxies.length > state.maxProxies);
  $("proxyCount").textContent = `${proxies.length} / ${state.maxProxies}`;
  $("proxyCount").classList.toggle("bad", proxies.length > state.maxProxies);
  updateFlow();
}

function jobRunning() {
  return (state.job?.accounts || []).some(account => ACTIVE_JOB_STATES.has(account.status));
}

function accountHasActivePayment(account) {
  return account?.status === "success" && (
    ACTIVE_PAYMENT_STATES.has(account.payment_status)
    || (account.payment_status === "expired" && account.refresh_available)
  );
}

function hasActivePayment() {
  return (state.job?.accounts || []).some(accountHasActivePayment);
}

function updateFlow() {
  const selectedCount = state.selected.size;
  const running = jobRunning();
  const proxies = proxyPool().length;
  $("accountCount").textContent = state.accounts.length;
  $("stepState1").textContent = state.accounts.length ? "完成" : "当前";
  $("stepState2").textContent = state.accounts.length ? `${selectedCount}/${state.accounts.length}` : "0";
  $("selectionBadge").textContent = `已选 ${selectedCount} / ${state.accounts.length}`;
  $("selectionText").textContent = `已选 ${selectedCount} 个账号`;
  $("selectionMeta").textContent = `自备节点 ${proxies} 条 · 重试 ${Number($("maxAttempts").value) || 5} 次`;
  $("stopJob").classList.toggle("hidden", !running);
  $("startJob").disabled = running || state.abandoning || !selectedCount || !proxies;
  $("backImport").disabled = running;
  $("deleteSelected").disabled = running || !selectedCount;
  $("selectVisible").disabled = running || !state.accounts.length;
}

function goStep(step) {
  if (step === 2 && !state.accounts.length) return toast("请先解析账号");
  if (step === 1 && jobRunning()) return toast("任务运行中，请先停止任务");
  document.querySelectorAll(".stage").forEach(node => node.classList.remove("active"));
  document.querySelectorAll(".step").forEach(node => node.classList.remove("active"));
  $(`stage${step}`).classList.add("active");
  document.querySelector(`.step[data-step="${step}"]`).classList.add("active");
}

function paymentLabel(account) {
  if (account.payment_success) return ["支付成功", "complete"];
  const labels = {
    starting: ["正在打开支付页", "pending"],
    waiting_scan: [account.qr_ready ? "等待扫码" : "正在获取二维码", "waiting"],
    refreshing: ["正在刷新二维码", "pending"],
    redirect_captured: ["已扫码，准备回调", "pending"],
    callback_processing: ["回调已受理，正在确认 Plus", "pending"],
    callback_failed: ["自动回调失败", "failed"],
    callback_unconfirmed: ["Plus 权益未确认", "failed"],
    expired: [account.refresh_available ? "二维码已过期，可刷新" : "支付监控已结束", "expired"],
    abandoned: ["已放弃支付监控", "neutral"],
    unavailable: ["仅付款链接", "neutral"],
  };
  return labels[account.payment_status] || labels.unavailable;
}

function progressPercent(account) {
  if (account.status === "queued") return 5;
  if (account.status === "failed" || account.status === "success") return 100;
  const steps = account.steps || [];
  const activeIndex = steps.findIndex(step => step.state === "active");
  if (activeIndex < 0) return 12;
  return Math.max(12, Math.min(92, Math.round(((activeIndex + 1) / Math.max(1, steps.length)) * 90)));
}

function progressText(account) {
  if (account.queue_position) return `队列第 ${account.queue_position} 位`;
  const active = [...(account.steps || [])].reverse().find(step => step.state === "active");
  if (active?.label) return active.label;
  if (account.status === "canceling") return "正在停止任务";
  return account.attempts_used ? `已尝试 ${account.attempts_used} 次` : "准备提链";
}

function expiryText(timestamp) {
  if (!timestamp) return {text: "--:--", expired: false};
  const seconds = Math.max(0, Number(timestamp) - Math.floor(Date.now() / 1000));
  if (!seconds) return {text: "已过期", expired: true};
  const minutes = Math.floor(seconds / 60);
  return {text: `${minutes}:${String(seconds % 60).padStart(2, "0")}`, expired: false};
}

function renderWarnings(warnings = []) {
  const node = $("warnings");
  node.textContent = warnings.map(item => `第 ${item.line} 行：${item.error}`).join(" · ");
  node.classList.toggle("hidden", !warnings.length);
}

function renderProgress(accounts) {
  $("progressGrid").classList.toggle("hidden", !accounts.length);
  $("progressGrid").innerHTML = accounts.map(account => `
    <article class="progressCard">
      <div class="progressTop"><b>${esc(account.email || account.name || account.id)}</b><span>${esc(account.status === "queued" ? "排队中" : account.status === "canceling" ? "停止中" : "提链中")}</span></div>
      <progress class="progressTrack" max="100" value="${progressPercent(account)}"></progress>
      <div class="progressBottom"><span>${esc(progressText(account))}</span><b>${progressPercent(account)}%</b></div>
    </article>`).join("");
}

function renderResultCard(account) {
  if (account.status === "failed") {
    return `
      <article class="resultCard failure">
        <div class="failureHead"><b>${esc(account.email || account.name || account.id)}</b><span>提链失败</span></div>
        <div class="failureMeta">已尝试 ${Number(account.attempts_used) || 0} 次</div>
        <div class="failureMessage">${esc(account.error || "暂时无法确定失败原因，请更换账号或代理后重试")}</div>
      </article>`;
  }

  const payment = paymentLabel(account);
  const expiry = expiryText(account.expires_at);
  const canAbandon = accountHasActivePayment(account);
  const qr = account.qr_ready
    ? `<button class="qrThumb" type="button" data-open-qr="${esc(account.id)}" title="查看二维码"><img src="${esc(account.qr_url)}?v=${encodeURIComponent(account.qr_version)}" alt="GCash 付款二维码"><span>${esc(expiry.text)}</span></button>`
    : account.payment_status === "abandoned"
      ? `<div class="qrThumb stopped"><strong>×</strong><b>已放弃</b><span>${esc(expiry.text)}</span></div>`
      : `<div class="qrThumb pending"><i></i><b>获取中</b><span>${esc(expiry.text)}</span></div>`;
  return `
    <article class="resultCard success">
      ${qr}
      <div class="resultInfo">
        <div class="resultEmail">${esc(account.email || account.name || account.id)}</div>
        <div class="resultSub">二维码到期：${esc(account.expires_at ? formatExpiry(account.expires_at) : "等待支付页")}</div>
        <div class="resultStatusRow"><span class="linkReady">✓ 提链成功</span><span class="countdown ${expiry.expired ? "expired" : ""}" data-expiry="${Number(account.expires_at) || 0}">${esc(expiry.text)}</span></div>
        <div class="callbackState ${payment[1]}"><i></i><span>${esc(payment[0])}</span></div>
        <details class="rawLink"><summary>付款链接</summary><div>${esc(account.link || "")}</div></details>
        <div class="resultButtons">
          <button class="secondary" type="button" data-copy="${esc(account.id)}">复制付款链接</button>
          <button class="secondary" type="button" data-save="${esc(account.id)}" ${account.qr_ready && !expiry.expired ? "" : "disabled"}>保存二维码</button>
          ${account.refresh_available ? `<button class="refreshButton" type="button" data-refresh="${esc(account.id)}">刷新二维码</button>` : ""}
          ${canAbandon ? `<button class="abandonButton" type="button" data-abandon="${esc(account.id)}">放弃当前支付</button>` : ""}
        </div>
      </div>
    </article>`;
}

function renderJob() {
  const accounts = state.job?.accounts || [];
  const running = accounts.filter(account => ACTIVE_JOB_STATES.has(account.status));
  const finished = accounts.filter(account => !ACTIVE_JOB_STATES.has(account.status));
  const ready = accounts.filter(account => account.link_ready).length;
  const failed = accounts.filter(account => account.status === "failed").length;
  const paid = accounts.filter(account => account.payment_success).length;

  $("readyCount").textContent = ready;
  $("failedCount").textContent = failed;
  $("paidCount").textContent = paid;
  $("totalCount").textContent = accounts.length;
  $("resultStats").classList.toggle("hidden", !accounts.length);
  $("resultEmpty").classList.toggle("hidden", accounts.length > 0);
  renderWarnings(state.job?.warnings || []);
  renderProgress(running);
  $("resultGrid").innerHTML = finished.map(renderResultCard).join("");

  document.querySelectorAll("[data-copy]").forEach(button => {
    button.onclick = () => copyAccountLink(button.dataset.copy);
  });
  document.querySelectorAll("[data-open-qr]").forEach(button => {
    button.onclick = () => openQr(button.dataset.openQr);
  });
  document.querySelectorAll("[data-save]").forEach(button => {
    button.onclick = () => saveQr(button.dataset.save);
  });
  document.querySelectorAll("[data-refresh]").forEach(button => {
    button.onclick = () => refreshAccount(button.dataset.refresh, button);
  });
  document.querySelectorAll("[data-abandon]").forEach(button => {
    button.onclick = () => abandonAccount(button.dataset.abandon, button);
  });
  if (state.modalAccountId) updateModal();
  updateFlow();
}

function accountById(id) {
  return (state.job?.accounts || []).find(account => account.id === id);
}

async function copyText(value, successMessage) {
  try {
    await navigator.clipboard.writeText(value);
  } catch {
    const area = document.createElement("textarea");
    area.value = value;
    document.body.appendChild(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  }
  toast(successMessage);
}

function copyAccountLink(id) {
  const account = accountById(id);
  if (account?.link) copyText(account.link, "付款链接已复制");
}

function qrUrl(account) {
  return account?.qr_url ? `${account.qr_url}?v=${encodeURIComponent(account.qr_version)}` : "";
}

function saveQr(id) {
  const account = accountById(id);
  if (!account?.qr_ready) return toast("二维码尚未就绪");
  const link = document.createElement("a");
  link.href = qrUrl(account);
  link.download = `gcash-${account.id}.png`;
  document.body.appendChild(link);
  link.click();
  link.remove();
}

function openQr(id) {
  state.modalAccountId = id;
  $("qrModal").classList.remove("hidden");
  updateModal();
}

function closeQr() {
  state.modalAccountId = "";
  $("qrModal").classList.add("hidden");
  $("qrImage").removeAttribute("src");
  $("qrImage").dataset.version = "";
}

function updateModal() {
  const account = accountById(state.modalAccountId);
  if (!account) return closeQr();
  const payment = paymentLabel(account);
  $("qrEmail").textContent = account.email || account.name || account.id;
  $("qrState").textContent = `${payment[0]} · ${expiryText(account.expires_at).text}`;
  $("copyModalLink").disabled = !account.link;
  $("refreshQr").disabled = !account.refresh_available;
  $("downloadQr").disabled = !account.qr_ready;
  $("qrPending").classList.toggle("hidden", account.qr_ready);
  $("qrImage").classList.toggle("hidden", !account.qr_ready);
  if (account.qr_ready) {
    const version = `${account.id}:${account.qr_version}`;
    if ($("qrImage").dataset.version !== version) {
      $("qrImage").src = qrUrl(account);
      $("qrImage").dataset.version = version;
    }
  } else {
    $("qrImage").removeAttribute("src");
    $("qrImage").dataset.version = "";
  }
}

async function refreshAccount(id, button = null) {
  if (!state.job) return;
  const target = button || $("refreshQr");
  target.disabled = true;
  try {
    const updated = await api(`/api/jobs/${state.job.job_id}/accounts/${id}/refresh`, {
      method: "POST", body: "{}",
    });
    const index = state.job.accounts.findIndex(account => account.id === id);
    if (index >= 0) state.job.accounts[index] = updated;
    renderJob();
    schedulePolling();
    toast("支付页已刷新，正在获取二维码");
  } catch (error) {
    toast(error.message);
  } finally {
    target.disabled = false;
  }
}

async function abandonAccount(id, button = null) {
  if (state.abandoning || !state.job) return false;
  if (!window.confirm("放弃后将停止该账号的二维码与回调监控，已经发起或完成的支付不会撤销。")) return false;
  state.abandoning = true;
  if (button) button.disabled = true;
  updateFlow();
  try {
    const updated = await api(`/api/jobs/${state.job.job_id}/accounts/${id}/abandon`, {
      method: "POST", body: "{}",
    });
    const index = state.job.accounts.findIndex(account => account.id === id);
    if (index >= 0) state.job.accounts[index] = updated;
    if (state.modalAccountId === id) closeQr();
    renderJob();
    toast("已放弃当前支付监控");
    return true;
  } catch (error) {
    toast(error.message);
    return false;
  } finally {
    state.abandoning = false;
    if (button) button.disabled = false;
    updateFlow();
  }
}

async function abandonActivePayments() {
  if (!hasActivePayment()) return true;
  if (state.abandoning) return false;
  if (!window.confirm("开始下一任务前将停止当前二维码与回调监控。已发起或完成的支付不会撤销。")) return false;
  state.abandoning = true;
  clearTimeout(state.pollTimer);
  updateFlow();
  try {
    const result = await api(`/api/jobs/${state.job.job_id}/abandon`, {method: "POST", body: "{}"});
    state.job = result.job;
    renderJob();
    toast("当前支付监控已释放");
    return true;
  } catch (error) {
    toast(error.message);
    return false;
  } finally {
    state.abandoning = false;
    updateFlow();
  }
}

async function pollJob() {
  clearTimeout(state.pollTimer);
  state.pollTimer = null;
  if (!state.job?.job_id) return;
  try {
    state.job = await api(`/api/jobs/${state.job.job_id}`);
    renderJob();
    updateQueue(state.job.queue);
  } catch (error) {
    toast(error.message);
  }
  if (jobRunning() || hasActivePayment()) schedulePolling();
}

function schedulePolling(delay = 1500) {
  clearTimeout(state.pollTimer);
  if (!state.job?.job_id || (!jobRunning() && !hasActivePayment())) return;
  state.pollTimer = setTimeout(pollJob, delay);
}

async function startJob() {
  if (jobRunning() || state.abandoning) return;
  if (hasActivePayment() && !(await abandonActivePayments())) return;
  const selectedAccounts = state.accounts.filter(account => state.selected.has(account.id) && !account.expired);
  const proxies = proxyPool();
  if (!selectedAccounts.length) return toast("请选择有效账号");
  if (!proxies.length) return toast("请填写 PH 住宅代理池");
  if (proxies.length > state.maxProxies) return toast(`自备代理最多 ${state.maxProxies} 条`);

  $("startJob").disabled = true;
  try {
    state.job = await api("/api/jobs", {
      method: "POST",
      body: JSON.stringify({
        accounts: selectedAccounts.map(account => ({
          access_token: account.token,
          email: account.email,
          name: account.name,
        })),
        proxy_pool: proxies,
        max_attempts: Number($("maxAttempts").value) || 5,
      }),
    });
    renderJob();
    updateQueue(state.job.queue);
    schedulePolling(500);
    $("resultsSection").scrollIntoView({behavior: "smooth", block: "start"});
    toast("提链任务已提交");
  } catch (error) {
    toast(error.message);
  } finally {
    updateFlow();
  }
}

async function stopJob() {
  if (!state.job || state.stopping) return;
  state.stopping = true;
  $("stopJob").disabled = true;
  $("stopJob").textContent = "正在停止";
  try {
    const result = await api(`/api/jobs/${state.job.job_id}/cancel`, {method: "POST", body: "{}"});
    state.job = result.job;
    renderJob();
    schedulePolling(500);
    toast("停止请求已提交");
  } catch (error) {
    toast(error.message);
  } finally {
    state.stopping = false;
    $("stopJob").disabled = false;
    $("stopJob").textContent = "停止任务";
  }
}

async function clearResults() {
  if (jobRunning()) return toast("任务运行中，请先停止任务");
  if (hasActivePayment() && !(await abandonActivePayments())) return;
  clearTimeout(state.pollTimer);
  state.pollTimer = null;
  state.job = null;
  closeQr();
  renderJob();
  toast("结果已清空");
}

function updateQueue(queue = {}) {
  const node = $("queueStatus");
  node.className = "queueState online";
  node.querySelector("span").textContent = `运行 ${Number(queue.running) || 0} · 排队 ${Number(queue.queued) || 0}`;
}

async function health() {
  try {
    const result = await api("/api/health");
    state.maxAccounts = Math.max(1, Number(result.limits?.max_accounts) || 50);
    updateQueue(result.queue);
    updateInputCounts();
  } catch {
    const node = $("queueStatus");
    node.className = "queueState offline";
    node.querySelector("span").textContent = "服务未连接";
  }
}

$("tokenInput").oninput = updateInputCounts;
$("proxyInput").oninput = updateInputCounts;
$("maxAttempts").onchange = updateFlow;
$("importTokenBtn").onclick = parseAccounts;
$("clearToken").onclick = () => {
  $("tokenInput").value = "";
  $("importStatus").textContent = "等待导入";
  updateInputCounts();
};
$("txtFile").onchange = async event => {
  const file = event.target.files?.[0];
  if (!file) return;
  $("tokenInput").value = await file.text();
  event.target.value = "";
  updateInputCounts();
};
$("search").oninput = renderAccounts;
$("stateFilter").onchange = renderAccounts;
$("selectVisible").onclick = () => {
  const visible = visibleAccounts().filter(account => !account.expired);
  const allSelected = visible.length && visible.every(account => state.selected.has(account.id));
  visible.forEach(account => allSelected ? state.selected.delete(account.id) : state.selected.add(account.id));
  renderAccounts();
};
$("deleteSelected").onclick = () => {
  if (!state.selected.size) return toast("请先选择账号");
  state.accounts = state.accounts.filter(account => !state.selected.has(account.id));
  state.selected.clear();
  state.accounts.filter(account => !account.expired).forEach(account => state.selected.add(account.id));
  renderAccounts();
};
$("backImport").onclick = () => goStep(1);
$("startJob").onclick = startJob;
$("stopJob").onclick = stopJob;
$("clearResults").onclick = clearResults;
$("closeQr").onclick = closeQr;
$("qrModal").onclick = event => { if (event.target === $("qrModal")) closeQr(); };
$("copyModalLink").onclick = () => copyAccountLink(state.modalAccountId);
$("downloadQr").onclick = () => saveQr(state.modalAccountId);
$("refreshQr").onclick = () => refreshAccount(state.modalAccountId);
document.querySelectorAll(".step").forEach(button => {
  button.onclick = () => goStep(Number(button.dataset.step));
});
document.addEventListener("keydown", event => { if (event.key === "Escape") closeQr(); });
setInterval(() => {
  document.querySelectorAll("[data-expiry]").forEach(node => {
    const value = expiryText(Number(node.dataset.expiry));
    node.textContent = value.text;
    node.classList.toggle("expired", value.expired);
  });
  if (state.modalAccountId) updateModal();
}, 1000);

updateInputCounts();
renderAccounts();
renderJob();
health();
setInterval(health, 10000);
