# Momo HAR 状态机（脱敏）

来源：本机 canonical HAR `C:\Users\Administrator\Desktop\提链\artifacts-local\momo-roxy-mitm-20260902-023932.har`，仅离线读取，未重放请求。原始 SHA-256 记录在验证制品中。

## 观察到的链路

1. 浏览器先以匿名壳请求 `/backend-anon/accounts/check`、`/backend-anon/me` 和 `/backend-anon/checkout_pricing_config`，再在同一 VN ChatGPT 会话中切换到 authenticated 账号/设置请求；随后 Sentinel SDK/frame/req/ping 建立 `chatgpt_checkout` 与 `checkout_session_approval` 挑战上下文。
2. `POST /backend-api/payments/checkout` 返回 `oaics_*`，界面类型为 custom，账单国家 `VN`、币种 `VND`；初始请求同时携带 `promo_campaign.plus-1-month-free`，否则服务端会按标准付费金额计算。
3. Checkout 后先请求 `.data?_routes=routes/checkout.$entity.$checkoutId`；响应会设置 `_account` 路由 Cookie。Stripe Elements 随后初始化，浏览器再连续三次 `POST /backend-api/payments/checkout/taxes`，地址按“空 state/postal → state → 完整 postal”渐进提交，HAR 没有 `checkout/update`。
4. Elements 后还有 Link 初始化：`link/get-cookie` 和三次 `consumers/sessions/lookup`。三次 lookup 共用同一个 `stripe_js_id`/client session ID；该 ID 也进入 ConfirmationToken 两层 attribution。
5. `POST /v1/confirmation_tokens` 的 `payment_method_data[type]=momo` 使用 Elements 会话、同一组 `__stripe_mid/__stripe_sid`、42 字符指纹和 `2025-03-31.basil`；请求来自 `https://js.stripe.com`，并带 `Accept-Language` 和当前 hCaptcha token（如 Elements challenge 被启用）。
6. ChatGPT `POST /backend-api/payments/checkout/confirm` 后，Custom 分支使用 Stripe `setup_intents/{seti_*}/confirm` 或 `payment_intents/{pi_*}/confirm`，版本仍为短版 `2025-03-31.basil`。
7. `pm-redirects.stripe.com/authorize/...` 返回 302，最终落到 `https://payment.momo.vn/v2/gateway/pay?t=<opaque>&s=<opaque>`。
8. Momo 页面通过空请求体的 `POST https://payment.momo.vn/v2/gateway/querySession`（会话由网关 Cookie 绑定）轮询；HAR 观察到多次 `status_code=1000`、`redirect=false`，最终 `status_code=9000`、`redirect=true`。

## 固定契约

- 渠道名称 `momo`，适配器 `payment_link_extractor.momo_channel`，结果字段 `momo_url`。
- Momo 只接受 `https://payment.momo.vn/v2/gateway/pay`，且必须同时存在 `t`、`s` 查询参数。
- 一个完整尝试固定同一个代理、ChatGPT/Stripe/Momo Cookie 会话；Checkout 提交后由任务层禁止在同一尝试创建第二单。
- AT、Cookie、Sentinel、代理凭据和订单标识不写入本文档、日志或 Git。

## 运行时适配

- ChatGPT 请求保持与 HAR 一致的设备/会话 UUID、账号标识、客户端构建号与版本、pending-updates、observation、浏览器指纹及 `oai-did` Cookie；observation 和 telemetry 在每次请求前刷新。
- MoMo 按 AT-only 路线运行：AT 通过 `Authorization: Bearer` 选择账号；NextAuth `session-token` 不是该适配器的前置条件，也不作为 `status=blocked` 的单一解释。
- ChatGPT 响应中的 `x-oai-is-receipt` 由 MoMo 适配器在内存中累积为下一请求的 `x-oai-is-pending-updates` v3 envelope；`x-oai-is-update` 不回显。ACK 作为元数据保留，队列有上限并去重。
- 浏览器 monitor 的 receipt 镜像在 Checkout 创建后才开启，并先标记启动壳的既有 receipt；首个 AT account check 保持空 envelope，Checkout 响应边界消费预检批次。
- AT-only 浏览器上下文在 ChatGPT backend 的 fetch/XHR 边界注入当前 Bearer AT、account/device/session 和 target path；Checkout 后先由 AT 的 account UUID 预置非认证 `_account` routing Cookie，再读取 `.data` 返回的更新值并同步到浏览器，避免后续请求丢失路由身份。
- bridge 在受保护 checkout 路径补齐当前 deployment attestation，并为缺省的 backend 请求设置 v3 空 pending envelope；`.data` 文档请求仍显式排除 API 头。
- `.data` hydration 的响应可能是授权页面/重定向（例如 HTTP 202）；诊断保留 `status`、尝试次数和 `redirect_to_login`，并继续收集同轮信息，不把它改写成 `status=blocked` 或固定 Cookie 缺失结论。
- AT-only jar 清理从浏览器导入的旧 auth/OAuth 分片，并在 jar 模式下移除固定 `Cookie` 快照，避免它遮蔽实时 `_account` 路由值。
- Sentinel 产生的 `oai-sc`、`__Secure-oai-is`、`oai-client-session-epoch` 等同源运行时 Cookie 随 jar 同步；它们与 NextAuth session-token 分开处理。
- 当前 HAR 的 taxes/confirm 请求使用空 pending envelope；如新抓包显示支付阶段也需 receipts，可通过 `OPLL_MOMO_ECHO_PAYMENT_PENDING_UPDATES=true` 切换，不改变 AT-only 主路线。
- 当前 VN HAR 的构建标识为 `10109010` / `prod-31e08510fe1189856ad77823ca134a25c60715b5`，MoMo 默认值跟随该标识，并允许运行时环境覆盖。
- 当前 VN HAR 观察到 Chrome 152；运行时默认只在 TLS/UA 成对一致的 Chrome 136/145/146/150 profile 中随机选择，`chrome152` 仅作为兼容别名映射到 Chrome 150。
- 同一完整尝试复用资格探测返回的 ChatGPT session，不重新生成 device/session/cookie 上下文；Stripe `muid/sid` 与 ChatGPT 侧 `__stripe_mid/__stripe_sid` 保持一致。
- Elements、Link lookup 和 ConfirmationToken 共用同一个 `stripe_js_id == stripe_client_session_id`；Link consumer lookup 使用 `Accept-Language: en`，payment-method 类型优先读取实时 Checkout 响应。
- Stripe API/merchant-ui 客户端保持无 Cookie；`__stripe_mid/__stripe_sid` 只在 ChatGPT 页面 Cookie 与 ConfirmationToken 的 `muid/sid` 字段之间同步。
- `x-openai-target-route` 使用当前 Web 客户端的模板：`accounts/check/{version}`、`checkout_pricing_config/configs/{country_code}`、`accounts/{account_id}/customer-balance` 和 `payments/checkout/{processor_entity}/{checkout_session_id}`；`x-openai-target-path` 仍保留实际路径。
- 需要 hCaptcha 时只接受当前运行时注入的 token（参数、环境或 Elements 响应），不使用 HAR 中的旧 token；缺失时错误诊断明确标记 `hcaptcha=absent`。
- Checkout 与 confirm 分别使用 `chatgpt_checkout`、`checkout_session_approval` 的短时 Sentinel proof。proof、deployment attestation 和 Cookie 只能由运行时浏览器上下文或环境注入，仓库不保存抓包值。
- `hk.1024proxy.io:3000:user:password` 导出格式在 Momo 适配器中解析为认证 SOCKS5H；同一代理在 ChatGPT、Stripe 和 Momo 会话中保持固定。
- 生成 `momo_url` 后先以文档导航头打开 `/v2/gateway/pay`，从 `<meta name="_csrf">` 或实时 Cookie 读取 CSRF，再以空 body、`Accept: */*`、`Origin`、`Referer`、`X-CSRF-Token` 请求 `querySession`；默认约 4.25 秒间隔轮询，必须达到 `status_code=9000` 和 `redirect=true` 才返回结果。
- 提链第一步先用当前 VN 代理请求 `/backend-api/accounts/check/v4-2023-04-27`，读取账号 `eligible_promo_campaigns.plus`；未返回活动标识时切换代理继续检查，确认资格后才创建 Checkout。
- 账号检查前复现三个只读 `/backend-anon/*` 壳请求；该壳不携带 Authorization，失败只作为顺序诊断，不改变 AT 资格判定。
- `momo_zero_trial_validation` 只控制金额闸门：开启时 taxes 后要求 VND payable minor units 为 0，关闭时跳过该金额判断；资格预检仍保持在 Checkout 之前。
- `checkout/confirm` 返回 `status=blocked` 时只记录为 `approval_context_rejected`，不推断为登录态失败；诊断同时记录当前 `pending_updates` 数量、`_account` Cookie、Sentinel、`oai-telemetry`、deployment attestation、Elements site key 和 hCaptcha 来源。只有获得真实 client secret 并完成后续 Stripe/MoMo 终态才产生 `momo_url`。
- `momo_fingerprint` 为空时只在受支持且 UA/TLS 成对一致的 Chrome 136/145/146/150 profile 中随机选择；一次完整尝试内固定同一 profile，重试才重新选择。
- 认证或协议失败发生在 Checkout 提交前时才切换下一代理；提交 `oaics_*` 后当前尝试不再创建第二个 Checkout。
