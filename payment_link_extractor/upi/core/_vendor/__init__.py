"""Vendored protocol helpers (原项目内部模块，独立打包时原样带过来)。

包含：
  http.py              curl_cffi 会话（浏览器 impersonate）
  oaics.py             ChatGPT 网页端通用头 / 预热
  risk.py              结账风控头（attestation + sentinel）
  attestation.py       部署证明抓取
  sentinel_token.py    OpenAI Sentinel token 铸造（Node 桥）
  openai_protocol.py   OpenAI 固定协议参数
  sentinel_assets/     sentinel 桥用的 JS 资产（真 sdk.js）
"""
