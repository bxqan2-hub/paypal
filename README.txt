OAI PayPal 独立去敏版

安装启动：双击 INSTALL_AND_START.bat
以后启动：双击 START.bat
停止服务：双击 STOP.bat
访问地址：http://127.0.0.1:18794/

默认配置：
- 仅使用 PayPal 提链页面
- 账单国家固定 DE，币种 EUR
- 支持 OAICS 与 CS Checkout
- 只有 paypal.com/agreements/approve?ba_token=BA-... 才会判定成功
- 支持常见 IPRocket/IPRoyal/1024Proxy 文本格式
- 代理粘贴后由浏览器本地保存

去敏内容：
- 未包含 Access Token
- 未包含代理账号、密码和代理池
- 未包含订阅地址、运行日志、历史结果、缓存、虚拟环境
- 工作台密码默认留空，仅监听本机 127.0.0.1
