# GoPay `checkout/approve=blocked` 双 HAR 对齐报告

## 结论

两个完整成功 HAR 证明，`blocked` 的关键不在 approve JSON body，而在 approval proof 的预取时机、browser ping 顺序、Cookie/Stripe 指纹连续性、pending-update 空封套以及动作级 telemetry。本站已一次性对齐这些共同不变量，并保持修改只进入 GoPay 专属模块。

## 证据样本

| 样本 | 摘要 SHA-256 | 原始 HAR SHA-256 |
|---|---|---|
| `gopay-cdp-capture-20260830-180333` | `D63DECD46B73E81D7724A4AAE002796C6B3F70159A080E2F7805E87B9ABF9B7F` | `14280D53DBC2C9E3AB901BDC0C43A007474211D1B017F06B4CF8ABE1E426A2FE` |
| `gopay-cdp-capture-browser-targets-20260830-next` | `9CE7293B924D49CD0BC8FEE6D52D7F450BAE2EDBC3C11C0F7725156476633329` | `8DF5163E0A2D57598B257435C2449EA0371A236C6114BAE85234A94108547E50` |

两份成功时序共同为：

```text
checkout proof → zero-length browser ping → checkout
elements → approval proof → zero-length browser ping
taxes/snapshot → Stripe confirm
zero-length browser ping → checkout/approve=approved → provider redirect
```

## 概率分析

### 高概率

1. **Approval challenge/token 生命周期错误**：旧实现到 approve 才请求 challenge，且先 HTTP `json={}` ping；成功 HAR 均在 Elements 后通过 `SentinelSDK.init(flow)` 预取 challenge，最终 approve 前由 `SentinelSDK.token(flow)` 消费缓存 challenge并执行 browser-origin 零长度 ping。
2. **Stripe `muid` 与 ChatGPT `__stripe_mid` 不一致**：两份 HAR 的 confirm `muid` 与 approve Cookie `__stripe_mid` 逐字节相同。旧实现独立随机生成 Stripe `muid`。
3. **Approve 会话字段错误**：两份 HAR 的 approve 均使用精确空 pending envelope，最终 ping 与 approve Cookie 完全相同；旧实现会携带税费响应的非空更新 receipt，并可能在二者之间再次生成 proof/改变 Cookie。
4. **Telemetry 使用错误来源**：HAR 的 8 元 telemetry 逐项对应紧邻 protected request 的 Sentinel ping 性能与 Cloudflare 响应头；旧实现使用整个提链会话累计时间并随机生成其他字段。

### 中概率

1. **Stripe runtime 自相矛盾**：HAR confirm 的 `version` 与 `payment_user_agent` 均为 `b0f5e7abe5`，旧 GoPay Confirm 使用 `692f102a8f`，但 RV build 已是 `b0f5e7…`。
2. **多余 PaymentMethod 请求**：旧流程在 confirm 前额外调用 `/v1/payment_methods`，两份 HAR 都没有该请求，且返回的 `pm_` 没有被 confirm 使用。

### 低概率

- `/v1/consumers/sessions/lookup` 属于 Stripe Link/consumer 初始化；两份 HAR 都存在，但不是 ChatGPT approve 的必需 body/header，暂不扩大改动面。
- 第一份 HAR 有可选 hCaptcha 流量，第二份没有；现有 passive captcha 可选字段已经覆盖。

## 修改

- `payment_link_extractor/gopay_transport.py`
  - 从两个 HAR 离线提取共同的 `sentinel/20260810913b/sdk.js`，SHA-256 为 `49d0284bf3eea8a59ebcad0e6b5dd8a53edd4c72606f15bbf51ebe5610a88efd`。
  - Elements 后调用 `SentinelSDK.init(checkout_session_approval)` 预取 challenge；approve 前调用 `token(flow)`，不再人工增加 ping。
  - 直接读取新版 SDK 的 `SentinelSDK.timing()`，并用 fetch observer 作为真实 ping telemetry 回退；正常路径不随机伪造。
  - Approve 强制 `{"v":3,"updates":[]}`。
  - 同步 Stripe `muid/sid` 与 ChatGPT browser Cookie；缺少 `__stripe_mid` 时在同一 browser context 动态建立。
- `payment_link_extractor/gopay_cs_live.py`
  - Elements 后只预取 `checkout_session_approval` challenge，不提前缓存会过期的 header token。
  - Taxes 不再额外生成 Sentinel token。
  - Confirm 使用 GoPay 专属 runtime `b0f5e7abe5`。
  - 删除实际流程中未使用的 `/v1/payment_methods` 调用。
  - Confirm 后由同一 browser context 的 `token(flow)` 完成最终 ping并生成新鲜 proof，随即携带当前 Cookie approve。
- `payment_link_extractor/gopay_checkout.py`
  - GoPay Checkout 不发送两份 HAR 都不存在的 `OpenAI-Sentinel-SO-Token`。

## Evidence → Finding → Path

| Evidence | Finding | Path |
|---|---|---|
| E-001：两个 HAR 都在 Elements 后出现 approval challenge req，最终 token 不再发 req | F-001：需要 `init(flow) → token(flow)` 生命周期 | `gopay_transport.py`、`gopay_cs_live.py` |
| E-002：最终 token 内建 ping 与 approve Cookie 哈希在每份 HAR 内完全相同 | F-002：token 后应同步当前 Cookie并立即 approve | `gopay_transport.py`、`gopay_cs_live.py` |
| E-003：Approve pending envelope 两份均为长度 20 的空 updates | F-003：税费 update receipt 不能透传到 approve | `gopay_transport.py` |
| E-004：Confirm `muid == __stripe_mid` 两份均成立 | F-004：Stripe 与 ChatGPT browser 必须共享指标身份 | `gopay_transport.py` |
| E-005：Confirm runtime 两份均为 `b0f5e7abe5`，且无 `/v1/payment_methods` | F-005：GoPay Confirm 必须使用独立 HAR runtime 并直接确认 | `gopay_cs_live.py` |

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_gopay_isolated_optimization.py
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pytest -q `
  tests\test_channel_isolation.py tests\test_gcash_support.py tests\test_gopay_support.py
```

测试覆盖 proof→ping 顺序、零长度 ping、approval proof 复用、当前 Cookie、空 pending、telemetry 值域、Taxes 无 Sentinel、GoPay runtime、禁止多余 PaymentMethod 请求和跨渠道隔离。
