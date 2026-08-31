# 工作区清理与脱敏体积报告（2026-08-31）

## 结果

本次修复后增长的数百 MiB 主要来自 Playwright/Chromium 运行时，而不是 GoPay
源代码。已删除失效的 GoPay 持久 profile、登录测试运行包、回滚副本、临时抓包
脚本和浏览器模型/缓存；保留可运行环境、历史抓包证据和本地参考源码。

| 指标 | 结果 |
|---|---:|
| 清理前工作区（实测） | 1,302,275,838 bytes |
| 删除完成后工作区（不含本报告/制品提交） | 382,844,400 bytes |
| 当前工作区（含本报告/制品与 Git 元数据） | 395,993,052 bytes（377.65 MiB） |
| 相对清理前净减少 | 906,282,786 bytes（864.06 MiB） |
| 当前文件数 | 5,461 |
| Git 索引跟踪、已脱敏文件 | 747 files / 11,780,411 bytes（11.23 MiB） |
| 仅源码（排除 tracked artifacts） | 2,818,392 bytes（2.69 MiB） |
| `git archive` 压缩包 | 3,712,210 bytes（3.54 MiB） |
| 跟踪文件敏感模式扫描 | 0 files |

## 已删除的垃圾/临时内容

| 路径 | 删除量 |
|---|---:|
| `data/gopay-sentinel-profiles` | 653,367,750 bytes / 7,180 files |
| `artifacts-local/gopay-runtime` | 116,039,399 bytes / 829 files；包含运行期账号材料、浏览器状态、探针副本 |
| `artifacts-local` 临时回滚副本、旧探针、旧摘要、原始 canonical HAR | 51,054,511 bytes / 1,525 files |
| `data/playwright-capture-profile`、`data/har-capture-profile` 可再生模型/缓存 | 62,532,020 bytes / 369 files |
| 全工作区 `__pycache__` / `.pytest_cache` | 28,713,818 bytes / 1,618 files |
| `data/payment-link.log` | 截断 7,381,047 bytes，保留空日志文件 |
| Git 松散对象 | 执行 `git gc --prune=now` |

删除内容均为可再生缓存、旧尝试副本或已完成的运行期输入；没有修改 GoPay
协议源码逻辑、GoPay/PayPal/GCash 渠道注册、GCash 本地只读上游或用户的原始
抓包目录。

原始 canonical HAR（27,750,725 bytes）也一并移除，因为它是完整浏览器原始流量，
包含不应进入脱敏项目目录的运行期 Cookie/令牌材料；其阶段、哈希和字段结论已保留
在 Git 跟踪的历史报告与验证制品中。后续抓包重新生成新的同渠道 canonical HAR。

## 保留内容

| 路径 | 当前大小 | 原因 |
|---|---:|---|
| `.venv` | 135,165,595 bytes | 已安装的 Python/Playwright 运行环境 |
| `data/captures` | 181,384,900 bytes | GCash/Roxy 历史抓包证据 |
| `artifacts-local/reference-GPT-utral-platform` | 43,311,337 bytes | 本地参考源码，不是缓存 |
| 两个 capture profile（清理后） | 9,630,444 bytes | 保留 profile 元数据与后续抓包可用状态 |
| `artifacts/` | 9,820,602 bytes | Git 跟踪的验证报告、差异和回滚制品 |

## 后续自动防膨胀

`payment_link_extractor/gopay_sentinel_playwright.py` 现在在隔离 profile 打开前、
以及 session 关闭后清理 Chromium 的 Cache、Code Cache、GPU/Shader/Dawn cache、
Optimization Guide、Crashpad、BrowserMetrics 等可再生目录；Cookie、Login Data、
Local Storage、Preferences、NextAuth session 文件不在清理集合内。

配置项：

```text
OPLL_GOPAY_PROFILE_CACHE_CLEANUP=true
```

设为 `false` 可暂时保留隔离 profile 缓存。

## 验证

```text
BASELINE: .\.venv\Scripts\python.exe -m pytest -q --disable-warnings --cache-clear
BASELINE_RESULT: 290 passed in 1.81s; EXIT_STATUS=0
MODIFIED: .\.venv\Scripts\python.exe -m pytest -q --disable-warnings --cache-clear
MODIFIED_RESULT: 292 passed in 1.81s; EXIT_STATUS=0
ROLLBACK: artifacts\workspace-cleanup-20260831\ROLLBACK.sh TARGET_COPY SOURCE_REPO
ROLLBACK_RESULT: separate Git worktree restored 6b1a8cc files; HASH_MATCH=True; EXIT_STATUS=0
GIT: git gc --prune=now; EXIT_STATUS=0
BROWSER_AFTER_CLEANUP: CDP=61908; SESSION_STATUS=200; SESSION_USER=1; MAIN_PAGE=1
```

当前提交已推送到 `origin/main`；本次源代码与测试变更提交为
`0ba7e0d`，缓存自动清理补丁为 `f855dec`，清理报告制品提交为 `9953bed`。
