# GoPay 提链

独立 GoPay 批量工作台。批量调度由 `payment_batch.py` 完成，协议核心位于
`gopay/gopay_extract.py`。每个账号只接受 Stripe
`pm-redirects.stripe.com/authorize` 授权链接，`oaics_` Checkout 会立即拒绝。

## 本地运行

从仓库根目录执行：

```bash
python3 -m pip install -r gopay-link/requirements.txt
python3 gopay-link/app.py --host 127.0.0.1 --port 8791
```

访问 `http://127.0.0.1:8791/`。

## Docker

```bash
docker compose -f gopay-link/compose.yaml up -d --build
```

运行时数据写入 `gopay-link/data/` 或 Docker 数据卷。整批结束后会删除 Token
和所有账号的代理输入原文，保留脱敏日志；启用“保存协议诊断”后同时保留脱敏 dump。

## 验证

```bash
python3 -m pytest -q gopay-link/tests tests/test_gopay_protocol.py
python3 -m py_compile gopay-link/app.py gopay/gopay_extract.py payment_batch.py
node --check gopay-link/static/app.js
```
