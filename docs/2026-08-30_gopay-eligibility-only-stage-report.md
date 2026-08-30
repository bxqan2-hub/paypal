# GoPay 第一步：0 元试用资格独立检测

## 结论

GoPay 新增了独立的第一步检测入口 `probe_gopay_zero_trial_eligibility`。该入口只调用 ChatGPT 的优惠资格接口，不创建 Checkout、不初始化 Stripe、不更新账单、不确认支付，也不生成后续长链。

检测会将账号、印度尼西亚 `ID/IDR` 配置和本次选中的 ID 代理绑定在同一会话中。代理池按任务随机、无放回尝试；遇到已验证但不具备资格的结果时继续下一个代理，遇到第一个 `state=eligible` 时立即停止并返回。

## 参考实现

参考仓库 `Torin-x/GPT-utral-platform` 的提交 `68a1f8faede7e41f10ac5f9af267465fa61d0e3d`：

- `vendor/turb_gpt_free_register/core/subscription_status.py`：通过 `/backend-api/promo_campaign/check_coupon`、`coupon=plus-1-month-free`、`is_coupon_from_query_param=true` 判断 `state == eligible`。
- 请求携带 Access Token、ChatGPT Account ID、设备 ID、Origin/Referer 和浏览器请求头，并使用指定代理。
- `services/oai_chain_audit.py`：将 `trial_eligible`、`trial_state`、HTTP 状态和 offer 来源分开保存，避免把网络失败误判成无资格。

本站仅采用上述资格检测契约，没有复制该项目的 Checkout、支付或其他套餐流程。

## 本站实现

- `payment_link_extractor/gopay_checkout.py`
  - 新增只读 `probe_coupon_eligibility`。
  - 返回 `coupon`、`http_status`、`state`、`eligible` 和非敏感 redemption 状态。
  - 原 `check_coupon_eligibility` 保留为严格包装器，供完整提链在需要时使用。
- `payment_link_extractor/gopay_eligibility.py`
  - 新增独立代理池检测器。
  - 一个代理只执行资格 GET；不进入第二步。
  - 依次区分 `eligible=True`、已验证无资格 `False`、全是网络失败 `None`。
  - 在任何网络访问前验证 JWT 结构和 payload。

## Evidence → Finding → Path

| Evidence | Finding | Path |
|---|---|---|
| E-001：参考实现以 `state == eligible` 作为 Plus 一月免费资格 | F-001：第一步不需要创建 Checkout | `payment_link_extractor/gopay_checkout.py` |
| E-002：参考实现区分 HTTP 成功无资格与网络失败 | F-002：本站结果必须支持 `True/False/None` 三态 | `payment_link_extractor/gopay_eligibility.py` |
| E-003：用户要求多个 ID 代理检测但成功后立即结束 | F-003：代理池无放回随机尝试，第一个 eligible 立即返回 | `tests/test_gopay_eligibility.py` |

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q `
  tests\test_gopay_eligibility.py `
  tests\test_gopay_isolated_optimization.py

.\.venv\Scripts\python.exe -m pytest -q
```

测试明确禁止独立检测器调用 Stripe，并覆盖前两个代理无资格、第三个代理 eligible、全部已验证无资格、代理会话关闭以及格式错误 AT 在联网前终止。

## 真实检测结果

使用用户提供的运行时 AT 和现有 10 个 ID 代理执行独立第一步检测，随机计划的第 1 次请求即返回 `HTTP 200 / state=eligible`。最终结果为 `eligible=true`、`source=chatgpt_check_coupon`；检测过程中没有创建 Checkout、没有初始化 Stripe，也没有执行第二步及后续流程。完整 AT、代理凭据和账号标识未写入报告或 Git。
