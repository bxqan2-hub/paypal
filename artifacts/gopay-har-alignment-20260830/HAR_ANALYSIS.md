# GoPay HAR 对齐分析（已脱敏）

## 高概率原因

1. **浏览器客户端版本过旧**：HAR 的 Checkout 请求使用 `oai-client-build-number=10012890` 与 `prod-7890a3be6202572c0e8e3bb4907574d660b4e4f4`；本站默认值更旧，已更新并保留环境变量覆盖。
2. **GoPay locale 不匹配**：HAR 使用 `oai-language=id-ID`，本站原先固定 `en-US`；现在按 GoPay 的 ID 配置自动发送 `id-ID`。
3. **Checkout telemetry 形状不完整**：HAR 的 `oai-telemetry` 是 8 项动态数组；本站原先 Checkout 使用 `[1,null]`，现改为每次请求生成 8 项动态值。

## 中概率原因

1. Sentinel 请求必须带 `flow=chatgpt_checkout`；本站已将空值、`default` 和 `__default__` 统一为该 flow。
2. Sentinel token 生成后可能更新 HttpOnly Cookie；本站已在 token/observer token 后重新读取 cookies，并同步 `oai-did` 到 `oai-device-id`。
3. HAR 的 Checkout、taxes、snapshot、approve 请求保持同一 device/session/attestation；本站在同一 transport session 内保持这些字段。

## 低概率/未证实原因

1. HAR 未记录 Checkout POST body，因此不能仅凭该文件确认 body 字段缺失。
2. HAR 中部分请求含 `chatgpt-account-id`、部分不含；本站按 AT 可解析结果附加，不强制伪造。
3. Sentinel SDK 的线上版本可能继续轮换；本站保留环境变量和浏览器 init-script 注入路径。

## 修改位置

- `payment_link_extractor/transport.py`
- `payment_link_extractor/checkout.py`
- `payment_link_extractor/errors.py`
- `payment_link_extractor/web/tasks.py`
- `payment_link_extractor/gopay_pro_core/validation.py`
