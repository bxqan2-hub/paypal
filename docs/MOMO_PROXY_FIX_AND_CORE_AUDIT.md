# MoMo 代理修复与核心对比（2026-09-11）

## 范围与结论

对比基准为桌面 `momo_only_current_9999_20260831` 独立包；当前项目为桌面 `提链`。排除 UI 外观，只对比提链、金额校验与结果判定。

本次仅修代理输入、网络链桥和本地证书配置。**没有修改用户的金额开关、默认值、阈值、Checkout、Update、Stripe、OAICS 或任务金额判定。**

MoMo 主请求流程同源，但完整运行效果不是完全一致。用户新增的金额开关本身传递正常；区别包括原包额外的最终严格零金额判定，以及当前项目已有的搬迁导入遗留。

## 代理与本地环境修复

- 替换 `momo/_transport.py:normalize_proxy_url` 的旧 URL-only 解析，复用现有共享格式解析和 `iprocket_chain_bridge.py`，未另建第二套提链/链式实现。
- 支持 Arxlabs 的 `HOST:PORT:USER:PASSWORD` 输入，默认按 SOCKS5；显式协议优先，保留原供应商格式。用户名后的第四段作为密码，保留冒号、@、百分号等原始字符；反复归一化不会损坏转义。
- MoMo HTTP、浏览器和该供应商的工作台代理检测使用同一条动态认证 HTTP 桥。Chromium 无预发认证时收到标准 407 挑战。
- 首跳失败立即报错，删除原来静默直连业务代理的回退。修复 HTTP CONNECT 空读等待，错误响应不回显凭据。
- 现有首跳配置补齐为 `IPROCKET_PRE_PROXY_HOST=127.0.0.1`、`IPROCKET_PRE_PROXY_PORT=7897`；桥端口仍为 18796。
- 中文绝对目录会触发本机 curl 错误 77。通过 `CURL_CA_BUNDLE=.venv/Lib/site-packages/certifi/cacert.pem` 指定相同 certifi 证书的相对路径，**证书验证仍然启用**。启动工作目录应为项目根目录。

实际链路：`MoMo → 127.0.0.1:18796 → 本机 SOCKS 127.0.0.1:7897 → 业务 SOCKS5 → 目标`。

## 金额开关效果核验：未改任何金额代码

开关链路：`web/routes.py:280–300,343 → ExtractionConfig.momo_zero_trial_validation → application / TaskManager / momo_core 的配置传递 → momo/_flow.py:271 与 momo/_oaics.py:392`。

默认值、请求 True/False、环境 False、请求覆盖环境等均正确；字符串和数字不会被误当成布尔开关。

### 协议内校验

开关开启时，两种协议的金额闸门与独立包相同：允许 `0 <= amount <= 50` minor units。关闭时跳过这两处闸门。两者都不是单独的严格 `amount == 0` 判断。

### 完整任务判定

独立包 `web/tasks.py:269` 另调用 `_zero_amount_validation_error()`，最终要求有限的金额字段严格为 0。

当前项目 `web/tasks.py:738–741` 在提取器正常返回后直接标记成功，没有这一层最终金额门。当前的 `_has_nonzero_amount()` 仅控制成功的非零任务是否可重试，不是成功校验。

对真实协议金额函数、core 结果构造和真实 TaskManager 进行隔离模拟，CS 与 OAICS 的主要边界结果如下：

| 金额 minor units | 独立包最终任务 | 当前项目开关开启 | 当前项目开关关闭 |
| --- | --- | --- | --- |
| 0 | 成功 | 成功 | 成功 |
| 1、50 | 最终严格零金额门失败 | 成功 | 成功 |
| 51、-1 | 协议金额门失败 | 协议金额门失败 | 成功 |
| 缺失、空值 | core 归零后成功 | 下述既有导入错误 | 同样的导入错误 |

因此：**开关开启时的协议校验相同，但不等于独立包的全部金额校验效果。** 此差异只报告，不擅自增加最终零金额门，也不修改开关。

测试包含 60 个协议金额组合、60 个 core→任务组合、36 个任务结果门组合与 10 个配置组合。网络连接在该测试进程中被强制禁用，不代表上游会接受模拟金额。

## 其他提链核心对比

- 四个对应协议文件共 60 个顶层函数，57 个 AST 完全相同；其余 3 个差异是金额开关及参数传递。此统计不代表导入路径完全相同。
- 主流程、请求 URL / 参数、VN/VND、Chrome 146 身份和 Sentinel SDK 构建同源：`Checkout → Checkout/update → Stripe/Elements → taxes → ConfirmationToken → confirm → Intent → MoMo redirect`。
- 当前 `momo/_stripe_common.py:64` 的回退导入仍为 `.flows.oaics`，搬入子包后指向不存在的 `payment_link_extractor.momo.flows`。金额字段缺失时，在 `momo_core.py:85` 结果构造阶段可复现 ModuleNotFoundError；与金额开关无关。
- 当前 `momo/_checkout.py:329` 的旧 helper 仍导入 `.config`，指向不存在的 `payment_link_extractor.momo.config`；当前主 MoMo 流程未调用这个 helper，属于潜伏问题。
- 当前 `momo/_oaics.py:435/441/447/453` 部分延迟导入仍指向全局金额/配置 helper；目前涉及函数语义相同，未发现由此导致的请求差异。
- 独立包的 `providers/momo.py` 将 `momoapp.page.link` 视为终点；当前项目不包含该 preferred host，会继续跟随短链，因此最终落点可能不同。
- 独立包默认有 10 条 VN 地址文件；当前 `momo/_config.py:31` 的默认地址文件路径不存在，且本机没有 `OPLL_VN_BILLING_FILE` 覆盖，因此使用源码的两条 fallback。生成算法相同，资源来源不同。
- 当前版本增强了 AT 导入格式、Windows/Linux Chrome 146 定位和结果审计字段；这些不是主提链协议重写。

上述非代理差异均未在本次修改中调整。

## 验证与交付自检 R8

1. 修改已有实现：`transport.py` 的代理识别、归一化与桥接；`momo/_transport.py:normalize_proxy_url`；`iprocket_chain_bridge.py` 的 CONNECT、open_chain、Handler；补齐 `.env.example` 的本地网络配置。
2. 删除了 MoMo 旧代理解析体和失败直连路径；没有保留平行实现。新增专项测试填补链桥测试空缺，另扩展已有 MoMo/传输测试。
3. 无文件搬迁，无新提链核心副本。
4. 配置读取：现有首跳环境变量经 dotenv 加载，由桥模块读取后用于 `open_chain`；CURL_CA_BUNDLE 经 dotenv → curl-cffi BaseSession.verify → CAINFO。它们是基础网络配置，不改变 ExtractionConfig 金额开关。代理输入经 routes → ExtractionConfig → MoMo set_proxy_url → 共享桥实际使用。
5. 删除路径核查，以下命令输出均为空：

   ```text
   rg -n 'socket.create_connection\(\(proxy_host, proxy_port\)' iprocket_chain_bridge.py docs .env.example tests
   rg -n 'enhanced' payment_link_extractor/transport.py
   rg -n 'urlunsplit|safe="%"' payment_link_extractor/momo/_transport.py
   ```

6. 本次增删统计为 **+498 / −74**；包含新增测试及本报告，原协议/金额文件不在修改列表。
7. 实测：真实业务 SOCKS5 的 MoMo curl HTTPS 返回 200、Chromium 146 返回 200、代理检测返回 VN。证书验证开启。全套回归最后五行：

   ```text
   FAILED tests/test_mk_gcash_replacement.py::test_gcash_local_upstream_is_the_read_only_authority
   FAILED tests/test_mk_gcash_replacement.py::test_direct_upstream_app_call_maps_result_and_payload
   FAILED tests/test_mk_gcash_replacement.py::test_upstream_loader_points_at_authoritative_local_app
   FAILED tests/test_mk_gcash_replacement.py::test_gcash_rejects_non_contract_success_url
   5 failed, 361 passed in 2.29s
   ```

8. 未做/存疑：未发起实际提链或支付；未修改金额开关及上述非代理差异。五项全套回归失败均依赖缺失的 GCash 只读本地上游，与本次代理修改无关，没有擅自下载/修改 GCash。已使用项目原 START.bat 重启完成，18794 工作台和 18796 链桥由同一新进程运行；在线健康检查及四段式代理检测均返回 200 / VN，另一个 5000 工作台保持运行。

原有 artifacts、sentinel_bootstrap.js 和未跟踪本地工具目录的用户改动保持原样，不纳入本次提交；`.env`、运行凭据和日志不入 Git。
