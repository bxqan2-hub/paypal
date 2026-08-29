# 半自动支付编排系统参考资料

本目录保存用户提供的《半自动支付编排系统：从账号结账到支付结果确认（紧凑版）》的原始文件、完整解析和项目对照摘要。

## 文件

- `半自动支付编排系统_从账号结账到支付结果确认_紧凑版.docx`：原始 DOCX 副本。
- `半自动支付编排系统_从账号结账到支付结果确认_紧凑版.md`：按正文顺序展开的可检索文本，包含标题、段落、代码样式文本和 12 个表格。
- `半自动支付编排系统_从账号结账到支付结果确认_紧凑版.json`：结构化解析，包含元数据、标题索引、正文块、表格、页眉页脚和 OOXML 检查结果。
- `README.md`：本索引和当前项目对照说明。

## 内容摘要

这份文档描述一个由账号结账/提链、支付方式路由、协议支付、人工介入和结果确认组成的半自动编排架构。核心概念是：

1. 账号结账层创建带地区/币种的 Checkout Session，并维护登录态、税务地址和会话字段。
2. 路由层读取 `payment_method_types`，把支付方式分到自动通道或人工通道。
3. 自动通道处理 Stripe Elements、ConfirmationToken、PaymentIntent、Approve、挑战和结果确认。
4. 人工通道输出本地支付链接/二维码，等待外部付款后轮询确认。
5. 统一状态机管理任务生命周期、重试、幂等、超时、订阅激活和审计。
6. 风控基础设施覆盖 TLS/UA 配对、设备指纹、代理固定、请求节奏和 Cookie 隔离。

文档还包含端到端时序、支付方式可行性矩阵、异常矩阵、耗时阈值、代理类型比较、日志字段和官方方案对照表。

## 与当前项目的对照

文档是架构参考，不是本项目的自动执行配置。当前项目只将其中与现有三个渠道相关的边界落到代码：

- PayPal：现有旧 Checkout/Stripe 通道。
- GoPay：独立适配器，复用 PayPal legacy Checkout/Stripe 核心，固定 ID/IDR，仅 provider 配置和结果字段独立。
- GoPay Pro：独立适配器和复制改造后的 GoPay Pro 核心，固定 ID/IDR，结果字段为 `gopay_pro_url`。
- GCash：独立适配器，运行时读取本地完整 Git 上游：`C:\Users\Administrator\AppData\Local\Temp\codex-upstreams\MK-GCash-Link-OpenSource`。

当前项目的渠道注册与隔离入口：`payment_link_extractor/channels.py`；总分发入口：`payment_link_extractor/application.py`。文档中提到的其他支付方式（例如 UPI、PIX、iDEAL、Kakao Pay、MoMo、BLIK、TWINT）只在资料中出现，不能据此视为当前项目已支持。

## 解析状态

根据 DOCX OOXML 检查：220 个段落、197 个非空段落、12 个表格、1 个节；无嵌入图片、批注、修订插入或修订删除。原始文件 SHA-256：

`fc774f040bbea12a2c0f7b8f6cb686439e941c898f56de86709a9785e6ffb506`

Markdown 与 JSON 由 `tools/extract_reference_docx.py` 生成。后续读取本资料时，优先查阅 Markdown；需要精确结构、表格或来源校验时查阅 JSON；需要原始排版时打开 DOCX。

## 渲染检查

已调用文档技能的 `render_docx.py`。当前环境缺少 LibreOffice/`soffice`，因此未生成页面 PNG；正文、表格和 OOXML 结构解析已完成，未对原始文档做任何修改。
