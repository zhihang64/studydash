"""任务实现：把前端的 ``kind`` 接到底层已有的登录 / 抓取 / 探测能力上。

这一层刻意不认识 HTTP、也不认识 FastAPI。它只拿 ``(state, reporter, options)``，
所以测试里可以同步调用它，不需要起服务器。

**密码处理**：``login_password`` 的密码只在 ``options`` 里短暂停留，读完立刻从字典里
删掉，不等任务结束。任务快照本来就不含 ``options``（见 ``tasks.Task.to_dict``），
删掉是为了连内存里的那点痕迹也一并清掉。
"""

from __future__ import annotations

import base64
import time
from datetime import timedelta
from typing import Any, Callable, Mapping

from .. import mooclogin
from ..aggregate import aggregate, build_payload
from ..http import HttpTrace
from ..log import Log
from ..login import Session
from ..model import iso, now
from ..passlogin import login as password_login
from ..providers import AccountConfig, build, discover, ids, metas
from ..qrlogin import REFRESH_SECONDS, login as qr_login
from ..store import atomic_write_json
from .errors import ApiError
from .state import AppState
from .tasks import Reporter, Task

__all__ = [
    "KINDS",
    "KIND_LABELS",
    "OPERATIONS",
    "kinds",
    "platform_options",
    "resolve",
]

Runner = Callable[[AppState, Reporter, Mapping[str, Any]], dict[str, Any]]

KINDS: tuple[str, ...] = (
    "login_qr",
    "login_password",
    "login_cookie",
    "login_browser",
    "login_live",
    "fetch",
    "probe",
)

KIND_LABELS: dict[str, str] = {
    "login_qr": "扫码登录",
    "login_password": "账号密码登录",
    "login_cookie": "粘贴 Cookie",
    "login_browser": "读取浏览器配置档",
    "login_live": "打开浏览器登录",
    "fetch": "抓取学业数据",
    "probe": "只读探测平台接口",
}


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #

def _as_float(value: Any, default: float) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    return num if num > 0 else default


def _as_text(value: Any, default: str = "") -> str:
    return str(value).strip() if value is not None else default


def _require_login_support(platform: str) -> None:
    """拒绝没有登录实现的平台，别拿超星的协议冒充它。

    这是一个真实踩过的坑：网页版以前对任何平台都调用超星的扫码 / 密码登录，
    于是选「中国大学 MOOC」弹出来的是学习通的码，用户拿 MOOC App 怎么都扫不上。
    平台的 ``meta.login`` 现在是显式声明，没有声明就老实拒绝。
    """
    meta = next((item for item in metas() if item.id == platform), None)
    if meta is None:
        raise ApiError("invalid_request", f"没有这个平台：{platform}")
    if meta.login:
        return
    raise ApiError(
        "invalid_request",
        f"{meta.name} 还没有网页登录支持",
        hint=(
            "先在它的网页里登录，再用「粘贴 Cookie」（取消勾选验证）导入；"
            "命令行也可以用 `studydash login --platform "
            f"{platform} --source manual --cookie \"…\" --no-check`。"
        ),
    )


def _png_data_url(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


#: 哪些平台没有哪种登录方式，以及为什么、该改用什么。
#:
#: 不写这张表的话，``login_qr`` 会拿超星的扫码协议去问别的平台（选「中国大学
#: MOOC」弹出学习通的码，就是这个问题），而 ``login_password`` 会拿超星表单去
#: 登头歌，报出来的错和「密码错了」一模一样。宁可直说不支持。
_UNSUPPORTED_LOGIN: dict[tuple[str, str], tuple[str, str]] = {
    ("icourse163", "login_password"): (
        "中国大学 MOOC 还不支持账号密码登录",
        "改用「扫码」（中国大学 MOOC App 扫）或「粘贴 Cookie」。",
    ),
    ("educoder", "login_qr"): (
        "头歌没有可复用的扫码登录协议",
        "改用「打开浏览器登录」（独立窗口，登录完自动取会话）或「粘贴 Cookie」。",
    ),
    ("educoder", "login_password"): (
        "头歌的账号密码登录要过验证码，程序没法替你完成",
        "改用「打开浏览器登录」或「粘贴 Cookie」。",
    ),
}


def _reject_login_method(platform: str, kind: str) -> None:
    found = _UNSUPPORTED_LOGIN.get((platform, kind))
    if found:
        raise ApiError("invalid_request", found[0], hint=found[1])


def _login_module(platform: str):
    """拿到这个平台的登录模块（``check`` / ``from_browser`` / ``from_live``）。

    三个模块的函数名和签名刻意保持一致，所以这里可以直接按模块分派，不用给
    每个平台写一遍 if。超星在 :mod:`studydash.login`，中国大学 MOOC 在
    :mod:`studydash.mooclogin`，头歌在 :mod:`studydash.educoder_login`。
    """
    if platform == "icourse163":
        return mooclogin
    if platform == "educoder":
        from .. import educoder_login

        return educoder_login
    from .. import login

    return login


def _platform_name(platform: str) -> str:
    discover()
    meta = next((item for item in metas() if item.id == platform), None)
    return meta.name if meta else platform


def _login_home(platform: str) -> str:
    return getattr(_login_module(platform), "LOGIN_HOME", "https://i.chaoxing.com/base")


def _task_log(rep: Reporter) -> Log:
    """把底层代码的日志搬进任务日志。

    ``level="debug"`` 是故意的：底层那些 ``· `` 开头的调试行在这里是**有用**的
    进度信息（用户能看见「正在读课程列表」），而任务日志本身有 200 条上限兜底。
    """
    return Log(level="debug", stream=rep.stream)


def _account_result(
    platform: str,
    label: str,
    *,
    uid: str = "",
    cookie_count: int = 0,
    ok: bool = True,
    message: str = "",
) -> dict[str, Any]:
    return {
        "platform": platform,
        "label": label,
        "uid": uid,
        "cookie_count": cookie_count,
        "ok": ok,
        "message": message,
    }


def _after_login(
    state: AppState,
    rep: Reporter,
    session: Session,
    *,
    platform: str,
    label: str,
    strict: bool = True,
) -> dict[str, Any]:
    """登录成功后的共同收尾：写进配置，回读确认，返回账号摘要。

    ``strict`` 决定「自检说不行」时要不要把整个登录判死。
    粘贴 Cookie / 读浏览器这两条路，Cookie 是用户自己给的，自检就是唯一的把关，
    所以必须严格。扫码和密码登录不一样：**服务端已经明确认证过我们**（扫码返回
    ``status: true`` 并下发会话 Cookie，或 ``/fanyalogin`` 返回成功），自检只是一个
    间接的旁证。旁证错了就丢掉一个真正可用的会话——这个坑已经踩过一次
    （自检把带 ``passport2`` 的页面全判成登录页，四条登录路径一起报错）。
    所以这两条路改成「存下来 + 标警告」，由后续抓取去定论：抓取被踢回登录页时会
    明确报 ``auth_error``，那时才是有依据的失败。
    """
    suspect = not session.ok
    if suspect and strict:
        raise ApiError("auth_error", session.message or "这个会话不管用")
    if suspect:
        rep.log(
            "warn",
            f"服务端已确认登录，但自检有疑问（{session.message}）；先保存，抓取时再定论",
        )
    rep.progress(0.92, "会话可用，写进配置")
    from ..login import save as save_session

    save_session(state.workspace, session, platform=platform, label=label)
    # 回读而不是拼返回值：label 可能是 login.save 自动生成的（默认（uid）），
    # 只有回读才拿得到真正落盘的那个名字。
    cfg = state.config()
    saved = next((a for a in cfg.accounts if a.cookie == session.cookie), None)
    if saved is None:                                   # pragma: no cover - 逻辑兜底
        raise ApiError("internal", "会话写进配置后没能回读出来")
    rep.log("ok", f"已保存账号「{saved.label or '默认'}」")
    return {
        "account": _account_result(
            saved.platform,
            saved.label or "默认",
            uid=session.uid,
            cookie_count=session.cookie_count,
            ok=not suspect,
            message=session.message,
        )
    }


# --------------------------------------------------------------------------- #
# 登录类
# --------------------------------------------------------------------------- #

def op_login_qr(state: AppState, rep: Reporter, options: Mapping[str, Any]) -> dict[str, Any]:
    timeout = _as_float(options.get("timeout"), 240.0)
    platform = _as_text(options.get("platform"), "chaoxing") or "chaoxing"
    label = _as_text(options.get("label"))
    _require_login_support(platform)
    _reject_login_method(platform, "login_qr")
    started = time.monotonic()
    is_mooc = platform == "icourse163"
    app_name = "中国大学 MOOC" if is_mooc else "学习通"
    refresh = mooclogin.REFRESH_SECONDS if is_mooc else REFRESH_SECONDS

    def on_code(ticket) -> None:
        # 二维码原地更新：码有两分钟有效期，过期就换一张，前端据此重画同一张卡片。
        rep.artifact("qr_data_url", _png_data_url(ticket.png))
        rep.artifact("qr_expires_at", iso(now() + timedelta(seconds=refresh)))
        rep.progress(0.05, f"二维码已就绪，用{app_name} App 扫它")

    def on_tick(payload: dict[str, Any]) -> None:
        mes = _as_text(payload.get("mes"))
        if mes:
            rep.message(mes)
        span = time.monotonic() - started
        rep.progress(min(0.9, 0.05 + 0.85 * span / max(timeout, 1.0)))

    if is_mooc:
        rep.progress(0.0, "正在向中国大学 MOOC 要一张登录二维码")
        session = mooclogin.login(
            timeout=timeout, log=_task_log(rep), on_code=on_code, on_tick=on_tick
        )
    else:
        rep.progress(0.0, "正在向超星要一张登录二维码")
        session = qr_login(timeout=timeout, log=_task_log(rep), on_code=on_code, on_tick=on_tick)
    return _after_login(
        state, rep, session, platform=platform, label=label, strict=False,
    )


def op_login_password(state: AppState, rep: Reporter, options: Mapping[str, Any]) -> dict[str, Any]:
    username = _as_text(options.get("username"))
    password = str(options.get("password") or "")
    # 读完立刻抹掉，不等任务结束
    try:
        options.pop("password", None)                   # type: ignore[attr-defined]
    except AttributeError:                              # pragma: no cover
        pass
    if not username:
        raise ApiError("invalid_request", "没有填账号", detail={"field": "username"})
    if not password:
        raise ApiError("invalid_request", "没有填密码", detail={"field": "password"})
    platform = _as_text(options.get("platform"), "chaoxing") or "chaoxing"
    label = _as_text(options.get("label"))
    _require_login_support(platform)
    _reject_login_method(platform, "login_password")
    rep.progress(0.1, "正在读取超星登录页的表单参数")
    session = password_login(username, password, timeout=15.0, log=_task_log(rep))
    result = _after_login(
        state, rep, session, platform=platform, label=label, strict=False,
    )
    result["account"]["message"] = session.message or "密码登录成功"
    return result


def op_login_cookie(state: AppState, rep: Reporter, options: Mapping[str, Any]) -> dict[str, Any]:
    from ..login import cookie_count, normalize_cookie, uid_from_cookie

    raw = _as_text(options.get("cookie"))
    if not raw:
        raise ApiError("invalid_request", "没有填 Cookie", detail={"field": "cookie"})
    platform = _as_text(options.get("platform"), "chaoxing") or "chaoxing"
    label = _as_text(options.get("label"))
    verify = bool(options.get("verify", True))

    rep.progress(0.15, "整理 Cookie")
    cookie = normalize_cookie(raw)
    if not cookie:
        raise ApiError("invalid_request", "这段文本里没解析出任何 Cookie", detail={"field": "cookie"})

    module = _login_module(platform)
    if verify:
        _require_login_support(platform)
        rep.progress(0.4, "验证这个 Cookie 还能不能用")
        session = module.check(cookie, timeout=10.0, log=_task_log(rep))
        if not session.ok:
            raise ApiError(
                "auth_error",
                session.message or "这个 Cookie 不管用",
                hint=(
                    f"确认复制的是{_platform_name(platform)}已登录状态下的完整 Cookie 请求头。"
                ),
            )
    else:
        session = Session(
            cookie=cookie,
            source="manual",
            # 超星的 uid 就写在 Cookie 里；其他平台得问了才知道，跳过自检时留空。
            uid=uid_from_cookie(cookie) if platform == "chaoxing" else "",
            cookie_count=cookie_count(cookie),
            message="未验证",
        )
    return _after_login(state, rep, session, platform=platform, label=label)


def op_login_browser(state: AppState, rep: Reporter, options: Mapping[str, Any]) -> dict[str, Any]:
    browser = _as_text(options.get("browser"), "edge") or "edge"
    profile = _as_text(options.get("profile"))
    platform = _as_text(options.get("platform"), "chaoxing") or "chaoxing"
    label = _as_text(options.get("label"))
    _require_login_support(platform)
    module = _login_module(platform)

    rep.progress(0.15, f"读取 {browser} 的 Cookie 库")
    cookie = module.from_browser(browser, profile=profile)
    rep.progress(0.5, "验证会话")
    session = module.check(cookie, timeout=10.0, log=_task_log(rep))
    if not session.ok:
        raise ApiError("auth_error", session.message or "读到的会话不管用")
    return _after_login(state, rep, session, platform=platform, label=label)


def op_login_live(state: AppState, rep: Reporter, options: Mapping[str, Any]) -> dict[str, Any]:
    browser = _as_text(options.get("browser"), "edge") or "edge"
    timeout = _as_float(options.get("timeout"), 300.0)
    platform = _as_text(options.get("platform"), "chaoxing") or "chaoxing"
    label = _as_text(options.get("label"))
    _require_login_support(platform)
    module = _login_module(platform)

    rep.progress(0.05, f"正在用 {browser} 打开登录页（独立配置档）")
    rep.artifact("browser_url", _login_home(platform))
    cookie = module.from_live(browser=browser, timeout=timeout, log=_task_log(rep))
    rep.progress(0.8, "已取到会话，验证一下")
    session = module.check(cookie, timeout=10.0, log=_task_log(rep))
    if not session.ok:
        raise ApiError("auth_error", session.message or "取到的会话不管用")
    return _after_login(state, rep, session, platform=platform, label=label)


# --------------------------------------------------------------------------- #
# 数据类
# --------------------------------------------------------------------------- #

def op_fetch(state: AppState, rep: Reporter, options: Mapping[str, Any]) -> dict[str, Any]:
    only = options.get("only")
    if isinstance(only, str):
        only = [p.strip() for p in only.split(",") if p.strip()]
    elif isinstance(only, (list, tuple)):
        only = [str(p).strip() for p in only if str(p).strip()]
    else:
        only = None

    cfg = state.config()
    rep.progress(0.05, "开始抓取")
    courses, accounts = aggregate(cfg, log=_task_log(rep), only=only)
    rep.progress(0.85, "整理看板数据")
    payload = build_payload(courses, accounts, cfg, mode="live")
    state.workspace.save_progress(payload)

    overview = payload.get("overview") or {}
    problems = [
        {"platform": a.platform, "label": a.label, "message": a.message}
        for a in accounts
        if not a.ok
    ]
    summary = {
        "courses": int(overview.get("courses", 0) or 0),
        "platforms": int(overview.get("platforms", 0) or 0),
        "pending": int(overview.get("pending", 0) or 0),
        "overdue": int(overview.get("overdue", 0) or 0),
        "avg_progress": overview.get("avg_progress", 0),
        "accounts": len(accounts),
    }
    if summary["courses"]:
        rep.log("ok", f"抓到 {summary['courses']} 门课，{summary['pending']} 项待办")
    else:
        rep.log("warn", "没抓到任何课程。可能这个账号确实没在学课程，也可能平台改版了。")
    return {"summary": summary, "problems": problems}


def op_probe(state: AppState, rep: Reporter, options: Mapping[str, Any]) -> dict[str, Any]:
    platform = _as_text(options.get("platform"))
    known = set(ids())
    if not platform:
        raise ApiError("invalid_request", "没有指定平台", detail={"field": "platform", "available": sorted(known)})
    if platform not in known:
        raise ApiError(
            "invalid_request",
            f"未知平台：{platform!r}",
            hint="可用的是：" + ", ".join(sorted(known)),
            detail={"field": "platform", "available": sorted(known)},
        )

    cfg = state.config()
    account = next((a for a in cfg.accounts if a.platform == platform), None)
    if account is None:
        account = AccountConfig(platform=platform)

    rep.progress(0.2, f"按 {platform} 的探查计划逐条请求")
    provider = build(account, cfg.http, log=_task_log(rep))
    traces: list[HttpTrace] = provider.probe()
    rep.progress(0.9, "把原始响应留档")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    archive = state.workspace.raw_dir / f"probe-{platform}-{stamp}.json"
    atomic_write_json(
        archive,
        {
            "platform": platform,
            "at": iso(now()),
            "meta": provider.meta.to_dict(),
            "traces": [t.to_dict() for t in traces],
        },
    )
    return {
        "traces": [t.to_dict() for t in traces],
        "archive": str(archive),
    }


# --------------------------------------------------------------------------- #
# 注册表
# --------------------------------------------------------------------------- #

OPERATIONS: dict[str, Runner] = {
    "login_qr": op_login_qr,
    "login_password": op_login_password,
    "login_cookie": op_login_cookie,
    "login_browser": op_login_browser,
    "login_live": op_login_live,
    "fetch": op_fetch,
    "probe": op_probe,
}


def kinds() -> list[dict[str, str]]:
    """给前端填下拉框用：kind + 中文名 + 是否要账号信息。"""
    discover()
    return [{"kind": k, "label": KIND_LABELS.get(k, k)} for k in KINDS]


def resolve(kind: str) -> Runner:
    runner = OPERATIONS.get(kind)
    if runner is None:
        raise ApiError(
            "invalid_request",
            f"不认识的任务类型：{kind!r}",
            hint="可用的是：" + ", ".join(KINDS),
            detail={"field": "kind", "available": list(KINDS)},
        )
    return runner


def platform_options() -> list[dict[str, Any]]:
    discover()
    return [m.to_dict() for m in metas()]
