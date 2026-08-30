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
- 用户已明确授权维护 Playwright 抓包配置：扫描不到可用 CDP 浏览器时，使用 `tools\playwright_capture_session.py prepare` 自动启动 `data\playwright-capture-profile`；抓包使用其 `capture --channel <渠道>` 入口，连接、profile 准备、保存和脱敏分析均自动执行。
- 每轮抓包结束后不得关闭浏览器或持久上下文；保存并分析 HAR 后必须把现有页面导航回 `https://chatgpt.com/` 登录后主界面，确认 `CAPTURE_BROWSER_PRESERVED=1`、`CAPTURE_RETURNED_MAIN` 和 `CAPTURE_NEXT_CYCLE_READY=1`，继续复用同一个 profile、登录态和 CDP 会话。
- 优化任务采用连续闭环：操作一次完整提链流程并实时抓包，分析缺口，实施一轮源码优化和测试，再从登录后主界面启动下一轮同渠道抓包；重复到真实抓包完整性通过、分析没有待修复缺口且回归测试通过为止。
- GoPay 默认使用浏览器级多目标记录器：
  `C:\Users\Administrator\Desktop\提链\.venv\Scripts\python.exe tools\har_capture_browser_attach.py --cdp-port <CDP_PORT> --output artifacts-local\gopay-cdp-capture-<YYYYMMDD-HHMMSS>.har`
  该模式使用 `Target.setAutoAttach(flatten=true)`，覆盖 page/iframe/worker，并默认采用非阻塞响应体采集。
- RoxyBrowser 页面默认使用 `tools\roxy_har_capture.py`；普通独立 Chrome 页面使用 `tools\har_capture.py`。根据用户指定渠道选择对应入口，不交叉复用渠道记录器。
- 启动后必须确认并返回 `CAPTURE_READY=1`、`CAPTURE_CDP`、`CAPTURE_OUTPUT` 和首个 `CAPTURE_TARGET_ATTACHED`；记录器保持运行，等待用户完成操作。用户说“停止抓包”后发送 Ctrl+C，确认 `CAPTURE_SAVED`、`CAPTURE_ENTRIES`、`CAPTURE_SHA256`、`CAPTURE_COMPLETENESS` 和 `CAPTURE_MISSING`。
- 停止后自动生成脱敏摘要并执行完整性审计；GoPay 至少核对 checkout、taxes、snapshot、Stripe init/elements/confirm、approve、redirect 和 Midtrans transaction。仅将完整性最高的同渠道 HAR 标记为 canonical，旧的同渠道原始 HAR 删除，其他渠道 HAR 保持隔离。
- AT、Cookie、Sentinel、代理用户名/密码和订单标识只允许从本地环境变量或运行时会话读取，不写入规则、日志、Git、摘要或回复；禁止重放 HAR 内请求。
- 每次抓包准备或停止后记录绝对路径、SHA-256、完整性结果和下一步；源码、规则或文档发生修改时遵循本文件的提交并推送规则。
