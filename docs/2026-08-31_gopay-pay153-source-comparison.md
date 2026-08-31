# GoPay：pay153 外部源码对比与 blocked 定向修复

## 结论

本轮只审计了 `C:\Users\Administrator\Downloads\pay153-checkout-link-main` 中与
GoPay 提链直接相关的代码，并与本站 GoPay 独立渠道实现及最近提交记录逐项比较。

下载目录包含 pay153 的本地重实现，以及对闭源 cccy 前端/API 的黑盒探查资料；
它不是 cccy 后端源码。其调试文档记录一次闭源 cccy 黑盒 job 的 Stripe confirm `10/10`
成功、ChatGPT approve `10/10 blocked`，并没有给出本地 `opll_extractor` 跑通证据。
因此不能把该目录的四段代理、approve 前换设备、空 JSON ping 或十次整链重建
直接移植为成功方案。

本站当前更接近真实成功 HAR。最近五个零元账号在不同修复组合下仍最终 blocked，
而成功的浏览器样本同时具有匹配账号的 NextAuth 登录 Cookie、实时 deployment
attestation 和连续 Cookie 演进。这个“同账号浏览器登录态”仍是最高概率、尚未用
匹配账号完成 one-shot 实测的变量。

本轮据此修复了一个实际校验漏洞：旧代码用 Bearer AT 请求 `/backend-api/me`，只能
证明 AT 自己有效，却把结果误当成浏览器 Cookie 与 AT 同账号。新实现会独立执行
cookie-only 身份核验，清除持久 profile 中陈旧的 NextAuth 分片；明确错号会在
Checkout POST/commit 前终止，瞬时不可核验则在同 runtime 重试后返回可重试状态。

## 外部项目中真正相关的 GoPay 文件

| 文件 | GoPay 职责 |
|---|---|
| `app.py:2249-2360` | GoPay 分支、ID/IDR 固定策略、四段代理选择、调用桥接层 |
| `opll_bridge.py:61-190` | 构造提链配置、进入整链重建、整理 `gopay_url` 结果 |
| `opll_extractor/application.py:63-138,263-395` | 整链重建与 Checkout/Stripe 分发 |
| `opll_extractor/checkout.py:131-332` | Checkout 创建、promo、checkout/update |
| `opll_extractor/flows/cs_live.py:51-687` | Stripe init/Elements/billing/confirm、ChatGPT approve、redirect |
| `opll_extractor/transport.py:88-307,740-815` | HTTP 会话、代理、Sentinel 子进程、approve 会话克隆 |
| `opll_extractor/sentinel_browser.py:89-323` | 单次 Playwright Sentinel token 生成 |
| `opll_extractor/providers/gopay.py:4-14` | GoPay/Midtrans 终态域名与 `gopay_url` 字段 |
| `provider_checkout.py:295-425` | 外层默认账单生成；其 ID 回退存在混合 US/ID 地址问题 |
| `docs/gopay-promo-debug-summary.md` | 对闭源 cccy 的黑盒 job 记录与本地改造说明 |

外部目录没有 Git 元数据和测试目录。它的 GoPay provider 文件与本站
`payment_link_extractor/providers/gopay.py` 的 SHA-256 完全一致：
`49554EB3017250A82144543A5CCBFC32089AF3C39A33629D4809BE73F4E88D7C`。
所以差别不在最终 Midtrans/GoPay 域名识别，而在 Checkout、浏览器登录态、Sentinel
和 approve 编排。

## 两边调用链

### 外部下载目录

```text
app.py GoPay 分支
→ opll_bridge.run_opll_extract
→ extract_payment_link_with_rebuild(max_attempts=10)
→ create_checkout（创建时内联 promo）
→ checkout/update
→ Stripe init / Elements
→ provider_proxy 上的 Stripe billing / tax_region / confirm
→ entry ChatGPT 会话上的 taxes / snapshot
→ clone ChatGPT 会话并轮换 device/UA
→ 手工 sentinel/ping
→ checkout/approve
→ 仍在 provider_proxy 上的 Stripe poll
→ Midtrans/GoPay redirect
```

### 本站

```text
channels.py 的 gopay 独立注册项
→ gopay_channel.extract_gopay_payment_link
→ gopay_core.extract_gopay_payment_link
→ check_coupon 资格预检
→ 四字段 Checkout
→ 同一持久 Playwright runtime 的第二次 checkout Sentinel init
→ checkout/update 应用 promo
→ Stripe init / Elements
→ approval challenge 预取
→ consumer lookup
→ tax_region / snapshot / taxes / payment-page cadence
→ Stripe confirm
→ 同一 approval runtime token + Cookie + attestation
→ checkout/approve
→ Stripe poll
→ Midtrans/GoPay redirect
```

## 关键差异

| 环节 | 外部下载目录 | 本站判断 |
|---|---|---|
| 成功证据 | 文档中的闭源 cccy 黑盒 job 为 confirm 10/10、approve blocked 10/10；本地引擎无成功证据 | 有两份完整成功 HAR和一份新成功非零 HAR；零元账号仍需匹配登录态验证 |
| Checkout promo | `checkout.py:143-153` 在创建正文内联 promo | 保留成功 HAR 的四字段创建正文，随后 update；不移植未证实的内联变体 |
| 资格检测 | 主流程跳过独立 check_coupon | 本站先预检，未创建 Checkout 时可换资格代理，避免消耗一次机会 |
| Sentinel 生命周期 | 每次子进程取 token，未持有 NextAuth/attestation | 持久 Playwright runtime/profile，`init → token` 连续 |
| Approve | `cs_live.py:525-574` 新 device/UA + 空 JSON ping；approve 未生成匹配 proof | 同一 runtime/device，最终 token 后立即 approve，带 attestation、当前 Cookie和空 pending envelope |
| 登录态 | `opll_extractor` 没有 NextAuth 或 deployment attestation 代码 | 精确解析并导入 NextAuth 分片及指定浏览器认证材料，跟踪 Cookie 演进并执行 readiness/binding gate |
| 代理 | 外部尝试为四段选择不同代理字符串，但不保证不同真实出口，且 provider pool/options、Stripe poll 分段仍有缺口 | 支持显式四段；默认保持完整尝试连续，避免把代理变化误当已验证根因 |
| 重试 | 最多 10 次重建，但 blocked 又被定义为内部 terminal | Checkout 真正提交后不再重建；资格/前置网络错误才换代理 |
| 账单 | 外层 ID 不在默认 rows 中，可能形成 ID 国家配 US 城市/州 | `config.py:33,51` 为原生 IDR/Jakarta/DKI Jakarta 账单 |
| 结果字段 | `gopay_url` + Midtrans/GoPay host | 与本站 provider 文件逐字节相同 |

## 为什么外部代码不能解释本站 blocked

1. 外部文档 `gopay-promo-debug-summary.md:97-103,174-180` 明确记录其调用的
   cccy 黑盒 job 仍是 `10/10 blocked`，而非本地引擎成功。
2. 同文档 `:107-125,182-197` 说明真正 cccy 后端闭源；下载目录由本地重实现和
   黑盒探查产物组成。
3. 外部 approve 在新设备上没有生成与新设备匹配的 approval proof，也没有本站已
   对齐的 attestation、pending envelope 和同 runtime Cookie 连续性。
4. 外部四段代理还存在 Sentinel token 出口记录仍指向 entry、provider pool 未完整
   传入任务 options 等实现缺口。
5. 外部默认 ID 账单会从 US row 回退，形成混合地址；本站的固定印尼账单更完整。

## 本站最近 blocked 证据

- `docs/2026-08-31_gopay-three-at-continuation-report.md:5-9,119-128`：三个零元账号
  最终 blocked；新成功非零 HAR 同时有匹配 NextAuth、291 字符 attestation 与
  Cookie 演进。
- `docs/2026-08-31_gopay-two-at-risk-final-report.md:5-7,67-74,87-92`：另两个账号
  最终 blocked；其中一个带实时 291 字符 attestation 仍 blocked，但缺匹配账号
  NextAuth。
- 最近提交已经逐项验证真实 Playwright、approval proof/ping、consumer lookup、
  Stripe ID、tax cadence、设备轮换和 attestation 单项；这些都不是充分条件。

因此结论是：匹配账号的完整浏览器登录态是最高优先级未验证变量，而不是已经确认的
唯一根因。若 matched-cookie + fresh attestation 的 one-shot 仍 blocked，下一项受控
实验应是让 protected Checkout/approve POST 在同一持久 Playwright context 内执行，
而不是回退到外部项目的 curl + 空 ping 路径。

## 本轮修复

### 1. cookie-only 三态账号绑定

`payment_link_extractor/gopay_sentinel_playwright.py:178-240,577-586,669-677`

- 在已打开页面里以原生 `fetch` 发起不带 Authorization 的 `/backend-api/me`。
- 只使用浏览器 Cookie，并与 AT 内 `expected_user_id` 比较。
- 明确区分 `matched / mismatched / unavailable / identity_missing`；只有明确不同账号
  才终态拒绝，网络/响应不可核验为 HTTP 503、`retryable=True`。
- `unavailable` 在同一 browser runtime 内最多做三次有界重试；Checkout 页面导航、
  Cookie 演进后会在 SDK init 前再次核验，不重建 Checkout。
- Bearer AT 检查使用 `credentials=omit` 单独执行，不再冒充 Cookie 账号检查。

### 2. 清理陈旧 NextAuth 分片

`payment_link_extractor/auth.py:20-22,154-156`、
`payment_link_extractor/gopay_sentinel_playwright.py:110-156,242-250`、
`payment_link_extractor/gopay_transport.py:540-592`

- NextAuth 名称统一只接受 unchunked 或数字 `.0/.1/...` 分片，拒绝 `.bad` 等后缀。
- 导入新 NextAuth 时只清理旧的 session-token Cookie。
- 清理仅针对 NextAuth session-token；其他 Cookie 保留，本轮不把它们判定为无关
  或当作账号绑定凭据。
- callback/CSRF 等辅助 Cookie 不会触发 NextAuth 清理，也不会被误设为 HttpOnly。
- 避免新旧账号分片并存或旧高编号分片残留。
- approval 切换显式代理时，优先采用当前 transport/browser 合并 Cookie 中的
  NextAuth 分片与最新 attestation，不再退回任务最初导入的旧 seed。

### 3. cookie-native 浏览器 bootstrap

`payment_link_extractor/gopay_sentinel_playwright.py:254-287,509-548`

- 检测到 NextAuth 浏览器登录态时，不再把 Bearer Authorization 和
  `chatgpt-account-id` 作为 context-wide 页面 header。
- 保留 device/session/language/build 与实时 attestation。
- AT-only 显式实验仍保留原 bearer fallback，但生产 readiness 不会把它当成匹配的
  NextAuth 登录态。
- 显式开启 AT-only fallback 且没有导入 NextAuth 时，会先移除 profile 中陈旧的
  NextAuth，避免旧登录态意外阻断 fallback。

### 4. Checkout 前 fail-closed

`payment_link_extractor/gopay_checkout.py:25-74,435-440`

- 有 NextAuth Cookie 且 cookie-only 身份明确不匹配时返回 HTTP 412。
- `failure_mode=browser_session_account_mismatch`、`retryable=False`。
- 探针不可用返回 `browser_session_binding_unavailable`、HTTP 503、`retryable=True`；
  AT 缺稳定 user id 返回独立 `access_token_identity_missing`。
- Checkout commit callback 与 POST 都不会发生，避免把错账号登录态推进到最终 blocked。

### 5. approval 新 runtime 再校验

`payment_link_extractor/gopay_cs_live.py:93-156`

- 显式 approval 代理创建的新 Playwright runtime 在 Stripe confirm/ChatGPT approve 前
  再做 cookie-only 绑定校验。
- 新 runtime 错号或不可核验时不发送最终 approve，避免把代理切换造成的会话丢失
  表现成新的 `blocked`。

### 6. 脱敏诊断

- `payment_link_extractor/gopay_transport.py:1355-1472` 传播绑定状态、来源和布尔值。
- `payment_link_extractor/gopay_cs_live.py:693-790` 在 blocked safe context 中记录状态。
- `tools/gopay_live_canary.py:112-120` 输出独立错误类别，不输出 Cookie、AT 或账号值。

## 验证边界

本轮单元测试验证了请求头、Cookie 清理、匹配/错配判断、Checkout 前终止、blocked
诊断和渠道隔离。测试不包含真实账号或代理，也不把 mock 通过描述为真实
`approve=approved`。下一轮真实验证应使用一个新的 eligible AT，并保证实时浏览器
登录的就是该账号；Checkout 只提交一次。
