# 提链方式分层

本站每种提链方式保持独立入口，新增方式不得把协议代码混入其他方式：

| 方式 | 入口 | 核心位置 | 代理入口 |
|---|---|---|---|
| PayPal | `paypal_channel.py` | `flows/oaics.py`、`flows/cs_live.py`、`providers/paypal.py` | 本站 PayPal transport |
| GoPay | `gopay_channel.py` | 与 PayPal 共用 legacy Checkout/Stripe 核心、`providers/gopay.py` | 本站 GoPay transport |
| GoPay Pro | `gopay_pro.py` | 复用 PayPal legacy Checkout/Stripe 核心，并接入 GCash 浏览器/Sentinel 优化层 | GoPay Pro 独立 ID 代理池 |
| GCash | `application.py` 的 GCash 分支 | `mk_gcash_open_source/` 内完整开源项目 | `mk_gcash.py` 的单一 `proxy_pool` |
| 后续方式 | 独立 `flows/<method>.py` 或 `providers/<method>.py` | 该方式自己的目录 | 该方式自己的代理适配 |

## GCash 调用边界

`payment_link_extractor/mk_gcash.py` 只做三件事：

1. 把本站账号和 `proxy_pool` 转成开源项目 `app.py` 的 `create_job()` 输入；
2. 直接轮询开源项目 `public_job()`；
3. 把开源项目结果转换成本站任务模型。

GCash 的建单、税费、确认、GCash 页面、二维码、回调监控和重试都留在
`payment_link_extractor/mk_gcash_open_source/`，不复用 PayPal 的 provider 或
checkout transport。GoPay 只切换 provider 配置和结果字段，不加载独立上游源码。

## 代理规范

- `host:port:user:pass`：按认证 HTTP 代理直接交给开源项目。
- `socks5://host:port`：无认证 SOCKS5，直接交给开源项目。
- `socks5://user:pass@host:port`：由
  `web/socks5_bridge.py` 在 `127.0.0.1` 创建 HTTP CONNECT 桥，再交给开源项目。
- 输入中出现 `\\@` 时会规范化为 `@`，因此带转义的 SOCKS5 用户名密码也能使用。

每个 SOCKS5 上游只创建一个复用桥，多个重试和账号共享同一个桥；进程退出时
统一关闭桥接监听器。
