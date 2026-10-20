"""头歌（educoder.net）适配器：签名头、登录判据、任务解析。

夹具用的是**脱敏后的真实响应结构**（字段名从真实账号的 ``/api/*.json`` 抄下来，
课程名、任务名、id 换成假数据），不联网。接口改版时这些用例会先炸。
"""

from __future__ import annotations

import base64
import hashlib

import pytest

from studydash import educoder_login
from studydash.providers.base import AccountConfig, AuthError, ProviderError
from studydash.providers.educoder import (
    COURSES_RPC,
    HOMEWORK_RPC,
    SIGN_AK,
    SIGN_SK,
    TYPE_SHIXUN,
    TYPE_SUBJECT,
    USER_RPC,
    EducoderProvider,
    signed_headers,
)

COOKIE = "_educoder_session=session-abc; uid=1"


def provider() -> EducoderProvider:
    return EducoderProvider(
        AccountConfig(platform="educoder", label="我的头歌", cookie=COOKIE)
    )


def courses_payload(*courses: dict) -> dict:
    return {"status": 0, "data": list(courses)}


def course(cid: int = 12345, name: str = "数据结构") -> dict:
    return {
        "id": cid,
        "name": name,
        "end_date": "2026-12-31",
        "created_at": "2026-09-01 10:00:00",
        "updated_at": "2026-10-18 09:12:31",
    }


def task(**overrides) -> dict:
    base = {
        "homework_id": 611,
        "name": "顺序表实训",
        "shixun_identifier": "abc123",
        "end_time": "2026-11-01 23:59",
        "status": ["提交中"],
        "shixun_finished_status": 0,
        "challenge_count": 5,
        "finished_challenge_count": 0,
    }
    base.update(overrides)
    return base


def subject_task(**overrides) -> dict:
    """``type=1`` 的图文作业。没交时 ``work_status`` 就是空数组。"""
    base = {
        "homework_id": 901,
        "name": "1、软件过程模型",
        "end_time": "2026-11-22 23:59",
        "status": ["提交中"],
        "work_status": [],
        "un_commit_work": True,
        "student_work_id": 4711,
        "work_id": 88,
        "challenge_count": 0,
    }
    # ``submitted=`` 比直接写 ``un_commit_work`` 读起来顺一点
    submitted = overrides.pop("submitted", None)
    if submitted is not None:
        overrides["un_commit_work"] = not submitted
    base.update(overrides)
    return base


def homework_payload(*tasks: dict, published: int = 1, finished: int = 0, unpublished: int = 0) -> dict:
    return {
        "status": 0,
        "published_count": published,
        "unpublished_count": unpublished,
        "finished_task_count": finished,
        "homeworks": list(tasks),
    }


class Stub:
    """顶替 ``EducoderProvider._get_json``。

    ``homework_commons.json`` 靠 ``type`` 区分实训和图文作业，所以键是
    ``(url, type)``；``(url, None)`` 当兜底。
    """

    def __init__(self, routes: dict[tuple[str, object], object]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, object]] = []

    def __call__(self, url, params=None, *, note=""):
        kind = (params or {}).get("type")
        self.calls.append((url, kind))
        key = (url, kind)
        if key not in self.routes:
            key = (url, None)
        if key not in self.routes:
            raise AssertionError(f"没预料到的请求：{url} type={kind!r}")
        value = self.routes[key]
        if isinstance(value, Exception):
            raise value
        return value


def wire(monkeypatch_provider: EducoderProvider, routes: dict[tuple[str, object], object]) -> Stub:
    stub = Stub(routes)
    monkeypatch_provider._get_json = stub                     # type: ignore[method-assign]
    return stub


def home_routes(cid: int, shixun: dict, subject: dict) -> dict:
    """一门课的两类任务。``homework_commons.json`` 靠 ``type`` 区分。"""
    url = HOMEWORK_RPC.format(cid=cid)
    return {(url, TYPE_SHIXUN): shixun, (url, TYPE_SUBJECT): subject}


EMPTY_SUBJECT = {"status": 0, "published_count": 0, "unpublished_count": 0, "homeworks": []}
EMPTY_SHIXUN = {"status": 0, "published_count": 0, "unpublished_count": 0,
                "finished_task_count": 0, "homeworks": []}


# --------------------------------------------------------------------------- #
# 签名头
# --------------------------------------------------------------------------- #

def test_signature_matches_the_frontend_algorithm(monkeypatch):
    """``md5(base64("method=GET&ak=…&sk=…&time=…"))``，时间戳是毫秒。"""
    monkeypatch.setattr("studydash.providers.educoder.time.time", lambda: 1760000000.123)
    stamp = str(int(1760000000.123 * 1000))

    headers = signed_headers()

    raw = f"method=GET&ak={SIGN_AK}&sk={SIGN_SK}&time={stamp}"
    expected = hashlib.md5(base64.b64encode(raw.encode())).hexdigest()
    assert headers["X-EDU-Timestamp"] == stamp
    assert headers["X-EDU-Signature"] == expected
    assert headers["X-EDU-Type"] == "pc"
    assert headers["X-Original-Host"] == "www.educoder.net"
    # 请求 id 每次都不一样
    assert headers["X-Request-Id"] != signed_headers()["X-Request-Id"]


# --------------------------------------------------------------------------- #
# 登录判据
# --------------------------------------------------------------------------- #

def test_guest_is_an_auth_error_even_though_the_http_call_succeeded():
    """游客也会拿到 HTTP 200；不先判 login 就会把过期显示成「0 门课」。"""
    prov = provider()
    wire(prov, {(USER_RPC, None): {"status": 0, "login": "", "username": "游客"}})

    with pytest.raises(AuthError) as exc:
        prov.fetch()

    assert "Cookie" in str(exc.value)


def test_missing_cookie_is_rejected_before_any_request():
    prov = EducoderProvider(AccountConfig(platform="educoder", label="空的", cookie=""))
    with pytest.raises(AuthError):
        prov.fetch()


def test_102_says_signature_or_clock_not_the_server_message():
    """头歌的 ``-102`` 文案是「设备时间不匹配」，真实原因往往是签名没带。"""
    prov = provider()
    wire(
        prov,
        {
            (USER_RPC, None): {"status": -102, "message": "服务器时间与您的设备时间不匹配"},
        },
    )

    with pytest.raises(ProviderError) as exc:
        prov.fetch()

    assert "签名" in str(exc.value)
    assert exc.value.hint


def test_http_401_is_mapped_to_auth_error():
    prov = provider()
    wire(prov, {(USER_RPC, None): {"status": 401, "message": "请先登录"}})

    with pytest.raises(AuthError):
        prov.fetch()


# --------------------------------------------------------------------------- #
# 课程与任务
# --------------------------------------------------------------------------- #

def test_fetch_attaches_tasks_and_progress():
    prov = provider()
    wire(
        prov,
        {
            (USER_RPC, None): {"status": 0, "login": "pxknporu8", "username": "小周"},
            (COURSES_RPC, None): courses_payload(course()),
            **home_routes(
                12345,
                homework_payload(
                    task(homework_id=611, name="顺序表实训"),
                    task(
                        homework_id=612,
                        name="链表实训",
                        end_time="2026-11-08 23:59",
                        shixun_finished_status=1,
                        finished_challenge_count=5,
                    ),
                    published=2,
                    finished=1,
                ),
                EMPTY_SUBJECT,
            ),
        },
    )

    courses = prov.fetch()

    assert [c.name for c in courses] == ["数据结构"]
    got = courses[0]
    assert got.id == "12345"
    assert got.platform == "educoder"
    assert got.extra["platform_name"] == "头歌"
    # 进度按「已完成任务 / 已发布任务」，不是 online_learning 里那些全为 0 的字段
    assert [(p.label, p.done, p.total) for p in got.progress] == [("已完成任务", 1, 2)]

    assert [a.id for a in got.assignments] == ["h611", "h612"]
    first, second = got.assignments
    assert first.kind == "homework"
    assert first.detail == "实训 · 任务点 0/5"
    # 教室里的作业详情页，不是会 404 的 /shixuns/.../shixun_exec
    assert first.url == (
        "https://www.educoder.net/classrooms/12345/shixun_homework/611/detail?tabs=0"
    )
    assert first.due is not None and first.due.strftime("%Y-%m-%d %H:%M") == "2026-11-01 23:59"
    assert first.platform_name == "头歌"
    assert second.status == "graded"


def test_subject_homework_is_captured_too():
    """``type=1`` 的图文作业跟实训是两批数据，默认请求根本拿不到。"""
    prov = provider()
    wire(
        prov,
        {
            (USER_RPC, None): {"status": 0, "login": "u"},
            (COURSES_RPC, None): courses_payload(course()),
            **home_routes(
                12345,
                EMPTY_SHIXUN,
                homework_payload(
                    subject_task(homework_id=901, name="1、软件过程模型", submitted=False),
                    subject_task(homework_id=902, name="2、可研分析", submitted=True),
                    published=2,
                ),
            ),
        },
    )

    got = prov.fetch()[0]

    assert [(p.done, p.total) for p in got.progress] == [(1, 2)]
    assert [a.id for a in got.assignments] == ["s901", "s902"]
    first, second = got.assignments
    assert first.title == "1、软件过程模型"
    assert first.detail == "图文作业 · 待提交"
    assert first.status == "pending"
    assert first.url == (
        "https://www.educoder.net/classrooms/12345/common_homework/901/detail"
    )
    assert second.detail == "图文作业 · 已提交"
    assert second.status == "submitted"
    # 图文作业的截止时间是 ``YYYY-MM-DD HH:MM``
    assert second.due is not None and second.due.strftime("%Y-%m-%d %H:%M") == "2026-11-22 23:59"


def test_both_task_types_are_requested_per_course():
    """实训是默认返回的，图文作业必须显式再要一次 ``type=1``。"""
    prov = provider()
    stub = wire(
        prov,
        {
            (USER_RPC, None): {"status": 0, "login": "u"},
            (COURSES_RPC, None): courses_payload(course()),
            **home_routes(12345, EMPTY_SHIXUN, EMPTY_SUBJECT),
        },
    )

    prov.fetch()

    home = HOMEWORK_RPC.format(cid=12345)
    assert stub.calls == [
        (USER_RPC, None),
        (COURSES_RPC, None),
        (home, TYPE_SHIXUN),
        (home, TYPE_SUBJECT),
    ]


def test_progress_sums_shixun_and_subject_work():
    prov = provider()
    wire(
        prov,
        {
            (USER_RPC, None): {"status": 0, "login": "u"},
            (COURSES_RPC, None): courses_payload(course()),
            **home_routes(
                12345,
                homework_payload(task(homework_id=1), published=3, finished=2),
                homework_payload(
                    subject_task(homework_id=2, submitted=True),
                    subject_task(homework_id=3, submitted=False),
                    published=2,
                ),
            ),
        },
    )

    got = prov.fetch()[0]

    assert [(p.done, p.total) for p in got.progress] == [(3, 5)]
    assert len(got.assignments) == 3


def test_unpublished_tasks_do_not_count_toward_progress():
    prov = provider()
    wire(
        prov,
        {
            (USER_RPC, None): {"status": 0, "login": "u"},
            (COURSES_RPC, None): courses_payload(course()),
            **home_routes(
                12345,
                homework_payload(task(homework_id=1), published=3, finished=2, unpublished=2),
                EMPTY_SUBJECT,
            ),
        },
    )

    got = prov.fetch()[0]

    assert [(p.done, p.total) for p in got.progress] == [(2, 3)]
    # 没发布的任务不该出现在作业列表里——列表只有服务端真的给回来的那一条
    assert len(got.assignments) == 1


def test_submission_window_label_is_not_completion():
    """``status: ["提交中"]`` 是「还能交」，不是「已经交了」。"""
    prov = provider()
    wire(
        prov,
        {
            (USER_RPC, None): {"status": 0, "login": "u"},
            (COURSES_RPC, None): courses_payload(course()),
            **home_routes(
                12345,
                homework_payload(
                    task(homework_id=1, status=["提交中"], shixun_finished_status=0),
                    task(homework_id=2, status=["已截止"], shixun_finished_status=1),
                    task(homework_id=3, status=["已提交"]),
                    task(
                        homework_id=4,
                        status=["提交中"],
                        student_passed_time="2026-10-01 12:00:00",
                    ),
                    published=4,
                ),
                EMPTY_SUBJECT,
            ),
        },
    )

    statuses = [a.status for a in prov.fetch()[0].assignments]

    assert statuses == ["pending", "graded", "submitted", "graded"]


def test_subject_completion_only_reads_un_commit_work():
    """``work_status`` 在没交的作业上可能是空数组，``student_work_id`` 也都非空。"""
    prov = provider()
    wire(
        prov,
        {
            (USER_RPC, None): {"status": 0, "login": "u"},
            (COURSES_RPC, None): courses_payload(course()),
            **home_routes(
                12345,
                EMPTY_SHIXUN,
                homework_payload(
                    subject_task(homework_id=1, submitted=True),
                    subject_task(homework_id=2, submitted=False),
                    published=2,
                ),
            ),
        },
    )

    statuses = [a.status for a in prov.fetch()[0].assignments]

    assert statuses == ["submitted", "pending"]


def test_a_course_whose_tasks_fail_is_still_kept():
    """一门课的任务接口挂了，不该让整门课从看板上消失。"""
    prov = provider()
    wire(
        prov,
        {
            (USER_RPC, None): {"status": 0, "login": "u"},
            (COURSES_RPC, None): courses_payload(course(1, "数据结构"), course(2, "算法")),
            **home_routes(
                1, homework_payload(task(homework_id=1), published=1), EMPTY_SUBJECT
            ),
            **home_routes(2, ProviderError("接口 500"), ProviderError("接口 500")),
        },
    )

    courses = prov.fetch()

    assert [c.name for c in courses] == ["数据结构", "算法"]
    assert len(courses[0].assignments) == 1
    assert courses[1].assignments == []
    assert courses[1].progress == []


def test_one_kind_failing_does_not_hide_the_other():
    """图文作业接口挂了，实训那批照样要出现在看板上。"""
    prov = provider()
    wire(
        prov,
        {
            (USER_RPC, None): {"status": 0, "login": "u"},
            (COURSES_RPC, None): courses_payload(course()),
            **home_routes(
                12345,
                homework_payload(task(homework_id=1), published=1),
                ProviderError("图文作业接口 500"),
            ),
        },
    )

    got = prov.fetch()[0]

    assert [a.id for a in got.assignments] == ["h1"]
    assert [(p.done, p.total) for p in got.progress] == [(0, 1)]


def test_course_without_id_or_name_is_dropped():
    prov = provider()
    wire(
        prov,
        {
            (USER_RPC, None): {"status": 0, "login": "u"},
            (COURSES_RPC, None): courses_payload({"name": "没有 id"}, {"id": 9}, course(3, "   ")),
        },
    )

    assert prov.fetch() == []


def test_task_without_title_or_id_is_dropped():
    prov = provider()
    wire(
        prov,
        {
            (USER_RPC, None): {"status": 0, "login": "u"},
            (COURSES_RPC, None): courses_payload(course()),
            **home_routes(
                12345,
                homework_payload(
                    {"homework_id": 1, "name": "  "},
                    {"name": "没有 id"},
                    task(homework_id=2, name="留下我"),
                    published=3,
                ),
                EMPTY_SUBJECT,
            ),
        },
    )

    assert [a.title for a in prov.fetch()[0].assignments] == ["留下我"]


# --------------------------------------------------------------------------- #
# meta
# --------------------------------------------------------------------------- #

def test_meta_is_beta_and_declares_login_support():
    meta = EducoderProvider.meta

    assert meta.id == "educoder"
    assert meta.name == "头歌"
    assert meta.status == "beta"
    assert meta.login is True        # 声明了就一定要有对应的登录实现
    assert meta.verified == "2026-10-20"
    assert set(EducoderProvider.endpoints) == {USER_RPC, COURSES_RPC, HOMEWORK_RPC}


# --------------------------------------------------------------------------- #
# 登录态自检（educoder_login.check）
# --------------------------------------------------------------------------- #

class FakeHttp:
    """顶替 ``studydash.http.HttpClient``。"""

    reply: object = {}
    last: "FakeHttp | None" = None

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.calls: list[str] = []
        self.last_headers: dict = {}
        FakeHttp.last = self

    def get_json(self, url, params=None, *, headers=None, note="", default=None):
        self.calls.append(url)
        self.last_headers = dict(headers or {})
        return FakeHttp.reply


def test_check_accepts_a_logged_in_session(monkeypatch):
    FakeHttp.reply = {"status": 0, "login": "pxknporu8", "username": "小周"}
    monkeypatch.setattr(educoder_login, "HttpClient", FakeHttp)

    session = educoder_login.check(COOKIE)

    assert session.ok
    assert session.uid == "pxknporu8"
    assert "小周" in session.message
    assert FakeHttp.last.calls == [USER_RPC]
    # 自检也必须带签名头，否则会拿到 -102
    assert "X-EDU-Signature" in FakeHttp.last.last_headers


def test_check_rejects_the_guest_response(monkeypatch):
    FakeHttp.reply = {"status": 0, "login": "", "username": "游客"}
    monkeypatch.setattr(educoder_login, "HttpClient", FakeHttp)

    session = educoder_login.check(COOKIE)

    assert not session.ok
    assert "游客" in session.message


def test_check_reports_network_failure_instead_of_claiming_expired(monkeypatch):
    class Boom(FakeHttp):
        def get_json(self, *_a, **_kw):
            raise OSError("timed out")

    monkeypatch.setattr(educoder_login, "HttpClient", Boom)
    session = educoder_login.check(COOKIE)

    assert not session.ok
    assert "timed out" in session.message


def test_check_on_empty_cookie_does_not_touch_the_network(monkeypatch):
    def explode(**_kw):                                   # pragma: no cover - 不该被调到
        raise AssertionError("空 Cookie 不该发请求")

    monkeypatch.setattr(educoder_login, "HttpClient", explode)
    assert not educoder_login.check("   ").ok
