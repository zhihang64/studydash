"""中国大学 MOOC 适配器：面板 / 学期详情 / 作业解析。

全部用**脱敏后的真实响应结构**做夹具，不联网。字段名都是从真实账号上
抄下来的（课程名、学校名等换成假数据），改版时这些用例会先炸。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from studydash.providers.base import AccountConfig, AuthError, ProviderError
from studydash.providers.icourse163 import (
    COURSE_RPC,
    TERM_RPC,
    Icourse163Provider,
)

COOKIE = "NTESSTUDYSI=csrf-abc; EDUWEBDEVICE=dev"


def provider(**options) -> Icourse163Provider:
    return Icourse163Provider(
        AccountConfig(platform="icourse163", label="我的慕课", cookie=COOKIE, options=options)
    )


# --------------------------------------------------------------------------- #
# 课程面板
# --------------------------------------------------------------------------- #

def panel_payload() -> dict:
    """真实响应的形状：``result.result`` 里是面板，外面还有一层分页信息。"""
    return {
        "code": 0,
        "message": "",
        "result": {
            "result": [
                {
                    "id": 1000000001,
                    "name": "线性代数",
                    "imgUrl": "https://example.invalid/a.png",
                    "mode": 0,
                    "learnedCount": 93,
                    "productType": 1,
                    "courseType": 1,
                    "schoolPanel": {"id": 1, "name": "示例大学"},
                    "termPanel": {
                        "id": 2000000001,
                        "courseId": 1000000001,
                        "lessonsCount": 96,
                        "startTime": 1756396800000,
                        "endTime": 1766678400000,
                    },
                },
                {
                    "id": 1000000002,
                    "name": "计算机网络",
                    "imgUrl": "https://example.invalid/b.png",
                    "mode": 20,
                    "learnedCount": 37,
                    "schoolPanel": {"name": "示例大学"},
                    "termPanel": {
                        "id": 2000000002,
                        "courseId": 1000000002,
                        "lessonsCount": 37,
                        "startTime": 1774368000000,
                        "endTime": 1785511800000,
                    },
                },
            ],
            "pagination": {"totleCount": 2},
        },
    }


def test_parse_courses_reads_the_nested_result_list():
    courses = provider().parse_courses(panel_payload())

    assert [c.name for c in courses] == ["线性代数", "计算机网络"]
    first, second = courses
    assert first.id == "1000000001"
    assert first.extra["term_id"] == "2000000001"
    assert first.teacher == "示例大学"
    assert first.url == "https://www.icourse163.org/learn/1000000001?tid=2000000001"
    # 进度就是页面上那句「已学 93/96 小节」
    assert [p.to_dict() for p in first.progress] == [
        {"label": "已学小节", "done": 93, "total": 96, "ratio": 0.9688}
    ]
    # SPOC 与 MOOC 用 mode 区分（20 = SPOC）
    assert second.extra["mode"] == "spoc"
    assert first.extra["mode"] == "mooc"


def test_same_course_in_two_terms_is_kept_twice():
    """同一门课可以修两个学期，按 courseId 去重会把后一个学期吞掉。"""
    payload = panel_payload()
    items = payload["result"]["result"]
    clone = {**items[0], "termPanel": {**items[0]["termPanel"], "id": 2000000003}}
    items.append(clone)

    courses = provider().parse_courses(payload)

    assert len(courses) == 3
    assert {c.extra["term_id"] for c in courses if c.id == "1000000001"} == {
        "2000000001",
        "2000000003",
    }


def test_parse_courses_falls_back_to_name_search_when_wrapping_changes():
    """接口多包一层时不该直接空手而归。"""
    payload = {"data": {"coursePanelList": [{"id": 1, "name": "离散数学"}]}}
    assert [c.name for c in provider().parse_courses(payload)] == ["离散数学"]


def test_course_without_a_name_is_dropped():
    payload = {"result": {"result": [{"id": 1}, {"id": 2, "name": "  "}]}}
    assert provider().parse_courses(payload) == []


# --------------------------------------------------------------------------- #
# 学期详情 / 作业
# --------------------------------------------------------------------------- #

def term_payload() -> dict:
    return {
        "code": 0,
        "result": {
            "id": "2000000001",
            "chapters": [
                {
                    "name": "第一章 行列式",
                    "homeworks": [
                        {
                            "name": "第一次作业",
                            "contentId": 3000000001,
                            "test": {
                                "id": 3000000001,
                                "type": 3,
                                "name": "第一次作业",
                                "deadline": 1757777400000,
                                "usedTryCount": 2,
                                "userScore": None,
                                "totalScore": 100.0,
                            },
                        }
                    ],
                    "quizs": [
                        {
                            "name": "单元测验一",
                            "test": {
                                "id": 3000000002,
                                "type": 1,
                                "name": "单元测验一",
                                "deadline": 1758382200000,
                                "usedTryCount": 1,
                                "userScore": 88.5,
                                "totalScore": 100.0,
                            },
                        }
                    ],
                    "exam": {
                        "id": 3000000003,
                        "name": "期中测试",
                        "deadline": 1778316045098,
                        "userScore": None,
                        "totalScore": 100.0,
                        "objectTest": {"id": 3000000003, "deadline": 1778316045098},
                        "objectTestVo": {"id": 3000000003, "usedTryCount": 2},
                    },
                }
            ],
            # 与章节里的 exam 是同一份，必须去重
            "exams": [
                {
                    "id": 3000000003,
                    "name": "期中测试",
                    "deadline": 1778316045098,
                    "userScore": None,
                    "totalScore": 100.0,
                }
            ],
        },
    }


def test_parse_term_keeps_homework_quiz_and_exam_once_each():
    course = provider().parse_courses(panel_payload())[0]
    items = provider().parse_term(term_payload(), course)

    assert [a.title for a in items] == ["第一次作业", "单元测验一", "期中测试"]
    assert [a.kind for a in items] == ["homework", "quiz", "exam"]
    assert all(a.course == "线性代数" and a.course_id == "1000000001" for a in items)
    # 考试 id 与作业 id 前缀不同，避免同号相撞
    assert items[0].id == "t3000000001"
    assert items[2].id == "e3000000003"


def test_assignment_status_follows_score_and_attempts():
    course = provider().parse_courses(panel_payload())[0]
    by_title = {a.title: a for a in provider().parse_term(term_payload(), course)}

    # 做过但没出分 → 已提交；有分 → 已批改；没做过 → 待完成
    assert by_title["第一次作业"].status == "submitted"
    assert by_title["单元测验一"].status == "graded"
    assert by_title["单元测验一"].score == 88.5


def test_a_zero_score_still_counts_as_graded():
    """0 分是成绩，不是「没有成绩」——别用真值判断。"""
    payload = term_payload()
    payload["result"]["chapters"][0]["homeworks"][0]["test"]["userScore"] = 0
    course = provider().parse_courses(panel_payload())[0]

    items = {a.title: a for a in provider().parse_term(payload, course)}
    assert items["第一次作业"].status == "graded"
    assert items["第一次作业"].score == 0.0


def test_deadline_is_parsed_from_epoch_milliseconds():
    course = provider().parse_courses(panel_payload())[0]
    items = {a.title: a for a in provider().parse_term(term_payload(), course)}

    due = items["第一次作业"].due
    assert isinstance(due, datetime)
    assert due.tzinfo is not None
    assert (due.year, due.month, due.day) == (2025, 9, 13)   # 23:30 +08


def test_term_without_chapters_yields_no_assignments():
    course = provider().parse_courses(panel_payload())[0]
    assert provider().parse_term({"code": 0, "result": {}}, course) == []
    assert provider().parse_term({"code": 0, "result": None}, course) == []


# --------------------------------------------------------------------------- #
# csrfKey / 错误语义
# --------------------------------------------------------------------------- #

def test_csrf_key_comes_from_the_ntesstudysi_cookie():
    """慕课没有叫 csrfKey 的 Cookie；取错名字会一直被拒「非法跨域」。"""
    assert provider()._csrf() == "csrf-abc"


def test_csrf_key_can_be_overridden_in_options():
    p = provider(csrf_key="explicit")
    assert p._csrf() == "explicit"
    assert p._with_csrf(COURSE_RPC) == f"{COURSE_RPC}?csrfKey=explicit"


def test_with_csrf_appends_instead_of_double_question_mark():
    p = provider()
    assert p._with_csrf(COURSE_RPC + "?x=1") == COURSE_RPC + "?x=1&csrfKey=csrf-abc"
    assert p._with_csrf(COURSE_RPC + "?csrfKey=old") == COURSE_RPC + "?csrfKey=old"


def test_missing_cookie_does_not_produce_an_empty_param():
    p = Icourse163Provider(AccountConfig(platform="icourse163", cookie=""))
    assert p._csrf() == ""
    assert p._with_csrf(COURSE_RPC) == COURSE_RPC


def test_panel_requires_type_not_paging():
    """面板接口的参数是 type=30；传 pageIndex 会被 -1007 顶回来。"""
    assert provider().query_form(COURSE_RPC) == {"type": 30}
    assert provider().query_form(TERM_RPC) == {}


def test_not_logged_in_is_an_auth_error_not_an_empty_list():
    with pytest.raises(AuthError) as caught:
        provider()._check_rpc({"code": -2, "message": "尚未登录!"}, "课程面板")
    assert "登录" in str(caught.value)


def test_illegal_cross_origin_is_also_an_auth_error():
    """csrfKey 丢了服务端会说「非法跨域请求」，本质还是会话参数不对。"""
    with pytest.raises(AuthError):
        provider()._check_rpc({"code": -1002, "message": "非法跨域请求"}, "课程面板")


def test_unknown_bean_is_a_provider_error_with_a_probe_hint():
    with pytest.raises(ProviderError) as caught:
        provider()._check_rpc({"code": -1003, "message": "Url里面的类名出错"}, "课程面板")
    assert "probe" in caught.value.hint


def test_success_code_passes_through():
    provider()._check_rpc({"code": 0, "result": {}}, "课程面板")
    provider()._check_rpc(None, "课程面板")


# --------------------------------------------------------------------------- #
# 元数据
# --------------------------------------------------------------------------- #

def test_meta_declares_verified_data_and_login_support():
    meta = Icourse163Provider.meta
    assert meta.status == "beta"
    assert meta.verified == "2026-10-20"
    assert meta.login is True


def test_endpoints_are_declared_for_the_readonly_whitelist():
    assert set(Icourse163Provider.endpoints) == {COURSE_RPC, TERM_RPC}
    # 只读承诺：这些地址里不能出现写操作的词
    for url in Icourse163Provider.endpoints:
        lowered = url.lower()
        for word in ("submit", "save", "answer", "commit", "upload", "delete"):
            assert word not in lowered
