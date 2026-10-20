"""头歌（educoder.net）登录：Cookie 校验 / 读浏览器 / 独立窗口登录。

头歌没有可用的扫码或账号密码协议（登录页是网易/自建表单，还会碰验证码），
所以这里只做三条路：

* :func:`check`：拿 Cookie 问一次 ``/api/users/get_user_info.json``；
* :func:`from_browser`：读本机 Chromium 系浏览器的 Cookie 库（浏览器要先关掉）；
* :func:`from_live`：起一个隔离配置档的浏览器窗口，你登录完自动取会话。

登录判据用的是 ``login`` 字段：游客（未登录 / 会话过期）也会拿到 **HTTP 200**，
只是 ``{"username":"游客","login":""}``。别拿 ``courses/mine.json`` 判——它对游客
同样返回 ``status:0`` 和空数组，看起来和「真的没选课」一模一样。
"""

from __future__ import annotations

import time
from typing import Any

from . import browsers, cdp
from .http import HttpClient
from .login import LoginError, Session, cookie_count
from .providers.educoder import BASE, USER_RPC, signed_headers

__all__ = ["BASE", "DOMAINS", "LOGIN_HOME", "check", "from_browser", "from_live"]

#: 登录页。用户在独立窗口里登录后，Cookie 会落在 ``.educoder.net``。
LOGIN_HOME = BASE + "/user/login"

DOMAINS = ("educoder.net",)


def check(cookie: str, *, timeout: float = 10.0, log=None) -> Session:
    """问一次用户信息，确认这条 Cookie 不是游客。"""
    if not cookie.strip():
        return Session(cookie="", source="", ok=False, message="Cookie 是空的")

    client = HttpClient(
        cookie=cookie,
        timeout=timeout,
        logger=(log.debug if log else None),
    )
    try:
        payload = client.get_json(
            USER_RPC, headers=signed_headers(), note="登录态自检", default=None
        )
    except OSError as exc:
        return Session(cookie=cookie, source="", ok=False, message=f"连不上头歌：{exc}")
    except Exception as exc:                            # noqa: BLE001 - 自检不该炸
        return Session(cookie=cookie, source="", ok=False, message=f"检查头歌会话失败：{exc}")

    if not isinstance(payload, dict):
        return Session(
            cookie=cookie,
            source="",
            ok=False,
            message="用户信息接口返回的不是 JSON，头歌大概改版了",
        )

    login = str(payload.get("login") or "").strip()
    name = str(payload.get("username") or payload.get("real_name") or "").strip()
    if not login or login == "游客":
        return Session(
            cookie=cookie,
            source="",
            cookie_count=cookie_count(cookie),
            ok=False,
            message="服务端把这次访问当成游客（Cookie 已过期或没带对）",
        )

    return Session(
        cookie=cookie,
        source="",
        uid=login or str(payload.get("user_id") or ""),
        cookie_count=cookie_count(cookie),
        message=f"会话可用（头歌{'，' + name if name else ''}）",
    )


# --------------------------------------------------------------------------- #
# 从浏览器拿
# --------------------------------------------------------------------------- #

def from_browser(browser: str = "edge", *, profile: str = "") -> str:
    """读日常浏览器配置档里的头歌 Cookie（浏览器得先关掉）。"""
    try:
        header, _hits = browsers.educoder_cookie(browser, profile=profile)
    except browsers.BrowserError as exc:
        raise LoginError(
            f"{exc}\n"
            "      → 或者改用 `--source live`，不用关浏览器。"
        ) from exc
    return header


def from_live(
    *,
    browser: str = "edge",
    port: int | None = None,
    timeout: float = 300.0,
    profile_root=None,
    log=None,
    poll: float = 2.0,
) -> str:
    """起隔离浏览器 → 等你登录 → 读出 Cookie。"""
    def say(message: str, level: str = "info") -> None:
        if log is None:
            print(message)
        else:
            getattr(log, level, log.info)(message)

    launched = cdp.launch(LOGIN_HOME, browser=browser, port=port, profile_root=profile_root)
    say(f"已用{browser}打开头歌登录页（独立配置档，不影响你原来开着的浏览器）。")
    say(f"配置档位置：{launched.profile_root}")
    say("请在那个窗口里登录；登进去之后不用管，我会自动取到会话并关掉这个窗口。")
    deadline = time.monotonic() + timeout
    checked = ""
    try:
        while time.monotonic() < deadline:
            time.sleep(poll)
            try:
                items = cdp.cookies_via_cdp(launched.port)
            except cdp.CdpError as exc:
                raise LoginError(f"读 Cookie 失败：{exc}") from exc
            hits = [
                cookie
                for cookie in _cdp_cookies(items)
                if _is_educoder_host(cookie.domain)
            ]
            header = browsers.cookie_header(hits, domain="www.educoder.net")
            if not header or header == checked:
                continue
            checked = header
            session = check(header)
            if session.ok:
                say("已取到登录会话。")
                launched.stop()
                return header
        raise LoginError(
            f"等了 {int(timeout)} 秒还没等到头歌登录会话。\n"
            f"      → 登录窗口还开着的话，确认一下是不是已经登进 educoder.net；\n"
            f"      → 想多等一会儿就加 `--timeout 600`；\n"
            f"      → 实在不行就走 `--source browser`（先关掉浏览器）或 `--source file`。"
        )
    finally:
        launched.stop()


def _is_educoder_host(domain: str) -> bool:
    host = (domain or "").lstrip(".").lower()
    return any(host == d or host.endswith("." + d) for d in DOMAINS)


def _cdp_cookies(items: list[dict[str, Any]]) -> list[browsers.Cookie]:
    out: list[browsers.Cookie] = []
    for item in items:
        name = str(item.get("name", ""))
        if not name:
            continue
        expires = item.get("expires")
        out.append(
            browsers.Cookie(
                name=name,
                value=str(item.get("value", "")),
                domain=str(item.get("domain", "")),
                path=str(item.get("path", "/")) or "/",
                expires=float(expires)
                if isinstance(expires, (int, float)) and expires > 0
                else None,
                secure=bool(item.get("secure")),
                http_only=bool(item.get("httpOnly")),
            )
        )
    return out
