# MoMo 收尾改造工单 v3

仓库根：`C:\Users\Administrator\Desktop\提链`　解释器：`C:/Python314/python.exe`

## 执行规则（必须遵守）

1. 只做本文件列出的步骤。未列出的文件一个字都不许动。
2. 全部改动按**锚点文本**定位，不按行号。锚点原样出现在下面代码块里。
3. 锚点找不到、或匹配到多处 → **停下，记进日志，跳过该步**。不许猜，不许改相邻代码凑。
4. 不许顺手格式化、重排 import、重命名、补类型注解、加注释。
5. 全部做完后，按文末《交付日志》模板产出 `docs/momo-cleanup-log.md`，命令输出原样粘贴，不许总结、不许截断。

顺序：S1 → S37。S22 起改 `transport.py`，每完成 4 步跑一次 `python -m pytest tests/ -q`。

---

# A 组 · 适配器三跳收成一跳

**S1** 删除文件 `payment_link_extractor/momo_channel.py`

**S2** 删除文件 `payment_link_extractor/momo/momo_channel.py`

**S3** `payment_link_extractor/momo/__init__.py`

```python
from .momo_channel import extract_momo_payment_link
from .momo_core import MOMO_RESULT_FIELD
```
→
```python
from .momo_core import MOMO_RESULT_FIELD, extract_momo_payment_link
```

**S4** `payment_link_extractor/channels.py`

```python
        adapter_module="payment_link_extractor.momo_channel",
```
→
```python
        adapter_module="payment_link_extractor.momo",
```

**S5** `tests/test_momo_support.py`

```python
    assert channel.adapter_module == "payment_link_extractor.momo_channel"
```
→
```python
    assert channel.adapter_module == "payment_link_extractor.momo"
```

---

# B 组 · 删掉 lean 包已不读的 3 个字段

依据：`momo/` 全包 grep `momo_fingerprint` / `momo_zero_trial_validation` / `momo_trial_eligibility_check` / `session_token` = 0 命中。无测试引用。

**S6** `payment_link_extractor/models.py` 删除：

```python
    # Momo-only zero-amount gate, kept separate from GoPay state.
    momo_zero_trial_validation: bool = True
    # Momo eligibility is checked before Checkout and may rotate the VN proxy pool.
    momo_trial_eligibility_check: bool = True
    # Runtime browser identity selected independently for each Momo attempt.
    momo_fingerprint: str = ""
```

**S7** `payment_link_extractor/web/routes.py` 删除：

```python
    momo_zero_trial_validation = payload.get(
        "momo_zero_trial_validation",
        _env_bool("OPLL_MOMO_ZERO_TRIAL_VALIDATION", True),
    )
    momo_trial_eligibility_check = payload.get(
        "momo_trial_eligibility_check",
        _env_bool("OPLL_MOMO_TRIAL_ELIGIBILITY_CHECK", True),
    )
    momo_fingerprint = str(
        payload.get(
            "momo_fingerprint",
            os.getenv("OPLL_MOMO_FINGERPRINT", ""),
        )
        or ""
    ).strip()
```

**S8** 同文件 删除：

```python
    if not isinstance(momo_zero_trial_validation, bool):
        raise ConfigurationError("momo_zero_trial_validation must be boolean")
    if not isinstance(momo_trial_eligibility_check, bool):
        raise ConfigurationError("momo_trial_eligibility_check must be boolean")
```

**S9** 同文件 删除：

```python
        momo_zero_trial_validation=momo_zero_trial_validation,
        momo_trial_eligibility_check=momo_trial_eligibility_check,
        momo_fingerprint=momo_fingerprint,
```

**S10** 同文件

```python
        session_token=(
            extract_session_token(payload)
            or (
                os.getenv("OPLL_MOMO_SESSION_TOKEN", "").strip()
                if payment_method == "momo"
                else ""
            )
        ),
```
→
```python
        session_token=extract_session_token(payload),
```

**S11** `payment_link_extractor/cli.py` 删除：

```python
    parser.add_argument(
        "--momo-fingerprint",
        default=os.getenv("OPLL_MOMO_FINGERPRINT", ""),
        help="optional supported MoMo browser profile; empty rotates profiles per attempt",
    )
    parser.add_argument(
        "--momo-session-token",
        default=os.getenv("OPLL_MOMO_SESSION_TOKEN", ""),
        help="optional runtime NextAuth session token for the MoMo browser context",
    )
```

**S12** 同文件 删除：

```python
                momo_fingerprint=args.momo_fingerprint,
                session_token=args.momo_session_token,
```

**S13** `payment_link_extractor/web/static/app.js` 删除：

```javascript
    const momoZeroTrialField = byId("momo-zero-trial-field");
    const momoFingerprintField = byId("momo-fingerprint-field");
```

**S14** 同文件 删除：

```javascript
    if (momoZeroTrialField) momoZeroTrialField.hidden = !isMomo;
    if (momoFingerprintField) momoFingerprintField.hidden = !isMomo;
```

**S15** 同文件 删除：

```javascript
    if (paymentMethod === "momo") {
      result.momo_zero_trial_validation = byId("momo-zero-trial-validation").checked;
      result.momo_fingerprint = byId("momo-fingerprint")?.value || "";
    }
```

**S16** `payment_link_extractor/web/templates/index.html` 删除从 `<section id="momo-zero-trial-field"` 起、到 `<section id="momo-fingerprint-field"` 那一节的 `</section>` 止的连续 20 行（两个 section 全删）。其前的 `#gopay-zero-trial-field`、其后的 `<section class="mk-proxy-panel">` 必须保留。

---

# C 组 · `.env.example`

lean 包实际读的 momo 相关变量全集只有 7 个 `OPLL_SENTINEL_*` / `OPLL_VN_BILLING_FILE`。

**S17** 删除：

```
# MoMo VN HAR keeps the payment-phase envelope empty by default; enable only
# when a newer capture demonstrates receipts on taxes/confirm.
OPLL_MOMO_ECHO_PAYMENT_PENDING_UPDATES=false
```

**S18** 删除从 `OPLL_MOMO_ZERO_TRIAL_VALIDATION=true` 起、到 `OPLL_MOMO_GATEWAY_POLLS=15` 止的连续 22 行（含其间全部注释），**但保留 `OPLL_MOMO_STRIPE_HCAPTCHA_TOKEN=` 那一行**。

**S19** 在 `OPLL_MOMO_STRIPE_HCAPTCHA_TOKEN=` 之后插入：

```
# MoMo Sentinel：证明必须由真实 Chrome 146 生成
OPLL_SENTINEL_BROWSER_EXECUTABLE=
OPLL_SENTINEL_PYTHON=
OPLL_SENTINEL_MAX_ATTEMPTS=3
OPLL_SENTINEL_RETRY_DELAY=1
OPLL_VN_BILLING_FILE=
```

`OPLL_SENTINEL_BROWSER=auto` / `OPLL_SENTINEL_HEADLESS=true` / `OPLL_GOPAY_SENTINEL_*` 一段属于 GoPay，不许动。

---

# D 组 · 文档

**S20** 删除 `docs/2026-09-02_momo-comparison-report.md` 和 `docs/2026-09-02_momo-comparison-report.diff`

**S21** `docs/MOMO_HAR_STATE_MACHINE.md` 把这两行

```
- `momo_zero_trial_validation` 只控制金额闸门：开启时 taxes 后要求 VND payable minor units 为 0，关闭时跳过该金额判断；资格预检仍保持在 Checkout 之前。
```
```
- `momo_fingerprint` 为空时只在受支持且 UA/TLS 成对一致的 Chrome 136/145/146/150 profile 中随机选择；一次完整尝试内固定同一 profile，重试才重新选择。
```

替换为

```
- 金额闸门固定生效，无开关：taxes 刷新后由 `momo/_flow.py` 断言 VND payable minor units 为 0。
- 浏览器身份固定 Chrome 146，无 profile 轮换：curl_cffi 指纹、UA/client hints、生成 Sentinel 证明的浏览器三者同版本。
```

---

# E 组 · `transport.py` 的 `enhanced` 死分支

以下全部在 `payment_link_extractor/transport.py`，类 `BrowserSentinelProvider` 内。
依据：该类唯一构造点在 `DefaultTransportFactory.chatgpt` 的 GCash 分支，不传 `enhanced`（默认 `False`）；全仓库无 `enhanced=True`。

**S22** 构造器签名删除：

```python
        # ``enhanced`` is deliberately opt-in.  Existing GCash callers retain
        # the historical lightweight browser bootstrap; Momo enables the
        # same-origin SDK/cookie/telemetry bridge explicitly.
        enhanced: bool = False,
        session_token: str = "",
```

**S23** 同签名删除：

```python
        timezone: str = "Asia/Manila",
```

**S24** 构造器体删除（连续 3 行）：

```python
        self.enhanced = bool(enhanced)
        self.session_token = str(session_token or "").strip()
        self.timezone = str(timezone or "Asia/Manila").strip() or "Asia/Manila"
```

**S25** 构造器体删除（连续 16 行）：

```python
        # The HTTP side uses a curl_cffi-supported profile by default. Keep the
        # browser's native UA override disabled unless an operator explicitly
        # chooses the installed Chrome engine for an A/B run.
        self.native_browser_ua = bool(
            self.enhanced
            and os.getenv("OPLL_MOMO_NATIVE_BROWSER_UA", "0").strip().lower()
            not in {"0", "false", "off", "no"}
        )
        self.browser_headed = bool(
            self.enhanced
            and os.getenv("OPLL_MOMO_BROWSER_HEADED", "1").strip().lower()
            not in {"0", "false", "off", "no"}
        )
        self.browser_profile = str(
            os.getenv("OPLL_MOMO_BROWSER_PROFILE_DIR", "") if self.enhanced else ""
        ).strip()
```

**S26** 构造器体删除：

```python
        self.sentinel_init_script: Path | None = None
```

**S27** 构造器体删除从

```python
        if self.enhanced and not self.native_browser_ua:
```

起、到

```python
            self.sentinel_init_script.write_text(
                self._build_sentinel_init_script(), encoding="utf-8"
            )
```

止的整段（两个 `if` 块，共 27 行）。
⚠️ 这段**之前**那句 `self.locale_script.write_text(` 是 GCash 在用的，必须保留。

**S28** 删除整个方法，从

```python
    @staticmethod
    def _build_sentinel_init_script() -> str:
```

到

```python
            "})();\n"
        )
```

止（下一行是 `    @property`）。

**S29** `_base_command` 内

```python
        if not bool(getattr(self, "native_browser_ua", False)):
            command.extend(["--user-agent", self.user_agent])
        if bool(getattr(self, "browser_headed", False)):
            command.append("--headed")
        # agent-browser applies navigation-scoped init scripts only when the
        # flags follow the open URL.  The enhanced Momo path appends them via
        # _enhanced_open_args(); the legacy path keeps its original command.
        if not self.enhanced:
            command.extend(["--init-script", str(self.locale_script)])
        if self.enhanced and self.browser_profile:
            command.extend(["--profile", self.browser_profile])
```
→
```python
        command.extend(["--user-agent", self.user_agent])
        command.extend(["--init-script", str(self.locale_script)])
```

**S30** 删除整个方法，从 `    def _enhanced_open_args(` 到其结尾 `        return args`（下一行是 `    def _run(`）。

**S31** `_run` 内

```python
        # Chromium uses TZ when constructing the browser fingerprint.  The
        # enhanced Momo context must override a stale process-level timezone;
        # the legacy GCash path keeps its historical default semantics.
        if self.enhanced:
            env["TZ"] = self.timezone
        else:
            env.setdefault("TZ", "Asia/Manila")
```
→
```python
        # Chromium uses TZ when constructing the browser fingerprint.
        env.setdefault("TZ", "Asia/Manila")
```

**S32** ⚠️ `_capture_bootstrap` **不是整个删**。只删从

```python
        if self._attestation or not self.enhanced:
            return
```

起、到该方法结尾（以 `                self._attestation = attestation` + `                return` 结束，下一行是 `    def _sync_pending_update_from_browser`）止的整段。
方法前半段（`self._eval("(() => {" ... "client-bootstrap" ...)` 到 `                self._attestation = attestation`）**必须保留**，GCash 的 `_start` 在调它。

**S33** 删除整个方法 `    def _sync_pending_update_from_browser(self) -> None:`（首行判据 `if not self.enhanced: return`），到下一行是 `    def _set_cookie(` 为止。

**S34** 删除整个方法 `    def _set_cookie(self, name: str, value: str, *, http_only: bool = True) -> None:`，到下一行是 `    def _sync_cookies(` 为止。

**S35** ⚠️ `_sync_cookies` **不是整个删**。`if not self.enhanced:` 里那段（带注释 `Preserve the original lightweight GCash behavior byte-for-byte.`）就是 GCash 正式路径。做法：

1. 删掉 `        if not self.enhanced:` 这一行；
2. 把原属于该 `if` 的块整体左移一级缩进；
3. 删掉该块末尾的 `            return`（左移后为 `        return`）；
4. 删掉方法**剩余全部内容**——从第二次出现的 `        value = self._run(["cookies", "get", "--json"])` 到方法结尾（含 `momo_cookie_allowlist`、`momo_cookie_jar_mode` 整段）。

改完 `_sync_cookies` 应为：读 cookie → 拼 `name=value` → 写 `self._cookies` 与 `headers["Cookie"]`，共约 20 行，方法内不含 `enhanced` / `momo` 字样。

**S36** 删除整个方法 `    def _start_enhanced(self) -> None:`，到下一行是 `    def _start(self) -> None:` 为止。

**S37** 其余 5 处，逐条改：

`_start` 开头删除：
```python
        if self.enhanced:
            self._start_enhanced()
            return
```

`_ping` 内删除 `        if self.enhanced:` 那一整块（内含 `window.__opllSentinelReferer`，以块内 `            return` 结束），保留其后的通用 `expression = (`。

删除整个方法 `    def prepare(self) -> None:`（全仓库零调用者）。

删除整个方法 `    def prepare_flow(self, *, flow: str, referer: str) -> None:`（全仓库零调用者），以及夹在两者之间的 `    @staticmethod` + `    def _normalize_flow(flow: str) -> str:` 整个方法。

删除整个方法 `    def _store_ping_telemetry(` 和 `    def _captured_ping_telemetry(`。

`headers` 内删除 `            if self.enhanced:` 那一整块（从块内 `selected_flow = self._normalize_flow(flow)` 到块内 `                return result`）。其后的 `            self._ping(referer)` 及之后全部保留。

---

# 不许动（顺手删这些会打断别的通道）

- `web/tasks.py` 的 `STAGE_PROGRESS` 里 `eligibility_*` / `zero_amount_*`：GoPay、PayPal、GCash 仍在发
- `web/static/app.js` 对应的中文 stage 文案：同上
- `models.py` 的 `session_token` 字段：GoPay 在用
- `config.py` 的两行 `VN`：`country_config("VN")` 仍走这里
- `channels.py` 的 `uses_checkout_update=False`：momo 单代理池靠它成立
- `transport.py`：`_capture_bootstrap` 前半段、`_sync_cookies` 的 GCash 分支、`self.locale_script.write_text(` 首次调用

---

# 交付日志

全部做完后新建 `docs/momo-cleanup-log.md`，严格按下列结构填写。命令在仓库根执行，输出**原样粘贴**。

## 1. 步骤表

逐行给出 S1–S37，格式 `S<n> <DONE|SKIP> <若 SKIP 写明锚点未命中还是命中多处>`。不许省略、不许写"同上"。

## 2. 行数对照

| 文件 | 改前 | 改后 | 预期改后 |
|---|---|---|---|
| `payment_link_extractor/transport.py` | 1514 | ? | 930 ~ 960 |
| `payment_link_extractor/web/routes.py` | 486 | ? | 457 |
| `payment_link_extractor/web/static/app.js` | 2002 | ? | 1994 |
| `payment_link_extractor/web/templates/index.html` | 263 | ? | 243 |
| `payment_link_extractor/cli.py` | 120 | ? | 108 |
| `payment_link_extractor/models.py` | 109 | ? | 103 |
| `.env.example` | 96 | ? | 78 |

超出预期即为删多或删少，在日志里标出并说明。

## 3. 命令输出（原样粘贴）

```bash
python -m compileall -q payment_link_extractor/
```

```bash
grep -rn "momo_channel\|momo_fingerprint\|momo_zero_trial_validation\|momo_trial_eligibility_check\|OPLL_MOMO_SESSION_TOKEN" --include=*.py --include=*.js --include=*.html --include=*.example . | grep -v __pycache__ | grep -v "^./artifacts" | grep -v "^./docs"
```
必须为空。

```bash
grep -n "enhanced" payment_link_extractor/transport.py
```
必须为空。

```bash
grep -n -i "momo" payment_link_extractor/transport.py
```
只允许剩 1 条：文件开头 `# client.  Keep this value in the transport layer so the optional Momo browser` 那条注释。

```bash
python -c "from payment_link_extractor.channels import payment_channel, invoke_payment_channel; c=payment_channel('momo'); print(c.adapter_module); import importlib; m=importlib.import_module(c.adapter_module); print(callable(getattr(m,c.adapter_callable)))"
```
应输出 `payment_link_extractor.momo` 和 `True`。

```bash
python -m pytest tests/ -q
```
最后 5 行原样贴出。基线为 **268 passed**，数字必须一致。

```bash
git status --short
```

```bash
git diff --stat
```

## 4. 未完成 / 存疑

列出所有 SKIP 步骤、所有与预期行数不符的文件、以及任何你判断"改了但不确定对"的位置，各写一句话。没有就写"无"。

---

# 附：前置条件（不是代码改动，不计入日志步骤）

`momo/_sentinel_runner.py` 把真实启动的浏览器版本 `browser.version` 送进版本闸门，Playwright 自带 Chromium 是 138，仓库无 `payment_link_extractor/runtime/` —— 不装 146 内核，momo 一次都跑不起来。

```bash
npx @puppeteer/browsers install chrome-headless-shell@146
```

装完设 `OPLL_SENTINEL_BROWSER_EXECUTABLE=<chrome-headless-shell 路径>`，或把目录放进 `payment_link_extractor/runtime/chrome-146.../`。
验证：`python -c "from payment_link_extractor.momo import _sentinel_runner as r; print(r.BROWSER_EXECUTABLE)"` 打印非空路径。
