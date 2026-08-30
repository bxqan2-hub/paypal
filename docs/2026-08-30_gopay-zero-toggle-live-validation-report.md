# GoPay 关闭 0 元开关实测修复报告

## 结论

关闭 `gopay_zero_trial_validation` 后的第一次真实提链暴露了两个 provider 子流程中的历史重复金额门禁。该门禁已从 CS/OAICS provider 层移除，金额判断现在只由 `gopay_core.py` 的开关分支统一控制：关闭时完整执行 Checkout 到长链返回，开启时在 provider 返回后执行最终 0 元校验。

## 失败概率分析

| 概率 | 原因 | 实测证据 | 处理 |
|---|---|---|---|
| 高 | CS/OAICS provider 内仍无条件调用 `validate_gopay_amount(..., promotion_applied=True)` | 第一次尝试已完成 Checkout、update、Stripe init/elements/tax_region/taxes，随后以 `expected zero amount, got 34900000` 失败 | 移除 provider 层重复门禁，保留 `gopay_core.py` 的唯一开关门禁 |
| 中 | Checkout 会话可能瞬时失活 | 一次 confirm 返回 `checkout_not_active_session`；后续全新任务首轮 confirm、redirect 均成功 | 保留失败后整条链路重建 |
| 低 | 个别随机代理或 Sentinel 浏览器证明偶发失败 | 观察到一次 `ERR_TUNNEL_CONNECTION_FAILED`，任务层随后切换代理 | 保留 10 个代理的无放回随机轮换 |

## HAR 核对

样本 `artifacts-local/gopay-cdp-capture-browser-targets-20260830-next.har` 的 SHA-256 为 `8DF5163E0A2D57598B257435C2449EA0371A236C6114BAE85234A94108547E50`。脱敏解析显示：

- Checkout、Stripe init/elements/confirm、approve、redirect 和 Midtrans transaction 均完整。
- ChatGPT taxes 权威金额为 `34900000`，Midtrans 金额为 `349000 IDR`。
- 非零金额没有阻断浏览器继续完成 confirm、approve 和 Midtrans 长链，因此关闭开关时 provider 子流程不得提前执行零金额拒绝。

## Evidence → Finding → Path

| Evidence | Finding | Path |
|---|---|---|
| E-001：实测在 taxes 后、payment confirmation 前抛出零金额错误 | F-001：provider 层存在绕过新开关的旧门禁 | `payment_link_extractor/gopay_cs_live.py`、`payment_link_extractor/gopay_oaics.py` |
| E-002：HAR 的非零链路继续到 confirm/approve/Midtrans | F-002：关闭开关时应允许非零 provider 完整生成 | `payment_link_extractor/gopay_core.py` |
| E-003：核心层已按开关执行最终校验 | F-003：provider 层重复门禁可删除，且开启行为仍由核心保持 | `tests/test_gopay_isolated_optimization.py` |

## 验证命令

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_gopay_isolated_optimization.py
.\.venv\Scripts\python.exe -m pytest -q
```

回归测试同时断言 CS/OAICS provider 源码不再包含 `validate_gopay_amount`，避免以后再次绕过网页开关。

## 真实结果

修复后以关闭开关、10 个运行时代理随机计划重新执行：第 1 次尝试依次完成 `checkout`、`checkout_update`、`stripe_init`、`elements_session`、`taxes`、`payment_confirmation`、`redirect_resolution`，最终状态为 `succeeded`。返回渠道为 GoPay、币种为 IDR、权威金额为 `34900000`，长链主机为 `app.midtrans.com`；完整运行时长链未写入报告或 Git。
