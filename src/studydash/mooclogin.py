"""扫码登录中国大学 MOOC（icourse163.org）。

和超星那套（:mod:`studydash.qrlogin`）是**两套完全不同的协议**，不要混用：

======================  ====================================================
超星                     ``passport2.chaoxing.com`` 的 ``createqr`` + ``getauthstatus``
中国大学 MOOC            ``www.icourse163.org`` 的 ``logonByQRCode`` + 网易 URS
======================  ====================================================

流程（端点都是从真实登录页的请求里读出来的，2026-10-19 实测）：

1. ``GET /logonByQRCode/code.do?width=182&height=182`` → JSON::

       {"code": 0, "result": {"codeUrl": "http://…/<key>-qrcode-logon.png",
                              "pollKey": "<key>"}}

2. 把 ``codeUrl`` 换成 https 下载下来，交给 :mod:`studydash.qr` 画进终端。
3. ``GET /logonByQRCode/poll.do?pollKey=<key>``，每 2 秒问一次::

       {"code": 0, "result": {"codeStatus": 0, "token": null}}

   ``codeStatus``：0 未扫 / 1 已扫待确认 / 2 已确认 / 3 已过期 / 4 别的 App 扫的。
4. 确认后拿到 ``token``，``GET /passport/logingate/mocMobChangeCookie.htm``
   换成本站会话 Cookie。
5. 用 ``mocMemberFollowBean.userInfo.rpc``（只读查询，POST）确认真的登录了。

第 5 步是必要的：``NTESSTUDYSI`` 这种 Cookie 在**未登录**的页面上也会下发，
拿「有没有 Cookie」当登录判据会把游客当成已登录。
"""

from __future__ import annotations

import base64
import dataclasses
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from . import browsers, cdp, qr
from .http import HttpClient, HttpError
from .login import LoginError, Session, cookie_count
from .providers.base import ProviderError
from .qrlogin import QrLoginError, QrTimeoutError

__all__ = [
    "BASE",
    "CODE_URL",
    "EXCHANGE_URL",
    "INTERVAL",
    "LOGIN_HOME",
    "POLL_URL",
    "REFRESH_SECONDS",
    "USERINFO_URL",
    "QrTicket",
    "check",
    "from_browser",
    "from_live",
    "login",
    "make_client",
    "poll_once",
    "request_code",
    "wait_for_scan",
]

BASE = "https://www.icourse163.org"
LOGIN_HOME = BASE + "/member/login.htm"
CODE_URL = BASE + "/logonByQRCode/code.do"
POLL_URL = BASE + "/logonByQRCode/poll.do"
EXCHANGE_URL = BASE + "/passport/logingate/mocMobChangeCookie.htm"
#: 登录态自检。GET 不支持（服务端回 ``Get Method Not Support``），只能 POST；
#: 它是纯查询 RPC，所以走 ``HttpClient`` 的查询端点白名单。
USERINFO_URL = BASE + "/web/j/mocMemberFollowBean.userInfo.rpc"

#: 二维码寿命。页面自己也是过期就换一张，这里跟着换。
REFRESH_SECONDS = 110.0
#: 轮询间隔，和登录页的定时器一致。
INTERVAL = 2.0

#: 兑换 Cookie 之后要落回哪里（页面里是 ``str2b64(location.href)``）。
_RETURN_URL = base64.b64encode((BASE + "/").encode("utf-8")).decode("ascii")

_AJAX_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
}

#: 未扫 / 已扫待确认 / 已确认 / 已过期 / 别的 App 扫的
NOT_SCAN, SCAN_NOT_CONFIRM, CONFIRMED, EXPIRED, SCAN_BY_OTHER_APP = 0, 1, 2, 3, 4
_STATUS_TEXT = {
    NOT_SCAN: "等待扫码",
    SCAN_NOT_CONFIRM: "已扫码，请在手机上确认",
    CONFIRMED: "手机已确认",
    EXPIRED: "二维码过期",
    SCAN_BY_OTHER_APP: "这个码已被别的 App 扫过",
}

_RE_COOKIE = re.compile(r"(?:^|;\s*)([^=;\s]+)=([^;]*)")


class MoocLoginError(QrLoginError):
    """中国大学 MOOC 登录这条路上的故障。"""


class MoocTimeoutError(QrTimeoutError):
    """等太久没人扫码。"""


@dataclass
class QrTicket:
    """一张待扫的码，连带轮询它需要的字段。"""

    poll_key: str
    code_url: str
    png: bytes

    @property
    def text(self) -> str:
        return qr.render_png(self.png, margin=2)


# --------------------------------------------------------------------------- #
# 客户端与取码
# --------------------------------------------------------------------------- #

def make_client(*, timeout: float = 15.0, log=None) -> HttpClient:
    say = (log.debug if log else (lambda _m: None))
    return HttpClient(
        timeout=timeout,
        delay=0.25,
        referer=LOGIN_HOME,
        headers={"Origin": BASE},
        query_endpoints=(USERINFO_URL,),
        logger=say,
    )


def _result(payload: Any, what: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise MoocLoginError(f"{what}返回的不是 JSON，中国大学 MOOC 大概改版了")
    if payload.get("code") not in (0, "0"):
        message = str(payload.get("message") or "").strip()
        raise MoocLoginError(
            f"{what}失败：{message or '服务端返回 code=' + str(payload.get('code'))}"
        )
    result = payload.get("result")
    if not isinstance(result, dict):
        raise MoocLoginError(f"{what}的响应里没有 result 对象，中国大学 MOOC 大概改版了")
    return result


def request_code(client: HttpClient, *, log=None) -> QrTicket:
    """要一张新的二维码。"""
    payload = client.get(
        CODE_URL,
        {"width": 182, "height": 182},
        headers=_AJAX_HEADERS,
        note="扫码二维码",
    ).json(None)
    result = _result(payload, "要二维码")

    poll_key = str(result.get("pollKey") or "").strip()
    code_url = str(result.get("codeUrl") or "").strip()
    if not poll_key or not code_url:
        raise MoocLoginError(
            "二维码响应里没有 pollKey / codeUrl，中国大学 MOOC 大概改版了。\n"
            "  → 跑 `studydash probe icourse163` 留档，或者改用 --source live 在浏览器里登"
        )
    # 服务端给的是 http://，在 https 页面里会被浏览器拦；直接换掉。
    code_url = re.sub(r"^http://", "https://", code_url)

    png = client.get(
        code_url,
        headers={"Accept": "image/avif,image/webp,image/*,*/*;q=0.8"},
        note="二维码图片",
    ).content
    if len(png) < 60 or not png.startswith(b"\x89PNG"):
        raise MoocLoginError(
            f"二维码图片只有 {len(png)} 字节（不是 PNG），不像有效图片；可能是 CDN 拦了请求"
        )

    return QrTicket(poll_key=poll_key, code_url=code_url, png=png)


# --------------------------------------------------------------------------- #
# 轮询
# --------------------------------------------------------------------------- #

def poll_once(client: HttpClient, ticket: QrTicket) -> dict[str, Any]:
    """问一次「扫了没」。返回 ``{"codeStatus": int, "token": str}``。"""
    payload = client.get(
        POLL_URL,
        {"pollKey": ticket.poll_key},
        headers=_AJAX_HEADERS,
        note="扫码状态",
    ).json(None)
    result = _result(payload, "查询扫码状态")
    try:
        status = int(result.get("codeStatus") or 0)
    except (TypeError, ValueError):
        status = NOT_SCAN
    return {"codeStatus": status, "token": str(result.get("token") or "")}


def wait_for_scan(
    client: HttpClient,
    ticket: QrTicket,
    *,
    timeout: float = 240.0,
    interval: float = INTERVAL,
    refresh: float = REFRESH_SECONDS,
    log=None,
    on_code: Callable[[QrTicket], None] | None = None,
    on_tick: Callable[[dict[str, Any]], None] | None = None,
) -> Session:
    """盯着一张码（或它的接班码），扫到为止。"""
    started = time.monotonic()
    ticket_started = started
    last_status = -1
    ticks = 0

    while True:
        if time.monotonic() - ticket_started >= refresh:
            if time.monotonic() - started >= timeout:
                break
            ticket = request_code(client, log=log)
            ticket_started = time.monotonic()
            last_status = -1
            if on_code:
                on_code(ticket)

        payload = poll_once(client, ticket)
        ticks += 1
        status = payload["codeStatus"]

        if status == CONFIRMED:
            return _finish(client, payload["token"], log=log)

        if status == EXPIRED:
            ticket = request_code(client, log=log)
            ticket_started = time.monotonic()
            last_status = -1
            if on_code:
                on_code(ticket)
            continue

        if on_tick and status != last_status:
            on_tick({"status": status, "mes": _STATUS_TEXT.get(status, "")})
            last_status = status

        if time.monotonic() - started >= timeout:
            break
        time.sleep(interval)

    raise MoocTimeoutError(
        f"等了 {timeout:.0f} 秒（问了 {ticks} 次）也没等到扫码确认"
        + (f"，服务端最后说的是「{_STATUS_TEXT.get(last_status, '')}」" if last_status >= 0 else "")
    )


def _finish(client: HttpClient, token: str, *, log=None) -> Session:
    """手机确认了：拿 token 换会话 Cookie，再验一道。"""
    if not token:
        raise MoocLoginError(
            "手机已经确认，但服务端没有下发 token。\n"
            "  → 再扫一次；如果总是这样，中国大学 MOOC 大概改了登录协议"
        )
    try:
        client.get(
            EXCHANGE_URL,
            {"token": token, "returnUrl": _RETURN_URL},
            note="换取会话",
        )
    except (HttpError, ProviderError) as exc:
        raise MoocLoginError(f"拿 token 换会话失败：{exc}") from exc

    cookie = session_cookie(client)
    if not cookie:
        raise MoocLoginError(
            "扫到了，但服务端没有下发会话 Cookie。\n"
            "  → 再扫一次；如果总是这样，中国大学 MOOC 大概改了登录协议"
        )
    session = check(cookie, log=log)
    return dataclasses.replace(session, source="qr")


def session_cookie(client: HttpClient) -> str:
    """从 Cookie 罐里取本站的会话头。"""
    header = client.cookie_header(BASE + "/")
    if header:
        return header
    return "; ".join(f"{name}={value}" for name, value in client.jar_cookies())


# --------------------------------------------------------------------------- #
# 校验会话
# --------------------------------------------------------------------------- #

def _cookie_value(cookie: str, name: str) -> str:
    for match in _RE_COOKIE.finditer(cookie or ""):
        if match.group(1) == name:
            return match.group(2).strip()
    return ""


def _jar_value(client: HttpClient, name: str) -> str:
    for key, value in client.jar_cookies():
        if key == name:
            return value
    return ""


def check(cookie: str, *, timeout: float = 10.0, log=None) -> Session:
    """拿这个 Cookie 问一句「我登录了吗」。

    ``NTESSTUDYSI`` 在未登录页面上也会下发，所以不能只看 Cookie 在不在；
    必须问用户信息接口：未登录时 ``{"code":0,"result":null}``，登录后 ``result``
    是用户对象。
    """
    if not cookie.strip():
        return Session(cookie="", source="", ok=False, message="Cookie 是空的")

    client = HttpClient(
        cookie=cookie,
        timeout=timeout,
        query_endpoints=(USERINFO_URL,),
        logger=(log.debug if log else None),
    )
    csrf = _cookie_value(cookie, "NTESSTUDYSI")
    if not csrf:
        # 有些导出的 Cookie 不带这个域下的项；访问一次首页就能让服务端补上。
        try:
            client.get(LOGIN_HOME, note="登录态自检（取会话参数）")
        except (HttpError, ProviderError):
            pass
        csrf = _jar_value(client, "NTESSTUDYSI")

    url = USERINFO_URL + (f"?csrfKey={csrf}" if csrf else "")
    try:
        payload = client.post_query_json(
            url,
            {"csrfKey": csrf} if csrf else {},
            headers=_AJAX_HEADERS,
            note="登录态自检",
            default=None,
        )
    except (HttpError, ProviderError) as exc:
        return Session(cookie=cookie, source="", ok=False, message=f"连不上中国大学 MOOC：{exc}")
    except OSError as exc:
        return Session(cookie=cookie, source="", ok=False, message=f"网络错误：{exc}")

    if not isinstance(payload, dict):
        return Session(
            cookie=cookie,
            source="",
            ok=False,
            message="用户信息接口返回的不是 JSON，中国大学 MOOC 大概改版了",
        )
    if payload.get("code") not in (0, "0"):
        return Session(
            cookie=cookie,
            source="",
            ok=False,
            message=f"服务端拒绝了这次查询：{payload.get('message') or payload.get('code')}",
        )

    result = payload.get("result")
    if not isinstance(result, dict) or not result:
        return Session(
            cookie=cookie,
            source="",
            cookie_count=cookie_count(cookie),
            ok=False,
            message="服务端说这个 Cookie 还没登录（用户信息为空）",
        )

    uid = ""
    for key in ("id", "userId", "uid", "userIdStr"):
        value = result.get(key)
        if value not in (None, ""):
            uid = str(value)
            break
    name = ""
    for key in ("nickName", "name", "userName"):
        value = result.get(key)
        if value not in (None, ""):
            name = str(value)
            break

    return Session(
        cookie=cookie,
        source="",
        uid=uid,
        cookie_count=cookie_count(cookie),
        message=f"会话可用（中国大学 MOOC{'，' + name if name else ''}）",
    )


# --------------------------------------------------------------------------- #
# 从浏览器拿
# --------------------------------------------------------------------------- #

def from_browser(browser: str = "edge", *, profile: str = "") -> str:
    """读日常浏览器配置档里的中国大学 MOOC Cookie（浏览器得先关掉）。"""
    try:
        header, _hits = browsers.icourse163_cookie(browser, profile=profile)
    except browsers.BrowserError as exc:
        raise LoginError(str(exc)) from exc
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
    say(f"已用{browser}打开中国大学 MOOC 登录页（独立配置档，不影响你原来开着的浏览器）。")
    say(f"配置档位置：{launched.profile_root}")
    say("请在那个窗口里扫码或登录；登进去之后不用管，我会自动取到会话并关掉这个窗口。")
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
                if _is_icourse_host(cookie.domain)
            ]
            header = browsers.cookie_header(hits, domain="www.icourse163.org")
            if not header or header == checked:
                continue
            checked = header
            session = check(header)
            if session.ok:
                say("已取到登录会话。")
                launched.stop()
                return header
        raise LoginError(
            f"等了 {int(timeout)} 秒还没等到中国大学 MOOC 登录会话。\n"
            f"      → 登录窗口还开着的话，确认一下是不是已经登进 icourse163.org；\n"
            f"      → 想多等一会儿就加 `--timeout 600`；\n"
            f"      → 实在不行就走 `--source browser`（先关掉浏览器）或 `--source file`。"
        )
    finally:
        launched.stop()


def _cdp_cookies(items: list[dict]) -> list[browsers.Cookie]:
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
                expires=float(expires) if isinstance(expires, (int, float)) and expires > 0 else None,
                secure=bool(item.get("secure")),
                http_only=bool(item.get("httpOnly")),
            )
        )
    return out


def _is_icourse_host(domain: str) -> bool:
    host = (domain or "").lstrip(".").lower()
    return any(host == d or host.endswith("." + d) for d in browsers.ICOURSE163_DOMAINS)


# --------------------------------------------------------------------------- #
# 一步到位
# --------------------------------------------------------------------------- #

def login(
    *,
    timeout: float = 240.0,
    interval: float = INTERVAL,
    refresh: float = REFRESH_SECONDS,
    log=None,
    on_code: Callable[[QrTicket], None] | None = None,
    on_tick: Callable[[dict[str, Any]], None] | None = None,
    png_path=None,
) -> Session:
    """出码 → 等扫 → 返回会话。"""
    client = make_client(log=log)
    ticket = request_code(client, log=log)

    if png_path is not None:
        try:
            with open(png_path, "wb") as handle:
                handle.write(ticket.png)
        except OSError:
            pass

    if on_code:
        on_code(ticket)

    return wait_for_scan(
        client,
        ticket,
        timeout=timeout,
        interval=interval,
        refresh=refresh,
        log=log,
        on_code=on_code,
        on_tick=on_tick,
    )
