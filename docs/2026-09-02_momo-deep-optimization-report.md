# MoMo VN 深度提链优化报告

## 摘要

本轮以本机完整 MoMo HAR 为事实基准，并对照看雪 Stripe Custom/Hosted 协议分析，修正了 OAICS Custom 路线的请求顺序、会话连续性、Stripe 参数、Sentinel 生命周期和 MoMo 网关状态机。运行时仍严格区分 MoMo 与 PayPal、GoPay、GCash。

看雪文章明确指出：HAR 中真实浏览器发出的字段、顺序、编码和时序优先于旧文档；`oaics_*` Custom 路线使用 Elements、ConfirmationToken、平台 Confirm 和 Stripe Intent Confirm，且 Custom 使用短 `_stripe_version`、双层 attribution、42 字符指纹和完整 Cookie 隔离。

## Evidence → Finding → Path

| Evidence | Finding | Path |
|---|---|---|
| HAR 1217 entries；审计关键节点完整 | 真实路线是 `accounts/check → promo check → checkout → Sentinel approval prefetch → Elements → taxes×3 → confirmation_tokens → checkout/confirm → setup_intents/confirm → pm-redirects → MoMo gateway → querySession` | `artifacts-local/momo-roxy-mitm-20260902-023932.har` |
| Checkout body 含 `promo_campaign`，税费地址逐步变化 | 初始优惠和三次税费请求必须保持同一 OAICS session | `payment_link_extractor/momo_checkout.py` |
| Checkout、taxes、confirm 共用 device/session；taxes/confirm 才出现 account header | 资格探测不能关闭后重新创建 ChatGPT session | `payment_link_extractor/momo_eligibility.py`, `momo_core.py`, `momo_transport.py` |
| Elements 先于 taxes；SetupIntent Confirm 使用短版 Stripe 版本和两项 attribution | 原先的调用顺序、版本和字段集合会改变 Custom 路线 | `payment_link_extractor/momo_core.py`, `momo_stripe.py` |
| `__stripe_mid/__stripe_sid` 在 taxes Cookie 与 ConfirmationToken 的 `muid/sid` 相同 | Stripe 浏览器 ID 必须跨 ChatGPT/Stripe session 同步 | `payment_link_extractor/momo_stripe.py` |
| gateway HTML 使用 `<meta name="_csrf">`；querySession 空 body、约 4.25 秒轮询 | 需要独立的文档导航头、XHR 头、CSRF 提取和终态闸门 | `payment_link_extractor/momo_transport.py`, `momo_core.py` |
| AT-only 实测 Confirm 返回 `{status: blocked}`；HAR 有 NextAuth cookie 分片、attestation、hCaptcha token | 目前阻塞点是登录态/风控上下文输入缺失，不是 0 元金额或 OAICS 顺序 | `VERIFICATION.txt` 与本报告“真实测试” |

## 高/中/低概率原因

### 高概率

1. **登录态上下文不完整**：新 AT 是有效的 RS256 JWT，但没有随附的 `__Secure-next-auth.session-token(.0/.1)`。成功 HAR 含该 cookie 分片和 291 字符 deployment attestation；AT-only 浏览器上下文没有它们。
2. **缺少当前 Stripe hCaptcha token**：成功 HAR 的 ConfirmationToken 请求含 `payment_method_data[radar_options][hcaptcha_token]`；新 AT 测试中该字段为空。代码只接受参数、运行时环境或同一 Elements 响应中的新 token，不重放 HAR 值。
3. **Sentinel 证明必须同一上下文**：已改为资格阶段预热、Checkout approval 阶段预取，并在后续请求复用同一 browser/session/proxy。

### 中概率

- 过期/错误的客户端构建号、locale、Stripe Origin、Accept-Language、telemetry。
- `muid/sid` 与 Stripe Cookie 不一致。
- querySession 过早轮询、CSRF meta 名称未识别、把未终态网关状态误当成功。
- `pm-redirects` 自动跟随造成 Stripe session 提前请求 MoMo 页面。

### 低概率

- Chrome 具体小版本本身；当前默认在 136/145/146/150 中随机，但单次尝试内固定一个 profile。
- Elements 请求参数顺序；已按 HAR 将 customer session secret 放到查询串前部，但服务端通常按语义解析。

## 已落地的路线优化

- 初始 Checkout 固定携带 VN trial campaign，Referer 与 HAR 一致。
- `Elements` 移到三次 taxes 之前；taxes 使用空 state/postal → state → 完整 postal 的渐进地址，保留无 `line2`、无 `tax_id` 的 HAR body 形状。
- 资格探测可保留 ChatGPT session；账号 header 只在 taxes/confirm 路由注入。
- Stripe Custom Intent Confirm 改用 `2025-03-31.basil`，只发送 `client_session_id` 和 `merchant_integration_source=l1` 两项顶层 attribution。
- `muid/sid` 与 ChatGPT、Stripe 两侧 Cookie 同步；`guid` 保持独立的新 42 字符值；`time_on_page` 默认约 20 秒并可运行时覆盖。
- 新增 MoMo 专属增强 Sentinel：真实 ChatGPT origin、SDK init script、VN timezone、approval `prepare_flow`、Cookie/receipt/attestation 同步、动态八元 telemetry。
- 浏览器启动避免 about:blank 抢占活动 tab；导航 init script 放在 URL 后，避免 agent-browser 把 SDK 注入到错误页面。
- Gateway GET 使用文档导航头；querySession 使用空 body、`Accept: */*`、CSRF 和 4.25 秒默认间隔；只有 `status_code=9000 && redirect=true` 才产生成功结果。
- UI 选择 MoMo 时固定显示 VN，并可选择或随机轮换支持的浏览器 profile。
- 支持从导入 JSON/Cookie header 重组 NextAuth session-token 分片；也支持 `OPLL_MOMO_SESSION_TOKEN`、`OPLL_MOMO_BROWSER_PROFILE_DIR`、`OPLL_MOMO_STRIPE_HCAPTCHA_TOKEN` 运行时注入。

## 真实测试

新附件包含 4 个 JWT AT，均为 3 段 RS256、`free` 计划、MFA required、有效期至 2026-09-12；四个账号的只读资格探测均返回 HTTP 200 / `eligible`。

在 `momo_zero_trial_validation=True` 下执行的完整尝试（前 3 次用于逐步校验，最后一次使用当前全部优化）：

| AT 槽位 | 结果 | 已观察阶段 |
|---:|---|---|
| 1 | `409 status=blocked` | eligibility、Sentinel prepare、checkout、Elements、taxes×3、zero amount confirmed、approval proof、confirm |
| 2 | `409 status=blocked` | 同上 |
| 3 | `409 status=blocked` | 同上 |
| 4 | `409 status=blocked` | 当前优化；诊断为 `hcaptcha=absent`, `hcaptcha_site_key=present`, `nextauth_cookie=absent`, `attestation=absent` |

四次都通过了 0 元金额闸门，均没有产生可确认的 `momo_url`。这证明本轮金额、优惠、顺序、Cookie ID、Sentinel 生命周期和网关终态修复已生效；剩余失败来自当前测试输入没有成功 HAR 中的登录态/挑战材料。

## 验证

```powershell
C:\Python314\python.exe -m compileall -q payment_link_extractor tools
C:\Python314\python.exe -m pytest -q
```

当前结果：`296 passed`，编译通过，`git diff --check` 通过。

HAR 审计结果：

- SHA-256：`FF4DA11170C0F47C33BB19610246E22A6478682A9D059F21F5ED6D31D76B01F6`
- `HAR_COMPLETE=True`
- `HAR_CRITICAL_COMPLETE=True`
- `HAR_ISSUES=[]`

## 运行时输入

要复现成功 HAR 的上下文，运行时提供以下任一组合：

- 已登录的 `OPLL_MOMO_BROWSER_PROFILE_DIR`；或
- `OPLL_MOMO_SESSION_TOKEN` / 导入 JSON 中的 NextAuth cookie 分片；以及
- 当前会话生成的 `OPLL_MOMO_STRIPE_HCAPTCHA_TOKEN`（如 Elements 要求）。

这些值只从运行时读取，不写入源码、日志、报告或 Git。

