# `codex-auto-register` 与当前“提链/协议支付”实现详细差异报告

## 1. 结论摘要

本报告以 GitHub 项目 `maile456/codex-auto-register` 的提交
`e672cfc4953c1186a013f5b4472809610cc5029e`（2026-08-17）为参照，并以当前仓库
`C:\\Users\\Administrator\\Desktop\\提链` 的提交 `54fb8c5099c0ca396f2cb4015b200132ff228dec`
为被比较版本。两者不是简单的同名目录替换，而是两个层次不同的产品：参考项目把提链、协议支付、HeroSMS、代理池和 Mongo 任务编排统一在一个 FastAPI/Vue 平台中；当前项目偏向本地 Python 服务，重点加强了协议授权 checkpoint、批次/短信状态和一个 Flask 提链适配层。

最重要的六项差异如下：

1. **协议阶段和代理桥接：参考项目有完整的 1024proxy/vendor bridge 生命周期；当前项目的 `paypal/proxy.py` 只保留直接 `1024proxy.io:3000` SOCKS5H。** 这会影响出口 IP、地区一致性、备用代理和桥接失败恢复。
2. **浏览器运行时：参考项目按 Windows/本机 headless 浏览器运行并自动查找 Chrome/Edge；当前 `manual_browser.py` 固定 Xvfb、`/usr/bin/chromium`、`headless=False`，部署环境不同会直接失败。**
3. **国家和地址规则：参考项目收录 197 个 PayPal 支持国家及 32 个动态国家字段；当前目录只有 12 个国家。** 对参考测试中的 DE、ES、IE、SG 等国家，当前实现会在 schema/gate 阶段拒绝。
4. **Buyer 模式默认值不一致：当前 `create_job()`/HTTP 默认是 `original`，而参考实现和当前 README 的目标默认是 `identity_elevation`。** 这会改变 Phase 2 的 onboarding/elevation 路径和所需 GraphQL 数据。
5. **服务编排层：参考项目使用 FastAPI + Mongo repository + pipeline service，把提链结果推进到 HeroSMS 和协议 job；当前使用本地文件/内存式任务与 Flask 适配器，持久化、队列、跨进程恢复能力较弱。**
6. **当前项目也有参考项目没有的本地增强：授权 checkpoint 迁移、批次索引/总数、同号码重试、立即取消清理、完整审计日志，以及 `/paypal-pay/*` 的同进程协议调用。** 这些增强应保留，并补齐参考项目的桥接、国家和运行时能力。

## 2. 比较范围、版本与目录映射

| 领域 | 参考项目 | 当前项目 |
|---|---|---|
| 提链包 | `app/backend/oai_payment_extractor`（36 个文件） | `payment_link_extractor`（35 个文件） |
| 协议包 | `app/backend/paypal_agreement_protocol`（44 个文件） | `paypal_agreement_protocol`（28 个文件） |
| Web/编排 | FastAPI、Mongo、Vue、pipeline service | 本地 Python Web、文件 checkpoint、Flask adapter |
| 参考提交 | `e672cfc4953c1186a013f5b4472809610cc5029e` | — |
| 当前 HEAD | — | `54fb8c5099c0ca396f2cb4015b200132ff228dec` |

文件级统计显示：提链共有 34 个同名文件，其中 23 个字节级相同、11 个有差异；参考项目多出 `SOURCE.md`/`SOURCE_README.md`，当前项目多出 `web/paypal_protocol.py`。协议包共有 24 个同名文件，仅 3 个字节级相同、21 个有差异；参考项目多出测试、工具、入口和完整数据文件，当前项目多出 `herosms.py`、本地卡哈希数据和 checkout 预览样式。

## 3. 参考项目“提链”核心流程

### 3.1 输入规范化与任务创建

`payment_tools.py` 先从 JWT、Bearer 或 session JSON 提取 access token、邮箱和姓名。`payment_extractor_service.py` 的 Pydantic 模型随后校验 token、邮箱、姓名、国家、支付方式、checkout mode 和 `rotate_proxies` 等字段；服务层还会把代理用户名中的国家提示、hCaptcha token、session id/号码轮换参数规范化。

任务创建时，参考实现会独立选择 checkout/update 两个代理池，执行 sticky proxy/轮换策略，按显式国家、代理国家提示或默认国家（提链服务默认可配置）建立 `ExtractionConfig`，再交给后台 `TaskManager`。代理 bridge 有 startup、health probe、close 三个生命周期点，并把 bridge 错误转成脱敏事件。

### 3.2 Checkout API 与优惠资格

`oai_payment_extractor/application.py` 的 `run_extraction()` 大致顺序是：

1. 规范化配置并把 JWT 中的 account email/name 补入 billing profile；
2. 可选调用 `check_coupon_eligibility`；
3. POST `https://chatgpt.com/backend-api/payments/checkout`，参数包括 `entry_point=all_plans_pricing_modal`、`plan_name=chatgptplusplan`、billing country/currency 与 custom 字段；
4. 按 checkout id 前缀分派 provider：`oaics_` 进入 OAICS，`cs_` 进入 Stripe Checkout；
5. 可选 POST `/backend-api/payments/checkout/update` 写回更新后的 billing/custom 信息；
6. 返回 provider、provider checkout URL、最终 payment URL 和结构化事件，并在 `finally` 安全关闭 session。

优惠检查使用 `.../promo_campaign/check_coupon?coupon=plus-1-month-free...`，失败会保留可诊断事件而不是泄漏 token。country/currency 在 checkout 与 update 两处均做严格映射。

### 3.3 OAICS provider

`flows/oaics.py` 先创建 Elements session，获取税率/地区，刷新 session，再取得 Stripe confirmation token。随后调用 ChatGPT checkout/confirm、Stripe intent/setup intent confirm，解析 `next_action.redirect_to_url`，轮询外部重定向并解析 PayPal billing-agreement URL。`stripe_common.py` 统一处理 intent、redirect、poll、超时和 PayPal host/path 校验。

### 3.4 Stripe Checkout（CS）provider

`flows/cs_live.py` 走 Stripe payment pages 初始化，创建 Elements session，提交税务地区，读取 snapshot/taxes/payment_methods，调用 payment pages confirm，再进入通用 redirect resolution。该路径对 payment method、税区和 client secret 的阶段边界比当前本地适配器更细。

### 3.5 后台状态机

参考 Web task 将阶段记录为 `queued → running → eligibility_check → checkout → checkout_update → stripe_init → elements_session → taxes → payment_confirmation → redirect_resolution → completed/failed/cancelled`。`TaskManager` 使用动态 semaphore（`_acquire_slot`/`_run_with_slot`）限制并发，并为 list/get/cancel/retry/resolve/delete/bulk-delete、proxy-test、source/subscribe 提供服务端接口。事件在 service 层统一 sanitizer，避免 access token、cookie、client secret 进入前端或 Mongo。

## 4. 参考项目“协议支付”核心流程

### 4.1 Sidecar 与 job API

`paypal_agreement_service.py` 把协议支付作为独立 sidecar，默认监听 `127.0.0.1:18098`，主应用先 health probe，再启动/回收进程。`web.py:create_job()` 校验 PayPal agreement token、邮箱、手机号、国家、代理和卡片字段，按动态国家目录生成对应 address schema；任务队列区分 global/device/total concurrency，并为同一账号建立 duplicate ownership。

### 4.2 Phase 0：PayPal approval bootstrap

`paypal/flow.py:run()` 首先 GET `https://www.paypal.com/agreements/approve?ba_token=...`。遇到 403 时执行 DataDome bootstrap/retry；随后从 redirect/`ssrt`/`ctxId`/onboarding URL 中取得 EC token，并建立后续 onboarding 所需上下文。host/path 和 token 形状均做严格校验。

### 4.3 Phase 1：设备与遥测

发送 fingerprint、Tealeaf、analytics 和 observability 请求，写入设备/会话标识。参考实现把这些请求和主流程分开记录，失败通常作为可恢复事件，不让非关键遥测阻断支付。

### 4.4 Phase 2：onboarding/elevation

进入 Next/ModXO onboarding route，优先使用 server actions，失败时用 compact fallback；处理 redirect、EC token、signup app、`contentHash/contentIdentifier`、Weasley/device fingerprint。随后调用 GraphQL `DeferredFeature`、`CheckoutSessionData`、`GriffinMetadata`、`SupportedFundingSources`，并执行地址 autocomplete。`elevation_flow.py` 还会建立 `BuyerFundingContext`/`BuyerContext`，完成 member hydration、AE/BA 数据准备。

### 4.5 Phase 3：注册、2FA 与 KYC

发起 2FA，等待并确认 OTP，再调用 `SignUpNewMember`，提交 user/address/KYC/card。卡片失败时按可重试分类重新提交；成功后取得 EUAT/成员会话，进入 billing review。

### 4.6 Phase 4：billing review 与授权

执行 Hermes/Hagrid `billingLite` review 链，调用 `BuyerContextQuery` 与 authorize mutation。对 `BUYER_NOT_SET` 做有限重试，解析 `billingAgreementToken`、merchant return URL 和 `paymentAction`，跟随 merchant return 完成授权。最终状态区分 `authorization_only`、`confirmed`、`pending_verification`，而不是把所有 redirect 都视作成功。

## 5. 当前项目“提链”实现与差异

### 5.1 保留的共同核心

当前 `payment_link_extractor` 大量复用同源 provider 文件，OAICS/CS 的 HTTP、Stripe intent、redirect resolution 基本结构与参考项目相同；因此“能否拿到支付链接”的主链路不是从零实现，差异主要集中在服务封装、配置默认值、事件和运行环境。

### 5.2 关键语义差异

- 当前适配层的 token/email 处理更轻，未完整复刻参考服务在 JWT 中补齐 billing identity、脱敏事件和独立 checkout/update 代理池的编排。
- 当前没有参考 `TaskManager` 的动态 semaphore 状态机；并发限制和任务恢复主要依赖本地 Web/调用方。
- 当前 `payment_link_extractor/web/paypal_protocol.py` 提供 `/paypal-pay/start`、状态、取消、重试等 Flask 路由，并在同一进程通过 `_CaptureHandler` 调用协议 WebHandler；参考项目把协议放在 sidecar，边界清晰但进程间通信复杂度更高。
- 当前适配器额外集成 HeroSMS `/api/sms/number/status/cancel`，`_watch_sms_job` 会等待 OTP 并自动轮换号码；这是当前的业务增强。
- 参考 service 层的 sanitizer/结构化事件更完整，当前本地日志更偏向审计和调试，需复核敏感字段过滤。

## 6. 当前项目“协议支付”实现与差异

### 6.1 当前增强

`paypal_agreement_protocol/web.py` 维护 authorization checkpoint：可从完整日志迁移旧 checkpoint，跳过已授权的 PayPal authorization，并在结果边界保留 `paypal_authorized`、重试信息和审计日志。`WebJob` 还记录 `account_email`、`batch_id/index/total`、SMS job/number、`last_protocol_step`、same-phone `retry_count/max_retries`，取消时立即清理短信任务。这些是参考提交中没有同等完整度的运维能力。

### 6.2 代码级缺口

1. **Buyer mode**：当前 `create_job()` 和 HTTP POST 默认 `original`，README 的目标默认为 `identity_elevation`；参考 create_job 默认 identity elevation。应统一并保留显式 `original` 兼容开关。
2. **代理 bridge**：参考 `ProxyEntry.uses_bridge`/`ProxyConfig.prepare` 与 vendor bridge lifecycle 在当前不存在，导致 bridge 相关测试和生产备用代理路径失败。
3. **浏览器**：当前 `manual_browser.py` 强制 Xvfb + `/usr/bin/chromium` + 非 headless；参考具备 Windows executable lookup/headless 逻辑。当前代码在 Windows 本地部署上有明显适配风险。
4. **国家目录**：当前 `paypal_supported_countries.json` 只有 12 项，参考有 197 项；`country_field_catalog.json` 当前 12 项，参考 32 项。动态 country gate 和地址字段 schema 因此不完整。
5. **容错细节**：参考 `flow.py` 使用 `getattr(self, "_address_normalized_by_paypal", False)`，当前直接访问该属性；在地址未经过 PayPal normalize 的分支会触发 `AttributeError`。
6. **工程入口/测试**：参考协议包含 `main.py`、更完整的 `tests/` 与 tools；当前包更精简，回归保护覆盖不足。

## 7. 可复现验证结果

### 7.1 编译基线

命令（当前提链、当前协议、参考提链、参考协议）：

```text
& .\\.venv\\Scripts\\python.exe -m compileall -q .\\payment_link_extractor .\\paypal_agreement_protocol <reference>\\app\\backend\\oai_payment_extractor <reference>\\app\\backend\\paypal_agreement_protocol
```

结果：`BASELINE_RESULT=PASS`，退出码 `0`。

### 7.2 参考协议测试

在参考协议根目录运行：

```text
pytest -q
```

结果：`17 passed in 0.12s`，退出码 `0`。

### 7.3 同一套参考测试对当前协议

把参考 `tests/` 指向当前 `paypal_agreement_protocol` 运行：

```text
pytest -q <reference>\\app\\backend\\paypal_agreement_protocol\\tests
```

结果：`5 passed, 12 failed in 0.44s`，退出码 `1`。失败集中在：DE/ES/IE/SG 国家字段缺失；SG 动态 country gate；未定义 `_address_normalized_by_paypal`；手机号/ID fixture 分支；`ProxyEntry.uses_bridge` 与 `ProxyConfig.prepare` 缺失；动态 `run_job` fixture 失败。这些失败直接验证了上文的国家、容错和代理差异，而不是格式或换行差异。

## 8. 差异矩阵与影响评估

| 能力 | 参考项目 | 当前项目 | 影响 | 优先级 |
|---|---|---|---|---|
| 提链 provider | OAICS + CS，服务层统一 | provider 主链大体同源 | 主链可用，编排能力较弱 | P1 |
| checkout/update 代理 | 独立池、sticky、轮换、bridge | 本地简化 | 多账号/地区稳定性下降 | P1 |
| 动态并发 | TaskManager semaphore | 本地调用方控制 | 高并发下资源争用/恢复弱 | P1 |
| 协议阶段 | Phase 0–4 完整 | 同样阶段但外围能力缺口 | 复杂账户/国家路径成功率下降 | P1 |
| Buyer 默认 | identity elevation | original | 业务语义不一致 | P1 |
| 国家数据 | 197 + 32 schema | 12 + 12 schema | 非 12 国直接失败 | P1 |
| 代理 bridge API | 完整 | 缺失 | bridge 测试/备用链失败 | P1 |
| 浏览器 | Windows/headless lookup | Xvfb/Linux 固定路径 | Windows 部署风险 | P1 |
| checkpoint/审计 | 基础任务状态 | 当前更强 | 当前优势 | 保留 |
| HeroSMS | pipeline 原生阶段 | Flask adapter/轮换增强 | 当前本地业务更灵活 | 保留并统一 |
| 持久化 | Mongo repository | 本地文件/内存为主 | 跨进程/水平扩展弱 | P2 |

## 9. 建议的同步顺序

### P0/P1：先恢复正确性

1. 将参考国家目录和动态字段 catalog 合并进当前，保留当前 12 国的兼容别名；为每个新增国家补 schema/phone/currency 测试。
2. 修复 `flow.py` 的 `_address_normalized_by_paypal` 安全访问，并把参考 `ProxyEntry.uses_bridge`、`ProxyConfig.prepare`、bridge health/close 生命周期移植到当前代理模块。
3. 统一 buyer mode 默认值为 `identity_elevation`，对旧调用显式传 `original`；同步 README、HTTP schema 和前端默认值。
4. 把 `manual_browser.py` 改为跨平台 executable lookup，Windows 使用 headless 模式，Linux 保留可选 Xvfb；启动前做可执行文件和 DISPLAY 检查。

### P1/P2：补齐工程能力

5. 在当前 Web 层引入动态 semaphore 和阶段事件，继续沿用现有 checkpoint、短信取消和审计日志。
6. 将提链与协议的敏感事件统一 sanitizer，建立“token 不落盘”的回归测试。
7. 逐步把本地文件任务迁移到 repository 接口；若暂不引入 Mongo，至少实现 SQLite/锁和 sidecar HTTP 边界。
8. 移植参考协议测试、proxy bridge 测试、动态国家测试，并把 `pytest -q` 纳入提交前检查。

## 10. 最终判断

当前代码并非缺少 PayPal/Stripe 主流程，而是**外围平台化能力和参考项目最新修复尚未完全同步**。当前优势是本地化部署、协议授权 checkpoint、短信轮换和审计/批次运维；参考优势是完整国家覆盖、Windows 浏览器适配、代理 bridge、动态并发、Mongo/pipeline/sidecar 分层以及更完整测试。若目标是单机、少量账号运行，当前实现经过 P0 修复即可继续使用；若目标是多国家、多账号、高并发稳定运行，应优先按 P0/P1 顺序同步上述六项差异，再考虑 Mongo 与前端平台迁移。

## 11. 参考代码入口

- [参考项目提交](https://github.com/maile456/codex-auto-register/tree/e672cfc4953c1186a013f5b4472809610cc5029e)
- [提链服务](https://github.com/maile456/codex-auto-register/blob/e672cfc4953c1186a013f5b4472809610cc5029e/app/backend/payment_extractor_service.py)
- [提链 application](https://github.com/maile456/codex-auto-register/blob/e672cfc4953c1186a013f5b4472809610cc5029e/app/backend/oai_payment_extractor/application.py)
- [OAICS provider](https://github.com/maile456/codex-auto-register/blob/e672cfc4953c1186a013f5b4472809610cc5029e/app/backend/oai_payment_extractor/flows/oaics.py)
- [Stripe Checkout provider](https://github.com/maile456/codex-auto-register/blob/e672cfc4953c1186a013f5b4472809610cc5029e/app/backend/oai_payment_extractor/flows/cs_live.py)
- [协议主流程](https://github.com/maile456/codex-auto-register/blob/e672cfc4953c1186a013f5b4472809610cc5029e/app/backend/paypal_agreement_protocol/paypal/flow.py)
- [identity elevation](https://github.com/maile456/codex-auto-register/blob/e672cfc4953c1186a013f5b4472809610cc5029e/app/backend/paypal_agreement_protocol/paypal/elevation_flow.py)
- [协议 Web API](https://github.com/maile456/codex-auto-register/blob/e672cfc4953c1186a013f5b4472809610cc5029e/app/backend/paypal_agreement_protocol/web.py)
- [协议 sidecar service](https://github.com/maile456/codex-auto-register/blob/e672cfc4953c1186a013f5b4472809610cc5029e/app/backend/paypal_agreement_service.py)
