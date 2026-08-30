# GoPay 浏览器登录态与三账号 one-shot 验证报告

## 结论

任务文档中的三个账号和十个代理已完成脱敏校验与分阶段验证。三个账号均曾在指定 ID 代理上返回 `eligible`，并严格遵守“Checkout 后不再完整重试”：账号 1、2 的浏览器直接 Checkout 实验均返回 HTTP 422；账号 3 恢复原 HTTP Checkout 后成功创建 `cs_`，但把后续 `checkout/update` 放入同一 Playwright 页面时同样返回 HTTP 422。三个账号均未生成 GoPay 长链。

实验结果否定了“只要把受保护支付 POST 改由 Playwright page fetch 发出即可解除 blocked”的假设。最终代码没有保留该失败的 browser-payment transport，而是恢复原支付 HTTP 传输，并保留经证据验证的浏览器状态修复：完整导入资格 HTTP 会话 Cookie、支持 NextAuth `.0/.1` 分片与每任务 attestation、按稳定 account ID 复用设备 profile、浏览器内验证 AT 对应 user，以及脱敏失败形状。

两份成功 HAR 证明，成功链在 Checkout 前已经带有 NextAuth `.0/.1` 和 291 字符 attestation；这些材料不是 Checkout/approve 响应首次产生。Checkout 页面随后还会轮换 NextAuth 分片并设置 `_account`。本次任务只提供 AT 和代理，因此最终稳定缺口仍是与每个 AT 匹配的已登录浏览器会话材料。

## 输入与机会边界

- AT：3 个，均为有效三段 RS256 JWT，签名解码长度 256 字节。
- 代理：10 个；预检时 9 个确认 ID，1 个发生瞬时连接失败，后续资格探测仍按槽位逐一处理。
- 所有凭据仅存在于 Git 忽略的 runtime 文件，验证结束后已删除。
- 账号 1、2 在 Checkout POST 返回 422 后立即停止。
- 账号 3 创建 Checkout 后在 update 返回 422，立即停止。
- 没有账号在 `checkout_committed` 后创建第二个 Checkout。

## 实验过程

### 账号 1

1. 代理槽位 1 曾返回 eligible，后续复验变为 not_eligible。
2. 代理槽位 2 返回 eligible。
3. 真实 Playwright Chrome 完成 account binding、Sentinel token 与 browser fetch Checkout。
4. Checkout POST 返回 HTTP 422，未取得 Checkout ID。
5. 账号机会按保守边界标记为已消费，不再重试。

### 第一次修复

- 将资格 HTTP session 中的 Cloudflare/OAI Cookie 原样合并进 Playwright context。
- Cookie jar 覆盖陈旧 Header，`oai-did` 强制与当前 device 一致。
- 增加 422 脱敏 response keys/长度/SHA-256/category。
- 浏览器 `/backend-api/me` 与 JWT `chatgpt_user_id` 绑定验证成功。

### 账号 2

1. 代理槽位 2 返回 eligible。
2. 资格会话 5 个 Cookie 名成功导入浏览器，浏览器最终观察到 8 个 Cookie；账号绑定为 true。
3. Browser fetch Checkout 仍返回 HTTP 422。
4. 脱敏响应：`response_keys=[detail]`、长度 136、SHA-256 `77c50d1c3152ca63a4b54947d26760b87f4549fd187aa1f92e6e9322b056b1cf`。
5. 未取得 Checkout ID，不再重试。

### 第二次修复

- 撤销 browser fetch Checkout；Checkout 恢复经过验证的 curl_cffi HTTP 路径。
- 仅把 Checkout 后的 ChatGPT update/taxes/approve 作为同浏览器实验。
- 保持同一 sticky proxy、同一 device、同一 Playwright runtime。

### 账号 3

1. 槽位 1、2 返回 not_eligible；槽位 3 返回 eligible。
2. 首次 Playwright 启动出现 pre-Checkout Sentinel 瞬时错误，未发送 Checkout；修复后继续同账号资格扫描。
3. 原 HTTP Checkout 成功，取得 Stripe Checkout 类型并触发唯一一次 `checkout_committed`。
4. 同页面 browser fetch `checkout/update` 返回 HTTP 422。
5. 立即停止，未进入 Stripe confirm/approve，未产生链接。

## HAR 证据

| Evidence | Finding | Path |
|---|---|---|
| E-401：两份成功 HAR 从最早请求就带 NextAuth `.0/.1` | 登录 Cookie 是支付链前置状态，不是 Checkout 响应生成 | `auth.py`、`gopay_sentinel_playwright.py` |
| E-402：Checkout 页面响应后 Cookie 分片轮换并新增 `_account` | 必须让同一浏览器处理页面 Cookie 演进 | Playwright persistent context |
| E-403：两个成功样本 attestation 均为 291 字符但哈希不同 | attestation 属于具体浏览器/部署会话，不能写死或跨账号复用 | `ExtractionConfig.gopay_deployment_attestation` |
| E-404：AT-only Playwright 能访问 `/backend-api/me` 且 user 绑定正确，但不生成 NextAuth/attestation | Bearer 身份与网页登录态是两套不同状态 | account-binding probe |
| E-405：账号 1、2 browser Checkout 均为 422 | 浏览器 fetch 直接替代原 HTTP Checkout 不等价 | 已撤销的实验分支 |
| E-406：账号 3 HTTP Checkout 成功、browser update 422 | browser fetch 对支付 mutation 仍不等价 | 已撤销的实验分支 |

## 最终保留修改

1. JSON 账号材料可携带：

```json
{
  "access_token": "TOKEN",
  "cookies": [
    {"name": "__Secure-next-auth.session-token.0", "value": "COOKIE_CHUNK_0"},
    {"name": "__Secure-next-auth.session-token.1", "value": "COOKIE_CHUNK_1"}
  ],
  "oai-web-deployment-attestation": "ATTESTATION"
}
```

2. NextAuth 分片名称和值保持原样并按数字顺序导入。
3. HTTP Cookie jar 与显式 Cookie Header 合并后进入 Playwright；jar 中的新值优先。
4. device/profile 从稳定 `chatgpt_account_id` 派生，同账号刷新 AT 不再创建新 profile。
5. Playwright 内 `/backend-api/me` 对照 `chatgpt_user_id`，只记录绑定布尔值。
6. Checkout/approve 仍使用原经过测试的 HTTP 协议路径；严格 readiness gate 保留。
7. Checkout 失败只输出 response keys、长度、哈希和分类，不输出响应内容或标识符。

## 当前限制

三个任务账号只提供 AT，未提供匹配的 NextAuth 分片或 attestation；已打开的 CDP 浏览器也没有 ChatGPT 登录 Cookie。因此本轮无法建立与成功 HAR 相同的已登录浏览器状态。继续只更换 Sentinel 外层、billing cadence 或代理不会补出该前置会话。

## 验证命令

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pytest -q tests\test_gopay_browser_session_material.py tests\test_gopay_sentinel_playwright.py tests\test_gopay_isolated_optimization.py tests\test_gopay_live_canary.py
.\.venv\Scripts\python.exe -m pytest -q tests\test_channel_isolation.py tests\test_gcash_support.py tests\test_gopay_support.py
```

本报告不包含 AT、Cookie、代理凭据、Checkout ID、邮箱、订单 ID 或重定向 nonce。
