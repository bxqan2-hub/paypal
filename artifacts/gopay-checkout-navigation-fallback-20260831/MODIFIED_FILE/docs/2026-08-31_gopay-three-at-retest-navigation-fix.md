# GoPay 三 AT 复测：Checkout 浏览器刷新超时修复

## 结论

本轮安全载入了用户提供的 3 个有效 AT 和 10 条 ID 代理。三个 AT 使用代理槽位 1
执行独立优惠资格 GET，均返回 `HTTP 200 / state=eligible`，资格阶段没有创建
Checkout。

当前 CDP `127.0.0.1:61908` 浏览器具有 NextAuth `.0/.1` 和长度 291 的实时
attestation，但 cookie-only `/backend-api/me` 与三个 AT 的匹配数为 0。因此本轮没有
跨账号复用该浏览器的 NextAuth，只向 AT-bound 实验路径提供实时 attestation。

首次完整实验暴露出一个独立于 `approve=blocked` 的真实缺陷：ChatGPT Checkout 已
成功创建后，GoPay core 在 `checkout_browser_refresh` 导航 Checkout 页面时等待
`domcontentloaded` 超时，流程在进入 promotion/Stripe 前结束。本轮已经修复这个
超时路径；后续 AT 槽位 2 复验能够完成 promotion、Stripe init、Elements、taxes、
Stripe confirm 和完整 approval proof，最终 ChatGPT approve 返回业务结果
`blocked`。

因此本轮修复结果分成两层：

1. **已修复**：AT-only Checkout 页面导航超时不再终止已创建的唯一 Checkout；同一
   Page、同一 browser runtime 通过 `window.stop()` 和 `history.replaceState()` 继续
   Sentinel init，没有重发 Checkout。
2. **仍然存在的最终差异**：AT 槽位 2 到达 approve 时具有真实 Chrome 151、持久
   runtime/profile、正确 SDK、291 字符 attestation、approval token、空 pending
   envelope，但 `session_cookie_binding_state=not_present`，Cookie 中没有 NextAuth
   session-token。最终仍为 `approve_blocked`。

## 输入与保护边界

- AT：3 个；JWT、稳定 user id、account id 和 email claim 均可解析。
- 代理：10 个；原始值仅保存在 Git 忽略的 runtime 文件。
- 资格 GET：三个 AT 在代理槽位 1 均为 eligible。
- 当前浏览器：NextAuth 分片 2 个，attestation 长度 291，cookie-only `/me` 为 200，
  但与三个 AT 均不匹配。
- 本报告不记录 AT、Cookie 值、代理凭据、账号标识、Checkout ID、邮箱或跳转 nonce。

## 实验过程

### AT 槽位 1：定位刷新超时

阶段：

```text
eligibility_check
→ eligibility_confirmed
→ checkout
→ checkout_committed
→ checkout_kind:stripe_checkout
→ checkout_browser_refresh
→ TimeoutError
```

该错误发生于唯一 Checkout 已返回之后的 Playwright `page.goto(checkout_url)`，不是
Checkout POST 自身，也没有到 Stripe confirm 或最终 approve。

### 修复 1：Checkout 导航超时同页继续

修改 `payment_link_extractor/gopay_sentinel_playwright.py`：

- Checkout 导航超时默认上限从固定 90 秒改为可配置、边界为 5–90 秒。
- 只捕获 Playwright navigation timeout，不吞 TLS、页面关闭等其他异常。
- 仅 AT-only browser session 可以 fallback；cookie-backed session 仍严格失败关闭。
- fallback 前确认 page 仍位于 `https://chatgpt.com`。
- 在同一 page 调用 `window.stop()`，随后恢复精确 Checkout history URL。
- 继续 SDK install、attestation 捕获、Sentinel init；不创建新 page、不重发 Checkout。
- runtime 记录 `checkout_navigation_fallback=true` 和错误类别 `timeout`。

### 修复 2：AT-only 绑定探针不再悬挂

- 没有 NextAuth 时，cookie-only `/me` 探针直接返回 `not_present`，不再发无意义请求。
- cookie-backed `/me` 加 10 秒 AbortController，并保持同 runtime 有界重试。
- Bearer 自检在 AT-only 模式保留 Cloudflare/browser Cookie，并显式带 account header；
  cookie-backed 模式继续 `credentials=omit`，防止 NextAuth 掩盖 Bearer 结果。

### AT 槽位 2：修复后完整复验

阶段：

```text
eligibility_check
→ eligibility_confirmed
→ checkout
→ checkout_committed
→ checkout_kind:stripe_checkout
→ checkout_browser_refresh
→ checkout_update
→ promotion_applied
→ stripe_init
→ elements_session
→ taxes
→ payment_confirmation
→ approve_blocked
```

最终脱敏上下文：

```text
browser_channel=chrome
browser_version=151.0.7922.170
persistent_runtime=true
persistent_profile=true
account_binding_verified=true
session_cookie_binding_verified=false
session_cookie_binding_state=not_present
session_cookie_source=none
checkout_navigation_fallback=true
checkout_navigation_error=timeout
sdk_sha256=49d0284bf3eea8a59ebcad0e6b5dd8a53edd4c72606f15bbf51ebe5610a88efd
approval_token_length=3910
attestation_length=291
pending_updates_length=20
```

approval 的 `prepare_events` 和 `token_events` 都包含正确的 zero-body Sentinel ping；
最终 Cookie 有 Cloudflare、OAI 和 Stripe 状态，但没有 NextAuth session-token。

## 浏览器级抓包

当前外部 CDP 浏览器抓包摘要已保存：

- `C:\Users\Administrator\Desktop\提链\artifacts-local\gopay-captures\gopay-cdp-capture-20260831-153331.summary.md`
- `C:\Users\Administrator\Desktop\提链\artifacts-local\gopay-captures\gopay-cdp-capture-20260831-153331.gopay.md`

```text
CAPTURE_ENTRIES=27
CAPTURE_SHA256=68338279FCB0BB75E76A9ACF6D1419D47720BC0C0662614316110215D51064C5
CAPTURE_API_STRIPE_ENTRIES=0
CAPTURE_COMPLETENESS=partial
```

该 recorder 连接的是外部 `61908` 浏览器；本次 canary 的 ChatGPT/Stripe mutation
由 curl session 和独立 Playwright runtime 发出，因此该 HAR 只覆盖外部页面背景流量，
未标记为 GoPay canonical。原始 partial HAR 在记录 SHA-256 后已删除，只保留脱敏摘要；
完整故障证据来自 canary 阶段和 provider safe context。

## 验证

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
```

结果：

```text
275 passed in 1.71s
FULL_TEST_EXIT=0
```

新增测试覆盖：

- AT-only Checkout navigation timeout 只调用一次 goto 并在同一 page fallback。
- timeout 后先 `window.stop()`，再修复 history URL。
- cookie-backed navigation timeout 保持严格失败。
- timeout 配置上下界。
- 无 NextAuth 时不发 cookie binding 网络请求。
- provider 传播 navigation fallback/error 诊断。

## 最终判断

本轮已经把流程从 `checkout_browser_refresh TimeoutError` 修到完整
`payment_confirmation → approve`。最终 `blocked` 继续与缺少同账号 NextAuth 登录态
一致；attestation、真实浏览器、Sentinel proof、Stripe 链路和导航 timeout 已不再是
本次最终阻塞点。
