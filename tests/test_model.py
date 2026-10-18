"""``studydash.model`` 的边界行为。

这些断言是看板上所有「还剩几天 / 该标红还是标黄」的根据，跑偏了看板就会
系统性地骗人，所以钉得比其它模块细。
"""

from __future__ import annotations

from datetime import timedelta, timezone

import pytest

from studydash.model import (
    Assignment,
    Course,
    Progress,
    classify,
    classify_all,
    days_left,
    iso,
    parse_dt,
    pct,
    sort_assignments,
)

from .conftest import make_assignment, make_course


# --------------------------------------------------------------------------- #
# 日期解析
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "不是日期",
        "2026-13-45",  # 月份非法
        [],
        {},
        object(),
    ],
)
def test_parse_dt_junk_returns_none(raw):
    """坏日期一律降级成 None，绝不能抛异常——一个字段烂掉不该搞崩整次抓取。"""
    assert parse_dt(raw) is None


def test_parse_dt_accepts_common_shapes():
    assert parse_dt("2026-03-14 23:59:00") is not None
    assert parse_dt("2026-03-14T23:59:00") is not None
    assert parse_dt("2026/03/14 23:59") is not None
    assert parse_dt(1773000000).year == 2026  # 秒级时间戳


def test_parse_dt_is_timezone_aware():
    """naive 时间要补上时区，否则和 aware 的 now() 相减会 TypeError。"""
    assert parse_dt("2026-03-14 23:59:00").tzinfo is not None
    assert parse_dt("2026-03-14T23:59:00+08:00").tzinfo is not None


def test_iso_roundtrip():
    dt = parse_dt("2026-03-14 23:59:00")
    assert parse_dt(iso(dt)).replace(microsecond=0) == dt.replace(microsecond=0)
    assert iso(None) is None


# --------------------------------------------------------------------------- #
# 剩余天数
# --------------------------------------------------------------------------- #

def test_days_left_none_and_math(ref):
    assert days_left(None) is None
    assert days_left(ref + timedelta(days=2), ref=ref) == 2.0
    assert days_left(ref - timedelta(hours=12), ref=ref) == -0.5


# --------------------------------------------------------------------------- #
# 状态分类
# --------------------------------------------------------------------------- #

def test_classify_boundaries(ref):
    """阈值是 <=，不是 <。差一点就会把「今天到期的」划到下一档。"""
    alert = 3.0
    cases = [
        (None, "none"),
        (-0.01, "overdue"),
        (-30, "overdue"),
        (0.0, "urgent"),          # 正好现在到期算「即将截止」而不是逾期
        (3.0, "urgent"),          # 正好卡在 alert_days 上
        (3.01, "soon"),
        (7.0, "soon"),            # 正好卡在 7 天上
        (7.01, "later"),
        (365, "later"),
    ]
    for offset, expected in cases:
        due = None if offset is None else ref + timedelta(days=offset)
        a = make_assignment("a", due=due)
        assert classify(a, alert_days=alert, ref=ref) == expected, f"offset={offset}"


def test_classify_alert_days_is_honoured(ref):
    a = make_assignment("a", due=ref + timedelta(days=5))
    assert classify(a, alert_days=3, ref=ref) == "soon"
    assert classify(a, alert_days=7, ref=ref) == "urgent"


@pytest.mark.parametrize("status", ["submitted", "graded"])
def test_finished_beats_due_date(ref, status):
    """交过的作业哪怕截止时间早在上个月，也不该再冒红——否则看板永远一片红。"""
    a = make_assignment("a", status=status, due=ref - timedelta(days=30))
    assert classify(a, ref=ref) == "done"


def test_classify_all_fills_course_and_platform_name(ref):
    course = make_course("c1", name="数据结构", platform="chaoxing")
    course.extra["platform_name"] = "超星学习通"
    a = make_assignment("a", course="", due=ref + timedelta(days=1))
    a.platform_name = ""
    a.course = ""            # 适配器没给课程名，等 classify_all 从课程补
    course.assignments = [a]

    classify_all([course], alert_days=3, ref=ref)

    assert a.state == "urgent"
    assert a.course == "数据结构"
    assert a.platform_name == "超星学习通"


def test_classify_all_does_not_overwrite_explicit_names(ref):
    """适配器明确给了课程名/平台名时要保留，别被课程字段盖掉。"""
    course = make_course("c1", name="课程名", platform="chaoxing")
    a = make_assignment("a", course="适配器给的名字")
    a.platform_name = "自定义平台"
    course.assignments = [a]

    classify_all([course], ref=ref)

    assert a.course == "适配器给的名字"
    assert a.platform_name == "自定义平台"


# --------------------------------------------------------------------------- #
# 进度
# --------------------------------------------------------------------------- #

def test_progress_ratio_clamps_and_guards():
    assert Progress("x", 3, 4).ratio == 0.75
    assert Progress("x", 0, 0).ratio is None       # 没有任务点 ≠ 进度 0
    assert Progress("x", 5, 0).ratio is None
    assert Progress("x", 9, 4).ratio == 1.0        # 平台偶尔给超 100%，夹住
    assert Progress("x", -3, 4).ratio == 0.0


def test_course_ratio_prefers_progress_bars():
    course = make_course("c1", progress=[Progress("a", 1, 2), Progress("b", 3, 4)])
    assert course.ratio == 0.625  # 未加权平均 (0.5 + 0.75) / 2


def test_course_ratio_falls_back_to_assignment_completion():
    """没有进度条的课程（例如线下课）用作业完成率兜底，而不是显示 0%。"""
    course = make_course(
        "c1",
        progress=[],
        assignments=[
            make_assignment("1", status="graded"),
            make_assignment("2", status="submitted"),
            make_assignment("3", status="pending"),
            make_assignment("4", status="pending"),
        ],
    )
    assert course.ratio == 0.5


def test_course_ratio_zero_when_nothing_known():
    assert make_course("c1", progress=[]).ratio == 0.0


def test_pct():
    assert pct(0.6742) == "67.4%"
    assert pct(0.0) == "0.0%"
    assert pct(None) == "—"


# --------------------------------------------------------------------------- #
# 排序 / 汇总
# --------------------------------------------------------------------------- #

def test_sort_assignments_overdue_first_then_by_due(ref):
    items = [
        make_assignment("later", due=ref + timedelta(days=30)),
        make_assignment("done", status="graded", due=ref - timedelta(days=9)),
        make_assignment("nodue", due=None),
        make_assignment("overdue_new", due=ref - timedelta(days=1)),
        make_assignment("overdue_old", due=ref - timedelta(days=8)),
        make_assignment("soon", due=ref + timedelta(days=5)),
    ]
    # classify 由调用方负责，这里手工模拟 aggregate 的顺序
    ref_alert = 3.0
    for a in items:
        a.state = classify(a, alert_days=ref_alert, ref=ref)

    got = [a.id for a in sort_assignments(items)]
    assert got == ["overdue_old", "overdue_new", "soon", "later", "nodue", "done"]


def test_course_count_and_next_due(ref):
    course = make_course("c1")
    for a in course.assignments:
        a.state = classify(a, ref=ref)
    course.assignments = [
        make_assignment("a", title="先到", due=ref + timedelta(days=2)),
        make_assignment("b", title="后到", due=ref + timedelta(days=9)),
        make_assignment("c", title="已交", status="graded", due=ref - timedelta(days=5)),
    ]
    classify_all([course], ref=ref)

    assert course.count("urgent") == 1
    assert course.count("done") == 1
    assert course.next_due(ref=ref) == ref + timedelta(days=2)
    assert course.next_due_title(ref=ref) == "先到"
    assert course.next_due_title(ref=None)  # 默认 ref 也不能炸


def test_next_due_ignores_finished(ref):
    course = make_course("c1")
    course.assignments = [make_assignment("a", status="graded", due=ref + timedelta(days=1))]
    assert course.next_due(ref=ref) is None
    assert course.next_due_title(ref=ref) == ""


def test_platform_name_falls_back_to_id():
    """extra 没盖平台名时退回平台 id —— 交给 aggregate 兜底，但模型层不能崩。"""
    course = Course(id="c1", name="课", platform="chaoxing")
    assert course.platform_name == "chaoxing"
    course.extra["platform_name"] = "超星学习通"
    assert course.platform_name == "超星学习通"


def test_progress_and_course_are_json_serialisable(ref):
    import json

    course = make_course("c1", assignments=[make_assignment("a", due=ref + timedelta(days=1))])
    classify_all([course], ref=ref)
    blob = json.dumps(course.to_dict(ref=ref), ensure_ascii=False)
    assert "2026-03-15" in blob or "2026-03-15T" in blob
