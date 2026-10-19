"""本机安全网关：只认回环来源，只认本次启动的令牌。

设计前提写死在这里，免得后来者以为可以「顺手」放开：**这个服务是给本机上的这个
用户看的**。它持有用户的登录会话，所以任何「让局域网也能访问」的诱惑都要掀掉——
那等于把会话暴露给同一网段的所有设备。

不用 Cookie 做会话，因此没有 CSRF 面：跨站请求带不上自定义头，而我们只认自定义头。
"""

from __future__ import annotations

import ipaddress
import secrets
from typing import Any

__all__ = [
    "LOOPBACK_NAMES",
    "TOKEN_HEADER",
    "assert_bind_host",
    "client_host",
    "is_loopback",
    "new_token",
    "token_ok",
]

#: 每次启动重新生成的令牌放在这个头里。前端从 ``window.__STUDYDASH__`` 读。
TOKEN_HEADER = "x-studydash-token"

#: 这些字符串等价于回环地址
LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1", ""})

_TOKEN_BYTES = 24


def new_token() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def is_loopback(host: str) -> bool:
    """判断来源是不是本机。

    除了 ``127.0.0.1`` / ``::1``，还要认 ``::ffff:127.0.0.1`` 这类 IPv4 映射地址——
    双栈环境下 uvicorn 常常就是这么报客户端地址的，不认它会把正常请求挡在外面。
    """
    host = (host or "").strip().strip("[]")
    if host in LOOPBACK_NAMES:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    mapped = getattr(ip, "ipv4_mapped", None)
    return bool(mapped is not None and mapped.is_loopback)


def client_host(request: Any) -> str:
    client = getattr(request, "client", None)
    if client is None:
        return ""
    return str(getattr(client, "host", "") or "")


def token_ok(expected: str, provided: str) -> bool:
    """常数时间比较，不让令牌长度之类的信息从耗时里漏出去。"""
    if not expected:
        return False
    return secrets.compare_digest(provided or "", expected)


def assert_bind_host(host: str) -> str:
    """只允许绑回环。``0.0.0.0`` 之类直接拒绝启动。"""
    wanted = (host or "").strip() or "127.0.0.1"
    if is_loopback(wanted):
        return wanted
    raise ValueError(
        f"拒绝监听 {wanted}：studydash 的网页版持有你的登录会话，只允许绑在本机回环地址上。\n"
        f"      → 想从手机上看，请用命令行的 `studydash serve`，它只发布静态看板、不发布会话。"
    )
