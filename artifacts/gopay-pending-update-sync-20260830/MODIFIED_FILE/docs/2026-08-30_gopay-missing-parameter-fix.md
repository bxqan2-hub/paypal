# GoPay CS-live 缺参修复记录

## 对象与结果

- 对象：`payment_link_extractor/gopay_cs_live.py` 的 Stripe GoPay `cs_live` 确认链路。
- 结果：确认请求现在生成 Stripe.js 的 `js_checksum`、`rv_timestamp`，保留 42 字符 metrics IDs，按 HAR 的五步顺序渐进提交税区，并在调用方提供新鲜 token 时转发 `passive_captcha_token`。
- 结果补充：`checkout/approve` 现在注入 HAR 中存在的动态 `oai-telemetry`，并请求 `checkout_session_approval` Sentinel proof。
- 任务可见性：资格探测成功后新增 `eligibility_confirmed` 阶段事件，随后才进入 `checkout`；因此“检测到试用”与“开始提链”在任务时间线中可明确区分。
- 目标：保持 `ID/IDR` 与 `gopay_url` 独立渠道策略；金额门禁仍只接受权威的零金额。

## 原因分层

### 高概率（已确认）

1. 原实现的 Stripe confirm 表单有 58 个键；完整 HAR 的 `api.stripe.com/.../confirm` 有 60 个键。
2. 缺少的两个固定字段是 `js_checksum` 与 `rv_timestamp`。两者由 `js.stripe.com` bundle 生成：前者绑定 `ppage_…` Payment Page id，后者绑定 Stripe.js build 常量。
3. 原实现每次 confirm 生成 32 字符 hex 的 `guid/muid/sid`；浏览器 metrics controller 使用 UUID 加六位后缀（42 字符）并在同一会话中复用。
4. GoPay 的资格检测调用本身没有被近期提交改成别的 URL；它沿用了 PayPal 的 `GET /backend-api/promo_campaign/check_coupon?...is_coupon_from_query_param=true`。对照本站 GCash HAR，该 URL 在浏览器会话中确实可返回 `state=eligible`。

### 中概率（已纳入兼容）

部分风险较高的 HAR confirm 还带 `passive_captcha_token`。配置存在新鲜 `stripe_hcaptcha_token` 时，现在按 Stripe.js 的顶层字段转发；没有 token 时不伪造值。

HAR 的 `checkout/approve` 也携带八项动态 `oai-telemetry`。此前 transport 只为 checkout/confirm 注入该字段，导致 approve 请求少一个浏览器参数；现在三类请求统一注入，approve/confirm 使用同一动态数组形状。

对照 GCash HAR 和 GoPay 完整 HAR 还确认了一个此前遗漏的会话字段：浏览器响应会返回 `x-oai-is-update: ois1.…`，下一次请求需要把它包装成 `x-oai-is-pending-updates: {"v":3,"updates":[…]}`。GoPay transport 现在在 HTTP 响应和浏览器 request monitor 两条路径同步该值，并在资格探测前预热浏览器会话。

### 低概率（非缺参）

代理出口、Sentinel 证明、设备/会话连续性会影响风控。参考 HAR 已保持 `chatgpt_checkout`、`id-ID`、`10012890`、同一 `oai-device-id`/`oai-session-id`；任务管理器继续从代理池完整重启并轮换尝试。

本轮实测中并非全部是网络错误：多数响应是 HTTP 200 + `state=not_eligible`，少数才是 SOCKS/TLS/连接超时。因此不能把所有结果归因于代理未连通；新增 pending-update 同步用于修复浏览器会话参数缺失，资格状态仍以服务端响应为准。

## HAR 对照修复

完整 HAR 观察到税区 POST 的字段集合依次为：

```text
country
country + line1
country + line1 + city
country + line1 + city + state
country + line1 + city + state + postal_code
```

GoPay 副本现在复用同一个 Elements session/Stripe JS id，逐步发送这五个累积集合，并在每次响应后刷新金额上下文。Stripe 与 Midtrans 的金额单位差异（例如 `34900000` 与 `349000`）保持分离，零金额门禁未放宽。

## 实时尝试

此前从附件读取到 3 个 AT，并按旧代理池轮换测试：

- AT-1 曾在部分出口返回 `401 access_denied` / `no_organization`。
- AT-2 曾在部分节点的资格探测返回 `eligible`，完整链路进入 Stripe，税后权威金额为 `0`，但 `checkout/approve` 返回 `{"result":"blocked"}`。
- 其它出口返回 `not_eligible` 或代理连接超时；AT-3 也曾返回 `not_eligible`。

完整 HAR 的 approve 请求包含 `oai-telemetry`，此前 GoPay transport 未覆盖 `/backend-api/payments/checkout/approve`。现已补齐动态八项 telemetry，并让 approve 请求使用 `checkout_session_approval` proof。实时尝试没有得到最终 `gopay_url`，但金额门禁和离线 HAR 契约均保持有效。

本轮 AT-1 的“检测到试用”来自资格探测阶段；后续完整提链由另一个浏览器会话执行时遇到代理连接/资格状态变化，旧 UI 只有 `eligibility_check` 标签，容易造成已开始提链的误解。现在会显示 `试用资格已确认，继续创建 Checkout`，再显示 `创建 Checkout`。

## 随机代理与重试策略

GoPay 任务的 proxy pool 现在在每个 AT 任务开始时独立随机打乱；每次完整尝试固定使用该次选中的代理，不会在资格通过后中途换出口。资格未通过、非零金额、网络失败或其它协议失败会在配置的重试次数内新建 Checkout/浏览器会话并选择下一个随机池项；GoPay AT 返回 HTTP 401 则终止当前 AT，不再浪费代理重试次数。

`blocked` 属于非 401 的协议失败，因此会触发新的完整尝试；多 AT 批处理应在该事件后由上层选择下一个 AT，同时使用新的随机代理和新浏览器指纹。

使用新一组 10 条 t-30 代理和 3 个 AT、每个 AT 重试 5 次（6 次总尝试）复测时，三个 AT 各自随机抽取 6 条不同出口；本轮每次均在资格检测阶段得到 `not_eligible` 或网络错误，没有进入 Checkout，因此没有产生链接。该结果表示本轮抽样未观察到 `eligible`，不表示剩余未抽取出口永久无资格。
