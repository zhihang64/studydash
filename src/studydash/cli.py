"""命令行界面。

命令一览（``studydash --help``）::

    init        生成配置文件和数据目录
    login       登录一次拿到会话并存进配置（扫码 / 账号密码 / 浏览器 / 文件 / 手粘）
    demo        不联网，用内置样例数据渲一张看板，先看看长什么样
    fetch       按配置抓取所有启用平台的进度，存到 data/progress.json
    build       拿 data/progress.json 渲染出 out/dashboard.html
    all         fetch + build + 打开浏览器
    open        打开已经生成的看板
    serve       在本机起一个 HTTP 服务（手机想在同一 WiFi 下看时用）
    import      手动导入 JSON/CSV（没有公开接口的平台走这条）
    probe       抓取并留档某平台的原始响应，用来排查改版
    platforms   列出已装载的平台适配器及其验证状态
    config      查看/校验配置（输出已脱敏）
    doctor      环境自检
    version     版本号

约定：任何命令都不写平台侧数据，只读。
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import traceback
import webbrowser
from pathlib import Path
from typing import Any, Sequence

from . import __version__, diagnostics, login, passlogin, qrlogin
from .aggregate import aggregate, build_payload, demo_courses, summary_lines
from .log import Log
from .model import SCHEMA_VERSION, Account, classify_all, iso, now
from .providers import AccountConfig, HttpSettings, ProviderError, build, discover, ids, metas
from .render import render
from .store import CONFIG_NAME, ENV_HOME, Config, Workspace, atomic_write_json, read_json

__all__ = ["main", "build_parser"]

FROZEN = bool(getattr(sys, "frozen", False))
PROG = "studydash"

# 网页版默认端口。双击 exe 与 `studydash web` 用同一个，免得用户记两个数，
# 端口被占时后端会自己往后找（web/app.py 的 _pick_port）。
QUICK_PORT = 8788


# --------------------------------------------------------------------------- #
# 参数解析
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="多平台学习进度聚合看板（只读）",
        epilog=(
            "例：studydash demo          先看看效果（不联网）\n"
            "    studydash init          生成配置，填好 Cookie\n"
            "    studydash all           抓取 + 生成 + 打开\n"
            "文档：https://github.com/zhihang64/studydash"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--home",
        metavar="DIR",
        help=f"工作区目录（默认取 ${ENV_HOME}，再默认当前目录）",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试信息")
    parser.add_argument("-q", "--quiet", action="store_true", help="只输出错误")
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")

    # 子命令自己的 -v/-q 必须能覆盖全局值，所以用 SUPPRESS 作为默认值，
    # 否则子命令解析出的 False 会把上面已经解析好的 True 覆盖掉。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS)
    common.add_argument("-q", "--quiet", action="store_true", default=argparse.SUPPRESS)
    # --home 也放进子命令，免得习惯性写成 `studydash demo --home X` 的人撞上
    # "unrecognized arguments"。argparse 不会自动继承全局可选参数。
    common.add_argument(
        "--home", metavar="DIR", default=argparse.SUPPRESS, help=argparse.SUPPRESS
    )

    subs = parser.add_subparsers(dest="cmd", metavar="<命令>")

    p = subs.add_parser("init", parents=[common], help="生成配置文件和数据目录")
    p.add_argument("--force", action="store_true", help="覆盖已存在的配置")
    p.add_argument("--cookie", default="", help="顺手写入某个平台的 Cookie")
    p.add_argument("--platform", default="chaoxing", help="配合 --cookie 使用")
    p.set_defaults(func=cmd_init)

    p = subs.add_parser(
        "login",
        parents=[common],
        help="登录一次取得会话并存进配置（默认扫码，不用关你原来的浏览器）",
    )
    p.add_argument(
        "--source",
        choices=login.SOURCES,
        default="qr",
        help="qr=终端出二维码用手机扫（默认）；password=输账号密码；"
             "live=起独立浏览器你来登；browser=读日常浏览器配置档；file=读导出文件；manual=直接粘 Cookie",
    )
    p.add_argument("--browser", default="edge", help="live/browser 用哪个浏览器（edge/chrome/brave/firefox）")
    p.add_argument("--profile", default="", help="browser 源：指定配置档名，默认取第一个")
    p.add_argument("--file", dest="cookie_file", metavar="FILE", help="file 源：导出的 JSON / cookies.txt")
    p.add_argument("--cookie", default="", help="manual 源：Cookie 请求头")
    p.add_argument("--user", default="", help="password 源：账号，不填就现场问")
    p.add_argument(
        "--password-stdin",
        action="store_true",
        help="password 源：从标准输入读一行当密码（给脚本用；不要在命令行里传密码）",
    )
    p.add_argument("--platform", default="chaoxing", help="存到哪个平台名下（默认 chaoxing）")
    p.add_argument("--label", default="", help="账号标签，同平台多账号时用来区分")
    p.add_argument("--port", type=int, default=0, help="live 源：调试端口，默认自动挑一个空闲的")
    p.add_argument("--timeout", type=float, default=0.0,
                   help="最多等多少秒（qr 默认 240，live 默认 300）")
    p.add_argument("--no-check", action="store_true", help="存之前不验证会话是否真能用")
    p.add_argument("--no-save", action="store_true", help="只取会话，不写进配置")
    p.add_argument("--no-fetch", action="store_true", help="拿到会话就停，不接着抓取")
    p.add_argument("--open", action="store_true", help="抓完后直接打开看板")
    p.set_defaults(func=cmd_login)

    p = subs.add_parser("demo", parents=[common], help="用内置样例数据生成看板（不联网）")
    p.add_argument("--open", action="store_true", help="生成后打开浏览器")
    p.add_argument("--save", action="store_true", help="同时写入 data/progress.json")
    p.add_argument("--json", action="store_true", help="把 payload 打到标准输出")
    p.set_defaults(func=cmd_demo)

    p = subs.add_parser("fetch", parents=[common], help="抓取所有启用平台的进度")
    p.add_argument("--only", default="", help="只抓某些平台，逗号分隔，如 chaoxing,manual")
    p.add_argument("--demo", action="store_true", help="用样例数据代替真实抓取")
    p.add_argument("--json", action="store_true", help="把 payload 打到标准输出")
    p.set_defaults(func=cmd_fetch)

    p = subs.add_parser("build", parents=[common], help="用 data/progress.json 渲染看板")
    p.add_argument("--open", action="store_true", help="生成后打开浏览器")
    p.add_argument("--from", dest="source", metavar="FILE", help="改用指定的 payload 文件")
    p.set_defaults(func=cmd_build)

    p = subs.add_parser("all", parents=[common], help="fetch + build + 打开浏览器")
    p.add_argument("--only", default="", help="只抓某些平台，逗号分隔")
    p.add_argument("--demo", action="store_true", help="用样例数据")
    p.add_argument("--no-open", action="store_true", help="不打开浏览器")
    p.set_defaults(func=cmd_all)

    p = subs.add_parser("open", parents=[common], help="打开已经生成的看板")
    p.set_defaults(func=cmd_open)

    p = subs.add_parser("web", parents=[common], help="在本机起网页版：浏览器里登录、看板、设置")
    p.add_argument("--port", type=int, default=QUICK_PORT, help=f"端口（默认 {QUICK_PORT}，被占用会自动往后找）")
    p.add_argument("--host", default="127.0.0.1", help="监听地址；只接受回环地址")
    p.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    p.add_argument("--static-dir", default="", help="用指定目录的前端产物（开发时用）")
    p.set_defaults(func=cmd_web)

    p = subs.add_parser("serve", parents=[common], help="在本机起 HTTP 服务看板")
    p.add_argument("--port", type=int, default=8787, help="端口（默认 8787）")
    p.add_argument("--host", default="127.0.0.1", help="监听地址；手机访问用 0.0.0.0")
    p.set_defaults(func=cmd_serve)

    p = subs.add_parser("import", parents=[common], help="手动导入 JSON/CSV 数据")
    p.add_argument("file", nargs="?", metavar="FILE", help="要导入的 .json / .csv")
    p.add_argument("--init", action="store_true", help="生成一份导入模板")
    p.add_argument("--json", action="store_true", help="把 payload 打到标准输出")
    p.add_argument("--open", action="store_true", help="生成后打开浏览器")
    p.set_defaults(func=cmd_import)

    p = subs.add_parser("probe", parents=[common], help="抓取并留档某平台的原始响应")
    p.add_argument("platform", nargs="?", help="平台 id；留空则探测所有已配置的平台")
    p.add_argument("--cookie", default="", help="临时指定 Cookie（会写进留档，注意分享时脱敏）")
    p.add_argument("--max-body", type=int, default=4000, help="每条留档最多保留多少字符响应体")
    p.set_defaults(func=cmd_probe)

    p = subs.add_parser("platforms", parents=[common], help="列出已装载的平台适配器")
    p.add_argument("--json", action="store_true", help="以 JSON 输出")
    p.set_defaults(func=cmd_platforms)

    p = subs.add_parser("config", parents=[common], help="查看或校验配置")
    p.add_argument("--show", action="store_true", help="打印当前配置（已脱敏）")
    p.add_argument("--path", action="store_true", help="只打印配置文件路径")
    p.add_argument("--validate", action="store_true", help="校验配置")
    p.set_defaults(func=cmd_config)

    p = subs.add_parser("doctor", parents=[common], help="环境自检")
    p.add_argument("--json", action="store_true", help="以 JSON 输出")
    p.set_defaults(func=cmd_doctor)

    p = subs.add_parser("version", parents=[common], help="打印版本")
    p.set_defaults(func=cmd_version)

    return parser


# --------------------------------------------------------------------------- #
# 共享上下文
# --------------------------------------------------------------------------- #

class Ctx:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.workspace = Workspace.resolve(getattr(args, "home", None))
        self.log = _make_log(args)
        self._config: Config | None = None

    @property
    def config(self) -> Config:
        if self._config is None:
            try:
                self._config = Config.load(self.workspace.config_path)
            except ValueError as exc:
                self.log.error(str(exc))
                self.log.warn("配置文件坏了。可以先备份再跑 `studydash init --force` 重建。")
                raise SystemExit(2) from exc
        return self._config

    def settings(self, **overrides: Any) -> HttpSettings:
        base = self.config.http
        if overrides:
            data = base.to_dict()
            data.update({k: v for k, v in overrides.items() if v is not None})
            return HttpSettings.from_dict(data)
        return base


def _make_log(args: argparse.Namespace) -> Log:
    if getattr(args, "quiet", False):
        level = "error"
    elif getattr(args, "verbose", False):
        level = "debug"
    else:
        level = "info"
    # --json 时把日志挤到 stderr：stdout 只留 JSON，`studydash demo --json | jq`
    # 才不会被「▸ 使用内置样例数据」那几行顶成非法输入。
    if _flag(args, "json"):
        return Log(level=level, stream=sys.stderr)
    return Log(level=level)


def _plugins_available() -> tuple[bool, bool]:
    """返回 (能否打开浏览器, 是否交互终端)。"""
    interactive = bool(getattr(sys.stdin, "isatty", lambda: False)())
    return True, interactive


# --------------------------------------------------------------------------- #
# 命令实现
# --------------------------------------------------------------------------- #

def cmd_init(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    ws = ctx.workspace.ensure()
    path = ws.config_path

    if path.exists() and not args.force:
        # 退出码要非零：脚本里 `studydash init && studydash all` 如果这里静默
        # 返回 0，后面就会拿着一个空配置一路跑下去。
        ctx.log.warn(f"配置已存在：{path}（要重建加 --force）")
        ctx.log.info("已有的 Cookie 没有被改动。想原地编辑就直接打开这个文件。")
        return 1
    else:
        template = {
            "schema_version": SCHEMA_VERSION,
            "accounts": [
                {
                    "platform": "chaoxing",
                    "label": "超星学习通",
                    "enabled": True,
                    "cookie": args.cookie if args.platform == "chaoxing" else "",
                    "options": {},
                },
                {
                    "platform": "manual",
                    "label": "手动登记",
                    "enabled": True,
                    "cookie": "",
                    "options": {},
                },
            ],
            "http": HttpSettings().to_dict(),
            "ui": {"theme": "auto", "alert_days": 3},
            "keep_raw": True,
        }
        for account in template["accounts"]:
            if args.cookie and account["platform"] == args.platform:
                account["cookie"] = args.cookie
        atomic_write_json(path, template)
        ctx.log.ok(f"已写入配置：{path}")

    _write_manual_template(ctx, quiet=True)
    ctx.log.info("")
    ctx.log.info("接下来：")
    ctx.log.info(f"  1. 打开 {path}，把 cookie 填进对应的账号")
    ctx.log.info("     （浏览器登录后 F12 → Network → 任一请求 → 复制 Cookie 整行）")
    ctx.log.info("  2. `studydash platforms` 看看有哪些平台被装载了")
    ctx.log.info("  3. `studydash all` 抓取并生成看板")
    ctx.log.info("")
    ctx.log.info("想先看效果可以直接 `studydash demo`，那条路不联网。")
    return 0


def _qr_callbacks(log: Log) -> tuple[Any, Any]:
    """给 qrlogin 造两个回调：一张新码、一次轮询结果。

    把它们放在这里而不是写成两个模块级函数，是为了让回调里能直接用 ``log`` 的
    样式（扫码进度该走 info，不该走 error，否则用户以为出错了）。
    """

    shown = {"message": ""}

    def on_code(ticket: Any) -> None:
        print()
        print(ticket.text)
        print()
        print("  用手机上的「学习通」App 扫上面这个码，然后在手机上点确认。")
        print("  二维码大约两分钟会失效，程序会自动换一张，不用重跑命令。")
        print()
        shown["message"] = ""

    def on_tick(payload: dict[str, Any]) -> None:
        # 服务端每次轮询都返回，未扫码时 mes 恒为「未登录」，重复打印只会刷屏；
        # 只在消息变了的时候说一句（例如变成「已扫码，请确认」）。
        message = str(payload.get("mes") or "").strip()
        if message and message != shown["message"]:
            shown["message"] = message
            log.info(f"  · {message}")

    return on_code, on_tick


def _ask_credentials(args: argparse.Namespace) -> tuple[str, str]:
    """问出账号和密码。

    密码只走 ``getpass`` / 标准输入，**绝不从命令行参数读**——命令行参数会进
    shell 历史和进程列表。这里拿到的密码只活在这个函数的返回值里，不落盘、不打日志。
    """
    username = _opt(args, "user").strip()
    if not username:
        try:
            username = input("  超星账号（手机号 / 学号 / 邮箱）：").strip()
        except (EOFError, KeyboardInterrupt):
            raise login.LoginError(
                "没有读到账号。换个交互式终端，或用 --user 指定账号"
            ) from None
    if not username:
        raise login.LoginError("账号不能为空")

    if _flag(args, "password_stdin"):
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        try:
            password = getpass.getpass("  密码（不回显、不保存、不写日志）：")
        except (EOFError, KeyboardInterrupt):
            raise login.LoginError("没有读到密码") from None
        except Exception as exc:  # getpass 在没有 tty 时会炸，给出可用的退路
            raise login.LoginError(
                f"读不了密码（{exc}）。用 --password-stdin 从管道喂，或改用 --source qr 扫码"
            ) from None
    if not password:
        raise login.LoginError("密码不能为空")
    return username, password


def cmd_login(args: argparse.Namespace) -> int:
    """取一次会话：取到 → 验一下 → 存进配置。"""
    ctx = Ctx(args)
    ctx.workspace.ensure()
    source = _opt(args, "source", "qr") or "qr"
    platform = _opt(args, "platform", "chaoxing") or "chaoxing"
    label = _opt(args, "label")

    # qr / password 这两条路自己就会验证会话，所以走过它们之后不必再验一次；
    # 其余四条只拿到 Cookie，验不验由 --no-check 决定。
    session: login.Session | None = None
    cookie = ""
    verified = False

    try:
        if source == "qr":
            on_code, on_tick = _qr_callbacks(ctx.log)
            session = qrlogin.login(
                timeout=float(_opt(args, "timeout") or 0) or 240.0,
                log=ctx.log,
                on_code=on_code,
                on_tick=on_tick,
            )
            cookie, verified = session.cookie, True
        elif source == "password":
            username, password = _ask_credentials(args)
            ctx.log.step(f"用账号密码登录（{username}）")
            session = passlogin.login(username, password, log=ctx.log)
            cookie, verified = session.cookie, True
        elif source == "live":
            ctx.log.step("起一个独立浏览器去登录（不影响你原来开着的浏览器）")
            cookie = login.from_live(
                browser=_opt(args, "browser", "edge") or "edge",
                port=int(_opt(args, "port") or 0) or None,
                timeout=float(_opt(args, "timeout") or 0) or 300.0,
                log=ctx.log,
            )
        elif source == "browser":
            ctx.log.step("读浏览器配置档里的超星 Cookie")
            cookie = login.from_browser(
                _opt(args, "browser", "edge") or "edge",
                profile=_opt(args, "profile"),
            )
        elif source == "file":
            path = _opt(args, "cookie_file")
            if not path:
                ctx.log.error("--source file 需要配 --file <导出的文件>")
                return 2
            ctx.log.step(f"从文件读 Cookie：{path}")
            cookie = login.from_file(path)
        elif source == "manual":
            cookie = login.normalize_cookie(_opt(args, "cookie"))
            if not cookie:
                ctx.log.error("--source manual 需要配 --cookie 'a=1; b=2'")
                return 2
        else:
            ctx.log.error(f"不认识的来源：{source}")
            return 2
    except login.LoginError as exc:
        ctx.log.error(str(exc))
        return 1

    if not cookie:
        ctx.log.error("没取到任何 Cookie。")
        return 1

    if verified and session is not None:
        if session.ok:
            ctx.log.ok(f"会话可用（uid={session.uid or '未知'}，来源：{source}）")
        else:
            # 服务端已经认证过（扫码确认 / 密码通过），自检只是旁证。旁证错了不该
            # 丢掉真能用的会话，所以这里只提醒、不中断，由下面的抓取去定论。
            ctx.log.warn(
                f"服务端已确认登录，但自检有疑问：{session.message}"
                f"（uid={session.uid or '未知'}，来源：{source}）"
            )
            ctx.log.info("→ 先存下来，接着抓一次看看；抓取被踢回登录页会明确报错。")
    else:
        ctx.log.ok(f"取到 {len([p for p in cookie.split(';') if '=' in p])} 条 Cookie（来源：{source}）")

    if verified and session is not None:
        pass
    elif not _flag(args, "no_check"):
        ctx.log.step("验证这个会话在超星上真能用")
        session = login.check(cookie, log=ctx.log)
        if not session.ok:
            ctx.log.error(f"这个会话不管用：{session.message}")
            ctx.log.info("→ 多半是没登录完成，或 Cookie 不全。重跑 `studydash login` 再登一次。")
            return 1
        ctx.log.ok(f"会话可用（uid={session.uid}）")
    else:
        # 没验证也顺手从 Cookie 里抠出 uid：账号名要靠它区分，不然多个账号在配置里
        # 全叫「默认」，用户根本分不清自己在看谁。
        session = login.Session(
            cookie=cookie,
            source=source,
            uid=login.uid_from_cookie(cookie),
            cookie_count=login.cookie_count(cookie),
            message="未验证",
        )

    session.source = source
    session.label = label

    if _flag(args, "no_save"):
        ctx.log.info("按 --no-save，没有写进配置。")
        print(cookie)
        return 0

    path = login.save(ctx.workspace, session, platform=platform, label=label)
    ctx.log.ok(f"已存入配置：{path}")

    if _flag(args, "no_fetch"):
        ctx.log.info("")
        ctx.log.info("按 --no-fetch，没有接着抓取。想抓的时候跑 `studydash all`。")
        return 0

    # 登录只是一半；用户要的是「登完就看到我的学业页面」，所以这里直接接着抓。
    ctx.log.info("")
    ctx.log.step(f"接着抓取「{platform}」的学业数据")
    only = [platform] if platform != "chaoxing" else ["chaoxing", "manual"]
    courses, accounts = aggregate(ctx.config, log=ctx.log, only=only)
    payload = build_payload(courses, accounts, ctx.config, mode="live")
    ctx.workspace.save_progress(payload)

    if not courses:
        ctx.log.warn("没抓到任何课程。可能是这个账号确实没在学课程，也可能是平台改版了。")
        ctx.log.info(f"→ 跑 `studydash probe {platform}` 把原始响应留档下来看看它到底回了什么。")
        return 1

    out = _render_payload(ctx, payload)
    ctx.log.ok(f"看板已生成：{out}")
    _print_summary(ctx, payload)
    for account in accounts:
        if not account.ok and account.message:
            ctx.log.warn(f"{account.platform}: {account.message}")
    if _flag(args, "open"):
        webbrowser.open(out.as_uri())
    else:
        ctx.log.info(f"想直接看就打开：{out}")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    as_json = _flag(args, "json")
    ctx.log.step("使用内置样例数据（不联网）")
    courses, accounts = _collect_demo(ctx)
    payload = build_payload(courses, accounts, ctx.config, mode="demo")
    if _flag(args, "save"):
        ctx.workspace.save_progress(payload)
        ctx.log.ok(f"已保存 {ctx.workspace.progress_path}")
    _emit_payload(ctx, payload, as_json=as_json)
    if not as_json:
        path = _render_payload(ctx, payload)
        ctx.log.ok(f"看板已生成：{path}")
        _print_summary(ctx, payload)
        if _flag(args, "open"):
            webbrowser.open(path.as_uri())
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    only = _split(_opt(args, "only"))
    as_json = _flag(args, "json")

    if _flag(args, "demo"):
        courses, accounts = _collect_demo(ctx)
        mode = "demo"
    else:
        if not ctx.workspace.config_path.exists():
            ctx.log.warn(f"还没配置：{ctx.workspace.config_path}")
            ctx.log.info("先跑 `studydash init`，或者用 `studydash demo` 看看效果。")
            return 1
        courses, accounts = aggregate(ctx.config, log=ctx.log, only=only)
        mode = "live"

    payload = build_payload(courses, accounts, ctx.config, mode=mode)
    ctx.workspace.save_progress(payload)
    if not as_json:
        ctx.log.ok(f"已保存 {ctx.workspace.progress_path}")
        _print_summary(ctx, payload)
    _emit_payload(ctx, payload, as_json=as_json)
    return 0 if courses else 1


def cmd_build(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    source = Path(args.source) if args.source else ctx.workspace.progress_path
    if not source.exists():
        ctx.log.error(f"找不到 {source}")
        ctx.log.info("先跑 `studydash fetch`（或 `studydash demo`）。")
        return 1
    try:
        payload = read_json(source)
    except ValueError as exc:
        ctx.log.error(str(exc))
        return 1
    if not isinstance(payload, dict):
        ctx.log.error(f"{source} 不是一个 payload 对象")
        return 1

    path = _render_payload(ctx, payload)
    ctx.log.ok(f"看板已生成：{path}")
    _print_summary(ctx, payload)
    if args.open:
        webbrowser.open(path.as_uri())
    return 0


def cmd_all(args: argparse.Namespace) -> int:
    # 用 _flag 而不是 args.demo / args.no_open：这两个开关只定义在 `all` 的 parser 上，
    # 一旦有人从别处调它（或给某个子命令少写一个默认值），直接属性访问就是
    # AttributeError。共用 cmd_* 不能假设 args 来自自己的 parser。
    rc = cmd_fetch(args)
    if rc != 0 and not _flag(args, "demo"):
        # 抓取失败不阻断渲染：data/progress.json 里带着警告，看板照出
        ctx = Ctx(args)
        payload = ctx.workspace.load_progress()
        if not payload:
            return rc
    ctx = Ctx(args)
    payload = ctx.workspace.load_progress()
    if not payload:
        ctx.log.error("没有可渲染的数据")
        return 1
    path = _render_payload(ctx, payload)
    ctx.log.ok(f"看板已生成：{path}")
    if not _flag(args, "no_open"):
        webbrowser.open(path.as_uri())
    return 0


def cmd_open(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    path = ctx.workspace.dashboard_path
    if not path.exists():
        ctx.log.error(f"还没生成看板：{path}")
        ctx.log.info("跑 `studydash all` 或 `studydash demo --open`。")
        return 1
    webbrowser.open(path.as_uri())
    ctx.log.ok(f"已在默认浏览器打开：{path}")
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    """起网页版。

    命令行版和网页版共用同一份工作区（config.json / data / out），所以先登录、
    再来网页里看是没问题的，反过来也行。这里不做任何业务，全交给 ``studydash.web``。
    """
    from .web import run as run_web

    ctx = Ctx(args)
    port = int(getattr(args, "port", QUICK_PORT) or QUICK_PORT)
    host = _opt(args, "host", "127.0.0.1") or "127.0.0.1"
    return run_web(
        home=ctx.workspace.root,
        port=port,
        host=host,
        open_browser=not _flag(args, "no_open"),
        static_dir=_opt(args, "static_dir") or None,
        log=ctx.log,
    )


def cmd_serve(args: argparse.Namespace) -> int:
    import functools
    import http.server
    import socketserver

    ctx = Ctx(args)
    directory = ctx.workspace.out_dir
    path = ctx.workspace.dashboard_path
    if not path.exists():
        ctx.log.error(f"还没生成看板：{path}")
        return 1

    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(directory)
    )
    socketserver.TCPServer.allow_reuse_address = True
    try:
        with socketserver.ThreadingTCPServer((args.host, args.port), handler) as httpd:
            url = f"http://{args.host}:{args.port}/dashboard.html"
            ctx.log.ok(f"服务已启动：{url}")
            ctx.log.info("  Ctrl+C 停止。同 WiFi 下手机也能打开（把监听地址换成 0.0.0.0）。")
            httpd.serve_forever()
    except KeyboardInterrupt:
        ctx.log.info("已停止服务")
    except OSError as exc:
        ctx.log.error(f"起不来：{exc}")
        ctx.log.info("端口可能被占用了，换一个：`studydash serve --port 8788`")
        return 1
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    from .providers.manual import DEFAULT_FILE, TEMPLATE, write_template

    ctx = Ctx(args)
    target = ctx.workspace.root / DEFAULT_FILE

    if args.init or not args.file:
        write_template(target)
        ctx.log.ok(f"已生成导入模板：{target}")
        ctx.log.info("")
        ctx.log.info("模板长这样（courses 数组里塞课程和作业）：")
        ctx.log.info(json.dumps(TEMPLATE, ensure_ascii=False, indent=2)[:1200])
        ctx.log.info("")
        ctx.log.info("填好后跑 `studydash all`，这份数据会和其它平台一起出现在看板上。")
        return 0

    source = Path(args.file)
    if not source.is_absolute():
        source = (Path.cwd() / source).resolve()
    if not source.exists():
        ctx.log.error(f"文件不存在：{source}")
        return 1

    # 导入 = 让 manual 适配器去读它，所以直接跑一遍 manual
    account = AccountConfig(
        platform="manual", label=str(source.name), options={"path": str(source)}
    )
    try:
        provider = build(account, ctx.settings(), log=ctx.log)
        courses = provider.fetch()
    except ProviderError as exc:
        ctx.log.error(str(exc))
        if exc.hint:
            ctx.log.info(f"  {exc.hint}")
        return 1

    ctx.log.ok(f"解析出 {len(courses)} 门课 / {sum(len(c.assignments) for c in courses)} 条作业")
    for course in courses:
        ctx.log.info(f"  · [{course.platform_name}] {course.name}（{len(course.assignments)} 条）")

    # 光解析不落盘等于白导。以前这里拿到 courses 打印完就 return 0，
    # 用户以为导进去了，跑 build 却还是旧数据。
    classify_all(courses, alert_days=ctx.config.alert_days())
    accounts = [
        Account(
            platform="manual",
            label=str(source.name),
            ok=True,
            message="",
            fetched_at=now(),
            course_count=len(courses),
        )
    ]
    payload = build_payload(courses, accounts, ctx.config, mode="import")
    ctx.workspace.save_progress(payload)
    ctx.log.ok(f"已保存 {ctx.workspace.progress_path}")

    as_json = _flag(args, "json")
    _emit_payload(ctx, payload, as_json=as_json)
    if not as_json:
        path = _render_payload(ctx, payload)
        ctx.log.ok(f"看板已生成：{path}")
        _print_summary(ctx, payload)
        if _flag(args, "open"):
            webbrowser.open(path.as_uri())
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    ws = ctx.workspace.ensure()
    discover()

    targets: list[AccountConfig] = []
    if args.platform:
        # 点名的平台不存在就直接失败，别装作探过了。之前这里让 build() 抛
        # ProviderError、被吞成一行错误，然后照样写出一份 0 条请求的留档并
        # 返回 0——用户敲错一个 id，看到的是「✓ 已留档」。
        if args.platform not in ids():
            ctx.log.error(f"未知平台：{args.platform!r}")
            ctx.log.info(f"可用的是：{', '.join(ids())}")
            ctx.log.info("跑 `studydash platforms` 看每个平台的状态说明。")
            return 1
        cookie = args.cookie or _cookie_for(ctx, args.platform)
        targets.append(AccountConfig(platform=args.platform, cookie=cookie))
    else:
        targets = ctx.config.enabled_accounts() or [
            AccountConfig(platform=m.id) for m in metas()
        ]
        if args.cookie:
            for account in targets:
                account.cookie = account.cookie or args.cookie

    if not targets:
        ctx.log.error("没有可探测的平台")
        return 1

    report: dict[str, Any] = {"generated_at": None, "probes": []}
    report["generated_at"] = iso(now())

    for account in targets:
        try:
            provider = build(account, ctx.settings(), log=ctx.log)
        except ProviderError as exc:
            ctx.log.error(f"{account.platform}: {exc}")
            continue

        ctx.log.step(f"探测 {provider.meta.name}（{provider.meta.status}）")
        if provider.meta.auth == "cookie" and not account.cookie:
            ctx.log.warn("  没配 Cookie，标记为 auth: cookie 的平台大概率返回登录页")

        try:
            traces = provider.probe()
        except Exception as exc:                            # noqa: BLE001
            ctx.log.error(f"  探测失败：{type(exc).__name__}: {exc}")
            continue

        for trace in traces:
            body = trace.body[: max(0, args.max_body)]
            report["probes"].append(
                {
                    "platform": provider.meta.id,
                    "platform_name": provider.meta.name,
                    "status": provider.meta.status,
                    **trace.to_dict(),
                    "body_preview": body,
                }
            )

    out = ws.raw_dir / f"probe-{now():%Y%m%d-%H%M%S}.json"
    atomic_write_json(out, report)
    ctx.log.ok(f"已留档 {len(report['probes'])} 条请求：{out}")
    ctx.log.info("")
    ctx.log.info("这个文件是给排查改版用的。贴到 issue 之前请先删掉响应里的个人信息")
    ctx.log.info("（姓名、学号、Cookie 回显），只保留字段结构就足够维护者定位问题了。")
    return 0


def cmd_platforms(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    discover()
    items = metas()
    if args.json:
        print(json.dumps([m.to_dict() for m in items], ensure_ascii=False, indent=2))
        return 0

    ctx.log.info("")
    ctx.log.info(f"  {'id':<14}{'名称':<16}{'状态':<8}{'鉴权':<8}最近验证")
    ctx.log.info("  " + "-" * 64)
    for meta in items:
        ctx.log.info(
            f"  {meta.id:<14}{meta.name:<16}{meta.to_dict()['status_label']:<8}"
            f"{meta.auth:<8}{meta.verified or '—'}"
        )
    ctx.log.info("")
    ctx.log.info("  状态含义：已验证 = 有人用真实账号跑通过；未验证 = 照接口形状写的，可能要修")
    ctx.log.info("  某个平台取不到数据？跑 `studydash probe <id>` 留档原始响应。")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    path = ctx.workspace.config_path

    if args.path:
        print(path)
        return 0

    if not path.exists():
        ctx.log.warn(f"配置不存在：{path}（跑 `studydash init` 生成）")
        return 1

    if args.validate:
        problems: list[str] = []
        known = {m.id for m in metas()}
        seen: set[str] = set()
        for account in ctx.config.accounts:
            if account.platform not in known:
                problems.append(f"未知平台 {account.platform!r}（可用：{', '.join(sorted(known))}）")
            if account.platform in seen:
                problems.append(f"平台 {account.platform!r} 重复配置，只会用第一条")
            seen.add(account.platform)
            meta = next((m for m in metas() if m.id == account.platform), None)
            if meta and meta.auth == "cookie" and not account.cookie:
                problems.append(f"{account.display}: 需要 Cookie 但没填")
        if problems:
            for problem in problems:
                ctx.log.warn(problem)
            return 1
        ctx.log.ok(f"配置看起来没问题：{path}")
        ctx.log.info(f"  启用账号 {len(ctx.config.enabled_accounts())} 个")
        return 0

    # 默认就是 --show
    print(json.dumps(ctx.config.redacted(), ensure_ascii=False, indent=2))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """环境自检。

    分三档：``ok`` / ``warn`` / ``fail``，**只有 fail 才让退出码非 0**。
    「还没建 config.json」这类是全新检出的正常状态，不是故障——doctor 如果因此
    报错，就没法拿来当 CI 门禁，用户第一次跑也会以为装坏了。
    """
    ctx = Ctx(args)
    items = diagnostics.checks(ctx.workspace, config=ctx.config)
    failed = diagnostics.failed_names(items)

    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return 1 if failed else 0

    ctx.log.info("")
    for item in items:
        mark = diagnostics.LEVEL_MARKS[item["level"]]
        ctx.log.info(f"  {mark} {item['name']}：{item['detail']}")
        if item["level"] != "ok" and item["fix"]:
            ctx.log.info(f"      → {item['fix']}")
    ctx.log.info("")
    if failed:
        ctx.log.warn(f"有 {len(failed)} 项没通过：" + "、".join(failed))
    return 1 if failed else 0


def cmd_version(args: argparse.Namespace) -> int:
    print(f"{PROG} {__version__}")
    print(f"payload schema v{SCHEMA_VERSION}")
    return 0


def cmd_quick(args: argparse.Namespace) -> int:
    """双击 exe 时的默认行为：把网页版开起来。

    v3 的主界面是网页版，所以双击之后不该是「生成一个静态 HTML 然后用浏览器打开
    一个文件」，而应该是一个真在跑的本地网页应用：能扫码登录、能刷新数据、能管理
    账号。静态看板退成兜底——只在网页版依赖缺失或前端产物没打进来时走，并且明确
    说清为什么退，而不是安静地少一半功能。
    """
    ctx = Ctx(args)
    ctx.log.info(f"{PROG} {__version__}")

    reason = _web_unavailable()
    if reason:
        ctx.log.warn(f"网页版开不起来：{reason}")
        ctx.log.info("退回静态看板（只能看，不能登录、不能刷新）。")
        return _quick_static(args, ctx)

    from .web import run as run_web

    return run_web(
        home=ctx.workspace.root,
        port=QUICK_PORT,
        host="127.0.0.1",
        open_browser=True,
        log=ctx.log,
    )


def _web_unavailable() -> str:
    """网页版能不能开？不能就返回一句人能看懂的原因，能就返回空串。

    刻意做在启动之前：让用户在按下回车前就知道自己会看到什么，而不是先蹦一个
    浏览器窗口再报错。
    """
    try:
        from .web import app as _web_app  # noqa: F401
    except ImportError as exc:
        return f'没装网页版依赖（{exc.name or exc}）—— pip install "studydash[web]"'

    static = Path(__file__).resolve().parent / "web" / "static" / "index.html"
    if not static.exists():
        return "前端产物没打进来 —— 跑 tools\\build_web.ps1，或换完整安装包"

    return ""


def _quick_static(args: argparse.Namespace, ctx: Ctx) -> int:
    """老路子：抓一次数据、生成静态看板、用浏览器打开那个文件。"""
    try:
        if ctx.workspace.config_path.exists():
            courses, accounts = aggregate(ctx.config, log=ctx.log)
            mode = "live"
        else:
            ctx.log.info("还没配置，先用内置样例数据给你看一眼。")
            ctx.log.info("想接自己的账号：`studydash init`，然后填 Cookie。")
            courses, accounts = _collect_demo(ctx)
            mode = "demo"

        payload = build_payload(courses, accounts, ctx.config, mode=mode)
        ctx.workspace.save_progress(payload)
        path = _render_payload(ctx, payload)
        _print_summary(ctx, payload)
        ctx.log.ok(f"看板已生成：{path}")
        webbrowser.open(path.as_uri())
        ctx.log.info("")
        ctx.log.info("浏览器没弹出？直接双击这个文件也能看：")
        ctx.log.info(f"  {path}")
    except Exception as exc:                                # noqa: BLE001
        _report_crash(exc, ctx.workspace)
        return 1
    finally:
        _pause_if_frozen()
    return 0


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #

def _split(value: str) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def _flag(args: argparse.Namespace, name: str, default: bool = False) -> bool:
    """读一个「可能不属于当前子命令」的开关。

    ``cmd_all`` 和 ``cmd_quick`` 会拿着自己那套 parser 解析出来的 args 去调
    ``cmd_fetch``——而 ``all`` 没有 ``--json``。直接用 ``args.json`` 就是
    AttributeError 崩在生成看板的前一步。共用函数一律从这里读。
    """
    return bool(getattr(args, name, default))


def _opt(args: argparse.Namespace, name: str, default: str = "") -> str:
    """同上，读一个字符串选项；顺带把 None 折叠成默认值。"""
    value = getattr(args, name, default)
    return default if value is None else str(value)


def _collect_demo(ctx: Ctx):
    courses, accounts = demo_courses(alert_days=ctx.config.alert_days(), log=ctx.log)
    ctx.log.ok(f"样例数据：{len(courses)} 门课 / {sum(len(c.assignments) for c in courses)} 条作业")
    return courses, accounts


def _render_payload(ctx: Ctx, payload: dict[str, Any]) -> Path:
    template = ctx.workspace.find_template()
    out = ctx.workspace.dashboard_path
    title = str(ctx.config.ui.get("title") or "")
    return render(payload, template, out, title=title)


def _emit_payload(ctx: Ctx, payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def _print_summary(ctx: Ctx, payload: dict[str, Any]) -> None:
    ctx.log.info("")
    for line in summary_lines(payload):
        ctx.log.info(line)
    ctx.log.info("")


def _cookie_for(ctx: Ctx, platform: str) -> str:
    for account in ctx.config.accounts:
        if account.platform == platform:
            return account.cookie
    return ""


def _write_manual_template(ctx: Ctx, *, quiet: bool = False) -> None:
    from .providers.manual import DEFAULT_FILE, write_template

    target = ctx.workspace.root / DEFAULT_FILE
    if target.exists():
        return
    try:
        write_template(target)
        if not quiet:
            ctx.log.ok(f"已写入手动导入模板：{target}")
    except OSError as exc:
        ctx.log.debug(f"写手动模板失败（不影响主流程）：{exc}")


def _report_crash(exc: BaseException, workspace: Workspace) -> None:
    """出错了也要留下痕迹。

    PyInstaller 的窗口版 exe 双击起来没有控制台，出了异常就是一黑框闪过去，
    用户完全不知道发生了什么。所以这里既落盘又弹窗。
    """
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    stamp = f"{PROG} {__version__}\n{python_info()}\n\n{text}"

    log_path = workspace.root / "crash.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n===== {_timestamp()} =====\n{stamp}\n")
    except OSError:
        log_path = Path("crash.log")

    message = (
        f"{PROG} 出错了，没能生成看板。\n\n"
        f"{type(exc).__name__}: {exc}\n\n"
        f"详情已写入：\n{log_path}\n\n"
        "把这个文件贴到 issue 里就能定位问题。"
    )
    if sys.stderr is not None:                              # 窗口版 exe 里它是 None
        print(message, file=sys.stderr)
    _message_box("studydash 出错了", message)


def python_info() -> str:
    return (
        f"python {sys.version.split()[0]} / {sys.platform}"
        + (" / frozen" if FROZEN else " / source")
    )


def _timestamp() -> str:
    from .model import iso, now

    return iso(now()) or ""


def _message_box(title: str, message: str) -> None:
    """Windows 上弹个原生对话框；其它平台静默跳过。

    **只在完全没有控制台时弹**（``sys.stderr is None``，即 PyInstaller
    ``--windowed`` 那种双击场景）——那才真的没有别的地方能看到错误。

    这个门槛是必须的：``MessageBoxW`` 会一直阻塞到有人点确定。早先无条件弹，
    结果 CI / 管道里一旦崩溃就永远挂住（我们自己的 pytest 就被它卡死过一次），
    而不是明确地失败退出。
    """
    if os.name != "nt" or sys.stderr is not None:
        return
    if os.environ.get("STUDYDASH_NO_GUI"):
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)  # type: ignore[attr-defined]
    except Exception:                                       # noqa: BLE001
        pass


def _setup_console() -> None:
    """让中文在 Windows 的任何输出目标上都打得出来。

    Windows 控制台默认用本机 ANSI 代码页（简中是 936/GBK），Python 的 stdout
    跟着它走，于是「✓/·/▸」这类字符要么变问号要么直接抛 UnicodeEncodeError
    ——后者会让程序在黑框里一闪就退，正是「双击没反应」的常见成因之一。

    三种情况都统一对齐到 UTF-8：

    * **连着真控制台**：顺手把输出代码页切成 65001，控制台才认得这些字节；
    * **管道 / 重定向**：consumer 无从协商编码，而本项目自己的 JSON、模板、
      配置全是 UTF-8，所以跟着 UTF-8 走 —— 否则 `studydash doctor --json | jq`
      拿到的 GBK 字节会直接解析失败；
    * **打包运行**：同上，另外 exe 可能被别的程序带着管道启动。

    非 Windows 平台不动：那里本来就是 UTF-8。
    """
    if os.name != "nt":
        return
    try:
        console = bool(getattr(sys.stdout, "isatty", lambda: False)())
        if console:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)  # type: ignore[attr-defined]
        for target in (sys.stdout, sys.stderr):
            if target is not None and hasattr(target, "reconfigure"):
                # UTF-8 的码位是完整的，errors="replace" 只是为了兜住
                # 上游塞进来的代理对之类脏东西，不影响正常输出。
                target.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                       # noqa: BLE001
        # 拿不到控制台就算了：日志层本来就有 errors="replace" 兜底
        pass


def _pause_if_frozen() -> None:
    """exe 双击运行时留住窗口，让人能看完输出。"""
    if not FROZEN:
        return
    try:
        if sys.stdin and sys.stdin.isatty():
            input("\n按回车键关闭…")
    except (EOFError, KeyboardInterrupt, OSError):
        pass


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def main(argv: Sequence[str] | None = None) -> int:
    _setup_console()
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if not getattr(args, "func", None):
        if FROZEN:
            args.func = cmd_quick
        else:
            parser.print_help()
            return 0

    # 全局默认值补齐（子命令用 SUPPRESS，可能没带这两个属性）
    for name in ("verbose", "quiet", "home"):
        if not hasattr(args, name):
            setattr(args, name, parser.get_default(name))

    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130
    except SystemExit:
        raise
    except Exception as exc:                                # noqa: BLE001
        if getattr(args, "quiet", False):
            print(f"{PROG}: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        _report_crash(exc, Workspace.resolve(getattr(args, "home", None)))
        if FROZEN:
            _pause_if_frozen()
        return 1
