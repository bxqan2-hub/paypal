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
