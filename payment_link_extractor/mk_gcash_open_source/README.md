# MK GCash Link

独立、造福大家的GCash 提链工作台。用户必须提供自己的 ChatGPT AT 和 PH 住宅代理池。

## 功能边界

- 按浏览器成功链路执行建单、税费、confirm、custom method start、GCash 页面和回调监控。
- 优惠随建单提交，不再额外调用 `checkout/update`；建单遇到风控拒绝时生成 `chatgpt_checkout` Sentinel 后原会话重试，confirm 使用独立的 `checkout_session_approval` Sentinel。
- Sentinel 由随项目发布的 Node/V8 运行时生成，并保持同一 device、Cookie、页面上下文和 PH 代理出口。
- 单次任务固定使用同一 Chrome/TLS 配置，重试时只轮换代理和 Checkout 状态。
- 提供与 CDK 版一致的双步骤工作台，可在本地解析、筛选并选择需要提链的账号。
- 支持真实二维码，抓取失败时回退为使用已验证 GCash 链接生成的二维码。
- 支持复制链接、保存/刷新二维码、停止提链、放弃支付监控和 Plus 权益到账确认。
- 不包含 CDK、扣次、管理员后台、公告、平台代理、用户数据库或服务器部署配置。
- AT、代理和任务只保存在当前 Python 进程内，重启后清空。

## 环境

- Python 3.10 或更高版本
- Node.js 18 或更高版本（`node` 或 `nodejs` 必须在 `PATH` 中）
- Windows、Linux 或 macOS
- 用户自备可访问 ChatGPT HTTPS 与 GCash 页面的 PH 住宅代理

## 安装

Windows 可双击 `INSTALL.cmd`，也可以手动执行：

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
```

## 启动

Windows 双击 `START.cmd`，或执行：

```bash
python app.py
```

默认地址为 `http://127.0.0.1:8931/`。默认只监听本机；该服务没有登录层，不应直接绑定公网地址。

可通过环境变量调整本地调度容量：

```text
MK_MAX_CONCURRENCY=4
MK_MAX_SESSION_CONCURRENCY=4
MK_MAX_QUEUE=50
MK_MAX_ACCOUNTS=50
```

物理上限分别为 12、12 和 200。修改调度容量不会改变单账号的链路或重试逻辑。
`MK_MAX_ACCOUNTS` 用于部署方设置每个任务的账号上限，开源版默认值和上限均为 50。

## 代理格式

每行一个，支持：

```text
host:port
host:port:user:pass
http://user:pass@host:port
https://user:pass@host:port
socks5://host:port
```

Playwright 不支持带账号密码的 SOCKS5，因此完整链路会直接拒绝该格式。

## API

提交任务：

```http
POST /api/jobs
Content-Type: application/json

{
  "tokens": "email@example.com|eyJ...",
  "proxy_pool": ["host:port:user:pass"],
  "max_attempts": 5
}
```

也可以使用结构化账号：

```json
{
  "accounts": [
    {
      "access_token": "eyJ...",
      "email": "email@example.com",
      "name": "Account Name"
    }
  ],
  "proxy_pool": ["host:port:user:pass"]
}
```

接口返回 `job_id` 和每个账号的随机 `id`。后续接口：

```http
GET  /api/jobs/{job_id}
GET  /api/jobs/{job_id}/accounts/{account_id}/qr.png
POST /api/jobs/{job_id}/accounts/{account_id}/refresh
POST /api/jobs/{job_id}/accounts/{account_id}/abandon
POST /api/jobs/{job_id}/cancel
POST /api/jobs/{job_id}/abandon
```

刷新、停止和放弃接口的请求体均为 `{}`。账号级 `abandon` 只释放指定账号，任务级 `abandon` 会释放当前任务内全部活动支付监控。`link_ready=true` 表示链接已经生成；只有 `payment_success=true` 且 `payment_status=completed` 才表示 Plus 权益已确认到账。

## 测试

```bash
python -m unittest discover -s tests -p "test_*.py"
```

测试不发送真实 AT、代理或支付请求。

## 注意

项目依赖第三方页面和非稳定上游接口，账号资格、优惠资格、代理地区与质量都会影响结果。请仅处理你有权使用的账号，并遵守相关服务条款和当地法律。

## License

MIT
