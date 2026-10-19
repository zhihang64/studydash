"""拿到一个能用的会话，并存进配置。

这是 studydash「真能用」的关键一环：过去要用户自己去开发者工具里翻 Cookie，
现在有四条路，按省事程度排：

============  ==========================================================
``live``      起一个**隔离配置档**的浏览器，你在里面正常登录；我们从调试端口
              把 Cookie 读出来。**不用关你日常在用的浏览器**，也不碰它的配置档。
``browser``   直接读你日常浏览器配置档里的超星 Cookie。最快，但要先关掉浏览器
              （Chromium 对 Cookie 库加了独占锁）。
``file``      浏览器扩展导出的文件：JSON / Netscape cookies.txt / 一行 Cookie 头。
``manual``    把 Cookie 头直接粘进来。
============  ==========================================================

只做一件事：**把 Cookie 拿过来**。不代你登录、不猜密码、不碰验证码。
登录这一步永远由你在真实浏览器里完成。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import browsers, cdp
from .http import HttpClient, HttpError
from .providers.base import AccountConfig, ProviderError, login_page_evidence
from .store import Config

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

#: 登录态自检地址：未登录会返回登录页而不是 302，所以要看正文判断。
LOGIN_CHECK = "https://i.chaoxing.com/base/settings"
#: 打开这个地址，浏览器会自己跳去登录页（如果没登录）。
LOGIN_HOME = "https://i.chaoxing.com/base"

RE_UID = re.compile(r"_uid=(\d+)")
RE_D = re.compile(r"(?:^|;)\s*_d=")
RE_COOKIE_PAIR = re.compile(r"^\s*([^=\s;]+)\s*=\s*(.*?)\s*$")
RE_JSESSIONID_IN_URL = re.compile(r"jsessionid=([0-9A-Fa-f]{16,})")

#: 判「返回的是不是登录页」用结构特征，不用关键词。
#:
#: 原先这里是一张中文词表（``用户登录`` / ``passport2`` …），在正文前 8000 字里扫。
#: 两个致命毛病：① 超星登录后的页面头部同样带 ``passport2``（静态资源域、退出
#: 链接），于是**有效会话被判死**；② 真正的登录表单在 13000 字之后，压根没进
#: 扫描窗口，判据实际上只剩几个碰巧命中的中文词。现在统一交给
#: :func:`studydash.providers.base.login_page_evidence`：看表单结构。

#: 取会话的所有途径。前两个由工具自己在内部走完整个登录流程
#: （``qr`` 在终端出二维码用手机扫，``password`` 输账号密码）；
#: 后四个只是把别处已经存在的 Cookie 搬过来而已。
SOURCES = ("qr", "password", "live", "browser", "file", "manual")


class LoginError(Exception):
    """拿会话的过程中出的问题（文案直接给用户看）。"""


@dataclass
class Session:
    """一次成功取到的会话。"""

    cookie: str
    source: str
    label: str = ""
    uid: str = ""
    ok: bool = True
    message: str = ""
    cookie_count: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        who = f"uid={self.uid}" if self.uid else "uid 未知"
        return f"{self.label or self.source}（{who}，{self.cookie_count} 条 Cookie）"


# --------------------------------------------------------------------------- #
# 一、把各种来源归一成 Cookie 请求头
# --------------------------------------------------------------------------- #

def _clean_pairs(pairs: list[tuple[str, str]]) -> str:
    seen: dict[str, str] = {}
    for name, value in pairs:
        name = name.strip()
        if not name or name.startswith("#"):
            continue
        seen[name] = value.strip()
    return "; ".join(f"{k}={v}" for k, v in seen.items())


def normalize_cookie(text: str) -> str:
    """把用户给的东西尽量猜成一行 Cookie 请求头。

    能吃：``a=1; b=2``、JSON 导出（列表或 ``{"cookies": [...]}``）、
    Netscape ``cookies.txt``、以及包在 ``Cookie:`` 前缀里的行。
    """
    raw = (text or "").strip()
    if not raw:
        return ""

    # 带 "Cookie: " 前缀
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()

    # JSON
    if raw[0] in "[{":
        return _from_json_text(raw)

    pairs: list[tuple[str, str]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") and not line.startswith("#HttpOnly_"):
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        if "\t" in line:                              # Netscape cookies.txt
            parts = line.split("\t")
            if len(parts) >= 7:
                pairs.append((parts[5], parts[6]))
                continue
        for chunk in line.split(";"):
            m = RE_COOKIE_PAIR.match(chunk)
            if m:
                pairs.append((m.group(1), m.group(2)))
    return _clean_pairs(pairs)


def _from_json_text(raw: str) -> str:
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise LoginError(f"这段内容看着像 JSON，但解析不了：{exc}") from exc

    pairs: list[tuple[str, str]] = []
    if isinstance(data, dict) and isinstance(data.get("cookies"), list):
        data = data["cookies"]
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                name = item.get("name") or item.get("key")
                if name:
                    pairs.append((str(name), str(item.get("value", ""))))
            elif isinstance(item, str):
                m = RE_COOKIE_PAIR.match(item)
                if m:
                    pairs.append((m.group(1), m.group(2)))
    elif isinstance(data, dict):
        for name, value in data.items():
            if isinstance(value, (str, int, float)):
                pairs.append((str(name), str(value)))
    if not pairs:
        raise LoginError("JSON 里没找到 name/value 形式的 Cookie")
    return _clean_pairs(pairs)


def from_file(path: str | Path) -> str:
    """从文件读 Cookie。"""
    p = Path(path)
    if not p.is_file():
        raise LoginError(f"找不到文件：{p}")
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise LoginError(f"读不了这个文件：{exc}") from exc
    cookie = normalize_cookie(text)
    if not cookie:
        raise LoginError(f"{p} 里没解析出任何 Cookie")
    return cookie


# --------------------------------------------------------------------------- #
# 二、从浏览器配置档读（要关浏览器）
# --------------------------------------------------------------------------- #

def from_browser(browser: str = "edge", *, profile: str = "", domains=None) -> str:
    """读日常浏览器配置档里的超星 Cookie（浏览器得先关掉）。"""
    try:
        header, hits = browsers.chaoxing_cookie(browser, profile=profile)
    except browsers.BrowserError as exc:
        raise LoginError(str(exc)) from exc
    if not hits:
        raise LoginError(
            f"{browser} 的配置档里没有超星的 Cookie。\n"
            f"      → 先在那个浏览器里打开 {LOGIN_HOME} 登录一次；\n"
            f"      → 或者改用 `--source live`，不用关浏览器。"
        )
    return header


# --------------------------------------------------------------------------- #
# 三、起隔离浏览器，你在里面登录（推荐，不用关自己的浏览器）
# --------------------------------------------------------------------------- #

def _cdp_cookies_to_objects(items: list[dict]) -> list[browsers.Cookie]:
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


def _chaoxing_cookies(items: list[dict]) -> list[browsers.Cookie]:
    hits = []
    for cookie in _cdp_cookies_to_objects(items):
        host = cookie.domain.lstrip(".").lower()
        if any(host == d or host.endswith("." + d) for d in browsers.CHAOXING_DOMAINS):
            hits.append(cookie)
    return hits


def from_live(
    *,
    browser: str = "edge",
    port: int | None = None,
    timeout: float = 300.0,
    profile_root: Path | None = None,
    log=None,
    poll: float = 2.0,
) -> str:
    """起隔离浏览器 → 等你登录 → 读出 Cookie。

    ``timeout`` 秒内没等到就放弃。期间每 ``poll`` 秒问一次浏览器。
    """
    def say(message: str, level: str = "info") -> None:
        if log is None:
            print(message)
        else:
            getattr(log, level, log.info)(message)

    launched = cdp.launch(LOGIN_HOME, browser=browser, port=port, profile_root=profile_root)
    say(f"已用{browser}打开登录页（独立配置档，不影响你原来开着的浏览器）。")
    say(f"配置档位置：{launched.profile_root}")
    say("请在那个窗口里登录超星学习通；登进去之后不用管，我会自动取到会话并关掉这个窗口。")
    say("（这个配置档会留着，所以下次登录不用再登一遍。不想留可以直接删掉上面那个目录。）")
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            time.sleep(poll)
            try:
                items = cdp.cookies_via_cdp(launched.port)
            except cdp.CdpError as exc:
                raise LoginError(f"读 Cookie 失败：{exc}") from exc
            hits = _chaoxing_cookies(items)
            header = browsers.cookie_header(hits, domain="chaoxing.com")
            if not header:
                continue
            # 有 _uid 才算真登录上了，否则可能只是登录页写下的临时 Cookie
            if RE_UID.search(header):
                say("已取到登录会话。")
                launched.stop()
                return header
        raise LoginError(
            f"等了 {int(timeout)} 秒还没等到超星登录会话。\n"
            f"      → 登录窗口还开着的话，确认一下是不是已经登进 i.chaoxing.com；\n"
            f"      → 想多等一会儿就加 `--timeout 600`；\n"
            f"      → 实在不行就走 `--source browser`（先关掉浏览器）或 `--source file`。"
        )
    finally:
        launched.stop()


# --------------------------------------------------------------------------- #
# 四、校验会话真的能用
# --------------------------------------------------------------------------- #

def uid_from_cookie(cookie: str) -> str:
    """从 Cookie 串里取 ``_uid``，取不到返回空串。

    单独抽出来是因为**未验证**那条路也需要它：``--no-check`` 时我们不请求超星，
    拿不到页面里的 uid，但 Cookie 本身通常就带着。少了它，账号名会退化成一模一样
    的「默认」，两个账号在配置里就分不清谁是谁了。
    """
    m = RE_UID.search(cookie or "")
    return m.group(1) if m else ""


def cookie_count(cookie: str) -> int:
    """数一数这条 Cookie 串里有几个 ``name=value``。"""
    return len([p for p in (cookie or "").split(";") if "=" in p])


def cookie_gaps(cookie: str) -> str:
    """检查这条 Cookie 串缺了什么关键项；齐全就返回空串。

    超星的登录态至少要有 ``_uid`` 和 ``_d``。用户从 DevTools 复制时最常见的
    两种残废：只复制了 ``_uid`` 一项，或者只复制了 ``Name`` 那一列（于是整串
    里一个 ``=`` 都没有）。这两种失败看起来和「会话真过期了」一模一样，不指出
    来用户只会反复重试同一个动作。
    """
    parts = [p.strip() for p in (cookie or "").split(";") if p.strip()]
    if not parts:
        return "Cookie 是空的"
    if "=" not in (cookie or ""):
        return (
            f"这 {len(parts)} 段里一个 name=value 都没有——大概率只复制了 DevTools "
            "表格的 Name 列，请点表格上方那一行整串复制"
        )
    missing = []
    if not RE_UID.search(cookie or ""):
        missing.append("_uid")
    if not RE_D.search(cookie or ""):
        missing.append("_d")
    if missing:
        return (
            f"缺少 {'、'.join(missing)}；超星最少需要 _uid 和 _d 两项，"
            f"当前只有 {cookie_count(cookie)} 项"
        )
    return ""


def check(cookie: str, *, timeout: float = 10.0, log=None) -> Session:
    """拿这个 Cookie 去超星问一句「我登录了吗」，确认它真能用。"""
    if not cookie.strip():
        return Session(cookie="", source="", ok=False, message="Cookie 是空的")
    early_gaps = cookie_gaps(cookie)
    if "一个 name=value 都没有" in early_gaps:
        # 这种串不用问服务端就知道没用，省一次请求，也省得用户以为是超星的问题。
        return Session(cookie=cookie, source="", ok=False, message=early_gaps)
    client = HttpClient(cookie=cookie, timeout=timeout)
    try:
        resp = client.get(LOGIN_CHECK, note="登录态自检")
    except (HttpError, ProviderError) as exc:
        return Session(cookie=cookie, source="", ok=False, message=f"连不上超星：{exc}")
    except OSError as exc:
        return Session(cookie=cookie, source="", ok=False, message=f"网络错误：{exc}")

    html = resp.text or ""
    location = resp.headers.get("location", "") or ""
    if resp.status in (301, 302, 303, 307, 308) and ("login" in location or "passport" in location):
        return Session(cookie=cookie, source="", ok=False, message="这个会话已经过期或被登出了")
    if "passport2" in location or "fanyalogin" in location:
        return Session(cookie=cookie, source="", ok=False, message=f"被跳去登录页了（{location}），这个会话不管用")
    evidence = login_page_evidence(html)
    if evidence:
        gaps = cookie_gaps(cookie)
        tail = f"；另外这条 Cookie {gaps}" if gaps else ""
        return Session(
            cookie=cookie,
            source="",
            ok=False,
            message=f"服务端返回的是登录页（命中结构特征 {evidence}），这个会话不管用{tail}",
        )

    uid = uid_from_cookie(cookie)
    if not uid:
        m = RE_UID.search(html)
        if m:
            uid = m.group(1)

    count = cookie_count(cookie)
    if not uid:
        # 「没看到 _uid」不是「会话失效」的证据。超星有些页面把 uid 写在脚本里拼、
        # 或者由前端另外取，正文里搜不到很正常；原先直接判死，等于拿「没找到证据」
        # 当「找到反证」。这里放行，交给真正的抓取去定论——抓取被踢回登录页时
        # 会带诊断信息报 auth_error，那时才是有依据的失败。
        return Session(
            cookie=cookie,
            source="",
            cookie_count=count,
            message="能连上，但页面里没看到 _uid；先按可用处理，抓取时若被踢回登录页会明确报错",
        )
    return Session(cookie=cookie, source="", uid=uid, cookie_count=count, message="会话可用")


# --------------------------------------------------------------------------- #
# 五、存进配置
# --------------------------------------------------------------------------- #

def save(
    workspace,
    session: Session,
    *,
    platform: str = "chaoxing",
    label: str = "",
    make: bool = True,
) -> Path:
    """把会话写进 ``config.json``（同平台同 label 的就地更新，不重复添加）。"""
    path = Path(workspace.config_path)
    cfg = Config()
    if path.is_file():
        try:
            cfg = Config.load(path)
        except Exception:                             # 配置坏了不该拦住登录
            cfg = Config()

    # 标签只在这三处都没给时才自动生成——显式传进来的必须留住，
    # 否则同平台多账号全被命名成「默认（uid）」，用户根本分不清哪个是哪个。
    label = (label or session.label or "").strip()
    if not label:
        label = f"默认（{session.uid}）" if session.uid else "默认"

    for account in cfg.accounts:
        if account.platform == platform and (account.label or "默认") == label:
            account.cookie = session.cookie
            account.enabled = True
            break
    else:
        cfg.accounts.append(
            AccountConfig(platform=platform, label=label, cookie=session.cookie, enabled=True)
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    cfg.save(path, redact=False)
    return path


__all__ = [
    "LOGIN_CHECK",
    "LOGIN_HOME",
    "SOURCES",
    "LoginError",
    "Session",
    "check",
    "from_browser",
    "from_file",
    "from_live",
    "normalize_cookie",
    "save",
]
