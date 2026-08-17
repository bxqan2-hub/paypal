# 精确优化说明

本次只移植参考提交 `e672cfc4953c1186a013f5b4472809610cc5029e` 中当前项目确实缺失、且能通过回归测试证明的能力。当前项目原有的授权 checkpoint、完整审计日志、批次任务、HeroSMS 自动换号、同号码重试、取消清理和 Flask 同进程适配器均保留。

## 已优化

- 国家目录扩展到 197 国，动态字段目录扩展到 32 国。
- 增加 vendor proxy bridge、共享后台生命周期、直接 SOCKS5H 回退。
- `_address_normalized_by_paypal` 缺失时安全回退 MANUAL 地址语义。
- Buyer 默认统一为 `identity_elevation`，显式 `original` 仍分派原版 flow。
- 浏览器按 Windows/Linux 选择 headless、Chrome/Edge/Chromium 与 Xvfb。
- 提链 TaskManager 支持运行时并发调整及认证 API。
- 提链、协议和 HeroSMS 阶段事件使用递归脱敏。
- 纳入参考协议测试并增加本地国家、代理、浏览器、并发、事件和兼容模式测试。

## 保留的当前项目优势

- 未替换 `paypal_agreement_protocol/web.py` 整体，只做目标字段和事件增量修改。
- 未删除 authorization checkpoint、BA 去重恢复、Grok-only bridge 边界。
- 未删除批次邮箱/手机号、SMS activation、自动换号、same-phone retry。
- 未改变 `.env`、本地卡哈希数据和运行日志。
