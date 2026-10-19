"""扫二维码登录超星：码出在终端里，手机扫一下就完事。

为什么值得单独做一条路：前面那几条路都要「你已经在某个地方登录过」
（浏览器配置档 / 扩展导出 / 手抄 Cookie）。扫码是**第一次登录**也能用的，
而且从头到尾**没有一个字符的密码经过本程序**——码是超星发的，扫是你扫的，
本程序只负责把码画到终端、然后盯着服务端问一句「他扫了没」。

流程（与浏览器登录页做的事完全一致，三处端点都是从页面上读出来的）：

1. ``GET /login`` → 页面里有 ``uuid`` / ``enc`` / ``doubleFactorLogin`` /
   ``forbidotherlogin`` 四个隐藏字段，以及 Cookie 罐要收下的 ``JSESSIONID``。
2. ``GET /createqr?uuid=…&fid=-1`` → 一张黑白点阵图，交给 :mod:`studydash.qr`
   画进终端。
3. ``POST /getauthstatus/v2``，每 2.5 秒问一次。未扫返回
   ``{"mes":"未登录","type":"3","status":false}``；扫了并确认后 ``status`` 为真，
   同时响应里下发会话 Cookie——那些 Cookie 就是我们要的东西。

二维码大约两分钟失效，所以超时会自动换一张重画，不至于让你对着一个死码发呆。
"""

from __future__ import annotations

import dataclasses
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from . import qr
from .http import HttpClient
from .login import (
    LoginError,
    Session,
    check as check_session,
)

__all__ = [
    "BASE",
    "LOGIN_URL",
    "POLL_PATH",
    "QR_PATH",
    "REFER",
    "QrTicket",
    "QrLoginError",
    "request_code",
    "poll_once",
    "wait_for_scan",
    "login",
]

BASE = "https://passport2.chaoxing.com"
LOGIN_URL = BASE + "/login"
QR_PATH = "/createqr"
POLL_PATH = "/getauthstatus/v2"
REFER = "http://i.chaoxing.com"

#: 服务端二维码的实际寿命，超过就换一张（页面上的定时器是 50 × 3 秒）
REFRESH_SECONDS = 110.0
#: 轮询间隔，跟浏览器登录页的定时器一致
INTERVAL = 2.5

POLL_URL = BASE + POLL_PATH
CREATEQR_URL = BASE + QR_PATH

#: 轮询请求要装的头，跟页面上的 jQuery 一致——服务端会挑这些头
AJAX_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}

_RE_INPUT_TAG = '<input[^>]*id="%s"[^>]*>'
_RE_VALUE = re.compile(r'value="([^"]*)"')


class QrLoginError(LoginError):
    """扫码这条路上的故障（拿不到码 / 页面结构变了 / 超时没扫）。"""


@dataclass
class QrTicket:
    """一张待扫的二维码，连带轮询它需要的字段。"""

    uuid: str
    enc: str
    double_factor: str
    forbid_other: str
    png: bytes
    refer: str = REFER

    @property
    def text(self) -> str:
        """可以直接 ``print`` 的终端二维码。"""
        return qr.render_png(self.png, margin=2)

    def form(self) -> dict[str, str]:
        """轮询要发的表单。"""
        return {
            "enc": self.enc,
            "uuid": self.uuid,
            "doubleFactorLogin": self.double_factor,
            "forbidotherlogin": self.forbid_other,
        }


# --------------------------------------------------------------------------- #
# 解析与取码
# --------------------------------------------------------------------------- #
def hidden_value(html: str, ident: str) -> str:
    """取 ``<input id="ident" value="…">`` 的 value。

    不写成 ``id="x"[^>]*value="…"``：真实页面里 ``value`` 在 ``id`` **前面**
    （``<input type="hidden" value="…" id="uuid"/>``），按固定顺序的匹配会全部落空。
    先框出整个标签再取 value，与属性顺序无关。
    """
    tag = re.search(_RE_INPUT_TAG % re.escape(ident), html)
    if not tag:
        return ""
    found = _RE_VALUE.search(tag.group(0))
    return found.group(1) if found else ""


def make_client(*, timeout: float = 15.0, log=None) -> HttpClient:
    """给扫码流程用的客户端：只额外允许 POST 到轮询端点。"""
    say = (log.debug if log else (lambda _m: None))
    return HttpClient(
        timeout=timeout,
        delay=0.25,
        referer=LOGIN_URL,
        headers={"Origin": BASE},
        query_endpoints=(POLL_URL,),
        logger=say,
    )


def request_code(client: HttpClient, *, log=None) -> QrTicket:
    """要一张新的二维码。"""
    html = client.get(
        LOGIN_URL, {"fid": "", "refer": REFER},
        headers={"Upgrade-Insecure-Requests": "1"},
        note="扫码登录页",
    ).text

    uuid = hidden_value(html, "uuid")
    enc = hidden_value(html, "enc")
    if not uuid or not enc:
        raise QrLoginError(
            "登录页里没有 uuid/enc 字段，超星大概改版了。\n"
            f"  → 跑 `studydash probe chaoxing` 留档，或者改用 --source live 在浏览器里登"
        )

    png = client.get(
        f"{CREATEQR_URL}?uuid={uuid}&fid=-1",
        headers={"Accept": "image/avif,image/webp,image/*,*/*;q=0.8"},
        note="二维码图片",
    ).content
    if len(png) < 60:
        raise QrLoginError(f"二维码图片只有 {len(png)} 字节，不像有效图片")

    return QrTicket(
        uuid=uuid,
        enc=enc,
        double_factor=hidden_value(html, "doubleFactorLogin"),
        forbid_other=hidden_value(html, "forbidotherlogin"),
        png=png,
    )


# --------------------------------------------------------------------------- #
# 轮询
# --------------------------------------------------------------------------- #
def poll_once(client: HttpClient, ticket: QrTicket) -> dict[str, Any]:
    """问一次「扫了没」。返回服务端的原始 JSON。"""
    payload = client.post_query(
        POLL_URL, ticket.form(), headers=AJAX_HEADERS, note="扫码状态",
    ).json(None)
    if not isinstance(payload, dict):
        raise QrLoginError("扫码状态接口返回的不是 JSON，超星大概改版了")
    return payload


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
    """盯着一张码（或它的接班码），扫到为止。

    ``refresh`` 到了还没扫就换一张新的并重新回调 ``on_code``——服务端两分钟就把
    码作废，继续对着旧码轮询只是白等。
    """
    started = time.monotonic()
    ticket_started = started
    last_mes = ""
    ticks = 0

    while True:
        if time.monotonic() - ticket_started >= refresh:
            if time.monotonic() - started >= timeout:
                break
            ticket = request_code(client, log=log)
            ticket_started = time.monotonic()
            last_mes = ""
            if on_code:
                on_code(ticket)

        payload = poll_once(client, ticket)
        ticks += 1

        if payload.get("status"):
            return _finish(client, payload, log=log)

        mes = str(payload.get("mes") or "").strip()
        if on_tick and mes != last_mes:
            on_tick(payload)
            last_mes = mes

        if time.monotonic() - started >= timeout:
            break
        time.sleep(interval)

    raise QrLoginError(
        f"等了 {timeout:.0f} 秒（问了 {ticks} 次）也没等到扫码确认"
        + (f"，服务端最后说的是「{last_mes}」" if last_mes else "")
        + "\n  → 再跑一次 `studydash login` 重新出码；"
        "手机要连着网，且用的是学习通 App（不是微信公众号）"
    )


def _finish(client: HttpClient, payload: dict[str, Any], *, log=None) -> Session:
    """扫到了：把会话 Cookie 抠出来，顺手验证一下它真能用。"""
    if payload.get("containTwoFactorLogin"):
        url = payload.get("twoFactorLoginPCUrl") or ""
        raise QrLoginError(
            "这个账号开了二次验证，扫码之后还要再过一道验证，程序做不了。\n"
            f"  → 用 `studydash login --source live` 在浏览器里过掉二次验证，"
            "再用 `--source browser` 把 Cookie 读回来\n"
            + (f"  → 二次验证页面：{url}" if url else "")
        )

    cookie = session_cookie(client)
    if not cookie:
        raise QrLoginError(
            "扫到了，但服务端没有下发会话 Cookie。\n"
            "  → 再扫一次；如果总是这样，超星大概改了登录协议"
        )

    session = check_session(cookie, log=log)
    return dataclasses.replace(session, source="qr")


def session_cookie(client: HttpClient) -> str:
    """从 Cookie 罐里取出能代表这次会话的 Cookie 串。

    先按域挑一遍（``i.chaoxing.com`` 上能用的），挑不出来就整罐全要——超星有的
    时候把会话 Cookie 挂在 ``.chaoxing.com``，有的时候只挂在 ``passport2`` 上，
    两种都得认。
    """
    for url in ("https://i.chaoxing.com/base", "https://mooc1.chaoxing.com/",
                "https://passport2.chaoxing.com/"):
        header = client.cookie_header(url)
        if header:
            return header
    pairs = client.jar_cookies()
    return "; ".join(f"{name}={value}" for name, value in pairs)


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
    """出码 → 等扫 → 返回已验证的会话。

    ``on_code`` 收到刚生成的 :class:`QrTicket`（``.text`` 就是终端二维码）；
    ``png_path`` 给了就把原图也存一份，终端画不出来时还有条后路。
    """
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
        client, ticket,
        timeout=timeout, interval=interval, refresh=refresh,
        log=log, on_code=on_code, on_tick=on_tick,
    )
