"""``studydash.aggregate``：把多平台抓取结果汇成前端契约。

多平台聚合最容易犯的错是「一个平台挂掉，整张看板变空」。这里的用例主要
就是围着这一条转。
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from studydash.aggregate import aggregate, build_payload, summary_lines
from studydash.model import Progress
from studydash.providers import AccountConfig, AuthError, ProviderError
from studydash.store import Config

from .conftest import FakeProvider, make_assignment, make_course


@pytest.fixture
def two_courses(recent):
    return [
        make_course(
            "c1",
            name="数据结构",
            platform="chaoxing",
            progress=[Progress("任务点", 1, 4)],
            assignments=[
                make_assignment("a1", title="实验二", due=recent - timedelta(days=1)),
                make_assignment("a2", title="第三周", due=recent + timedelta(days=2)),
                make_assignment("a3", title="已交", status="graded"),
            ],
        ),
        make_course(
            "c2",
            name="计算机网络",
            platform="icourse163",
            progress=[Progress("课时", 4, 4)],
            assignments=[],
        ),
    ]


# --------------------------------------------------------------------------- #
# payload 形状
# --------------------------------------------------------------------------- #

def test_payload_has_the_contract_keys(two_courses, config):
    payload = build_payload(two_courses, [], config)
    assert set(payload) == {"meta", "overview", "platforms", "timeline", "courses", "accounts"}
    assert payload["meta"]["readonly"] is True
    assert payload["meta"]["mode"] == "live"
    assert payload["meta"]["version"]


def test_payload_is_json_serialisable(two_courses, config):
    blob = json.dumps(build_payload(two_courses, [], config), ensure_ascii=False)
    assert json.loads(blob)["overview"]["courses"] == 2


def test_overview_counts(two_courses, config):
    overview = build_payload(two_courses, [], config)["overview"]
    assert overview["courses"] == 2
    assert overview["platforms"] == 2
    assert overview["overdue"] == 1
    assert overview["due_soon"] == 1          # alert_days=3，2 天后到期
    assert overview["done"] == 1
    assert overview["pending"] == 2           # 逾期 + 三天内
    assert overview["alert_days"] == 3


def test_timeline_is_sorted_overdue_first(two_courses, config):
    timeline = build_payload(two_courses, [], config)["timeline"]
    assert [a["title"] for a in timeline] == ["实验二", "第三周", "已交"]


def test_courses_sorted_by_urgency(two_courses, config):
    courses = build_payload(two_courses, [], config)["courses"]
    assert [c["name"] for c in courses] == ["数据结构", "计算机网络"]


def test_platform_name_is_backfilled_from_registry(two_courses, config):
    """适配器没盖平台名时，payload 里不该出现 platform_name="chaoxing" 这种生 id。"""
    for course in two_courses:
        course.extra.pop("platform_name", None)
    payload = build_payload(two_courses, [], config)
    assert {c["platform_name"] for c in payload["courses"]} == {"超星学习通", "中国大学 MOOC"}


def test_explicit_platform_name_wins(two_courses, config):
    two_courses[0].extra["platform_name"] = "我自己的叫法"
    payload = build_payload(two_courses, [], config)
    got = {c["name"]: c["platform_name"] for c in payload["courses"]}
    assert got["数据结构"] == "我自己的叫法"


# --------------------------------------------------------------------------- #
# 平台卡片
# --------------------------------------------------------------------------- #

def test_failed_platform_still_gets_a_card(two_courses, config):
    """抓不到课的平台也必须出现在看板上，否则用户以为「我没这门课」。"""
    from studydash.model import Account

    results = [Account(platform="zhihuishu", label="我的智慧树", ok=False, message="登录态失效")]
    cards = {c["id"]: c for c in build_payload(two_courses, results, config)["platforms"]}
    assert cards["zhihuishu"]["ok"] is False
    assert cards["zhihuishu"]["message"] == "登录态失效"
    assert cards["zhihuishu"]["courses"] == 0


def test_healthy_but_empty_platform_is_not_a_card(two_courses, config):
    """抓取成功却零课程的平台不建卡——否则 demo 账号会留一张「0 门课」的空卡。"""
    from studydash.model import Account

    results = [Account(platform="demo", label="离线演示", ok=True, course_count=0)]
    ids = [c["id"] for c in build_payload(two_courses, results, config)["platforms"]]
    assert "demo" not in ids


def test_unverified_platform_raises_a_warning(two_courses, config):
    warnings = build_payload(two_courses, [], config)["meta"]["warnings"]
    assert any("中国大学 MOOC" in w and "未验证" in w for w in warnings)


def test_empty_result_gets_an_actionable_warning(config):
    payload = build_payload([], [], config)
    assert payload["overview"]["courses"] == 0
    assert any("demo" in w for w in payload["meta"]["warnings"])


def test_warnings_are_deduped(config):
    payload = build_payload([], [], config, extra_warnings=["同一个", "同一个"])
    assert payload["meta"]["warnings"].count("同一个") == 1


def test_platform_card_counts_aggregate_over_courses(two_courses, config):
    cards = {c["id"]: c for c in build_payload(two_courses, [], config)["platforms"]}
    assert cards["chaoxing"]["courses"] == 1
    assert cards["chaoxing"]["overdue"] == 1
    assert cards["chaoxing"]["pending"] == 2
    assert cards["icourse163"]["avg_progress"] == 1.0


# --------------------------------------------------------------------------- #
# aggregate() 的容错
# --------------------------------------------------------------------------- #

def test_one_broken_provider_does_not_kill_the_others(config):
    """本文件存在的理由。chaoxing 炸了，icourse163 的数据必须照常出来。"""
    accounts = [
        AccountConfig(platform="chaoxing", label="超星", cookie="x=1"),
        AccountConfig(platform="icourse163", label="慕课", cookie="y=2"),
    ]
    cfg = Config(accounts=accounts)
    courses, results = aggregate(
        cfg,
        providers={
            "chaoxing": FakeProvider(error=ProviderError("接口改版了", hint="去看 docs/probe.md")),
            "icourse163": FakeProvider([make_course("c2", platform="icourse163", name="网络")]),
        },
    )
    assert [c.name for c in courses] == ["网络"]
    by_platform = {r.platform: r for r in results}
    assert by_platform["chaoxing"].ok is False
    assert "接口改版了" in by_platform["chaoxing"].message
    assert by_platform["icourse163"].ok is True
    assert by_platform["icourse163"].course_count == 1


def test_auth_error_is_reported_as_cookie_problem(config):
    cfg = Config(accounts=[AccountConfig(platform="chaoxing", cookie="stale")])
    _, results = aggregate(
        cfg, providers={"chaoxing": FakeProvider(error=AuthError("跳登录页了"))}
    )
    assert results[0].ok is False
    assert "登录态失效" in results[0].message


def test_unexpected_exception_becomes_a_failed_account_not_a_crash(config):
    """适配器里任何没预料到的异常都要被收成「这个平台失败」，绝不能冒到顶层。"""
    cfg = Config(accounts=[AccountConfig(platform="chaoxing", cookie="x"), AccountConfig(platform="manual")])
    courses, results = aggregate(
        cfg,
        providers={
            "chaoxing": FakeProvider(error=ZeroDivisionError("适配器写错了个除法")),
            "manual": FakeProvider([make_course("m1", platform="manual", name="线下课")]),
        },
    )
    assert [c.name for c in courses] == ["线下课"]
    assert {r.platform: r.ok for r in results} == {"chaoxing": False, "manual": True}


def test_only_filter_restricts_platforms():
    cfg = Config(
        accounts=[AccountConfig(platform="chaoxing"), AccountConfig(platform="icourse163")]
    )
    courses, results = aggregate(
        cfg,
        only=["icourse163"],
        providers={"icourse163": FakeProvider([make_course("c", platform="icourse163")])},
    )
    assert [r.platform for r in results] == ["icourse163"]
    assert len(courses) == 1


def test_no_enabled_accounts_is_not_an_error(config):
    courses, results = aggregate(Config(accounts=[]))
    assert courses == [] and results == []


# --------------------------------------------------------------------------- #
# 命令行摘要
# --------------------------------------------------------------------------- #

def test_summary_lines_mention_the_important_stuff(two_courses, config):
    lines = "\n".join(summary_lines(build_payload(two_courses, [], config)))
    assert "2 门课" in lines
    assert "逾期" in lines
    assert "实验二" in lines          # 最要紧的待办会被点名


def test_summary_lines_survive_an_empty_payload(config):
    lines = summary_lines(build_payload([], [], config))
    assert any("0 门课" in line for line in lines)
