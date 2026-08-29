# GoPay HAR/live 对齐复核（脱敏）

## 高概率原因

- `agent-browser --args` 与 init-script 组合会让 `window.SentinelSDK` 不注入；已移除冲突的 `--args`，并在真实 `https://chatgpt.com/` 页面验证 `typeof window.SentinelSDK.token === "function"`。
- 认证 SOCKS5 直接交给 Chromium 会产生 `ERR_NO_SUPPORTED_PROXIES`；已将 1024proxy 认证 SOCKS5 转为本机 HTTP CONNECT bridge，真实页面可打开。
- JSON 凭据里的 NextAuth JWE 超过单 Cookie 4096 字节；已拆成 `.0/.1/...` Cookie chunk，并清理旧浏览器 Cookie 后再设置，避免重复域 Cookie。
- HAR 的 GoPay Checkout 使用 `oai-client-build-number=10012890`、`prod-7890a3be6202572c0e8e3bb4907574d660b4e4f4`、`oai-language=id-ID` 和 8 项 `oai-telemetry`；本站已对齐。

## 中概率原因

- Sentinel flow 需要 `chatgpt_checkout`；空/default alias 已归一化。
- token 生成后必须重新读取 HttpOnly `oai-did` 并写回 `oai-device-id`；已实现。
- GoPay Checkout/approve/taxes 在无 proof 时继续发送会触发风控；GoPay 保护请求现在 `required=True`，proof 失败会 fail-closed，不再静默降级为旧 token/无 token。

## 低概率/未证实原因

- HAR 未保存 Checkout POST body；body 字段完整性仍由服务端版本决定。
- HAR 的部分动态遥测/attestation 每次会话变化，本站只使用结构化动态值，不复制静态 token。

## 实测

- 新 AT + 1024proxy：真实浏览器 Sentinel proof 生成成功；无 sessionToken 时直接 AT 流程曾完整到达 `completed` 并返回非空 `gopay_url`。
- 带 4092 字节 JWE sessionToken：Cookie chunk 写入与浏览器 proof 生成成功。

## Amount verification

The generated Midtrans URL was opened in a browser and its transaction endpoint returned `currency=IDR` and `gross_amount=349000`. This is a non-zero 349,000 IDR checkout, so it does not satisfy the zero-amount promotion filter. The GoPay core now rejects non-zero amounts when the promotion/update path is enabled, before returning `gopay_url`.
