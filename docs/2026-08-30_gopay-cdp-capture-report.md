# GoPay CDP 完整操作抓包报告

> 抓包日期：2026-08-30
> CDP：`127.0.0.1:61375`
> 记录方式：附着现有 ChatGPT page target，监听 `Network`、`Fetch`、`Runtime`

## 1. 范围与结果

本次只读取 CDP 事件和浏览器产生的网络数据，不执行 HAR 内的 URL、JSON 或脚本。原始 HAR 保存在本机但不提交到 Git：

```text
C:\Users\Administrator\Desktop\提链\artifacts\gopay-cdp-capture-20260830\raw_capture.har
```

| 属性 | 值 |
|---|---:|
| entries | 345 |
| size | 13,474,625 bytes |
| SHA-256 | `6D1F47CC1CFC4F739D66A3A5FD41EE9CE011DA6C6EA524D1AF54A99382E75F45` |
| 页面 | ChatGPT → Midtrans redirection |

结论：ChatGPT 与 Midtrans 的请求/响应正文已捕获；Stripe API 请求未进入当前 page target，因此本次是“核心业务正文完整、Stripe API 子目标缺失”的部分完整抓包。

## 2. ChatGPT Checkout 正文

### Checkout 创建

```text
POST /backend-api/payments/checkout -> 200
request body length: 151 bytes
request keys: entry_point, plan_name, billing_details, checkout_ui_mode
entry_point: all_plans_pricing_modal
plan_name: chatgptplusplan
checkout_ui_mode: custom
billing_details.country: ID
billing_details.currency: IDR
```

响应正文长度为 1812 bytes，关键状态为：

```text
checkout_provider: stripe
processor_entity: openai_llc
status: open
payment_status: unpaid
requires_manual_approval: true
automatic_tax_enabled: true
billing_details: {country: ID, currency: IDR}
```

### taxes

```text
POST /backend-api/payments/checkout/taxes -> 200  (2 次)
request keys:
checkout_session_id, checkout_email, billing_country, billing_name,
currency, processor_entity, billing_address
billing_country: ID
currency: idr
processor_entity: openai_llc
billing_address keys: line1, city, country, postal_code, state
request lengths: 398, 403 bytes
```

两次响应正文均为 4612 bytes，且字段一致：

```text
using_automatic_tax: true
amount_subtotal: 34900000
amount_total: 34900000
currency: idr
payment_method_types: [card, gopay]
payment_status: unpaid
mode: subscription
approval_method: manual
automatic_tax: enabled=true, status=complete
total_details.amount_discount: 0
total_details.amount_tax: 3458559
```

### snapshot

```text
POST /backend-api/payments/checkout/snapshot -> 204  (2 次)
request lengths: 206, 211 bytes
request keys: snapshot
snapshot.billing_address.address keys:
line1, city, country, postal_code, state
response body: empty
```

### approve

```text
POST /backend-api/payments/checkout/approve -> 200
request keys: checkout_session_id, processor_entity
processor_entity: openai_llc
response: {"result":"approved"}
```

## 3. Sentinel 与身份 Header

`POST /backend-api/sentinel/req` 共 4 次，全部返回 200。请求体字段为 `flow`、`id`、`p`：

| flow | 次数 | id 长度 | p 长度 |
|---|---:|---:|---:|
| `chatgpt_checkout` | 2 | 36 | 573 |
| `checkout_session_approval` | 2 | 36 | 573 |

本次没有 `flow=default`。

| 字段 | 出现次数 | 观测值 |
|---|---:|---|
| `OpenAI-Sentinel-Token` | 2 | 长度 6125、6186 |
| `oai-web-deployment-attestation` | 6 | 长度 291 |
| `oai-device-id` | 18 | UUID 长度 36 |
| `oai-session-id` | 18 | UUID 长度 36 |
| `oai-language` | 18 | `id-ID` |
| `oai-client-build-number` | 18 | `10012890` |
| `oai-client-version` | 18 | `prod-7890a3be6202572c0e8e3bb4907574d660b4e4f4` |
| `x-oai-is-client-observation` | 16 | 长度 23，按请求变化 |

HAR 没有 request Cookie 数组，因此没有直接取得 `oai-did` Cookie 原值；只能确认业务请求使用了 `oai-device-id`。

## 4. Midtrans 结果

```text
GET  /snap/v4/redirection/<UUID> -> 200
GET  /snap/v1/transactions/<UUID> -> 200
POST /snap/v1/promos/<UUID>/search -> 200
GET  /snap/v3/experiment -> 200
```

交易响应的非敏感摘要：

```text
transaction_details.gross_amount: 349000
transaction_details.currency: IDR
recommended_payment_method: gopay
enabled_payments: [gopay, qris]
gopay.tokenization: true
gopay.enforce_tokenization: true
```

## 5. Stripe API 捕获缺口

当前 page target 的主机统计为：

```text
chatgpt.com: 36
js.stripe.com: 12
pm-redirects.stripe.com: 1
app.midtrans.com: 4
api.stripe.com: 0
```

缺少的关键请求：

- `api.stripe.com/v1/payment_pages/<CS>/init`
- `api.stripe.com/v1/elements/sessions`
- `api.stripe.com/v1/payment_pages/<CS>` 的 tax_region POST/GET
- `api.stripe.com/v1/payment_pages/<CS>/confirm`
- Stripe confirm 响应正文

这与 ChatGPT 和 Midtrans 已有正文不矛盾，最可能是 Stripe iframe/OOPIF 使用了未附着的 CDP target。

## 6. 当前代码对照

| 抓包观察 | 当前 GoPay 副本 | 结果 |
|---|---|---|
| `chatgpt_checkout` proof | `gopay_transport.py` / `gopay_checkout.py` | 已对齐 |
| `oai-device-id` 连续 | `gopay_transport.py` | 已对齐；Cookie 原值未导出 |
| Checkout body 四字段 | `gopay_checkout.py` | 已对齐 |
| taxes body 七个顶层字段 | `gopay_checkout.py` | 已对齐 |
| snapshot 204 空响应 | `gopay_cs_live.py` | 已对齐语义 |
| 两次 taxes + 两次 snapshot | `gopay_cs_live.py` | 已观察到；需保留顺序容错 |
| 五步 Stripe tax_region | `gopay_cs_live.py` | 本次未捕获，下一次补齐 |
| `34900000` 与 `349000` | `gopay_core.py` | 保留单位差异，不直接覆盖 |
| approve `{result: approved}` | `gopay_cs_live.py` | 已对齐 |

## 7. Evidence → Finding → Path

### Scope 摘要

- `scope_type`: local CDP capture
- `in_scope`: `127.0.0.1:61375` 当前 ChatGPT page target 及其本地 HAR
- `network_profile`: browser-generated traffic only; no HAR replay

### Evidence

#### E-001

- title: CDP 记录器保存本次操作
- source_type: file
- source_ref: `artifacts/gopay-cdp-capture-20260830/raw_capture.har`
- content_hash: `6D1F47CC1CFC4F739D66A3A5FD41EE9CE011DA6C6EA524D1AF54A99382E75F45`
- repro_command: `\.venv\\Scripts\\python.exe tools\\har_analyze.py artifacts\\gopay-cdp-capture-20260830\\raw_capture.har --format markdown --host chatgpt.com --limit 200`
- raw_excerpt: `CAPTURE_ENTRIES=345; ChatGPT selected=36; Sentinel proof lengths=6125,6186`
- linked_workitem: WI-001

#### E-002

- title: ChatGPT 业务正文
- source_type: network
- source_ref: Checkout、taxes、snapshot、approve entries
- content_hash: n/a
- repro_command: `\.venv\\Scripts\\python.exe tools\\har_analyze.py artifacts\\gopay-cdp-capture-20260830\\raw_capture.har --format json --contains checkout --limit 100`
- raw_excerpt: `Checkout request=151 bytes; taxes response=4612 bytes; approve response={result: approved}`
- linked_workitem: WI-002

#### E-003

- title: Midtrans GoPay 交易正文
- source_type: network
- source_ref: `app.midtrans.com/snap/v1/transactions/<UUID>`
- content_hash: n/a
- repro_command: `\.venv\\Scripts\\python.exe tools\\har_analyze.py artifacts\\gopay-cdp-capture-20260830\\raw_capture.har --format json --host app.midtrans.com --limit 100`
- raw_excerpt: `gross_amount=349000; currency=IDR; recommended=gopay; enabled=[gopay,qris]`
- linked_workitem: WI-003

#### E-004

- title: Stripe API 未进入 page target
- source_type: command
- source_ref: `tools.har_capture.audit_har_completeness`
- content_hash: n/a
- repro_command: `\.venv\\Scripts\\python.exe -c "from tools.har_capture import audit_har_completeness; ..."`
- raw_excerpt: `api.stripe.com=0; gopay_stripe_init/element/confirm entry_missing`
- linked_workitem: WI-004

### Findings

#### F-001

- title: ChatGPT Checkout/taxes/snapshot/approve 正文已可用于代码对照
- severity: info
- category: reverse_algo
- status: validated
- evidence_ids: [E-002]
- location: `chatgpt.com/backend-api/payments/*`
- impact: 后续可直接核对 GoPay 副本的 JSON 字段和状态码。
- confidence: high

#### F-002

- title: page-target 仍缺 Stripe API 子目标
- severity: medium
- category: design
- status: validated
- evidence_ids: [E-004]
- location: `api.stripe.com/v1/payment_pages/*`
- impact: 五步 tax_region、confirm 表单和 Stripe redirect 响应还不能从本次 HAR 验证；下一次需要 browser-level `Target.setAutoAttach(flatten=true)`。
- confidence: high

#### F-003

- title: Midtrans 样本是非零金额
- severity: medium
- category: design
- status: validated
- evidence_ids: [E-003]
- location: Midtrans transaction response
- impact: `349000 IDR` 不满足当前 GoPay 零金额门禁。
- confidence: high

### Path

#### P-001

- title: CDP 捕获到的 GoPay 调用路径
- path_type: callflow
- start: ChatGPT Sentinel page
- goal: Midtrans GoPay transaction query
- steps:
  1. action: Sentinel SDK/frame/req/ping；evidence: E-002；finding: F-001
  2. action: 创建 `cs_live` Checkout；evidence: E-002；finding: F-001
  3. action: 两次 snapshot、两次 taxes，返回 Stripe checkout state；evidence: E-002；finding: F-001
  4. action: approve 返回 `result=approved`；evidence: E-002；finding: F-001
  5. action: Stripe authorize 302 到 Midtrans；evidence: E-003；finding: F-003
  6. action: Midtrans transaction 返回 IDR、gopay、qris；evidence: E-003；finding: F-003
- residual_risks: Stripe API iframe/OOPIF 尚未附着；金额单位转换仍需单独验证。

### Timeline 摘要

1. 19:41:51Z：Sentinel SDK/frame 开始加载。
2. 19:41:56Z：Checkout 200，正文捕获。
3. 19:42:32Z：第一次 snapshot 204。
4. 19:42:34Z：第一次 taxes 200。
5. 19:42:43Z：approve 200，`result=approved`。
6. 19:42:48Z：approval Sentinel req 完成。
7. 随后：Stripe authorize 302 进入 Midtrans。
8. 19:43 左右：Midtrans transaction 200。

## 8. 下一次完整抓包

下一次应使用 [`har_capture_browser_attach.py`](../tools/har_capture_browser_attach.py) 连接浏览器级 WebSocket，启用 `Target.setDiscoverTargets` 和 `Target.setAutoAttach(flatten=true)`，对所有 `page`、`iframe`、`worker` target 分别启用 Network/Fetch。重点补齐：

1. Stripe init、Elements、5 次 tax_region、confirm 和响应正文。
2. Stripe `next_action`/`redirect_to_url` 与 Midtrans UUID 的绑定。
3. `Storage.getCookies` 中 `oai-did`、NextAuth 分片和域属性。
4. taxes/snapshot 与 Stripe 更新的实际并发顺序。

## 9. 抓包导致页面卡加载的根因与修复

本次实时监测确认，问题来自浏览器级记录器的事件路由，而不是 GoPay 页面业务逻辑：

1. `Target.setAutoAttach(flatten=true)` 后，Network/Fetch 事件带有 target `sessionId`。
2. 旧主循环只读取 browser-level 全局事件队列，未读取各 target 队列。
3. `Fetch.enable` 在响应阶段暂停资源；`Fetch.requestPaused` 没有被转交给对应 `HARRecorder`，所以页面请求持续处于 paused 状态。
4. 停止记录器后 Fetch 会话关闭，浏览器请求恢复，因此出现“关闭抓包后页面恢复”。

已修复 [`har_capture_browser_attach.py`](../tools/har_capture_browser_attach.py)：

- 新增 `BrowserConnection.recv_any()`，优先处理 `Target.attachedToTarget` 等生命周期事件，再返回带 session ID 的 target 事件。
- 主循环按 session ID 将每个 Network/Fetch/Runtime 事件转交给对应 `TargetRecorder`。
- `Fetch.requestPaused` 会立即执行 `Fetch.getResponseBody` 和 `Fetch.continueRequest`，不再阻塞页面资源。

修复后的回归结果：

```text
专属事件路由测试：2 passed
完整仓库测试：180 passed
```

下一次开始抓包时继续使用浏览器级多 target 模式；实时监测重点为 `CAPTURE_TARGET_ATTACHED` 输出、记录器进程存活和页面 `readyState`。

## 10. 60943 浏览器级修复后抓包复核

本次使用修复后的 `browser-auto-attach-flatten` 记录器，原始 HAR 位于本机 `artifacts-local`：

```text
artifacts-local/gopay-cdp-capture-browser-targets-20260830-60943-fixed.har
entries=401
size=16155304 bytes
sha256=7E7AB2715B3728314C67CBAD5C477E44FC353F0AC00ADAACC0320F06FA3A48C1
```

本次已捕获真实正文：

- ChatGPT Checkout：请求 151 bytes、响应 1812 bytes。
- ChatGPT taxes：2 次 200，响应各 4687 bytes，`amount_total=34900000`、`payment_method_types=[card,gopay]`。
- ChatGPT snapshot：2 次 204，请求正文存在、响应按 204 为空。
- ChatGPT approve：请求 124 bytes，响应 `{"result":"approved"}`。
- Sentinel：`chatgpt_checkout` 2 次、`checkout_session_approval` 2 次；proof 长度 6421、6858。
- Midtrans transaction：200，`gross_amount=349000`、`currency=IDR`、推荐 `gopay`、启用 `gopay/qris`。

完整性结论仍为**协议层未完整**：主机统计中 `api.stripe.com=0`、`js.stripe.com=12`，所以 Stripe init、Elements、tax_region、confirm 及 Stripe 响应正文没有进入这次 HAR。`audit_har_completeness` 的关键缺口为 `gopay_stripe_init/element/confirm:entry_missing`；这不是页面卡死，页面已到达 Midtrans 且停止记录器后 `readyState=complete`、`pending=0`。

本次脱敏摘要由 [`har_cdp_gopay_summary.py`](../tools/har_cdp_gopay_summary.py) 生成；下一次如需 Stripe API 全量，应继续确认 API 请求是否由独立浏览器上下文或服务端路径产生，并同时保留 browser target 与代理层记录。

## 11. 抓包工具完整性优化（本次）

`har_capture_browser_attach.py` 现以浏览器级 CDP 为唯一入口，启动时先枚举已有 target，再通过 `Target.setDiscoverTargets`、`Target.setAutoAttach(flatten=true)` 监听后续创建的 page/iframe/worker/service-worker/shared-worker。每个 target 独立启用 `Network`，并在 target 内再次尝试子 target 自动附着，避免 Stripe OOPIF 在流程中途漏记。

为避免再次造成页面卡在加载状态，默认路径改为非阻塞的 `Network.streamResourceContent`/`Network.getResponseBody` 双路正文采集；只有明确传入 `--fetch-responses` 才启用 Fetch 响应暂停。主循环现在持续排空每个 flattened session 队列，停止时会 flush 未完成请求并保留 `_capture` 正文来源、截断标记、错误和 target 元数据。

此外，记录器接收 `Network.requestWillBeSentExtraInfo` 与 `Network.responseReceivedExtraInfo`，将真实 wire headers、关联 cookie 属性和响应头合并进 HAR；ExtraInfo 先到或后到都能按 requestId 对齐。保存时新增主机计数、target 类型、API Stripe 条目计数和脱敏完整性审计输出：`CAPTURE_HOST_COUNTS`、`CAPTURE_TARGET_TYPES`、`CAPTURE_API_STRIPE_ENTRIES`、`CAPTURE_COMPLETENESS`、`CAPTURE_MISSING`。因此一次运行结束即可区分“工具漏记”和“业务流程本身没有产生 api.stripe.com 请求”。

验证命令与结果：

```text
.\.venv\Scripts\python.exe -m py_compile tools\har_capture.py tools\har_capture_browser_attach.py  -> exit 0
.\.venv\Scripts\python.exe -m pytest -q                                      -> 184 passed
```

完整抓包仍使用：

```text
.\.venv\Scripts\python.exe tools\har_capture_browser_attach.py --cdp-port <端口> --output artifacts-local\gopay-cdp-full.har
```

看到 `CAPTURE_READY=1` 后再操作完整 GoPay 流程；结束后先读取 `CAPTURE_COMPLETENESS` 和 `CAPTURE_MISSING`，再按 HAR 中的 `log._capture.completenessAudit` 修改提链核心。

## 12. 最终保留抓包（停止抓包后）

停止优化抓包工具后，浏览器级 `browser-auto-attach-flatten` HAR 已完成关键完整性审计：

```text
source: artifacts-local/gopay-cdp-capture-browser-targets-20260830-next.har
entries: 483
size: 16491140 bytes
sha256: 8DF5163E0A2D57598B257435C2449EA0371A236C6114BAE85234A94108547E50
targets: 14 (page + iframe)
api.stripe.com entries: 12
completenessAudit.complete: true
criticalComplete: true
issues: []
```

该文件已替代早先的 401-entry 抓包；`artifacts-local` 中只保留这一份 GoPay 原始 HAR。脱敏摘要位于 `artifacts/gopay-cdp-capture-20260830/FINAL_REDACTED_SUMMARY.md`。
