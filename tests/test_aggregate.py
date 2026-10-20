"""``studydash.aggregate``：把多平台抓取结果汇成前端契约。

多平台聚合最容易犯的错是「一个平台挂掉，整张看板变空」。这里的用例主要
就是围着这一条转。
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from studydash.aggregate import aggregate, build_payload, course_key, filter_payload, summary_lines
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
    """慕课已验证（beta），所以拿还没验证的智慧树来测这条警告。"""
    courses = [*two_courses, make_course("z1", name="创业基础", platform="zhihuishu")]
    warnings = build_payload(courses, [], config)["meta"]["warnings"]
    assert any("智慧树" in w and "未验证" in w for w in warnings)


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


# --------------------------------------------------------------------------- #
# 「没数据」与「进度 0%」必须分得开
# --------------------------------------------------------------------------- #

def test_course_without_any_data_is_flagged_not_shown_as_zero(config):
    """没进度条也没作业的课程，是「适配器没取到数据」，不是「你一节课没上」。

    ``Course.ratio`` 在这时会退化成 0.0，前端如果照直画就是一条 0% 的空进度条，
    用户会以为自己的学习状态出了问题。所以 payload 里带一个 ``has_data``，
    卡片据此显示「无数据」。
    """
    courses = [
        make_course("c1", name="有数据", platform="chaoxing",
                    progress=[Progress("任务点", 3, 4)]),
        make_course("c2", name="没数据", platform="chaoxing",
                    progress=[], assignments=[]),
    ]
    payload = build_payload(courses, [], config)
    by_id = {c["id"]: c for c in payload["courses"]}

    assert by_id["c1"]["has_data"] is True
    assert by_id["c2"]["has_data"] is False
    # 没数据的课其 ratio 仍是 0.0（模型层行为，前端靠 has_data 才能区分）
    assert by_id["c2"]["progress"] == 0.0
    # 也不能把平均进度拉下来
    assert payload["overview"]["avg_progress"] == pytest.approx(0.75, abs=1e-3)
    assert payload["overview"]["has_progress"] is True


def test_platform_card_without_any_data_says_so(config):
    """平台上所有课都没数据时，不能报「平均 0%」。"""
    courses = [
        make_course("c1", name="有数据", platform="chaoxing",
                    progress=[Progress("任务点", 1, 2)]),
        make_course("c2", name="没数据", platform="manual", progress=[], assignments=[]),
    ]
    payload = build_payload(courses, [], config)
    cards = {p["id"]: p for p in payload["platforms"]}

    assert cards["chaoxing"]["has_data"] is True
    assert cards["chaoxing"]["avg_progress"] == pytest.approx(0.5, abs=1e-3)
    assert cards["manual"]["has_data"] is False
    assert cards["manual"]["avg_progress"] == 0.0


def test_overview_marks_a_total_lack_of_progress_data(config):
    """全部课程都没进度数据时，平均进度是「没得平均」而不是 0%。"""
    courses = [
        make_course("c1", platform="manual", progress=[], assignments=[]),
        make_course("c2", platform="manual", progress=[], assignments=[]),
    ]
    payload = build_payload(courses, [], config)
    assert payload["overview"]["has_progress"] is False
    assert payload["overview"]["avg_progress"] == 0.0
    # 课还是 2 门——有课没进度不等于没课
    assert payload["overview"]["courses"] == 2


def test_course_with_only_assignments_counts_as_having_data(config):
    """只有作业没进度条也算有数据——作业完成率就是它的进度来源。"""
    courses = [
        make_course("c1", platform="chaoxing", progress=[],
                    assignments=[make_assignment("a1", title="论文提纲", status="graded")]),
    ]
    payload = build_payload(courses, [], config)
    assert payload["courses"][0]["has_data"] is True
    assert payload["overview"]["avg_progress"] == pytest.approx(1.0, abs=1e-3)


def test_pending_points_surfaces_when_only_remainder_is_known(config):
    """平台只给「还剩 N 个任务点」时，把 N 透出来，别把整门课压成一句「无数据」。

    这是超星旧版课程页的真实形状：表头没有「已完成任务点 X/Y」，
    每个章节节点上只有余量（``knowledgeJobCount`` 就是余量，不是总量），
    所以算不出百分比——但「还剩 14 个」对用户是有信息量的。
    """
    courses = [
        make_course("c1", name="只给余量", platform="chaoxing", progress=[], assignments=[]),
    ]
    courses[0].extra["task_points_pending"] = 14
    payload = build_payload(courses, [], config)
    card = payload["courses"][0]

    assert card["has_data"] is False      # 仍然算不出百分比
    assert card["pending_points"] == 14   # 但余量要透出来


def test_pending_points_hidden_when_real_progress_exists(config):
    """有百分比的时候不提余量——两个数字并排只会让人以为哪个算错了。"""
    courses = [
        make_course("c1", platform="chaoxing", progress=[Progress("任务点", 3, 4)]),
    ]
    courses[0].extra["task_points_pending"] = 1
    payload = build_payload(courses, [], config)
    assert "pending_points" not in payload["courses"][0]


def test_pending_points_zero_is_not_reported(config):
    """余量为 0（这门课本来就没有任务点）时不要挂个 0 出来冒充信息。"""
    courses = [
        make_course("c1", platform="chaoxing", progress=[], assignments=[]),
    ]
    courses[0].extra["task_points_pending"] = 0
    payload = build_payload(courses, [], config)
    assert "pending_points" not in payload["courses"][0]


def test_chapters_pass_through_to_the_course_card(config):
    """章节明细由适配器放在 ``extra`` 里，聚合层只负责透传，不改形状。"""
    chapters = [
        {"name": "折线图", "unit": "第13章 SPSS操作", "sbar": "13.1.1", "depth": 1, "pending": 3},
    ]
    courses = [
        make_course("c1", platform="chaoxing", progress=[Progress("任务点", 1, 4)]),
    ]
    courses[0].extra["chapters"] = chapters
    payload = build_payload(courses, [], config)
    assert payload["courses"][0]["chapters"] == chapters


def test_empty_chapters_are_not_emitted(config):
    """没有章节数据时别塞一个空数组——前端会以为「有这块但没内容」。"""
    courses = [make_course("c1", platform="chaoxing")]
    payload = build_payload(courses, [], config)
    assert "chapters" not in payload["courses"][0]


# --------------------------------------------------------------------------- #
# 隐藏课程
# --------------------------------------------------------------------------- #

def test_course_key_includes_the_term(config):
    """同一门课的两个学期必须是两个键，否则藏一个学期会把另一个也藏了。"""
    first = make_course("c1", name="高数", platform="icourse163")
    first.extra["term_id"] = "t1"
    second = make_course("c1", name="高数", platform="icourse163")
    second.extra["term_id"] = "t2"

    assert course_key(first) == "icourse163:c1:t1"
    assert course_key(first) != course_key(second)
    # 没有学期信息时也不能崩，只是第三段为空
    assert course_key(make_course("c9", platform="demo")) == "demo:c9:"


def test_filter_payload_moves_the_course_and_its_assignments(two_courses, config):
    payload = build_payload(two_courses, [], config)
    chaoxing = next(c for c in payload["courses"] if c["platform"] == "chaoxing")

    filtered = filter_payload(payload, [chaoxing["key"]])

    assert [c["id"] for c in filtered["courses"]] == ["c2"]
    assert filtered["hidden"][0]["key"] == chaoxing["key"]
    assert filtered["hidden"][0]["pending"] == chaoxing["pending"]
    # 隐藏页不需要作业明细，省得把整份列表再传一遍
    assert "assignments" not in filtered["hidden"][0]
    # 它的作业必须从时间线上消失
    assert all(a["course_id"] != "c1" for a in filtered["timeline"])
    assert filtered["meta"]["hidden_count"] == 1


def test_filter_payload_recounts_overview_instead_of_leaving_stale_numbers(two_courses, config):
    payload = build_payload(two_courses, [], config)
    before = payload["overview"]
    hidden = next(c for c in payload["courses"] if c["platform"] == "chaoxing")

    after = filter_payload(payload, [hidden["key"]])["overview"]

    assert after["courses"] == before["courses"] - 1
    assert after["platforms"] == 1
    assert after["overdue"] == before["overdue"] - hidden["overdue"]
    assert after["done"] == before["done"] - hidden["done"]


def test_filter_payload_drops_a_platform_card_with_nothing_left(two_courses, config):
    payload = build_payload(two_courses, [], config)
    hidden = next(c for c in payload["courses"] if c["platform"] == "chaoxing")

    filtered = filter_payload(payload, [hidden["key"]])

    assert {c["id"] for c in filtered["platforms"]} == {"icourse163"}


def test_filter_payload_does_not_mutate_the_stored_payload(two_courses, config):
    """progress.json 必须保持完整，取消隐藏才能不重新抓取。"""
    payload = build_payload(two_courses, [], config)
    hidden = next(c for c in payload["courses"] if c["platform"] == "chaoxing")

    filter_payload(payload, [hidden["key"]])

    assert len(payload["courses"]) == 2
    assert "hidden" not in payload
    assert "hidden_count" not in payload["meta"]


def test_filter_payload_with_no_hidden_keys_is_a_noop_on_content(two_courses, config):
    payload = build_payload(two_courses, [], config)
    filtered = filter_payload(payload, [])

    assert [c["id"] for c in filtered["courses"]] == [c["id"] for c in payload["courses"]]
    assert filtered["hidden"] == []
    assert filtered["overview"]["courses"] == payload["overview"]["courses"]


def test_filter_payload_tolerates_old_cards_without_a_key(config):
    """升级前生成的 progress.json 没有 key 字段，读的时候要能补齐。"""
    payload = build_payload([make_course("c1", platform="demo")], [], config)
    for card in payload["courses"]:
        card.pop("key", None)
        card.pop("term_id", None)

    filtered = filter_payload(payload, ["demo:c1:"])

    assert filtered["courses"] == []
    assert filtered["hidden"][0]["name"] == payload["courses"][0]["name"]
