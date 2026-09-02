# Momo HAR 状态机（脱敏）

来源：本机 `payment.momo.vn.har`，仅离线读取，未重放请求。原始 SHA-256 记录在验证制品中。

## 观察到的链路

1. Sentinel SDK/frame/req/ping 建立 `chatgpt_checkout` 与 `checkout_session_approval` 挑战上下文。
2. `POST /backend-api/payments/checkout` 返回 `oaics_*`，界面类型为 custom，账单国家 `VN`、币种 `VND`。
3. 浏览器连续三次 `POST /backend-api/payments/checkout/taxes` 刷新税费和可用支付方式；HAR 没有 `checkout/update`。初始 Checkout 请求同时携带 `promo_campaign.plus-1-month-free`，否则服务端会按标准付费金额计算。
4. Stripe 使用 Elements 会话，然后 `POST /v1/confirmation_tokens`，表单字段明确为 `payment_method_data[type]=momo`，Stripe 版本为 `2025-03-31.basil`；请求来自 `https://js.stripe.com`，并带 `Accept-Language`。
5. ChatGPT `POST /backend-api/payments/checkout/confirm` 后，Stripe `payment_intents/{pi_*}/confirm` 返回外部跳转。
6. `pm-redirects.stripe.com/authorize/...` 返回 302，最终落到 `https://payment.momo.vn/v2/gateway/pay?t=<opaque>&s=<opaque>`。
7. Momo 页面通过空请求体的 `POST https://payment.momo.vn/v2/gateway/querySession`（会话由网关 Cookie 绑定）轮询；HAR 观察到多次 `status_code=1000`、`redirect=false`，最终 `status_code=9000`、`redirect=true`。

## 固定契约

- 渠道名称 `momo`，适配器 `payment_link_extractor.momo_channel`，结果字段 `momo_url`。
- Momo 只接受 `https://payment.momo.vn/v2/gateway/pay`，且必须同时存在 `t`、`s` 查询参数。
- 一个完整尝试固定同一个代理、ChatGPT/Stripe/Momo Cookie 会话；Checkout 提交后由任务层禁止在同一尝试创建第二单。
- AT、Cookie、Sentinel、代理凭据和订单标识不写入本文档、日志或 Git。

## 运行时适配

- ChatGPT 请求保持与 HAR 一致的设备/会话 UUID、账号标识、客户端构建号与版本、pending-updates、observation、浏览器指纹及 `oai-did` Cookie；observation 和 telemetry 在每次请求前刷新。
- 当前 VN HAR 的构建标识为 `10109010` / `prod-31e08510fe1189856ad77823ca134a25c60715b5`，MoMo 默认值跟随该标识，并允许运行时环境覆盖。
- 当前 VN HAR 使用 Chrome 152 的 HTTP 头形状；curl_cffi 的 TLS 伪装使用其当前支持的 Chrome 150 wire profile，避免选择未支持的 `chrome152` impersonate 名称。
- Checkout 与 confirm 分别使用 `chatgpt_checkout`、`checkout_session_approval` 的短时 Sentinel proof。proof、deployment attestation 和 Cookie 只能由运行时浏览器上下文或环境注入，仓库不保存抓包值。
- `hk.1024proxy.io:3000:user:password` 导出格式在 Momo 适配器中解析为认证 SOCKS5H；同一代理在 ChatGPT、Stripe 和 Momo 会话中保持固定。
- 生成 `momo_url` 后先打开 `/v2/gateway/pay`，从实时页面/响应 Cookie 读取 CSRF（或使用运行时环境值），再按 HAR 的 `Origin`、`Referer`、`X-CSRF-Token` 请求 `querySession`；不会重放 HAR 中的令牌。
- 提链第一步先用当前 VN 代理请求 `/backend-api/accounts/check/v4-2023-04-27`，读取账号 `eligible_promo_campaigns.plus`；未返回活动标识时切换代理继续检查，确认资格后才创建 Checkout。
- `momo_zero_trial_validation` 只控制金额闸门：开启时 taxes 后要求 VND payable minor units 为 0，关闭时跳过该金额判断；资格预检仍保持在 Checkout 之前。
- 认证或协议失败发生在 Checkout 提交前时才切换下一代理；提交 `oaics_*` 后当前尝试不再创建第二个 Checkout。
