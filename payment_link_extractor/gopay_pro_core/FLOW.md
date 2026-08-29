# GoPay CS 链路

```text
executeAttempt
  -> 创建并校验 cs_ Checkout
  -> stripeBootstrapAttachmentCSGoPay
  -> 可选 checkout/update 优惠同步
  -> stripeInitAttachmentCSGoPay（权威金额/支付方式）
  -> finishCSGoPay
       -> 随机选择一条内置虚构 ID 地址
       -> fetchCSGoPayElementsSession
       -> 四次 progressive tax_region 更新
       -> postCSGoPayTaxes + postCSGoPaySnapshot
       -> refreshCSGoPayPaymentPage
       -> finishAlignedCSGoPayAfterTax
            -> pre_confirm
            -> 创建 pm_ GoPay PaymentMethod
            -> confirm
            -> approveGoPayCheckoutWithProxyRotation
            -> 轮询 Payment Page
            -> resolveAttachmentCSGoPayProviderRedirect
            -> 输出 GoJek/GoPay 或 Midtrans 跳转链接
```

主要共享依赖来自原项目：`Executor`、`chatSession`、`checkoutSession`、`stripeSnapshot`、`billingDetails`、`browserhttp.Profile`、`domain.CheckoutInput/CheckoutResult`、`jobs.ProgressReporter`，以及公共的 HTTP、指纹、Captcha/Sentinel、诊断和进度日志辅助函数。

其他 Checkout family 的入口、状态机、尾链和相关测试均未纳入本包。
