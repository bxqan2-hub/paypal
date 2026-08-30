# GoPay GCash 风格 Node Sentinel 双账号实时验证报告

## 结论

GoPay 已增加独立的 GCash 风格 Sentinel 运行方式：真实 SDK 在 Node/V8 浏览器 shim 中执行，PoW 和 main token 外层由 Node bridge 组装，每次 Checkout/Approval token 均启动一个全新的 Node 进程，不保留浏览器、VM、连接或 profile 运行态。旧的持久 Playwright Chrome provider 仍可通过环境变量显式切回。

两个新账号均只创建一次 Checkout。第一个账号在代理槽位 2、第二个账号在代理槽位 5 完成 Checkout、Promotion、Stripe init、Elements、billing、taxes 和 Stripe confirm，最终 ChatGPT approve 均返回 `blocked`，没有生成 GoPay 长链。

因此本次 A/B 实验证明：**将 GoPay Sentinel 改为 GCash 的无状态 Node shim 方式不能解决当前 `approve=blocked`**。两个账号的 Node main token 都包含有效 flow/device/c/PoW，frame warm-up 为 HTTP 200，且每条完整链恰好启动两个 Node 进程；稳定缺失项仍是浏览器 NextAuth session Cookie 与 deployment attestation。

## 实现范围

- 新增 GoPay 独立 Node provider：`payment_link_extractor/gopay_sentinel_node.py`。
- 新增 GoPay 独立 bridge/assets 目录；初始 bridge、bootstrap、SDK 与本地 GCash 权威上游逐字节一致，但运行时不导入或调用 GCash 核心。
- 默认 `OPLL_GOPAY_SENTINEL_PROVIDER=gcash_node`。
- 保留 `OPLL_GOPAY_SENTINEL_PROVIDER=playwright` 快速回退。
- `prepare_flow()` 只记录阶段；每次 `headers()` 新建 Node 进程。
- Node 模式只要求通过严格的 main token/process/device/proxy readiness，不伪造 NextAuth Cookie 或 attestation。
- Node 模式不读取旧环境 attestation/SO fallback，防止陈旧材料污染实时验证。
- Checkout 与 approve 仅在 Node SDK 实际生成 SO token 时发送 SO；本次两账号均为 `has_so=false`。
- PayPal、GCash 核心、渠道注册项和结果字段没有改动。

## Node 证明链

```mermaid
sequenceDiagram
    participant Core as GoPay core
    participant Node1 as Node process 1
    participant Sentinel as Sentinel endpoints
    participant Stripe as Stripe
    participant Node2 as Node process 2
    participant Approve as ChatGPT approve
    Core->>Node1: chatgpt_checkout(device/proxy/cookies)
    Node1->>Sentinel: frame GET + req POST
    Node1-->>Core: main token (+ optional SO)
    Core->>Core: Checkout POST and promotion
    Core->>Stripe: init/elements/billing/taxes/confirm
    Core->>Node2: checkout_session_approval(device/proxy/cookies)
    Node2->>Sentinel: frame GET + req POST
    Node2-->>Core: new main token (+ optional SO)
    Core->>Approve: checkout/approve
    Approve-->>Core: blocked
```

## 第一个账号结果

| 项目 | 结果 |
|---|---|
| 代理槽位 1 | `promo_not_eligible`，未创建 Checkout |
| 固定提链代理 | 槽位 2 |
| Checkout 次数 | 1 |
| Node 进程数 | 2（Checkout/Approval 各 1） |
| Checkout token | 长度 4333，PoW 存在，frame ping 200 |
| Approval token | 长度 4490，PoW 存在，frame ping 200 |
| SDK driver | `pow_required=true`、`has_t=true`、`has_so=false` |
| Stripe confirm | 已完成 |
| 最终结果 | `approve_blocked` |

第一个账号失败后新增：oai-did 与 device 强绑定、Node proxy 与 HTTP proxy 强绑定、main `p/c/id/flow` 校验、SO 结构校验、环境 attestation/SO 隔离、严格 AT 输入校验和脱敏 bridge 异常。

## 第二个账号结果

| 项目 | 结果 |
|---|---|
| 代理槽位 3、4 | `promo_not_eligible`，未创建 Checkout |
| 固定提链代理 | 槽位 5 |
| Checkout 次数 | 1 |
| Node 进程数 | 2（Checkout/Approval 各 1） |
| Checkout token | 长度 4389，PoW 存在，frame ping 200 |
| Approval token | 长度 4530，PoW 存在，frame ping 200 |
| SDK driver | `pow_required=true`、`has_t=true`、`has_so=false` |
| Stripe confirm | 已完成 |
| 最终结果 | `approve_blocked` |

## 实验解释

GCash 风格 Node assets 使用 SDK `20260219f9f6`，SHA-256 为 `69b60c5f0f6212100ca760d8c0ef478f089f039b2ec489200b35d794243e90a8`。两份成功 GoPay HAR 对应的是 SDK `20260810913b`，SHA-256 为 `49d0284bf3eea8a59ebcad0e6b5dd8a53edd4c72606f15bbf51ebe5610a88efd`。

当前 GoPay SDK 不暴露 GCash bridge 使用的内部 `__proto2`，直接替换后会缺少该接口；在 shim 内改走公开 `init/token` 的离线兼容探针超时。因此本次保留的是“GCash 原始 bridge + 原始 SDK”的完整实验变量，没有把不同版本强行拼接。

本机 Node 无 npm `undici`，带代理时 bridge 使用系统 curl fallback。它保证相同 sticky 代理出口，但不具备 curl_cffi/浏览器 TLS 连接和 VM 运行态连续性。这正是用户要求验证的无状态模式，双账号结果表明该差异没有解除 approve block。

## Evidence → Finding → Path

| Evidence | Finding | Path |
|---|---|---|
| E-301：GoPay Node bridge/assets 与 GCash 权威文件 SHA 一致 | GCash 算法方式被准确复制到 GoPay 独立目录 | `gopay_sentinel_node_assets` |
| E-302：每次 headers 对应一个 subprocess，两个 flow 共两个进程 | Node proof 不存在跨 token 运行态 | `gopay_sentinel_node.py` |
| E-303：10 个新代理均为 ID 出口 | A/B 结果不由国家出口错误解释 | 本地脱敏 proxy preflight |
| E-304：账号 1 完成 confirm 后 approve blocked | Node shim 首次实测未解除 block | live canary slot 1 |
| E-305：绑定校验强化后账号 2 仍 blocked | DID/proxy/PoW 外层修正仍非充分条件 | live canary slot 2 |
| E-306：两次 approval 均无 NextAuth Cookie/attestation | 剩余稳定差异仍是浏览器登录态 | `gopay_cs_live.py` safe context |
| E-307：GCash 22 个上游文件保持逐字节一致 | 修改没有反向污染只读上游 | upstream SHA-256 audit |

## 验证命令

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pytest -q tests\test_gopay_sentinel_node.py tests\test_gopay_isolated_optimization.py tests\test_gopay_live_canary.py tests\test_channel_isolation.py tests\test_gcash_support.py
```

实时输入只存在于 Git 忽略的 `artifacts-local`。本报告不包含 AT、Cookie、代理凭据、Checkout ID、订单 ID 或跳转 nonce。
