"""studydash —— 把散在不同学习平台上的课程进度和作业进度，收进一张只读看板。

设计上只有三个不变量，其它都是实现细节：

1. **只读**。唯一的网络出口是 :mod:`studydash.http`，只发 GET；POST 仅限各
   适配器显式声明的**查询**端点（白名单，见 http 模块文档）。没有登录实现、
   没有提交、没有自动刷课。
2. **Cookie 鉴权，不存密码**。你在浏览器里登录，把 Cookie 贴进配置。
3. **平台适配器是插件**。内置超星 / 慕课 / 智慧树 / 手动导入，第三方包可以
   通过 ``studydash.providers`` entry point 塞自己的适配器进来。
"""

from __future__ import annotations

__version__ = "3.1.0"
__all__ = ["__version__", "main"]


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。延迟导入，``import studydash`` 保持轻量。"""
    from .cli import main as _main

    return _main(argv)
