# GoJek GoPay CS 提链源码切片（脱敏版）

本包只包含 GoPay `cs_` 链路的生产代码切片，不包含其他 Checkout family、前端、任务存储、运行配置、日志、抓包响应或测试录制数据。

## 内容

- `_source/internal/payment/gopay_cs.go`：CS Checkout、Elements、税区/账单、确认与轮询。
- `_source/internal/payment/gopay_attachment_cs.go`：原生 Go 实现的 Stripe Payment Page 与 GoJek/Midtrans 跳转解析。
- `_source/internal/payment/gopay_aligned_cs.go`：CS 主流程后半段编排。
- `_source/internal/payment/gopay_approve.go`：Checkout approve 与受阻后的代理轮换。
- `_source/internal/payment/gopay_provider_client.go`：Provider 跳转专用、独立 CookieJar 的客户端。
- `_source/internal/payment/gopay_promotion_recovery.go`：优惠资格探测与恢复。
- `_source/internal/payment/gopay_proxy_cooldown.go`：代理随机选择与冷却调度。
- `internal/gopayaddress`：替代外部地址服务的内置随机印度尼西亚虚构地址库。

`_source` 以原项目的 `package payment` 与内部类型为边界保留源码；它是一份可回填原项目的源码切片，不是完整服务。目录名前缀 `_` 让 Go 工具忽略不完整的项目切片，因此压缩包根目录执行 `go test ./...` 时只验证独立地址库。

## 地址替换

`selectGoPayBilling` 已改为从 `internal/gopayaddress` 随机选择记录。地址库不读取文件、不请求网络、不连接数据库；街道、单元、姓名与电话均为明确的虚构测试值。每次提链只选择一次，同一次 CS 流程继续复用同一条账单地址。

## 脱敏范围

压缩包排除了账号、访问令牌、Cookie、代理凭据、真实邮箱/电话、真实订单和支付链接、环境变量、配置文件、数据库内容、运行日志、HAR/响应样本及原地址数据。源码中的公共协议域名、API 路径、Header 名称和版本常量是链路实现的一部分，予以保留。

Stripe publishable key 不再使用项目内置 fallback；抽离代码只接受本次 Checkout 响应携带的 key，缺失时直接结束该次流程。因此包内不含 `pk_live_*` 或任何商户关联 key。

## 回填方式

1. 将 `_source/internal/payment/*.go` 放回目标项目的 `internal/payment/`。
2. 将 `internal/gopayaddress/` 放入目标项目同名目录。
3. 保留目标项目已有的 `Executor`、`chatSession`、`checkoutSession`、`stripeSnapshot`、`billingDetails`、HTTP/指纹/Sentinel 与任务进度基础设施。
4. 执行 `gofmt`、`go test ./...` 和 `go vet ./...`。

抽取基线：工作区提交 `10983e082d`，抽取日期 `2026-08-29`。
