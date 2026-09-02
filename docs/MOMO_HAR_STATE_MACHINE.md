# Momo HAR 状态机（脱敏）

来源：本机 `payment.momo.vn.har`，仅离线读取，未重放请求。原始 SHA-256 记录在验证制品中。

## 观察到的链路

1. 账号资格与 promo endpoint 在同一个 VN ChatGPT 会话中完成；随后 Sentinel SDK/frame/req/ping 建立 `chatgpt_checkout` 与 `checkout_session_approval` 挑战上下文。
2. `POST /backend-api/payments/checkout` 返回 `oaics_*`，界面类型为 custom，账单国家 `VN`、币种 `VND`；初始请求同时携带 `promo_campaign.plus-1-month-free`，否则服务端会按标准付费金额计算。
3. Stripe Elements 先于税费刷新初始化；浏览器随后连续三次 `POST /backend-api/payments/checkout/taxes`，地址按“空 state/postal → state → 完整 postal”渐进提交，HAR 没有 `checkout/update`。
4. `POST /v1/confirmation_tokens` 的 `payment_method_data[type]=momo` 使用 Elements 会话、同一组 `__stripe_mid/__stripe_sid`、42 字符指纹和 `2025-03-31.basil`；请求来自 `https://js.stripe.com`，并带 `Accept-Language`。
5. ChatGPT `POST /backend-api/payments/checkout/confirm` 后，Custom 分支使用 Stripe `setup_intents/{seti_*}/confirm` 或 `payment_intents/{pi_*}/confirm`，版本仍为短版 `2025-03-31.basil`。
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
- 当前 VN HAR 观察到 Chrome 152；运行时默认只在 TLS/UA 成对一致的 Chrome 136/145/146/150 profile 中随机选择，`chrome152` 仅作为兼容别名映射到 Chrome 150。
- 同一完整尝试复用资格探测返回的 ChatGPT session，不重新生成 device/session/cookie 上下文；Stripe `muid/sid` 与 ChatGPT 侧 `__stripe_mid/__stripe_sid` 保持一致。
- 需要 hCaptcha 时只接受当前运行时注入的 token（参数、环境或 Elements 响应），不使用 HAR 中的旧 token；缺失时错误诊断明确标记 `hcaptcha=absent`。
- Checkout 与 confirm 分别使用 `chatgpt_checkout`、`checkout_session_approval` 的短时 Sentinel proof。proof、deployment attestation 和 Cookie 只能由运行时浏览器上下文或环境注入，仓库不保存抓包值。
- `hk.1024proxy.io:3000:user:password` 导出格式在 Momo 适配器中解析为认证 SOCKS5H；同一代理在 ChatGPT、Stripe 和 Momo 会话中保持固定。
- 生成 `momo_url` 后先以文档导航头打开 `/v2/gateway/pay`，从 `<meta name="_csrf">` 或实时 Cookie 读取 CSRF，再以空 body、`Accept: */*`、`Origin`、`Referer`、`X-CSRF-Token` 请求 `querySession`；默认约 4.25 秒间隔轮询，必须达到 `status_code=9000` 和 `redirect=true` 才返回结果。
- 提链第一步先用当前 VN 代理请求 `/backend-api/accounts/check/v4-2023-04-27`，读取账号 `eligible_promo_campaigns.plus`；未返回活动标识时切换代理继续检查，确认资格后才创建 Checkout。
- `momo_zero_trial_validation` 只控制金额闸门：开启时 taxes 后要求 VND payable minor units 为 0，关闭时跳过该金额判断；资格预检仍保持在 Checkout 之前。
- AT-only 运行时如果没有可用的 NextAuth session-token 分片、deployment attestation 或当前 Stripe hCaptcha token，服务端可能在 `checkout/confirm` 返回 `status=blocked`；代码保留失败状态，不把未确认的中转地址误报为 `momo_url`。可通过运行时 `OPLL_MOMO_SESSION_TOKEN`、已登录浏览器 profile 和 `OPLL_MOMO_STRIPE_HCAPTCHA_TOKEN` 注入同一路上下文。
- `momo_fingerprint` 为空时只在受支持且 UA/TLS 成对一致的 Chrome 136/145/146/150 profile 中随机选择；一次完整尝试内固定同一 profile，重试才重新选择。
- 认证或协议失败发生在 Checkout 提交前时才切换下一代理；提交 `oaics_*` 后当前尝试不再创建第二个 Checkout。
