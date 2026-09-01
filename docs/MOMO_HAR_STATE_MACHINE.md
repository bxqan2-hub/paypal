# Momo HAR 状态机（脱敏）

来源：本机 `payment.momo.vn.har`，仅离线读取，未重放请求。原始 SHA-256 记录在验证制品中。

## 观察到的链路

1. Sentinel SDK/frame/req/ping 建立 `chatgpt_checkout` 与 `checkout_session_approval` 挑战上下文。
2. `POST /backend-api/payments/checkout` 返回 `oaics_*`，界面类型为 custom，账单国家 `VN`、币种 `VND`。
3. 浏览器连续三次 `POST /backend-api/payments/checkout/taxes` 刷新税费和可用支付方式；HAR 没有 `checkout/update`。
4. Stripe 使用 Elements 会话，然后 `POST /v1/confirmation_tokens`，表单字段明确为 `payment_method_data[type]=momo`，Stripe 版本为 `2025-03-31.basil`。
5. ChatGPT `POST /backend-api/payments/checkout/confirm` 后，Stripe `payment_intents/{pi_*}/confirm` 返回外部跳转。
6. `pm-redirects.stripe.com/authorize/...` 返回 302，最终落到 `https://payment.momo.vn/v2/gateway/pay?t=<opaque>&s=<opaque>`。
7. Momo 页面通过 `POST https://payment.momo.vn/v2/gateway/querySession` 携带 JSON `sessionId` 轮询；HAR 观察到 `status_code=1000`、`redirect=false` 的待确认状态。

## 固定契约

- 渠道名称 `momo`，适配器 `payment_link_extractor.momo_channel`，结果字段 `momo_url`。
- Momo 只接受 `https://payment.momo.vn/v2/gateway/pay`，且必须同时存在 `t`、`s` 查询参数。
- 一个完整尝试固定同一个代理、ChatGPT/Stripe/Momo Cookie 会话；Checkout 提交后由任务层禁止在同一尝试创建第二单。
- AT、Cookie、Sentinel、代理凭据和订单标识不写入本文档、日志或 Git。

## 运行时适配

- ChatGPT 请求保持与 HAR 一致的设备/会话 UUID、账号标识、客户端构建号与版本、pending-updates、observation、浏览器指纹及 `oai-did` Cookie；observation 和 telemetry 在每次请求前刷新。
- Checkout 与 confirm 分别使用 `chatgpt_checkout`、`checkout_session_approval` 的短时 Sentinel proof。proof、deployment attestation 和 Cookie 只能由运行时浏览器上下文或环境注入，仓库不保存抓包值。
- `hk.1024proxy.io:3000:user:password` 导出格式在 Momo 适配器中解析为认证 SOCKS5H；同一代理在 ChatGPT、Stripe 和 Momo 会话中保持固定。
- 生成 `momo_url` 后先打开 `/v2/gateway/pay`，从实时页面/响应 Cookie 读取 CSRF（或使用运行时环境值），再按 HAR 的 `Origin`、`Referer`、`X-CSRF-Token` 请求 `querySession`；不会重放 HAR 中的令牌。
- 认证或协议失败发生在 Checkout 提交前时才切换下一代理；提交 `oaics_*` 后当前尝试不再创建第二个 Checkout。
