# HAR 抓包与解析工具

工具位于本站根目录的 `tools` 文件夹，使用 Python 标准库和本机 Chrome/Edge，不需要额外安装抓包库。

## 0. 一键 BAT

直接双击根目录的 `HAR_CAPTURE.bat` 即可启动手动抓包；脚本会先要求手动输入认证 SOCKS5 代理，连通性检查通过后才启动独立 Chrome，默认保存到 `data\captures`。也可以在命令行传入输出路径和起始 URL：

```cmd
HAR_CAPTURE.bat "data\captures\gcash-success.har" "https://chatgpt.com/?promo_campaign=plus-1-month-free"
```

双击 `HAR_ANALYZE.bat` 会要求输入 HAR 路径并生成同目录的 `.report.md`；命令行调用方式：

```cmd
HAR_ANALYZE.bat "data\captures\gcash-success.har" "data\captures\gcash-success.report.md"
```

正常手动抓包时在 `Proxy:` 提示处输入 `HOST:PORT:USERNAME:PASSWORD`；脚本会通过 `chatgpt.com` 检查实际目标链路和延迟，默认超过 10000ms 就要求重新输入代理，不回显代理账号密码。也可以通过 `OPLL_CAPTURE_PROXY_MAX_LATENCY_MS` 调整阈值，或设置 `OPLL_CAPTURE_SOCKS5` 与 `OPLL_CAPTURE_SKIP_PROXY_PROMPT=1` 用于自动化运行。
每次 BAT 启动默认使用新的 `data\har-capture-profile\run-随机值` 配置目录，因此 Cookie、Local Storage 和会话状态是新的；Chrome/Edge 可执行文件版本、User-Agent 等浏览器本身属性仍保持本机版本。设置 `OPLL_CAPTURE_REUSE_PROFILE=1` 才会复用 `data\har-capture-profile\default`。
代理条目中的 `region-XX` 会自动选择语言、`Accept-Language` 和时区。例如 `region-PH` 使用 `en-US` 与 `Asia/Manila`；没有识别到地区时默认使用英文 `en-US`。需要覆盖时可传 `--lang`、`--accept-lang` 和 `--timezone-id`。
自动化 smoke test 可额外设置 `OPLL_CAPTURE_HEADLESS=1` 和 `OPLL_CAPTURE_DURATION=10`；手动抓包时不设置这两个变量。

## 1. 启动手动抓包

在 PowerShell 中执行：

```powershell
Set-Location 'C:\Users\Administrator\Desktop\提链'
New-Item -ItemType Directory -Force data\captures | Out-Null
.\.venv\Scripts\python.exe tools\har_capture.py `
  --url 'https://chatgpt.com/?promo_campaign=plus-1-month-free' `
  --output 'data\captures\gcash-success-20260824.har'
```

脚本会启动一个独立的 Chrome 配置目录并打开页面。浏览器窗口中按正常流程手动登录、打开订阅、填写资料、选择 GCash 并完成跳转。流程结束后回到启动脚本的终端按 **Ctrl+C**，脚本会写出：

```text
CAPTURE_SAVED=...\data\captures\gcash-success-20260824.har
CAPTURE_ENTRIES=...
CAPTURE_SHA256=...
```

`--duration 60` 可用于固定时长自动保存；不传 `--duration` 时一直抓到 Ctrl+C。测试启动方式：

```powershell
.\.venv\Scripts\python.exe tools\har_capture.py `
  --url 'https://example.com' `
  --headless --duration 10 `
  --output 'data\captures\smoke.har'
```

### 代理启动

一键 BAT 已经包含代理连通性和延迟检查。底层命令也可单独检查代理，不会启动 Chrome：

```powershell
$env:OPLL_CAPTURE_SOCKS5 = 'HOST:PORT:USERNAME:PASSWORD'
.\.venv\Scripts\python.exe tools\har_capture.py --check-proxy --socks5-proxy-env OPLL_CAPTURE_SOCKS5
```

检查结果会输出 `status` 和 `latency_ms`；代理握手、TLS 或目标站点连接失败时会返回非零状态。

Chrome 的 `--proxy-server` 参数使用无认证代理地址：

```powershell
.\.venv\Scripts\python.exe tools\har_capture.py `
  --url 'https://chatgpt.com/' `
  --proxy-server 'http://127.0.0.1:8080' `
  --output 'data\captures\gcash-proxy.har'
```

无认证 SOCKS5 代理可写成 `socks5://HOST:PORT`。带用户名和密码的代理直接交给工具临时桥接，推荐把条目放进环境变量，避免在命令历史中留下凭据：

```powershell
$env:OPLL_CAPTURE_SOCKS5 = 'HOST:PORT:USERNAME:PASSWORD'
.\.venv\Scripts\python.exe tools\har_capture.py `
  --url 'https://chatgpt.com/' `
  --socks5-proxy-env OPLL_CAPTURE_SOCKS5 `
  --output 'data\captures\gcash-proxy.har'
```

也可以直接使用 `--socks5-proxy 'HOST:PORT:USERNAME:PASSWORD'`。脚本只向 Chrome 暴露临时的 `127.0.0.1` HTTP CONNECT 地址，终端输出不会打印代理账号和密码。

可选参数：

| 参数 | 作用 |
|---|---|
| `--browser PATH` | 指定 Chrome/Edge 可执行文件 |
| `--user-data-dir PATH` | 指定可复用的浏览器配置目录 |
| `--socks5-proxy-env NAME` | 从环境变量读取认证 SOCKS5 条目并临时桥接 |
| `--max-body-bytes N` | 单个响应最多保存的字节数，默认 8 MiB |
| `--headless` | 无界面运行，适合自动化 smoke test |
| `--ignore-certificate-errors` | 测试环境忽略证书错误 |

## 2. 解析 HAR

默认输出脱敏 Markdown 报告：

```powershell
.\.venv\Scripts\python.exe tools\har_analyze.py `
  'data\captures\gcash-success-20260824.har' `
  --format markdown `
  --output 'data\captures\gcash-success-20260824.report.md'
```

输出 JSON，方便后续程序直接读取：

```powershell
.\.venv\Scripts\python.exe tools\har_analyze.py `
  'data\captures\gcash-success-20260824.har' `
  --format json `
  --output 'data\captures\gcash-success-20260824.report.json'
```

常用筛选：

```powershell
# 只看 GCash 页面和下游请求
.\.venv\Scripts\python.exe tools\har_analyze.py input.har --host gcash --format markdown

# 只看 ChatGPT checkout API 的成功请求
.\.venv\Scripts\python.exe tools\har_analyze.py input.har --host chatgpt.com --path /backend-api --status 200

# 搜索 Sentinel 或短链操作
.\.venv\Scripts\python.exe tools\har_analyze.py input.har --contains sentinel/req
.\.venv\Scripts\python.exe tools\har_analyze.py input.har --contains short.dynamic.link
```

报告包含 HAR SHA-256、主机/状态/方法计数、客户端 build/version、Sentinel 头长度、短链、关键操作索引以及每个请求的 URL、状态、请求体摘要和响应体摘要。默认会遮蔽 `Authorization`、Cookie、AT、签名、密码和加密字段；需要在本机检查原始字段时使用 `--include-sensitive`，并将报告保存在本地。

## 3. 在 Python 中调用

```python
from tools.har_utils import analyze_har, markdown_report

report = analyze_har(
    r"data\captures\gcash-success-20260824.har",
    host="m.gcash.com",
    redact=True,
)
print(report["observations"]["notable_operations"])
print(markdown_report(report))
```

推荐的后续优化流程是：分别抓取一份成功会话和一份失败会话，先用 JSON 报告按 `host/path/status` 对齐请求，再把差异中的请求头、body 字段、重定向和时间顺序映射到提链模块；原始 HAR 保留完整内容，提交到仓库前只提交脱敏报告或字段摘要。
