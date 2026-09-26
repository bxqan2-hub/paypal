"""upi-zero-link · 印度 ₹0 UPI Autopay 提链（协议层，无浏览器）。

模块：
  identity   设备指纹 / 通用头
  addresses  印度真实地址池
  verify     交付前核验（intent_state + fam）
  protocol   8 步提链流程
  cli        命令行入口
"""

__version__ = "1.0.0"

from .protocol import extract  # noqa: F401

__all__ = ["extract", "__version__"]
