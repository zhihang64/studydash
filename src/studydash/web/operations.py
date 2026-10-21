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
from ..aggregate import aggregate, build_payload, sync_payload
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

# 上报最多开几个线程。学习通对同一账号的并发请求不算宽容，超过这个数收益也归零
# （瓶颈变成平台响应时间），所以给个硬上限，不让前端随便填。
MAX_WRITEBACK_WORKERS = 8

KINDS: tuple[str, ...] = (
    "login_qr",
    "login_password",
    "login_cookie",
    "login_browser",
    "login_live",
    "fetch",
    "probe",
    "writeback",
    "quiz",
    "learning",
)

KIND_LABELS: dict[str, str] = {
    "login_qr": "扫码登录",
    "login_password": "账号密码登录",
    "login_cookie": "粘贴 Cookie",
    "login_browser": "读取浏览器配置档",
    "login_live": "打开浏览器登录",
    "fetch": "抓取学业数据",
    "probe": "只读探测平台接口",
    "writeback": "上报视频观看时长",
    "quiz": "章节测验答题",
    "learning": "刷章节学习次数",
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


def _as_kinds(value: Any) -> list[str] | None:
    """把前端传来的任务点种类规整成列表；空/没给返回 None（表示全都要）。

    前端是复选框，可能给数组；命令行/接口调试也可能给 ``"video,read"`` 这样的串。
    """
    if value is None:
        return None
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        items = [str(item) for item in value]
    else:
        return None
    out = [item.strip().lower() for item in items if item.strip()]
    return out or None


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
# 写平台数据（唯一一处）
# --------------------------------------------------------------------------- #

def op_writeback(state: AppState, rep: Reporter, options: Mapping[str, Any]) -> dict[str, Any]:
    """把视频观看时长上报给学习通。

    这是网页端唯一会写平台数据的任务：``options["apply"]`` 不为真时只演练，
    与命令行 ``studydash writeback`` 共用 ``ChaoxingVideoWriter``，
    所以两边的白名单、签名、判定标准完全一致。
    """
    from ..writeback import ChaoxingVideoWriter, WritebackError  # 只在用到时导入

    platform = _as_text(options.get("platform"), "chaoxing") or "chaoxing"
    if platform != "chaoxing":
        raise ApiError(
            "invalid_request",
            f"目前只支持 chaoxing，收到 {platform!r}",
            detail={"field": "platform", "available": ["chaoxing"]},
        )

    apply = bool(options.get("apply"))
    paced = bool(options.get("paced"))
    ramp = bool(options.get("ramp"))
    limit = int(_as_float(options.get("limit"), 0.0))
    interval = _as_float(options.get("interval"), 59.8)
    needle = _as_text(options.get("course")).strip()
    course_id = _as_text(options.get("course_id")).strip()
    workers = int(_as_float(options.get("workers"), 1.0))
    if workers < 1:
        workers = 1
    if workers > MAX_WRITEBACK_WORKERS:
        workers = MAX_WRITEBACK_WORKERS
    kinds = _as_kinds(options.get("kinds"))

    cfg = state.config()
    account = next((a for a in cfg.accounts if a.platform == platform), None)
    if account is None:
        raise ApiError(
            "invalid_request",
            f"配置里没有 {platform} 账号",
            hint="先去「账号」页绑一个，再回来上报。",
        )
    if not account.cookie.strip():
        raise ApiError(
            "auth_error",
            f"{platform} 账号的 Cookie 是空的",
            hint="先去「账号」页扫码登录。",
        )

    if not apply:
        rep.log("warn", "演练模式：不会向平台发出任何上报。勾上「真的上报」才会写。")
    if paced and not apply:
        rep.log("info", "演练模式下逐分钟上报只影响预估次数，不会真的等。")

    rep.progress(0.05, f"读取 {platform} 课程与章节")
    courses, _ = aggregate(cfg, log=_task_log(rep), only=[platform])
    # 课程卡上的那个按钮传 course_id，精确锁死一门课；面板里的模糊搜索走 needle。
    if course_id:
        courses = [c for c in courses if str(c.id) == course_id]
    elif needle:
        courses = [c for c in courses if needle.lower() in (c.name or "").lower()]
    if not courses:
        rep.log("warn", "没有可处理的课程（Cookie 过期或还没选课？）")
        return {"platform": platform, "applied": False, "targets": [], "counts": _empty_counts()}

    def _sleep(seconds: float) -> None:
        # 逐分钟上报一跑就是几十分钟，中间必须能取消。
        rep.check_cancelled()
        time.sleep(seconds)

    rep.progress(0.15, "逐个任务点上报")
    writer = ChaoxingVideoWriter(
        account,
        cfg.http,
        log=_task_log(rep),
        apply=apply,
        sleep=_sleep,
        interval=interval,
        force=bool(options.get("force")),
    )
    if not writer.user_id:
        rep.log("warn", "Cookie 里没有 _uid，签名拼不出来，上报会被平台拒绝")

    def _mission() -> dict[str, dict[str, Any]]:
        """课程页上的「已完成任务点 X/Y」——唯一能证明上报真生效的判据。"""
        out: dict[str, dict[str, Any]] = {}
        for one in courses[:5]:
            got = writer.mission_counts(one)
            if got:
                out[str(one.id)] = {"name": one.name, "done": got[0], "total": got[1]}
        return out

    mission_before = _mission()
    if mission_before:
        line = "；".join(
            f"{v['name']} {v['done']}/{v['total']}" for v in mission_before.values()
        )
        rep.log("info", f"上报前的已完成任务点：{line}")

    # 先清点，才知道进度条的分母；plan() 只读，不会发出上报。
    rep.progress(0.15, "清点任务点")
    todo = writer.plan(courses, limit=limit, kinds=kinds)
    total = len(todo)
    if total:
        rep.log("info", f"共 {total} 个任务点待上报")
    else:
        rep.log("warn", "没找到任何待上报的任务点")
    # 平台那个「已完成任务点 X/Y」把章节测验也算进去了，我们只做六类任务点。
    # 不把这个差说清楚，用户只会看到「报完了还是 0」。
    unsupported = {k: dict(v) for k, v in writer.unsupported.items() if v}
    if unsupported:
        parts: list[str] = []
        for bucket in unsupported.values():
            parts.extend(f"{label} {count} 个" for label, count in bucket.items())
        rep.log(
            "warn",
            "另有 " + "、".join(parts) + " 任务点本工具不做（章节测验要题库、讨论/考试要人工），"
            "平台的任务点总数里包含它们",
        )

    def _on_step(done: int, count: int, target: Any) -> None:
        # 0.15 起步，0.90 收尾，剩下的留给汇总。
        rep.check_cancelled()
        span = (done / count) if count else 1.0
        rep.progress(0.15 + 0.75 * span, f"第 {done}/{count} 个：{target.label}")

    if workers > 1:
        rep.log("info", f"并发上报：{workers} 个线程同时推进")
    try:
        results = writer.run(
            courses,
            limit=limit,
            paced=paced,
            ramp=ramp,
            on_step=_on_step,
            work=todo,
            workers=workers,
            kinds=kinds,
        )
    except WritebackError as exc:
        raise ApiError("provider_error", str(exc), hint=exc.hint) from exc

    counts = {
        "total": len(results),
        "done": sum(1 for r in results if r.get("ok") is True),
        "failed": sum(1 for r in results if r.get("ok") is False),
        "skipped": sum(1 for r in results if r.get("ok") is None),
        "stuck": sum(1 for r in results if r.get("status") == "stuck"),
        "unsupported": sum(sum(b.values()) for b in unsupported.values()),
        "requests": sum(int(r.get("requests") or 0) for r in results),
    }
    rep.progress(0.98, "汇总")
    mission_after = _mission()
    if mission_before and mission_after:
        for key, before in mission_before.items():
            after = mission_after.get(key)
            if not after:
                continue
            gain = after["done"] - before["done"]
            rep.log(
                "ok" if gain > 0 else "info",
                f"{before['name']}：已完成任务点 {before['done']} → {after['done']}"
                f" / {after['total']}（{gain:+d}）"
                + ("" if gain > 0 else "（平台计数滞后几十秒，隔会儿再查）"),
            )
    # 报完了看板还是旧数字，是因为 data/progress.json 只有 fetch 会重写。这里用
    # 刚刚活读到的计数器就地改对：一次额外请求都不用发，看板立刻就是新数字。
    sync: dict[str, Any] | None = None
    if apply and counts["done"] and mission_after:
        rep.progress(0.99, "同步看板数据")
        sync = _sync_dashboard(state, mission_after, platform=platform, rep=rep)

    verb = "已上报" if apply else "演练通过"
    if not results:
        rep.log("warn", "没找到任何待上报的任务点")
        rep.log("info", "可能是：章节页没有任务点节点、任务点已全部完成、或 Cookie 已过期。")
    elif counts["failed"]:
        rep.log("warn", f"{verb} {counts['done']} 个，未通过 {counts['failed']} 个")
        if counts["stuck"]:
            rep.log(
                "warn",
                f"其中 {counts['stuck']} 个的播放位置早就满了却没判通过（平台死态），"
                "再发请求也不会变，只能手动重看或找老师重置",
            )
    else:
        rep.log("ok", f"{verb} {counts['done']} 个任务点，发出 {counts['requests']} 次请求")

    return {
        "platform": platform,
        "applied": apply,
        "paced": paced,
        "ramp": ramp,
        "counts": counts,
        "mission": {"before": mission_before, "after": mission_after},
        "unsupported": unsupported,
        "sync": sync,
        "targets": results[-200:],
    }


def _sync_dashboard(
    state: AppState,
    mission: Mapping[str, Mapping[str, Any]],
    *,
    platform: str,
    rep: Reporter,
) -> dict[str, Any] | None:
    """把上报后的任务点计数写回 ``data/progress.json``，看板不用等下一次抓取。

    只在真的写了平台（``apply``）且至少有一个任务点通过时调用；读不出来、写不进去
    都只记一条日志，绝不让同步失败把「已经上报成功」这件事变成任务失败。
    """
    try:
        payload = state.workspace.load_progress()
    except (OSError, ValueError) as exc:
        rep.log("warn", f"看板数据读不出来，跳过同步：{exc}")
        return None
    if not payload:
        rep.log("info", "还没有看板数据（没抓取过），跳过同步")
        return None
    touched = sync_payload(payload, mission, platform=platform)
    if not touched:
        return None
    try:
        state.workspace.save_progress(payload)
    except OSError as exc:
        rep.log("warn", f"看板数据写不进去，跳过同步：{exc}")
        return None
    overview = payload.get("overview") or {}
    avg = overview.get("avg_progress", 0) or 0
    rep.log(
        "ok",
        f"看板已同步：{touched} 门课的任务点进度按平台读数更新"
        f"（平均 {float(avg) * 100:.1f}%）",
    )
    return {"courses": touched, "avg_progress": avg}


def _chaoxing_account(cfg: Any, platform: str) -> AccountConfig:
    """取学习通账号；缺账号或 Cookie 空了就抛带指引的 ApiError。"""
    if platform != "chaoxing":
        raise ApiError(
            "invalid_request",
            f"目前只支持 chaoxing，收到 {platform!r}",
            detail={"field": "platform", "available": ["chaoxing"]},
        )
    account = next((a for a in cfg.accounts if a.platform == platform), None)
    if account is None:
        raise ApiError(
            "invalid_request",
            f"配置里没有 {platform} 账号",
            hint="先去「账号」页绑一个，再回来。",
        )
    if not account.cookie.strip():
        raise ApiError(
            "auth_error",
            f"{platform} 账号的 Cookie 是空的",
            hint="先去「账号」页扫码登录。",
        )
    return account


def _pick_courses(courses: list[Any], *, course_id: str, needle: str) -> list[Any]:
    """课程卡按钮传 course_id 精确锁课；面板里的搜索框走模糊匹配。"""
    if course_id:
        return [c for c in courses if str(c.id) == course_id]
    if needle:
        return [c for c in courses if needle.lower() in (c.name or "").lower()]
    return courses


def op_quiz(state: AppState, rep: Reporter, options: Mapping[str, Any]) -> dict[str, Any]:
    """把章节测验做掉（题库查答案 → 覆盖率够就交卷）。

    默认演练：``apply`` 不为真时只清点「哪门课几道题、题库能覆盖几道」，
    连交卷接口都不会碰。
    """
    from ..quiz import QuizRunner  # 只在用到时导入

    platform = _as_text(options.get("platform"), "chaoxing") or "chaoxing"
    apply = bool(options.get("apply"))
    limit = int(_as_float(options.get("limit"), 0.0))
    cfg = state.config()
    account = _chaoxing_account(cfg, platform)

    cover_rate = _as_float(options.get("cover_rate"), cfg.tiku.cover_rate)
    tiku = cfg.tiku.client(log=lambda msg: rep.log("debug", msg))
    if tiku is None:
        rep.log(
            "warn",
            "没配题库 token：只能把测验列出来，一道也答不了。去「设置」页填题库 token。",
        )
    if not apply:
        rep.log("warn", "演练模式：不会交卷。勾上「真的提交」才会作答并交卷。")

    rep.progress(0.05, f"读取 {platform} 课程与章节")
    courses, _ = aggregate(cfg, log=_task_log(rep), only=[platform])
    courses = _pick_courses(
        courses,
        course_id=_as_text(options.get("course_id")).strip(),
        needle=_as_text(options.get("course")).strip(),
    )
    if not courses:
        rep.log("warn", "没有可处理的课程（Cookie 过期或还没选课？）")
        return {"platform": platform, "applied": apply, "counts": _empty_counts(), "targets": []}

    runner = QuizRunner(
        account,
        cfg.http,
        log=_task_log(rep),
        apply=apply,
        tiku=tiku,
        cover_rate=cover_rate,
    )

    rep.progress(0.2, "清点章节测验")
    todo = runner.plan(courses, limit=limit)
    total = len(todo)
    if total:
        rep.log("info", f"共 {total} 个章节测验")
    else:
        rep.log("warn", "没找到章节测验")

    results: list[dict[str, Any]] = []
    for index, (_course, target) in enumerate(todo, start=1):
        rep.check_cancelled()
        rep.progress(0.2 + 0.72 * (index - 1) / max(total, 1), f"第 {index}/{total} 份：{target.label}")
        results.append(runner.solve(target))

    counts = {
        "total": len(results),
        "done": sum(1 for r in results if r.get("ok") is True),
        "failed": sum(1 for r in results if r.get("ok") is False),
        "skipped": sum(1 for r in results if r.get("ok") is None),
        "questions": sum(int(r.get("total") or 0) for r in results),
        "answered": sum(int(r.get("answered") or 0) for r in results),
        "requests": sum(int(r.get("requests") or 0) for r in results),
    }
    rep.progress(0.98, "汇总")
    if not results:
        rep.log("warn", "没有可做的章节测验")
    elif not apply:
        rep.log(
            "ok",
            f"演练通过：{counts['total']} 份测验，{counts['questions']} 道题，"
            f"题库覆盖 {counts['answered']} 道",
        )
    elif counts["failed"]:
        rep.log("warn", f"已交卷 {counts['done']} 份，未通过 {counts['failed']} 份")
    else:
        rep.log("ok", f"已交卷 {counts['done']} 份测验，发出 {counts['requests']} 次请求")
    return {
        "platform": platform,
        "applied": apply,
        "cover_rate": cover_rate,
        "tiku": bool(tiku),
        "counts": counts,
        "targets": results[-200:],
    }


def op_learning(state: AppState, rep: Reporter, options: Mapping[str, Any]) -> dict[str, Any]:
    """刷章节学习次数。

    计数由服务端按时间窗口算：访问 → 计次 → 心跳 → 等 30 秒 → 心跳，一轮约 30 秒，
    所以串行、慢、没有捷径。默认演练：只报「几个章节 × 几轮 ≈ 多久」。
    """
    from ..learning import HEARTBEAT_WAIT, LearningCounter

    platform = _as_text(options.get("platform"), "chaoxing") or "chaoxing"
    apply = bool(options.get("apply"))
    limit = int(_as_float(options.get("limit"), 0.0))
    times = max(1, int(_as_float(options.get("times"), 1.0)))
    cfg = state.config()
    account = _chaoxing_account(cfg, platform)
    if not apply:
        rep.log("warn", "演练模式：不会真的访问章节。勾上「真的去刷」才会请求平台。")

    rep.progress(0.05, f"读取 {platform} 课程与章节")
    courses, _ = aggregate(cfg, log=_task_log(rep), only=[platform])
    courses = _pick_courses(
        courses,
        course_id=_as_text(options.get("course_id")).strip(),
        needle=_as_text(options.get("course")).strip(),
    )
    if not courses:
        rep.log("warn", "没有可处理的课程（Cookie 过期或还没选课？）")
        return {"platform": platform, "applied": apply, "counts": _empty_counts(), "targets": []}

    counter = LearningCounter(account, cfg.http, log=_task_log(rep), apply=apply)

    rep.progress(0.2, "清点章节")
    todo = counter.plan(courses, limit=limit)
    if todo:
        estimate = len(todo) * times * HEARTBEAT_WAIT
        rep.log(
            "info",
            f"共 {len(todo)} 个章节，刷 {times} 轮，"
            f"每轮等 {HEARTBEAT_WAIT:.0f} 秒心跳 ⇒ 预计 {estimate / 60:.0f} 分钟",
        )
    else:
        rep.log("warn", "没找到任何章节")

    def _on_step(done: int, count: int, target: Any) -> None:
        rep.check_cancelled()
        rep.progress(0.2 + 0.74 * (done / count if count else 1.0), f"第 {done}/{count} 次：{target.label}")

    results = counter.run(courses, times=times, limit=limit, on_step=_on_step, work=todo)

    counts = {
        "total": len(results),
        "done": sum(1 for r in results if r.get("ok") is True),
        "failed": sum(1 for r in results if r.get("ok") is False),
        "skipped": sum(1 for r in results if r.get("ok") is None),
        "requests": sum(int(r.get("requests") or 0) for r in results),
    }
    rep.progress(0.98, "汇总")
    if not results:
        rep.log("warn", "没有可刷的章节")
    elif not apply:
        rep.log("ok", f"演练通过：{counts['total']} 次访问（{len(todo)} 章节 × {times} 轮）")
    elif counts["failed"]:
        rep.log("warn", f"已刷 {counts['done']} 次，失败 {counts['failed']} 次")
    else:
        rep.log("ok", f"已刷 {counts['done']} 次，发出 {counts['requests']} 次请求")
    return {
        "platform": platform,
        "applied": apply,
        "times": times,
        "counts": counts,
        "targets": results[-200:],
    }


def _empty_counts() -> dict[str, int]:
    return {
        "total": 0,
        "done": 0,
        "failed": 0,
        "skipped": 0,
        "stuck": 0,
        "unsupported": 0,
        "requests": 0,
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
    "writeback": op_writeback,
    "quiz": op_quiz,
    "learning": op_learning,
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
