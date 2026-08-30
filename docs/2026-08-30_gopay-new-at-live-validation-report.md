# GoPay 新 AT 实时提链验证报告

## 结论

新代理池的 10 个出口均在线且落地印度尼西亚。新 AT 在正确清理聊天转义后，于代理槽位 1 检测到 `eligible`，并在同一代理、同一 Playwright Chrome profile 中完成 Checkout、Promotion、Stripe init、Elements、billing、taxes 和 Stripe confirm；最终 ChatGPT `checkout/approve` 仍返回 `blocked`，没有产生 GoPay 链接。

本次还定位出一个会造成假 401 的输入缺陷：用户粘贴的 JWT 签名含多重 Markdown 转义，旧 canary 直接从 JSONL 原始文本做正则，错误吞入 JSON 的 `\n` 转义，得到 258 字节签名并触发 `Could not parse your authentication token`。现在 canary 先解析 JSONL 消息，再清理所有 JWT 非法反斜杠；正确结果为 256 字节 RS256 签名。

结合此前两份成功 HAR、四个旧 AT 和本次新 AT，剩余稳定差异进一步收敛为浏览器登录态：成功 HAR 同时拥有 NextAuth session-token Cookie 与 291 字符 deployment attestation；本次 AT-only profile 两者均不存在。后续流程新增 pre-Checkout readiness gate，缺少任一项时在 Checkout POST 前停止，避免继续消耗账号唯一机会。

## 输入证据（脱敏）

- 新 AT：`1` 个；规范化 SHA-256：`EC33C0F2D5E0F7BFE93057C533F24A1D351C475CD3168EF93E3E4B0031963B0C`
- JWT 段长度：`106 / 1464 / 342`
- RS256 签名解码长度：`256` 字节
- 过期状态：未过期
- 新代理：`10` 个；全部为 `ID`，10 个出口 IP 哈希均不同
- 运行时输入仅保存在 Git 忽略的 `artifacts-local`，没有进入报告、日志或提交

## 实时执行

### 代理预检

```text
PROXY_COUNT=10
PROXY_SLOT=1..10 STATUS=OK COUNTRY=ID ID_MATCH=True
VALID_ID_SLOTS=1,2,3,4,5,6,7,8,9,10
```

### 初次假 401

旧 loader 从序列化 JSONL 原文提取出长度 `1916`、签名 `258` 字节的错误 token；服务端返回：

```text
HTTP 401
Could not parse your authentication token. Please try signing in again.
```

该请求只执行资格 GET，没有创建 Checkout，不消耗账号机会。

### 规范化后完整流程

```text
proxy_slot=1
eligibility_check
eligibility_confirmed
checkout
checkout_committed
checkout_kind:stripe_checkout
checkout_update
promotion_applied
stripe_init
elements_session
taxes
payment_confirmation
approve_blocked
```

- Checkout 次数：`1`
- 固定代理槽位：`1`
- Stripe confirm：已到达并完成
- ChatGPT approve：HTTP 200 业务结果 `blocked`
- GoPay URL：未产生

## Approval 脱敏上下文

```text
sentinel_provider=PlaywrightSentinelProvider
browser_channel=chrome
browser_version=151.0.7922.170
persistent_runtime=true
persistent_profile=true
sdk_sha256=49d0284bf3eea8a59ebcad0e6b5dd8a53edd4c72606f15bbf51ebe5610a88efd
approval_token_length=4426
approval_init=req(body>0,frame) -> ping(body=0,checkout)
approval_token=ping(body=0,checkout)
pending_updates_length=20
attestation_length=0
nextauth_session_cookie=false
```

## Evidence → Finding → Path

| Evidence | Finding | Path |
|---|---|---|
| E-201：错误签名为 258 字节，服务端无法解析 | JSONL 必须先反序列化，再提取 JWT | `tools/gopay_live_canary.py` |
| E-202：正确签名为 256 字节且资格为 eligible | 新 AT 与代理槽位 1 有效 | canary runtime |
| E-203：真实 Chrome、精确 SDK、proof/ping 顺序均已生效 | 本次 blocked 不是 Node/jsdom token | `gopay_sentinel_playwright.py` |
| E-204：成功 HAR 有 session-token+attestation，本次两者均缺 | AT-only profile 与成功浏览器登录态仍不等价 | `gopay_checkout.py` readiness gate |
| E-205：Checkout 只发送一次 | 不可逆机会门禁有效 | `web/tasks.py`、canary |

## 新增保护

零元试用模式在 Checkout POST 前要求：

```text
real Playwright Sentinel token
+ __Secure-next-auth.session-token 或分片 Cookie
+ deployment attestation 长度 >= 64
```

缺失时返回 `browser_session_incomplete`、HTTP 412 语义错误，并设置 `retryable=False`；不会执行 Checkout POST。

## 复验命令

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pytest -q tests\test_gopay_live_canary.py tests\test_gopay_isolated_optimization.py tests\test_gcash_support.py
```
