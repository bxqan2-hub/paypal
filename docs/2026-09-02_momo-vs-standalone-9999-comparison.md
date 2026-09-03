# MoMo 核心 vs. 独立 9999 源码包 —— 对比报告

- **报告日期**：2026-09-02（Asia/Shanghai）
- **对象 A（本站核心）**：`C:\Users\Administrator\Desktop\提链\payment_link_extractor\momo\`（用户指定的"核心 MoMo"）
- **对象 B（独立包）**：`C:\Users\Administrator\Downloads\momo_only_current_9999_20260831\`
- **方法**：纯静态源码审查。**未发起任何支付/网络请求、未重放 HAR、未读取或写入运行时凭据。**

---

## 0. 一句话结论

两个项目**业务方向相同**（Access Token → 越南代理 → ChatGPT Checkout → Stripe → MoMo → `payment.momo.vn`），但**不是同一份代码**：

- **A（本站核心）** 是一个多渠道开发主仓库里的 **MoMo 专用适配器**，协议实现更深、更贴 VN HAR：它会真正驱动 `payment.momo.vn` 网关并轮询 `querySession`，还带代理池资格预检和大量遥测。
- **B（独立包）** 是从 "9999" 产品线**裁剪出来的、只保留 VN+MoMo 的独立分发包**，用通用 `flows/providers` 架构，协议链更短：拿到 Stripe 重定向、解析出 momo.vn 链接就返回，**不驱动 momo.vn 网关、不做 `querySession` 轮询**。

**方向一样，状态机不同；B 不是 A 的简单复制，A 也不是 B。二者是同源思路下的两条独立实现分支。**

---

## 1. 定位与打包形态

| 维度 | A · 本站 `momo/` | B · 独立 9999 包 |
|---|---|---|
| 项目性质 | 多渠道**开发主仓库**的一个子模块 | **单渠道独立分发包**（可直接部署） |
| 支持渠道 | PayPal / GoPay / GCash / MoMo（+ OAICS 分支） | **仅 MoMo** |
| 支持国家 | 16 国（`config.py:25-42`） | **仅 VN**，`config.py` 硬锁：非 VN/非 momo 直接抛错（`config.py:74,106`） |
| 仓库附属 | `.git`、`.venv`、`artifacts/`（大量变更历史）、`docs/`、HAR 工具、测试、gopay/gcash/paypal 兄弟模块、`gcash_chain.py`/`payment_monitor.py` 遗留脚本 | 干净分发件：`README.md`(中文)、`.env.example`、`requirements.txt`、`deploy/`(systemd + install.sh)、`data/`(10 组示例地址)。**无历史、无测试、无凭据** |
| 脱敏 | 开发态，含真实配置/历史 | 已脱敏：无真实 AT/代理/服务器，仅 10 组示例地址 |
| Chrome UA | Chrome **151**（`config.py:14`） | Chrome **146**（`config.py:17`） |
| 客户端构建号 | 默认 `9748354`（`transport.py`） | `9999461` + `prod-d040bc6b02…`（`sentinel_client.py:17-18`） |

> B 的 README 原文：*"从 9999 当前版本裁出的独立源码包，只保留越南 VN + MoMo 提链"*。所谓 "9999" 即其 `sentinel_client` 里的客户端构建号 `9999461`，与本站的 `9748354` 是**不同的构建线**。

---

## 2. 代码架构（最大差异）

### A · 本站：MoMo 专用适配器（"胖模块"）

```
payment_link_extractor/
├── momo_channel.py        ← channels.py 实际注册并调用的活动入口
├── momo_core.py    (467)  ← 编排：资格→建单→Stripe→momo.vn 网关轮询→结果
├── momo_checkout.py(365)  ← OpenAI custom checkout / route-data / 税费
├── momo_eligibility.py(419)← 代理池逐条 accounts/check 资格预检
├── momo_stripe.py  (806)  ← Elements/confirmation token/intent confirm/URL 校验
├── momo_transport.py(1646)← curl_cffi + BrowserSentinelProvider(CDP) + CSRF + 网关头
│   （以上 5 个顶层 momo_*.py 是"影子副本"——见下）
├── momo_channel.py        ← channels.py 实际注册的入口；它 `from .momo.momo_core import …`
└── momo/  ← **用户指定的"核心"，也是运行时真正执行的一份**
    （momo_core/checkout/eligibility/stripe/transport，与顶层 5 文件近乎逐字相同）
```

- **关键点（已核实）**：运行时链路是 `channels.py:60 → momo_channel.py → .momo.momo_core`，即**真正跑的是 `momo/` 包**；而**测试套件** `tests/test_momo_support.py` 却 `import payment_link_extractor.momo_core`（顶层那份）。于是同一套 MoMo 逻辑存在**两份近乎逐字副本**（`diff` 只差 import 路径深度 + 1 空行）：**运行时用 `momo/` 包，测试用顶层 `momo_*.py`**，必须手动保持同步。详见 §5。
- MoMo 单份 ≈ **3.7k 行**，其中 `momo_transport.py` 独占 1646 行；两份并存即 ~7.4k 行。

### B · 独立包：通用 flows/providers 架构（"薄模块 + 共享层"）

```
payment_link_extractor/
├── flows/momo.py   (536)  ← 单文件汇总：建单+update+stripe init/confirm+approve+链接解析
├── flows/oaics.py  (455)  ← custom checkout 走 OAICS 分支
├── checkout.py     (343)  ← 通用 checkout 会话工具
├── stripe_common.py(364)  ← 通用 Stripe 头/参数/重定向解析
├── transport.py    (171)  ← 精简传输层
├── providers/      (thin) ← momo.py 仅 12 行：name + preferred_hosts
└── sentinel_client.py + sentinel_runner.py + sentinel_sdk_20260810913b.js
```

- 结构上更接近"通用本地支付框架 + 薄 provider"，MoMo 逻辑集中在 `flows/momo.py` 一个文件里。

---

## 3. MoMo 协议链对比（核心业务差异）

两边都跑：`payments/checkout(custom, VN/VND, plus-1-month-free, trial 30d)` → `checkout/update` → Stripe Elements → confirmation token → confirm。**共同点确凿。** 差异出现在**确认之后拿到最终链接的方式**：

| 阶段 | A · 本站 `momo/` | B · 独立包 `flows/momo.py` |
|---|---|---|
| 资格预检 | **代理池逐条** `accounts/check/v4` 循环，命中 `eligible_promo_campaigns.plus` 才建单（`momo_eligibility.py:51-203`） | 无独立代理池资格循环 |
| 确认后取链 | 解析 `pm-redirects.stripe.com` | 若无 `redirect_to` 且 `requires_approval` → 调 `checkout/approve`，再**轮询 Stripe `payment_pages/{id}`** 取 `redirect_to`（`flows/momo.py:439-490`） |
| 最终 URL 校验 | **严格**：必须 `https://payment.momo.vn/v2/gateway/pay` 且带 `t` 和 `s` 参数（`momo_stripe.py:806`） | 仅 `resolve_external_redirect` 到 preferred host，返回即止 |
| momo.vn 网关 | **真实驱动**：GET `/v2/gateway/pay` 建 Cookie/CSRF，再**轮询 POST `/v2/gateway/querySession`**（默认至多 15 次）直到 `status_code=9000, redirect=true`（`momo_core.py:196-225`） | **无**。全仓库仅在 `preferred_hosts` 常量里出现 `payment.momo.vn`；**没有 `querySession`、没有网关轮询** |
| 结果遥测 | `PaymentLinkResult.extra` 塞入数十个 `momo_*` 诊断字段（网关状态码、轮询次数、hcaptcha 源、pending updates、sentinel 模式、时区…）（`momo_core.py:461`） | 返回精简字典：`payment_method_id / stripe_redirect_url / provider_url / momo_url` |
| 0 元校验 | 有 `momo_zero_trial_validation` 配置项 | `MOMO_MAX_MINOR_AMOUNT=50` 上限断言（`flows/momo.py:44,270`） |

**要点**：A 把 MoMo 的最终态验证到 **momo.vn 网关会话层**（querySession 轮询 + t/s 强校验）；B 在 **Stripe 重定向层**就收尾（拿到指向 momo.vn 的链接即返回）。这是二者最实质的行为差异——A 更"重"、更贴 HAR，B 更"轻"、更快返回。

---

## 4. Sentinel（反机器人）实现对比

两边**同一 SDK 构建 `20260810913b`**，但注入方式不同：

| 维度 | A · 本站 | B · 独立包 |
|---|---|---|
| 机制 | `BrowserSentinelProvider`（`transport.py:509`）驱动 Chrome，**CDP `connect_over_cdp`**（`momo_transport.py:975`），内置 `sentinel_assets/`（bootstrap+sdk）+ **Node 兜底**（`sentinel.py` / `node_sentinel.mint_sentinel_sync`，`momo_transport.py:486-515`） | `sentinel_client.py` 调 `sentinel_runner.py` 作为**短命子进程 Playwright** 跑 `chrome-headless-shell`（146），JS 缓存文件 `sentinel_sdk_20260810913b.js`（`sentinel_runner.py:17-38`） |
| 传输 | curl_cffi（Chrome 指纹）+ 浏览器仅用于 Sentinel | curl_cffi 主传输；浏览器仅在子进程里出 Sentinel token |
| 复杂度 | 与 gopay/gcash 共享底座，`MomoSentinelProvider` 适配器 + 多重环境变量兜底 | 单一职责子进程，逻辑集中、易读 |

设计理念一致（"curl_cffi 发请求，浏览器只负责出 Sentinel 信号"），但 A 是多渠道共享的复杂底座，B 是自包含的精简子进程。

---

## 5. 本站内部一个值得注意的点：`momo/` 包与顶层 `momo_*.py` 是重复的（且运行时与测试各用一份）

`payment_link_extractor/momo/`（包）与顶层 `payment_link_extractor/momo_*.py`（5 个文件）**几乎逐字相同**：

```
momo_core.py    : 仅 import 由 `..auth` ↔ `.auth` 等 + 末尾 1 空行
momo_checkout.py: 仅 import 由 `..config` ↔ `.config` + 末尾 1 空行
（momo_eligibility / momo_stripe / momo_transport 同理）
```

**核实后的真实关系（与直觉相反，务必看清再动手）：**

| 谁在用 | 用的是哪一份 | 证据 |
|---|---|---|
| **运行时** | `momo/` **包** | `channels.py:60` → `momo_channel.py:7` `from .momo.momo_core import …`；`EXTRACTION_ARCHITECTURE.md` 亦如此标注 |
| **测试套件** | 顶层 `momo_*.py` | `tests/test_momo_support.py:14-18` `import payment_link_extractor.momo_core / momo_stripe / …` |

也就是说：**运行时跑 `momo/` 包，测试测顶层 5 文件**，两份必须手动保持一致——这正是"改一份、跑/测另一份"的漂移温床。

> **建议**：二选一收敛成单一权威源（推荐保留 `momo/` 包，因为它是运行时路径），把测试的 import 改指向包，然后删除顶层 5 个 `momo_*.py`（保留顶层 `momo_channel.py` 这个薄适配器）。这样可省掉 ~3.7k 行重复代码，并消除同步风险。**删除前需先改测试 import，否则测试会红。**

---

## 6. 关系判定

```
                 同源思路（AT→VN代理→Checkout→Stripe→MoMo）
                          │
        ┌─────────────────┴──────────────────┐
   A 本站 momo_*                         B 独立 9999 包
   (多渠道主仓库里的                    (从 9999 产品线裁出的
    MoMo 专用胖适配器，                   VN+MoMo 独立分发包，
    深到 momo.vn 网关 querySession)       浅到 Stripe 重定向层)
   构建线 9748354 / Chrome151            构建线 9999461 / Chrome146
```

- **不是**父子拷贝关系：架构（胖适配器 vs 通用 flows）、Sentinel 注入方式、客户端构建线、UA、以及最关键的 **momo.vn 网关是否驱动** 都不同。
- 更准确的描述：**两条独立实现分支**。B 的通用 `flows/providers` 风格反而更接近"通用支付框架"血统，A 是本站针对 VN MoMo HAR 做的更严格的私有实现。

---

## 7. 差异速查表（TL;DR）

| | A · 本站 `momo/` | B · 独立 9999 包 |
|---|---|---|
| 渠道/国家 | 多渠道 / 16 国 | 仅 MoMo / 仅 VN（硬锁） |
| 架构 | 专用 `momo_*` 胖适配器（~3.7k 行） | 通用 `flows`+`providers`（薄） |
| momo.vn 网关 | **GET pay + querySession 轮询 + t/s 强校验** | **不驱动**，Stripe 重定向即收尾 |
| 资格预检 | 代理池逐条循环 | 无独立循环 |
| Sentinel | BrowserSentinelProvider(CDP)+Node 兜底 | 短命子进程 Playwright |
| 遥测 | 数十个 `momo_*` 诊断字段 | 4 个精简字段 |
| 构建线 / UA | 9748354 / Chrome 151 | 9999461 / Chrome 146 |
| 形态 | 开发主仓库（含历史/测试） | 干净可部署分发包 |
| 内部注意 | `momo/` 包(运行时)与顶层 `momo_*.py`(测试)双份重复 | 单一 `flows/momo.py` |

---

*本报告仅做静态架构与协议链对比，未执行代码、未发请求、未触碰任何凭据。行号引用基于 2026-09-02 当前工作副本。*
