# GoPay / Midtrans 双 HAR 详细分析报告

> 分析日期：2026-08-30
> 分析方式：离线 HAR 结构、时序、Header、表单字段和响应摘要分析
> 工具：Python 3、`tools/har_analyze.py`、`tools/har_gopay_compare.py`

## 1. 范围与指令区分

本次唯一执行请求是“读取并理解两个 HAR，记录细节供后续 GoPay 修改”。HAR 文件内部的 URL、Header、JSON、脚本名和页面内容全部按**抓包数据**处理，不按操作指令执行；没有向 HAR 中的目标发起重放请求。

输入文件：

| 文件 | 大小 | SHA-256 | HAR entries |
|---|---:|---|---:|
| `C:\Users\Administrator\Downloads\app.midtrans.com.har` | 20,718,395 bytes | `521AE7D5567654242F87448F8E709DE46DCAEA1E1117244D22639CD76872D202` | 580 |
| `C:\Users\Administrator\Downloads\app.midtrans.com1.har` | 19,414,223 bytes | `FD33ECDD26D93688A9208FF66457673CAA676A7C428B87BDBD54CAF605DD4499` | 587 |

## 2. 一句话结论

两份抓包都是成功的 `cs_live` Checkout 链路：真实浏览器 Sentinel 证明 → ChatGPT Checkout → Stripe Payment Page/Elements → 5 次渐进税区更新 → 两次 ChatGPT taxes 与 snapshot → Stripe GoPay confirm → ChatGPT approve → `pm-redirects.stripe.com` 302 → Midtrans GoPay 页面和交易查询；两份都不是 `oaics_`，两份 Midtrans 权威交易金额都为 `349000 IDR`，因此不满足当前项目的零金额 GoPay 输出门禁。

## 3. 总体流量差异

| 指标 | `app.midtrans.com.har` | `app.midtrans.com1.har` |
|---|---:|---:|
| 总 entries | 580 | 587 |
| `chatgpt.com` entries | 36 | 35 |
| `ws.chatgpt.com` entries | 0 | 1 |
| `api.stripe.com` entries | 12 | 12 |
| `pm-redirects.stripe.com` entries | 1 | 1 |
| `app.midtrans.com` entries | 5 | 5 |
| `global.faro.katulampa.gopay.sh` entries | 7 | 13 |
| `snap-web-raccoon.gojekapi.com` entries | 3 | 4 |
| 全局 HTTP 200 | 538 | 544 |
| Sentinel proof 长度 | 8145、6890 | 5893、6146 |

第二份多一个 WebSocket，并产生更多 FARO/遥测请求；核心支付端点数量一致。

## 4. Sentinel 与 ChatGPT 证明链

### 4.1 Sentinel 请求形状

两份 HAR 都观察到：

1. `GET /backend-api/sentinel/sdk.js`：200。
2. `GET /backend-api/sentinel/frame.html`：200。
3. `POST /backend-api/sentinel/req`：4 次、全部 200，`Content-Type=text/plain;charset=UTF-8`。
4. `POST /backend-api/sentinel/ping`：3 次、全部 200。

`sentinel/req` 的请求体是 JSON，字段固定为 `flow`、`id`、`p`：

| 文件 | `chatgpt_checkout` | `checkout_session_approval` | `id` 长度 | `p` 长度 |
|---|---:|---:|---:|---:|
| `app.midtrans.com.har` | 2 | 2 | 36 | 777 |
| `app.midtrans.com1.har` | 2 | 2 | 36 | 613 |

两份均没有 `flow=default`。这直接支持后续代码固定使用 `chatgpt_checkout`，不要把 SDK 默认值 `default` 传到受保护 Checkout 请求。

### 4.2 证明 Header 与设备连续性

在 Checkout、taxes、snapshot、approve 请求中，以下字段保持同一会话内连续：

- `oai-device-id`：UUID 形状，长度 36。
- `oai-session-id`：UUID 形状，长度 36。
- `oai-web-deployment-attestation`：长度 291。
- `oai-language=id-ID`。
- `oai-client-build-number=10012890`。
- `oai-client-version=prod-7890a3be6202572c0e8e3bb4907574d660b4e4f4`。
- `x-oai-is-client-observation`：长度 23，按请求变化。
- User-Agent：Chrome 151 / Windows，Midtrans 页面也使用 `id-ID,id;q=0.9`。

`OpenAI-Sentinel-Token` 只在初始 Checkout 和 approve 中出现：

| 文件 | Checkout proof 长度 | Approve proof 长度 |
|---|---:|---:|
| `app.midtrans.com.har` | 8145 | 6890 |
| `app.midtrans.com1.har` | 5893 | 6146 |

两份 HAR 的请求 Cookie 数组为空，未直接观察到 `oai-did` Cookie，也未观察到 `OpenAI-Sentinel-SO-Token`。因此“CDP 读取 HttpOnly `oai-did` 并写入 `oai-device-id`”仍是代码实现策略，但不能声称这两个 HAR 直接保存了 Cookie 值。

### 4.3 ChatGPT 端点状态

| 端点 | 次数 | 状态 |
|---|---:|---|
| `POST /backend-api/payments/checkout` | 1 | 200 |
| `POST /backend-api/payments/checkout/taxes` | 2 | 200、200 |
| `POST /backend-api/payments/checkout/snapshot` | 2 | 204、204 |
| `POST /backend-api/payments/checkout/approve` | 1 | 200 |

这几条 ChatGPT payment 请求的 `postData` 未被 HAR 导出（`bodySize=0` 且没有 `postData` 对象），响应正文也未保存；只能确认 Header、状态码和时序，不能从这两份文件推断 Checkout/taxes/approve 的完整 JSON body。

## 5. Stripe Payment Page 细节

### 5.1 初始化与 Elements

两份 HAR 的 Stripe init 都是：

```text
POST https://api.stripe.com/v1/payment_pages/<CS_SESSION>/init -> 200
GET  https://api.stripe.com/v1/elements/sessions -> 200
```

Stripe 请求使用 `Accept: application/json`、`Content-Type: application/x-www-form-urlencoded`、`Origin: https://js.stripe.com`、`Referer: https://js.stripe.com/`。

init 表单包含 13 个字段，关键值为：

- `_stripe_version=2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1`
- `browser_locale=id-ID`
- `browser_timezone=Asia/Jakarta`
- `elements_session_client[locale]=id-ID`
- `elements_init_source=custom_checkout`
- `referrer_host=chatgpt.com`
- `elements_options_client[saved_payment_method][enable_save]=auto`
- `elements_options_client[saved_payment_method][enable_redisplay]=auto`

Elements session 查询包含 19 个键，关键值为：

| 字段 | 值 |
|---|---|
| `deferred_intent[mode]` | `subscription` |
| `deferred_intent[amount]` | `34900000` |
| `deferred_intent[currency]` | `idr` |
| `deferred_intent[setup_future_usage]` | `off_session` |
| `deferred_intent[payment_method_types][0]` | `card` |
| `deferred_intent[payment_method_types][1]` | `gopay` |
| `currency` | `idr` |
| `locale` | `id` |
| `type` | `deferred_intent` |

### 5.2 五次渐进税区更新

两份 HAR 都明确显示 5 次 Stripe Payment Page POST，字段是累积式增长：

| 步骤 | 新增/保留字段 | `field_count` |
|---:|---|---:|
| 1 | `tax_region[country]` | 1 |
| 2 | `country` + `line1` | 2 |
| 3 | `country` + `line1` + `city` | 3 |
| 4 | `country` + `line1` + `city` + `state` | 4 |
| 5 | `country` + `line1` + `city` + `state` + `postal_code` | 5 |

当前 GoPay 副本的 [`gopay_cs_live.py`](../payment_link_extractor/gopay_cs_live.py) `cs_update_tax_region()` 会把已有地址字段一次性放进一个 POST；这是后续最明确的协议对齐候选点。修改时应保留同一个 Elements session、Stripe JS ID、Checkout config/checksum，并按上表依次发送。

### 5.3 Confirm

两份 confirm 都是 62 个表单字段，关键值一致：

- `expected_payment_method_type=gopay`
- `payment_method_data[type]=gopay`
- `link_brand=link`
- `expected_amount=34900000`
- `version=b0f5e7abe5`
- `payment_method_data[time_on_page]`：第一份 `41223`，第二份 `47421`
- `init_checksum`：长度 32（具体值不同）
- `js_checksum`：第一份长度 96，第二份长度 100
- 包含 guid/muid/sid、账单地址、Elements session/config、Checkout config、税区、归因元数据和 passive captcha 字段

## 6. Approve、Stripe redirect 与 Midtrans

### 6.1 Redirect

两份均观察到：

```text
GET https://pm-redirects.stripe.com/authorize/<ACCOUNT>/<NONCE> -> 302
Location: https://app.midtrans.com/snap/v4/redirection/<UUID>
```

provider redirect 的下一跳主机是 `app.midtrans.com`，不是任意外部主机。

### 6.2 Midtrans 页面与交易响应

每份 HAR 均有：

1. `GET /snap/v4/redirection/<UUID>`：200，HTML 页面。
2. `GET /snap/v1/transactions/<UUID>`：200，JSON。
3. `POST /snap/v1/promos/<UUID>/search`：200。
4. `GET /snap/v3/experiment`：200。

交易 JSON 的非敏感摘要完全一致：

| 字段 | 值 |
|---|---|
| `transaction_details.gross_amount` | `349000` |
| `transaction_details.currency` | `IDR` |
| `recommended_payment_method` | `gopay` |
| `enabled_payments` | `gopay`、`qris` |
| `gopay.tokenization` | `true` |
| `gopay.enforce_tokenization` | `true` |
| `x-source` | `snap` |
| `x-source-app-type` | `redirection` |
| `x-source-version` | `2.3.0` |
| `Accept-Language` | `id-ID,id;q=0.9` |

Stripe confirm/Elements 使用 `34900000`，Midtrans 交易使用 `349000`。这不是两个抓包之间的差异，而是同一链路中两个系统的金额表示差异；后续修改应先确认币种小数位/单位转换，不应直接把一个值覆盖另一个值。当前项目的 GoPay 零金额门禁会将这两份样本判定为非零并拒绝输出 `gopay_url`。

## 7. 两份 HAR 的时序差异

### 7.1 `app.midtrans.com.har`

```text
checkout 16:45:45.813
-> Stripe init 16:45:52.546
-> Elements 16:45:56.909
-> taxes 16:46:27.752
-> snapshot 16:46:28.255
-> taxes 16:46:30.460
-> snapshot 16:46:30.959
-> confirm 16:46:33.595
-> approve 16:46:34.764
-> Stripe 302 16:46:38.400
-> Midtrans page 16:46:40.597
-> transaction 16:46:44.202
```

从 Checkout 到交易查询约 58.389 秒；approve 本身耗时 2835.26 ms。

### 7.2 `app.midtrans.com1.har`

```text
checkout 18:49:49.488
-> Stripe init 18:49:56.147
-> Elements 18:49:59.892
-> snapshot 18:50:31.906
-> snapshot 18:50:34.415
-> taxes 18:50:34.442
-> taxes 18:50:39.086
-> confirm 18:50:43.107
-> approve 18:50:45.613
-> Stripe 302 18:50:51.831
-> Midtrans page 18:50:55.445
-> transaction 18:51:09.346
```

从 Checkout 到交易查询约 79.858 秒；approve 本身耗时 4387.76 ms。第二份中 snapshot 与 taxes 的开始时间交错，说明页面存在并行/异步背景请求，不能把 HAR 的单一总排序硬编码成唯一状态机。

## 8. 当前代码的后续修改建议

1. **税区协议**：在 `gopay_cs_live.py` 增加 GoPay 专属 1→5 字段渐进更新；不要改共享 PayPal flow。
2. **金额单位**：同时保留 Stripe 的 `expected_amount=34900000` 与 Midtrans 的 `gross_amount=349000` 证据，明确转换规则后再决定是否加入归一化字段；零金额门禁继续在最终结果前执行。
3. **Sentinel flow**：保持 `chatgpt_checkout`，approve 使用新的 proof；`checkout_session_approval` 是 Sentinel 内部请求体 flow，不等同于把 Checkout API 的 flow 改成该值。
4. **设备连续性**：继续在真实浏览器中生成 token，读取 HttpOnly `oai-did` 后同步 `oai-device-id`；不要把 HAR 中的 proof、JWE、Cookie 或 nonce 固定写入代码。
5. **请求并行性**：允许 taxes/snapshot 的后台事件出现不同先后，只对业务确认点（Stripe confirm、approve、302、Midtrans transaction）做严格状态校验。
6. **响应解析**：保留 `cs_`/`oaics_` 分类和支付方式合并去重，但这两份样本只能证明 `cs_live` 分支；不能据此删除 OAICS 兼容逻辑。

## 9. Evidence → Finding → Path

### Scope 摘要

- `scope_type`: offline file analysis
- `in_scope`: 两个用户提供的 HAR 文件和当前仓库 GoPay 副本
- `network_profile`: no replay; no new network request
- `scope_ref`: 本报告第 1 节；本次没有生成网络目标 scope 文件，因为只读取本地附件

### Evidence

#### E-001

- title: 两份 HAR 文件身份
- observed_at: 2026-08-30
- source_type: file
- source_ref: `C:\Users\Administrator\Downloads\app.midtrans.com.har`、`app.midtrans.com1.har`
- content_hash: `521AE7D5567654242F87448F8E709DE46DCAEA1E1117244D22639CD76872D202`; `FD33ECDD26D93688A9208FF66457673CAA676A7C428B87BDBD54CAF605DD4499`
- repro_command: `Get-FileHash -Algorithm SHA256 C:\Users\Administrator\Downloads\app.midtrans.com.har,C:\Users\Administrator\Downloads\app.midtrans.com1.har`
- raw_excerpt: `entries=580/587; sizes=20718395/19414223`
- linked_workitem: WI-001

#### E-002

- title: Sentinel flow 与 proof Header
- observed_at: 2026-08-30
- source_type: command
- source_ref: `tools/har_gopay_compare.py`
- content_hash: n/a
- repro_command: `\.venv\\Scripts\\python.exe tools\\har_gopay_compare.py C:\\Users\\Administrator\\Downloads\\app.midtrans.com.har C:\\Users\\Administrator\\Downloads\\app.midtrans.com1.har --output artifacts\\gopay-har-dual-analysis-20260830\\DUAL_ANALYSIS.md`
- raw_excerpt: `sentinel_flows={chatgpt_checkout:2, checkout_session_approval:2}; proof lengths=[8145,6890]/[5893,6146]; oai-device-id length=36`
- linked_workitem: WI-002

#### E-003

- title: Stripe 五次渐进税区更新
- observed_at: 2026-08-30
- source_type: network
- source_ref: `DUAL_ANALYSIS.md` Stripe contract section
- content_hash: n/a
- repro_command: `\.venv\\Scripts\\python.exe tools\\har_analyze.py <HAR> --format markdown --host stripe.com --limit 200`
- raw_excerpt: `field_count 1,2,3,4,5; country -> line1 -> city -> state -> postal_code`
- linked_workitem: WI-003

#### E-004

- title: Midtrans GoPay 交易摘要
- observed_at: 2026-08-30
- source_type: network
- source_ref: `app.midtrans.com` `/snap/v1/transactions/<UUID>` entries
- content_hash: n/a
- repro_command: `\.venv\\Scripts\\python.exe tools\\har_analyze.py <HAR> --format json --host app.midtrans.com --contains gopay --limit 100`
- raw_excerpt: `gross_amount=349000; currency=IDR; recommended=gopay; enabled=[gopay,qris]`
- linked_workitem: WI-004

#### E-005

- title: ChatGPT payment body omission
- observed_at: 2026-08-30
- source_type: network
- source_ref: Checkout/taxes/snapshot/approve HAR entries
- content_hash: n/a
- repro_command: `\.venv\\Scripts\\python.exe tools\\har_gopay_compare.py <HAR1> <HAR2> --output <report>`
- raw_excerpt: `postData absent; bodySize=0; response content text omitted`
- linked_workitem: WI-005

### Findings

#### F-001

- title: `chatgpt_checkout` 是两份样本的有效 Sentinel flow
- severity: info
- category: reverse_algo
- status: validated
- evidence_ids: [E-002]
- location: `chatgpt.com/backend-api/sentinel/req`
- impact: 后续 GoPay proof 调用若使用 `default`，会偏离抓包契约；`checkout_session_approval` 只出现在 Sentinel 内部请求体。
- confidence: high

#### F-002

- title: GoPay 业务请求绑定真实浏览器身份
- severity: info
- category: design
- status: validated
- evidence_ids: [E-002]
- location: ChatGPT Checkout/taxes/snapshot/approve headers
- impact: device/session/attestation/client build/version 必须在同一浏览器会话中连续；HAR 未直接保存 Cookie，因此 `oai-did` 同步逻辑仍需通过 CDP 实现并单独验证。
- confidence: high

#### F-003

- title: Stripe 地址更新是五步累积提交
- severity: medium
- category: design
- status: validated
- evidence_ids: [E-003]
- location: `api.stripe.com/v1/payment_pages/<CS_SESSION>`
- impact: 一次性提交地址字段可能改变 config/税务响应时序；这是当前 GoPay flow 最明确的后续对齐点。
- confidence: high

#### F-004

- title: Stripe 与 Midtrans 的金额表示不同且样本非零
- severity: medium
- category: design
- status: validated
- evidence_ids: [E-004]
- location: Stripe Elements/confirm 与 Midtrans transaction
- impact: `34900000` 与 `349000` 不能直接互换；两份样本都不应通过当前零金额输出门禁。
- confidence: high

#### F-005

- title: HAR 缺少 ChatGPT payment body
- severity: info
- category: other
- status: validated
- evidence_ids: [E-005]
- location: Checkout/taxes/snapshot/approve entries
- impact: body 字段只能来自代码或后续完整抓包，不能从这两个文件臆测。
- confidence: high

### Path

#### P-001

- title: GoPay `cs_live` 调用与数据流
- path_type: callflow
- start: Sentinel browser bootstrap
- goal: provider redirect 到 Midtrans transaction
- steps:
  1. action: 加载 Sentinel SDK、发送 `sentinel/req` 和 ping；evidence: E-002；finding: F-001
  2. action: 创建 `cs_live` Checkout 并保持 device/session/attestation/client Header；evidence: E-002；finding: F-002
  3. action: Stripe init 与 Elements session；evidence: E-003；finding: F-003
  4. action: 依次提交 5 次 tax_region，再执行两次 taxes/snapshot 刷新；evidence: E-003；finding: F-003
  5. action: 发送 62 字段 Stripe GoPay confirm；evidence: E-003；finding: F-004
  6. action: ChatGPT approve 后接收 Stripe 302 到 Midtrans；evidence: E-004；finding: F-004
  7. action: 查询 Midtrans transaction，确认 `IDR`、`gopay`、`qris` 和金额；evidence: E-004；finding: F-004
- residual_risks: ChatGPT payment body/response 未被导出；金额单位转换和后台请求并行策略仍需后续完整抓包验证。

### Timeline 摘要

1. 2026-08-29 16:45:45Z：第一份 HAR Checkout 200。
2. 2026-08-29 16:46:33Z：第一份 Stripe confirm 200。
3. 2026-08-29 16:46:38Z：第一份 Stripe authorize 302。
4. 2026-08-29 16:46:44Z：第一份 Midtrans transaction 200。
5. 2026-08-29 18:49:49Z：第二份 HAR Checkout 200。
6. 2026-08-29 18:50:43Z：第二份 Stripe confirm 200。
7. 2026-08-29 18:50:51Z：第二份 Stripe authorize 302。
8. 2026-08-29 18:51:09Z：第二份 Midtrans transaction 200。

## 10. 可复现命令与产物

```powershell
$env:PYTHONPATH='.'
\.venv\Scripts\python.exe tools\har_analyze.py `
  'C:\Users\Administrator\Downloads\app.midtrans.com.har' `
  --format markdown --host chatgpt.com --limit 200

\.venv\Scripts\python.exe tools\har_gopay_compare.py `
  'C:\Users\Administrator\Downloads\app.midtrans.com.har' `
  'C:\Users\Administrator\Downloads\app.midtrans.com1.har' `
  --output 'artifacts\gopay-har-dual-analysis-20260830\DUAL_ANALYSIS.md'

\.venv\Scripts\python.exe -m pytest -q tests/test_har_gopay_compare.py
```

已生成的脱敏对比结果：

- [`DUAL_ANALYSIS.md`](../artifacts/gopay-har-dual-analysis-20260830/DUAL_ANALYSIS.md)
- [`har_gopay_compare.py`](../tools/har_gopay_compare.py)
- [`test_har_gopay_compare.py`](../tests/test_har_gopay_compare.py)
