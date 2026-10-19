"""studydash 的 Web 层。

这是一个**本机应用**：后端只监听回环，每次启动换一个新的访问令牌，前端是同一次
启动里发出去的那个页面。这里不做多租户鉴权，因为前提就是「这台机器上的这个用户，
就是账号的主人」。

分层：

``state.py``
    一次运行的全部状态：工作区、令牌、任务中心。
``security.py``
    回环来源校验与令牌比对。
``tasks.py``
    任务中心。登录、抓取、探测都跑在这里，进度按任务汇报。
``operations.py``
    各个任务的真正实现，把 kind 映射到已有的登录/抓取/探测能力。
``app.py``
    FastAPI 装配：中间件、异常处理、静态资源与 SPA 回退。
``routers/``
    各个业务域的路由。

接口契约冻结在 ``docs/web-api.md``，改接口先改那份文档。

**注意**：本包依赖 FastAPI，而它是可选依赖。所以 ``studydash/__init__.py`` 与
``cli.py`` 都不在模块顶层 import 本包，只在真的要起服务时延迟导入。
"""

from __future__ import annotations

from typing import Any

__all__ = ["BOOT_PLACEHOLDER", "create_app", "run"]

#: 前端 ``index.html`` 里必须原样出现这一行。后端把整串替换成真实启动参数，
#: 所以直接用浏览器打开构建产物（没有后端）时它是合法的 ``null``，
#: 前端据此显示「请从命令行启动」而不是白屏。
BOOT_PLACEHOLDER = "window.__STUDYDASH__ = null;"


def create_app(*args: Any, **kwargs: Any):
    """延迟导入，避免没装 FastAPI 时 ``import studydash.web`` 就炸。"""
    from .app import create_app as _factory

    return _factory(*args, **kwargs)


def run(*args: Any, **kwargs: Any) -> int:
    """起服务（阻塞）。返回进程退出码。"""
    from .app import run as _run

    return _run(*args, **kwargs)
