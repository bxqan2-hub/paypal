# 仓库同步规则

- 本项目唯一远程仓库为 `https://github.com/bxqan2-hub/paypal.git`，默认分支为 `main`。
- 每次修改后立即提交并推送到 `origin/main`；依次执行 `git add -A`、创建说明本次修改的提交，并执行 `git push origin main`。
- 不创建额外的本地项目备份副本；回滚和对比统一使用 Git 提交历史与 GitHub 远程仓库。
- 不得把 `.env`、`.venv`、Python 缓存或运行日志提交到仓库；只上传源码、文档和脱敏配置示例。
- 不得修改或覆盖 `https://github.com/bxqan2-hub/-pp-.git`；本项目的提交只能推送到 `paypal.git`。
- 推送失败时保留提交并重试；远程推送成功前不得把修改标记为完成。
- 除非用户明确要求，否则不得强制推送或改写远程历史。

# GCash 本地上游规则

- GCash 开源项目的唯一权威来源是本机完整 Git 工作树：`C:\Users\Administrator\AppData\Local\Temp\codex-upstreams\MK-GCash-Link-OpenSource`。
- GCash 上游远程仓库已取消；以后读取、校验、同步 GCash 开源源码时只读取上述本地工作树，不再通过网络 fetch、pull、clone 或读取远程仓库。
- 上述本地工作树是只读上游，不得修改。本站目录 `payment_link_extractor/mk_gcash_open_source` 必须与该本地工作树当前提交的 22 个跟踪文件逐字节一致；本站只允许在上游目录之外通过适配器接入和调用。
- 同步前后必须记录本地上游提交并执行文件集合与 SHA-256 对比；出现差异时，以本地上游工作树为准修复本站副本，不得把本站改动反向写入本地上游。

# 提链渠道隔离规则

- PayPal、GoPay、GCash 以及以后新增的每一种提链方式必须拥有独立的渠道注册项、独立适配器/核心目录、固定国家与币种策略、独立结果字段和独立测试；禁止把一种渠道的核心调用、状态或结果字段复用到另一种渠道。
- `application.py` 只负责配置规范化与按注册表分发，不承载任何具体渠道协议；具体协议修改只能进入对应渠道模块。
- 新增渠道时必须先在渠道注册表声明唯一名称、适配器入口、结果字段、国家/币种和是否使用旧 Checkout 更新，再单独接入 UI 与测试；不得通过继续堆叠交叉条件分支混入已有渠道。
- 每次修改渠道后必须验证：三个现有渠道指向不同适配器、结果字段互不相同、GoPay/GCash 不构造 PayPal 旧传输、UI 选项与渠道注册表一致。

# 抓包工具快速准备规则

- 当用户说“抓包”“开始抓包”“准备抓包”或“继续抓包”时，先扫描当前浏览器的 `DevToolsActivePort`，确认可用 CDP 端口和页面，再准备记录器；不创建新的浏览器配置，除非用户明确要求。
- PayPal、GoPay、GCash 默认统一使用 `tools\mitm_capture.py` 和系统安装的 mitmproxy；通过 `--channel` 隔离输出、完整性规则和摘要，不交叉复用渠道结果。根目录 `HAR_CAPTURE.bat` 是默认入口。
- mitmproxy 使用 `data\mitmproxy-capture-profile` 持久 Chrome、`127.0.0.1:8899` regular proxy、`http://127.0.0.1:8081/` mitmweb 管理页和禁用 QUIC 的 HTTPS/TCP 捕获。管理页默认自动打开；代理端口与 Web 端口可分别用 `OPLL_MITM_PROXY_PORT`、`OPLL_MITM_WEB_PORT` 选择。已有非 mitmproxy 浏览器只用于识别页面，不作为正式抓包来源；需要抓包时复用上述唯一持久 profile，不创建逐轮 profile。
- 只有 mitmproxy 无法启动时才允许临时回退到渠道旧记录器：GoPay 使用 `tools\har_capture_browser_attach.py`，RoxyBrowser 使用 `tools\roxy_har_capture.py`，普通独立 Chrome 使用 `tools\har_capture.py`；回退原因必须写入当轮结果。
- 启动后必须确认并返回 `CAPTURE_READY=1`、`CAPTURE_CDP`、`CAPTURE_OUTPUT` 和首个 `CAPTURE_TARGET_ATTACHED`；记录器保持运行，等待用户完成操作。用户说“停止抓包”后发送 Ctrl+C，确认 `CAPTURE_SAVED`、`CAPTURE_ENTRIES`、`CAPTURE_SHA256`、`CAPTURE_COMPLETENESS` 和 `CAPTURE_MISSING`。
- 停止后自动生成脱敏摘要并执行完整性审计；GoPay 至少核对 checkout、taxes、snapshot、Stripe init/elements/confirm、approve、redirect 和 Midtrans transaction。仅将完整性最高的同渠道 HAR 标记为 canonical，旧的同渠道原始 HAR 删除，其他渠道 HAR 保持隔离。
- AT、Cookie、Sentinel、代理用户名/密码和订单标识只允许从本地环境变量或运行时会话读取，不写入规则、日志、Git、摘要或回复；禁止重放 HAR 内请求。
- 每次抓包准备或停止后记录绝对路径、SHA-256、完整性结果和下一步；源码、规则或文档发生修改时遵循本文件的提交并推送规则。

# 代码修改规则：只改不堆

**每次改代码前必须先读本节。** 完整版与示例见 `docs/GPT修改规则_只改不堆.md`。

本仓库的实测教训：`5d1b2c0`（+1770/−99）、`8175062`（+2785/−142）、`3a8a1c0`（+4484/−2）三次以"对齐/抽包"为名的提交全部只加不减，结果是 MoMo 每个模块在仓库里存在两份副本、`transport.py` 挂着 590 行永不执行的 `enhanced` 分支、3 个从 UI 传到底却无人读取的配置字段；最后靠 `50cd090` 删掉 10045 行才收拾干净。以下规则就是为了不再发生这件事。

- **R1 先读后写**：动手前先 grep 相关符号、读现有实现；禁止在没读过现有代码的情况下新增函数、类、模块或字段。写完要能回答"我改的是哪一处已有代码"；若确无已有代码，先给出证明它不存在的检索结果再新增。
- **R2 禁止平行实现**：不许用新开关、新参数、新标志开出第二条代码路径让新旧实现并存；需求变了就改原路径本身。确需灰度并存时，必须在同一次回复里写明旧路径的删除条件与删除时机，并列入未完成项。
- **R3 搬迁是移动不是复制**：把代码挪到新文件、新目录或新包，必须在同一次改动里删掉源文件；"先复制稍后再删"视为未完成。自查同一个函数名或类名是否同时存在于两个文件。
- **R4 新增的东西必须有读取点**：新增配置字段、环境变量、CLI 参数、UI 控件或结果字段，必须给出"入口 → 解析 → `ExtractionConfig` → 业务代码里真正读它的那一行"的完整链路；给不出这条链路就不许加。
- **R5 替换后回扫死引用**：改完 grep 被替换掉的旧函数名、旧字段、旧环境变量、旧模块路径，结果必须为空；`docs/`、`.env.example`、`tests/` 一起扫。
- **R6 增删比自检**：以"对齐、重构、迁移、优化、清理、适配"为名的改动，删除行数不得为 0。若 diff 是纯增加，先自问"这次加的替代了什么、被替代的为什么还在"，答案写进交付说明。
- **R7 不扩大范围**：只改任务点名的文件；不顺手格式化、不重排 import、不重命名、不补类型注解、不夹带顺便优化。发现别的问题单独列出来报告。
- **R8 交付自检**：每次改完必须逐条输出——① 修改的已有代码（文件:函数）；② 新增的代码及其替代对象、被替代物是否已删；③ 搬迁项源文件是否已删；④ 新增配置项的完整链路；⑤ 死引用 grep 的命令与输出（必须为空）；⑥ `+X / −Y`，若 `Y=0` 说明原因；⑦ `python -m pytest tests/ -q` 最后 5 行原始输出；⑧ 未做的与存疑的，没有就写"无"。不许只回复"已完成"或"已优化"。

# MoMo 通道现状与保留清单

**改 MoMo 或 `transport.py` 前必须先读本节。** 全过程记录见 `docs/2026-09-03_momo优化审计日志.md`。

- 现状（`33b6d1d`）：MoMo 生产代码只在 `payment_link_extractor/momo/` 一处，10 个文件共 2526 行；调用链是 `channels.py` → `payment_link_extractor.momo` → `momo_core.extract_momo_payment_link`，中间没有任何 shim，不许再加。
- 流程边界固定为 Checkout → Checkout/update → Stripe Elements → taxes → ConfirmationToken → confirm → Intent → 解析 `payment.momo.vn` 链接。**不许重新引入** gateway `querySession` 轮询和代理池资格预检重试：两者都不参与产出链接，只制造失败面。
- 浏览器身份**硬钉 Chrome 146**，三处必须同版本：`momo/_transport.py` 的 `PAYMENT_BROWSER_IMPERSONATE = "chrome146"`、同文件的 UA 与 `sec-ch-ua`、`momo/_sentinel_runner.py` 启动的真实 headless Chrome 146。风控校验的就是这三者一致性，混版本直接 `status=blocked`。不许加 profile 轮换、不许加"自动选择版本"。运行前提是 `payment_link_extractor/runtime/chrome-146.0.7680.165/` 存在。
- 以下代码长得像 MoMo 残留、实际是别的通道在用，**一律不得删除或改写**：
  - `transport.py` 的 `_capture_bootstrap`：GCash 的 `_start` 调它取 `webDeploymentAttestation`。
  - `transport.py` 的 `_sync_cookies`：这是 GCash 唯一的 cookie 同步实现，注释写明"byte-for-byte 保持原行为"。
  - `transport.py` 构造器里的 `self.locale_script.write_text(...)`：GCash 的 locale shim 靠它落盘。
  - `transport.py` 第 33 行提到 "Momo browser" 的注释：说明 UA 常量为何放在传输层，`grep -i momo transport.py` 只剩这一条是预期结果，不是漏删。
  - `models.py` 的 `session_token`：GoPay 在 `gopay_transport.py` 使用。
  - `channels.py` 的 `uses_checkout_update=False`：MoMo 单代理池成立的前提，改成 `True` 会让 checkout 与 update 分别取代理。
  - `channels.py` 中 `{"paypal", "gopay", "momo"}` 那个集合：决定哪些渠道接受注入 `transport_factory`，测试依赖。
  - `web/tasks.py` 的 `eligibility_*` / `zero_amount_*` 进度阶段与 `app.js` 对应中文文案：GoPay、PayPal、GCash 仍在发，MoMo 不发不等于可以删枚举。
  - `config.py` 中的两行 `VN`：`country_config("VN")` 仍从这里取账单模板。
  - `.env.example` 的 `OPLL_SENTINEL_BROWSER`、`OPLL_SENTINEL_HEADLESS`、`OPLL_GOPAY_SENTINEL_*`：名字带 SENTINEL 但属于 GoPay。
- `momo/_sentinel_client.py` 的 `RUNNER_SCRIPT` 必须指向真实存在的 `momo/_sentinel_runner.py`。四个受保护调用（`_momo_checkout`、`_momo_checkout_update`、`_momo_approve`、`_oaics.openai_checkout_confirm`）全走它起子进程；文件名写错会让 MoMo 在第一步 checkout 就抛 `502 payment Sentinel SDK failed`，而所有流程测试都 monkeypatch 掉了 `payment_sentinel_headers`，测试全绿也发现不了。守卫是 `tests/test_momo_support.py::test_sentinel_runner_script_is_spawnable`，不得删除。
- `momo_zero_trial_validation` 是**已接线的活开关**，不是死字段：`momo/_flow.py` 的金额闸门与 `momo/_oaics.py` 的 MoMo 分支都读它，UI、`routes.py`、`.env.example` 三处入口齐备。改它必须整条链路同步。
- `transport.py` 中 `enhanced` 相关代码已全部清除，`grep -n "enhanced" payment_link_extractor/transport.py` 必须为空。任何时候都不许为 MoMo 在 `transport.py` 里重开分支——MoMo 有自己的 `momo/_transport.py`。
