# GoPay cccy 整链日志对齐报告

## 结论

cccy 日志最后一段适合用于提高 **Stripe GoPay 链路稳定性**，不能单独解释 ChatGPT `approve=blocked`：其统计已经明确显示 Stripe confirm `10/10 OK`，但 ChatGPT approve 仍为 `10/10 blocked`。

本站已吸收其中可独立验证的三项：

1. `amount <= 50` 的 bootstrap/promotion/taxes 多层 fail-fast；最终结果仍必须严格等于 0。
2. billing 改为四个累积阶段：`country → partial(line1) → final(city+postal_code) → state`。
3. Elements 在税费刷新后无条件携带原 `elements_session_id` 强制 reuse。
4. 增加 GoPay 专属 Checkout/Promotion/Provider/Approve 四段逻辑代理；默认仍为 `A/A/A/A`，只有显式配置才拆分。

cccy 的“失败后整链重建 10 次”没有直接启用。当前账号一旦发送 Checkout POST，仍禁止创建第二个 Checkout；网络重试只允许发生在同一个 Checkout 和同一个逻辑代理段内。

## 日志映射

| cccy 阶段 | 当前实现 | 结果 |
|---|---|---|
| Checkout inline promo | `gopay_checkout.create_checkout` | 保留 |
| Custom Checkout amount gate | Checkout/Stripe init fail-fast | 新增 |
| Promotion update/refresh gate | `update_checkout` 后金额守门 | 新增 |
| Elements session | 初次创建 + 税费后强制 reuse | 已对齐 |
| Billing address | country/partial/final/state | 已对齐 |
| Taxes/final refresh | 两轮 snapshot/taxes/page GET + `<=50` | 已对齐 |
| Stripe confirm | 同一 Stripe session/provider 段 | 保持 |
| ChatGPT approve | 独立 approval Sentinel/HTTP 段 | 显式化 |
| Full rebuild ×10 | Checkout 后禁止重建 | 未启用 |

## 四段代理语义

| 字段 | 作用 | 默认值 |
|---|---|---|
| `gopay_checkout_proxy` | 资格检测、Checkout、Checkout Sentinel | 本轮选中的代理 A |
| `gopay_promotion_proxy` | Checkout update | A |
| `gopay_provider_proxy` | Stripe、taxes/snapshot、confirm/poll | A |
| `gopay_approve_proxy` | approval Playwright Sentinel、ChatGPT approve | A |

显式配置 A/B/C/D 时：

- 资格检测与 Checkout 永远使用 A。
- Promotion 请求使用 B，结束后不改变其他段。
- Stripe 与 ChatGPT taxes 使用 C；Elements session ID 和 Stripe HTTP session保持连续。
- approval 预取时关闭 A 的浏览器 context，用同一持久 device profile 在 D 上创建 approval context；approve HTTP 也切到 D。

## 金额守门

```text
Checkout response amount present  → require <= 50
Promotion refresh amount present  → require <= 50
Stripe custom checkout bootstrap  → require <= 50 and require present
Each taxes/page refresh            → require <= 50 and require present
Final returned GoPay link          → require == 0
```

`51+` 会在进入 confirm/approve 前抛出协议错误，避免继续消耗后续链路。

## Evidence → Finding → Path

| Evidence | Finding | Path |
|---|---|---|
| E-101：cccy confirm 10/10，approve 0/10 | Phase 3–5 主要提高 Stripe 稳定性 | `gopay_cs_live.py` |
| E-102：cccy 三处 amount refresh 均为 0 | 应在每个权威响应后 fail-fast | `gopay_core.py`、`gopay_cs_live.py` |
| E-103：cccy billing 为累积阶段 | 一次性全字段不等价 | `gopay_cs_live.py` |
| E-104：cccy 强制 reuse Elements | amount 未变化也应复用原 session ID | `gopay_cs_live.py` |
| E-105：cccy 四段均为 ID sticky route | 四段应显式且整轮不可变 | `models.py`、`gopay_core.py`、`gopay_transport.py` |

## 验证命令

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pytest -q tests\test_gopay_isolated_optimization.py tests\test_extraction_full_retry.py tests\test_gopay_support.py tests\test_frontend_error_display.py
```
