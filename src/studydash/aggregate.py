"""聚合层：把多个平台的抓取结果合成一份给前端看的 payload。

这一层是整个项目「多平台」承诺兑现的地方：Provider 只管把各自平台的脏数据
规整成 :mod:`studydash.model`，聚合层在这里做三件事——

1. **算紧急度**：哪些逾期、哪些三天内截止。跨平台统一按本机时间算。
2. **排优先级**：待办列表按「逾期 → 快截止 → 作业量」排，而不是按平台分组。
   跨平台看板的价值就是让你先做最该做的那一份，而不是先做完超星的再看慕课。
3. **如实报告失败**：某个平台 Cookie 过期了、改版了，都要在 ``meta.warnings``
   和 ``platforms[].message`` 里写清楚。静默返回空列表是最糟的选择——
   用户会以为自己这学期没课。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping

from . import __version__
from .log import Log
from .model import (
    Account,
    Course,
    classify_all,
    iso,
    now,
    pct,
    sort_assignments,
)
from .providers import (
    AccountConfig,
    AuthError,
    HttpSettings,
    Provider,
    ProviderError,
    build,
    discover,
    metas,
)
from .store import Config

__all__ = ["aggregate", "build_payload", "demo_courses", "summary_lines"]

STATUS_ORDER = {"overdue": 0, "urgent": 1, "soon": 2, "later": 3, "none": 4, "done": 5}

#: 平台卡的排序权重。和上面的 STATUS_ORDER 不是一回事：那个排作业状态，
#: 这个排适配器的成熟度。曾经把两件事混用一个表，结果所有平台卡的 status
#: 分量都取到默认值 9，排序里那一段等于没写。
PROVIDER_STATUS_ORDER = {"stable": 0, "beta": 1, "unverified": 2, "broken": 3}


def aggregate(
    config: Config,
    *,
    log: Log | None = None,
    only: Iterable[str] | None = None,
    providers: Mapping[str, Provider] | None = None,
) -> tuple[list[Course], list[Account]]:
    """跑完所有启用的账号，返回 ``(课程列表, 账号结果列表)``。

    单个平台失败不影响其它平台——这是刻意的，多平台看板最不该出现的就是
    「慕课挂了所以你超星的课也看不到了」。
    """
    log = log or Log(quiet=True)
    discover()

    accounts = config.enabled_accounts(only)
    if not accounts:
        log.warn("配置里没有任何启用的账号。先跑 `studydash init` 生成配置。")
        return [], []

    courses: list[Course] = []
    results: list[Account] = []

    for account in accounts:
        meta_name = account.display
        try:
            # providers 是一个测试接缝：注入假适配器就能在不联网的前提下
            # 验证「一个平台挂掉、其它平台照常出数」这条最重要的行为。
            injected = providers.get(account.platform) if providers else None
            provider = injected if injected is not None else build(account, config.http, log=log)
        except ProviderError as exc:
            log.error(f"{meta_name}: {exc}")
            if exc.hint:
                log.warn(f"  {exc.hint}")
            results.append(
                Account(
                    platform=account.platform,
                    label=meta_name,
                    ok=False,
                    message=str(exc),
                    fetched_at=now(),
                )
            )
            continue

        log.step(f"抓取 {provider.meta.name}（{meta_name}）…")
        try:
            fetched = provider.fetch()
        except AuthError as exc:
            results.append(
                Account(
                    platform=account.platform,
                    label=meta_name,
                    ok=False,
                    message=f"登录态失效：{exc}",
                    fetched_at=now(),
                )
            )
            continue
        except ProviderError as exc:
            results.append(
                Account(
                    platform=account.platform,
                    label=meta_name,
                    ok=False,
                    message=str(exc),
                    fetched_at=now(),
                )
            )
            continue
        except Exception as exc:                            # noqa: BLE001 - 适配器是第三方代码
            log.error(f"{meta_name}: 未预期的错误 {type(exc).__name__}: {exc}")
            results.append(
                Account(
                    platform=account.platform,
                    label=meta_name,
                    ok=False,
                    message=f"适配器异常 {type(exc).__name__}: {exc}",
                    fetched_at=now(),
                )
            )
            continue

        for course in fetched:
            course.platform = course.platform or account.platform
            course.extra.setdefault("platform_name", provider.meta.name)
        courses.extend(fetched)
        results.append(
            Account(
                platform=account.platform,
                label=meta_name,
                ok=True,
                message="" if fetched else "登录成功，但没有读到课程",
                fetched_at=now(),
                course_count=len(fetched),
            )
        )

    classify_all(courses, alert_days=config.alert_days())
    return courses, results


# --------------------------------------------------------------------------- #
# 演示数据
# --------------------------------------------------------------------------- #

def demo_courses(
    *,
    alert_days: float = 3.0,
    log: Log | None = None,
) -> tuple[list[Course], list[Account]]:
    """内置演示数据。

    命令行的 ``studydash demo`` 与网页版的「先看看演示数据」共用这一份，
    免得两边的样例各长各的、用户看到的东西对不上。
    """
    account = AccountConfig(platform="demo", label="内置样例")
    provider = build(account, HttpSettings(), log=log or Log(quiet=True))
    courses = provider.fetch()
    classify_all(courses, alert_days=alert_days)
    accounts = [
        Account(
            platform="demo",
            label="内置样例",
            ok=True,
            message="",
            fetched_at=now(),
            course_count=len(courses),
        )
    ]
    return courses, accounts


# --------------------------------------------------------------------------- #
# payload
# --------------------------------------------------------------------------- #

def build_payload(
    courses: list[Course],
    accounts: list[Account],
    config: Config,
    *,
    mode: str = "live",
    extra_warnings: Iterable[str] = (),
) -> dict[str, Any]:
    """把课程与账号结果组装成前端契约 payload（见 docs/payload.example.json）。"""
    ref = now()

    # 兜底盖平台名。aggregate() 已经盖过一次，但 demo / 手动拼数据 / 第三方
    # 调用方可能没盖，payload 里出现 platform_name="chaoxing" 这种生 id 很难看。
    known_names = {m.id: m.name for m in metas()}
    for course in courses:
        display = course.extra.get("platform_name") or known_names.get(
            course.platform, course.platform
        )
        course.extra["platform_name"] = display
        for a in course.assignments:
            if not a.platform_name or a.platform_name == a.platform:
                a.platform_name = display

    # 算 state 必须在这里做，不能只依赖 aggregate()。build_payload 是公开缝：
    # demo、import --init 之后的渲染、第三方调用方都会直接调它。漏掉这一步的
    # 后果是 timeline 里每条作业的 state 都停在默认的 "none"，于是看板上
    # 「已逾期 0 · 三天内 0」，所有预警静默失效——而页面看起来完全正常。
    classify_all(courses, alert_days=config.alert_days(), ref=ref)

    timeline = sort_assignments([a for c in courses for a in c.assignments])

    warnings: list[str] = list(extra_warnings)
    for meta in metas():
        used = any(c.platform == meta.id for c in courses)
        if used and meta.status == "unverified":
            warnings.append(f"{meta.name}: 适配器未验证，字段可能与实际不符，请核对后再采信")
        elif used and meta.status == "broken":
            warnings.append(f"{meta.name}: 适配器标记为已失效，数据仅供参考")
    for account in accounts:
        if not account.ok:
            warnings.append(f"{account.label}: {account.message}")
    if not courses:
        warnings.append(
            "没有读到任何课程。检查配置文件里的 Cookie 是否填了、是否已过期，"
            "或先跑 `studydash demo` 看看看板长什么样。"
        )

    pending = sum(1 for a in timeline if a.state in {"overdue", "urgent", "soon", "later", "none"})
    overdue = sum(1 for a in timeline if a.state == "overdue")
    due_soon = sum(1 for a in timeline if a.state == "urgent")
    done = sum(1 for a in timeline if a.state == "done")
    ratios = [c.ratio for c in courses if c.progress or c.assignments]

    overview = {
        "courses": len(courses),
        "platforms": len({c.platform for c in courses}),
        "avg_progress": round(sum(ratios) / len(ratios), 4) if ratios else 0.0,
        # 一门课都没进度数据时，avg_progress 的 0.0 不是「平均 0%」而是「没得平均」
        "has_progress": bool(ratios),
        "pending": pending,
        "overdue": overdue,
        "due_soon": due_soon,
        "done": done,
        "alert_days": config.alert_days(),
    }

    return {
        "meta": {
            "version": __version__,
            "generated_at": iso(ref),
            "mode": mode,
            "readonly": True,
            "theme": config.theme(),
            "warnings": _dedupe(warnings),
        },
        "overview": overview,
        "platforms": _platform_cards(courses, accounts),
        "timeline": [a.to_dict(ref=ref) for a in timeline],
        "courses": [_course_card(c, ref) for c in _sort_courses(courses)],
        "accounts": [a.to_dict() for a in accounts],
    }


def _sort_courses(courses: list[Course]) -> list[Course]:
    """先看有麻烦的课：逾期多的、快截止的排前面，其余按完成度升序。"""
    def key(course: Course):
        overdue = sum(1 for a in course.assignments if a.state == "overdue")
        urgent = sum(1 for a in course.assignments if a.state == "urgent")
        soon = sum(1 for a in course.assignments if a.state == "soon")
        return (-overdue, -urgent, -soon, course.ratio, course.name)

    return sorted(courses, key=key)


def _course_card(course: Course, ref: datetime) -> dict[str, Any]:
    data = course.to_dict(ref=ref)
    # ``Course.ratio`` 在「没有任何进度和作业」时退化成 0.0，但那门课并不是「进度 0%」，
    # 而是「适配器没取到数据」。两者在界面上必须能分开，否则用户会以为自己一节课没上。
    # overview.avg_progress 早就把这类课排除在外了（见上面的 ratios），这里补上卡片侧。
    data["has_data"] = bool(course.progress or course.assignments)
    # 超星旧版课程页只写「还有 N 个任务点」，不给总数（knowledgeJobCount 就是余量，
    # 不是总量，别拿它当分母）。这种课算不出百分比，但 N 仍然值得展示。
    pending = course.extra.get("task_points_pending")
    if isinstance(pending, int) and pending > 0 and not course.progress:
        data["pending_points"] = pending
    # 每门课单独算一次「当前最扎眼的状态」，前端不用自己推导
    states = [a.state for a in course.assignments]
    if "overdue" in states:
        data["flag"] = "overdue"
    elif "urgent" in states:
        data["flag"] = "urgent"
    elif course.assignments and all(a.state == "done" for a in course.assignments):
        data["flag"] = "done"
    else:
        data["flag"] = "normal"
    return data


def _platform_cards(courses: list[Course], accounts: list[Account]) -> list[dict[str, Any]]:
    known = {m.id: m for m in metas()}
    grouped: dict[str, list[Course]] = {}
    for course in courses:
        grouped.setdefault(course.platform, []).append(course)

    # 抓取失败的平台即使没拿到课也要出现——「这个平台挂了」本身就是重要信息。
    # 但抓取成功却零课程的空平台（典型：demo 账号）只会制造噪声，不建卡。
    for account in accounts:
        if not account.ok:
            grouped.setdefault(account.platform, [])

    cards: list[dict[str, Any]] = []
    for platform, items in grouped.items():
        meta = known.get(platform)
        by_platform = [a for a in accounts if a.platform == platform]
        ok = all(a.ok for a in by_platform) if by_platform else True
        message = next((a.message for a in by_platform if not a.ok), "")
        ratios = [c.ratio for c in items if c.progress or c.assignments]
        cards.append(
            {
                "id": platform,
                "name": meta.name if meta else (items[0].platform_name if items else platform),
                "short": meta.short if meta else platform,
                "status": meta.status if meta else "unknown",
                "status_label": meta.to_dict()["status_label"] if meta else "未装载",
                "color": meta.color if meta else "#94a3b8",
                "homepage": meta.homepage if meta else "",
                "note": meta.note if meta else "",
                "courses": len(items),
                "avg_progress": round(sum(ratios) / len(ratios), 4) if ratios else 0.0,
                # 平台下所有课都没数据时，0.0 不是「平均 0%」而是「无可平均的东西」
                "has_data": bool(ratios),
                "pending": sum(
                    1 for c in items for a in c.assignments
                    if a.state in {"overdue", "urgent", "soon", "later", "none"}
                ),
                "overdue": sum(1 for c in items for a in c.assignments if a.state == "overdue"),
                "ok": ok,
                "message": message,
            }
        )
    cards.sort(
        key=lambda c: (
            not c["ok"],
            PROVIDER_STATUS_ORDER.get(c["status"], 9),
            -c["overdue"],
            c["id"],
        )
    )
    return cards


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


# --------------------------------------------------------------------------- #
# 终端摘要
# --------------------------------------------------------------------------- #

def summary_lines(payload: dict[str, Any]) -> list[str]:
    """CLI 跑完在终端打一段摘要，不用打开浏览器也能知道情况。"""
    meta = payload.get("meta", {})
    overview = payload.get("overview", {})
    ref = now()
    lines: list[str] = []

    lines.append(
        "  共 {courses} 门课 / {platforms} 个平台，平均进度 {avg}".format(
            courses=overview.get("courses", 0),
            platforms=overview.get("platforms", 0),
            avg=pct(overview.get("avg_progress")),
        )
    )
    lines.append(
        "  待办 {pending} 项（逾期 {overdue} · 三天内 {soon}），已完成 {done} 项".format(
            pending=overview.get("pending", 0),
            overdue=overview.get("overdue", 0),
            soon=overview.get("due_soon", 0),
            done=overview.get("done", 0),
        )
    )

    urgent = [
        a for a in payload.get("timeline", [])
        if a.get("state") in {"overdue", "urgent"}
    ][:8]
    if urgent:
        lines.append("")
        lines.append("  最要紧的几件：")
        for item in urgent:
            mark = "!" if item["state"] == "overdue" else "·"
            left = item.get("days_left")
            when = (
                f"逾期 {abs(left):.1f} 天" if isinstance(left, float) and left < 0
                else f"剩 {left:.1f} 天" if isinstance(left, float)
                else "无截止"
            )
            lines.append(
                f"   {mark} [{item.get('platform_name', '')}] {item.get('course', '')}"
                f" · {item.get('title', '')}（{when}）"
            )

    warnings = meta.get("warnings") or []
    if warnings:
        lines.append("")
        lines.append("  注意：")
        for w in warnings:
            lines.append(f"   ! {w}")

    return lines
