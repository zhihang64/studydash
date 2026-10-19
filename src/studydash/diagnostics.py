"""环境自检。

CLI 的 ``studydash doctor`` 与 Web 的 ``GET /api/doctor`` 共用这一份，避免两边
各自长出一套「什么算健康」的判断——那种分叉迟早会让命令行说没问题、网页说有问题。

三档 ``ok`` / ``warn`` / ``fail``，**只有 fail 才算故障**。「还没建 config.json」
这类是全新检出的正常状态：如果因此报错，doctor 就没法当 CI 门禁，用户第一次跑
也会以为装坏了。
"""

from __future__ import annotations

import sys
from typing import Any, Callable

from .providers import discover, metas
from .store import Config, Workspace

__all__ = ["LEVEL_MARKS", "checks", "failed_names"]

LEVEL_MARKS = {"ok": "✓", "warn": "!", "fail": "×"}


def checks(
    workspace: Workspace,
    *,
    config: Config | None = None,
    readonly_scan: Callable[[], list[str]] | None = None,
) -> list[dict[str, Any]]:
    """跑一遍自检，返回检查项列表。

    :param config: 已加载的配置；不给且 ``config.json`` 存在时自己加载。
    :param readonly_scan: 只读约束扫描函数，默认 ``studydash.http.assert_readonly``。
        开放它是为了测试能注入结果，而不必真的往源码目录里写一个违规文件。
    """
    ws = workspace
    out: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str, fix: str = "", *, level: str = "") -> None:
        lvl = level or ("ok" if ok else "fail")
        out.append({"name": name, "ok": lvl != "fail", "level": lvl, "detail": detail, "fix": fix})

    add("Python 版本", sys.version_info >= (3, 10), f"{sys.version.split()[0]}（需要 ≥3.10）")

    if ws.root.exists():
        add("工作区", True, str(ws.root))
    else:
        add("工作区", True, f"{ws.root}（还不存在，首次运行时会自动创建）", level="warn")

    if ws.config_path.exists():
        add("配置文件", True, str(ws.config_path))
    else:
        add(
            "配置文件",
            True,
            f"{ws.config_path} 还没创建",
            "跑 `studydash init` 生成；只看演示数据的话不需要它",
            level="warn",
        )

    if readonly_scan is None:
        from . import http as _http

        readonly_scan = _http.assert_readonly
    offenders = readonly_scan()
    add(
        "只读约束",
        not offenders,
        "没有模块绕开 http 层" if not offenders else f"这些文件直接用了网络库：{offenders}",
        "只读是这个项目的核心承诺，请改回用 studydash.http",
    )

    try:
        template = ws.find_template()
        add("渲染模板", True, f"{template}（{template.stat().st_size} 字节）")
    except FileNotFoundError as exc:
        add("渲染模板", False, str(exc), "确认 templates/dashboard.html 存在")

    discover()
    loaded = metas()
    add(
        "平台适配器",
        bool(loaded),
        f"已装载 {len(loaded)} 个：" + ", ".join(m.id for m in loaded),
    )
    unverified = [m.name for m in loaded if m.status == "unverified"]
    if unverified:
        add(
            "未验证适配器",
            True,
            "、".join(unverified) + " 尚未用真实账号验证过",
            "取不到数据属正常，跑 `studydash probe <平台>` 留档后提 issue",
            level="warn",
        )

    if ws.config_path.exists():
        cfg = config
        if cfg is None:
            try:
                cfg = Config.load(ws.config_path)
            except Exception:                          # 配置坏了不该让 doctor 崩
                cfg = None
        accounts = cfg.enabled_accounts() if cfg is not None else []
        with_cookie = [a for a in accounts if a.cookie]
        add(
            "账号配置",
            True,
            f"启用 {len(accounts)} 个，其中 {len(with_cookie)} 个填了 Cookie",
            "在网页上扫码登录，或编辑配置文件的 cookie 字段" if not with_cookie else "",
            level="ok" if with_cookie else "warn",
        )

    web_ok, web_detail, web_fix = _web_check()
    # 网页版依赖是可选的：缺了就只是「用不了网页版」，不是故障，所以给 warn
    add("Web 界面依赖", web_ok, web_detail, web_fix, level="ok" if web_ok else "warn")

    return out


def _web_check() -> tuple[bool, str, str]:
    """``studydash web`` 需要 FastAPI + uvicorn，但它们是可选依赖。"""
    missing = []
    for name in ("fastapi", "uvicorn"):
        try:
            __import__(name)
        except ImportError:
            missing.append(name)
    if missing:
        return (
            False,
            "缺少 " + "、".join(missing) + "，网页版启动不了（命令行版不受影响）",
            '装一下：`pip install "studydash[web]"`',
        )
    return True, "FastAPI 与 uvicorn 都在", ""


def failed_names(items: list[dict[str, Any]]) -> list[str]:
    return [c["name"] for c in items if c.get("level") == "fail"]
