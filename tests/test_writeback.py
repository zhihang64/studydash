"""写回模块的测试。

这条链路真的会改平台上的学习进度，所以测试的重心不是「功能对不对」，而是
**边界守得住守不住**：不加 ``--apply`` 一个请求都不许发、白名单外的地址必须
被拒、签名串的每个字段都不能错位。全部走桩，不联网。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

import pytest

from studydash.http import HttpError
from studydash.model import Course
from studydash.providers import AccountConfig, HttpSettings
from studydash.writeback import (
    _ENC_SALT,
    READ_URL_PREFIXES,
    REFERER_REPORT,
    REFERER_STATUS,
    SEND_FAILED,
    WRITE_URL_PREFIXES,
    ChaoxingVideoWriter,
    VideoTarget,
    WritebackError,
    build_enc,
    build_report_url,
    cookie_user_id,
    parse_mission_counts,
)

COOKIE = "UID=12345; _uid=98765; fid=8888; _d=1700000000000"
REPORT_BASE = "https://mooc1.chaoxing.com/multimedia/log/a/201009813/abc123"


# --------------------------------------------------------------------------- #
# 桩
# --------------------------------------------------------------------------- #


class FakeResponse:
    def __init__(self, payload: Any = None, text: str = "") -> None:
        self._payload = payload
        self._text = text

    def json(self, default: Any = None) -> Any:
        return self._payload if self._payload is not None else default

    @property
    def content(self) -> bytes:
        return self._text.encode("utf-8")

    @property
    def text(self) -> str:
        return self._text


class Recorder:
    """记录每次写请求的 url 与 headers，回固定响应。"""

    def __init__(self, payload: Any = None, text: str = "") -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.payload = payload
        self.text = text

    def get(self, url: str, headers: dict[str, str] | None = None, **_kw: Any) -> FakeResponse:
        self.calls.append((url, dict(headers or {})))
        return FakeResponse(self.payload, self.text)


def _account(cookie: str = COOKIE) -> AccountConfig:
    return AccountConfig(platform="chaoxing", label="测试", cookie=cookie)


def _writer(*, apply: bool = False, **kwargs: Any) -> ChaoxingVideoWriter:
    kwargs.setdefault("sleep", lambda _s: None)
    kwargs["apply"] = apply
    return ChaoxingVideoWriter(_account(), HttpSettings(delay=0.0, timeout=1.0, retries=0), **kwargs)


def _target(**kwargs: Any) -> VideoTarget:
    base: dict[str, Any] = {
        "course_id": "111",
        "course_name": "高等数学",
        "clazz_id": "222",
        "cpi": "333",
        "knowledge_id": "444",
        "chapter": "第一章",
        "jobid": "job-1",
        "object_id": "obj-1",
        "title": "1.1 极限",
        "other_info": "nodeId_444",
        "rt": 0.75,
        "report_url": REPORT_BASE,
    }
    base.update(kwargs)
    return VideoTarget(**base)


def _param(url: str, name: str) -> str:
    match = re.search(rf"[?&]{name}=([^&]*)", url)
    return match.group(1) if match else ""


# --------------------------------------------------------------------------- #
# 签名与地址
# --------------------------------------------------------------------------- #


def test_build_enc_matches_platform_algorithm():
    """md5 的输入串必须逐字段对得上，错一个字符平台就不认。"""
    got = build_enc(
        clazz_id="222",
        user_id="98765",
        jobid="job-1",
        object_id="obj-1",
        playing_time=60,
        duration=300,
    )
    raw = "[222][98765][job-1][obj-1][60000][d_yHJ!$pdA~5][300000][0_300]"
    assert raw.count(_ENC_SALT) == 1
    assert got == hashlib.md5(raw.encode("utf-8")).hexdigest()
    assert len(got) == 32
    assert got == got.lower()


def test_build_enc_is_sensitive_to_every_field():
    """任何一个字段变了签名都得变——否则说明有字段没拼进去。"""
    base = {
        "clazz_id": "222",
        "user_id": "98765",
        "jobid": "job-1",
        "object_id": "obj-1",
        "playing_time": 60,
        "duration": 300,
    }
    seen = {build_enc(**base)}
    for field, value in (
        ("clazz_id", "999"),
        ("user_id", "11111"),
        ("jobid", "job-2"),
        ("object_id", "obj-2"),
        ("playing_time", 120),
        ("duration", 600),
    ):
        changed = dict(base)
        changed[field] = value
        seen.add(build_enc(**changed))
    assert len(seen) == 7


def test_build_enc_uses_seconds_times_a_thousand():
    """时长按秒传进来，拼串时要乘 1000——直接传毫秒会翻倍，不能靠猜。"""
    a = build_enc(
        clazz_id="1", user_id="2", jobid="3", object_id="4", playing_time=30, duration=90
    )
    b = build_enc(
        clazz_id="1", user_id="2", jobid="3", object_id="4", playing_time=30000, duration=90000
    )
    assert a != b
    raw = "[1][2][3][4][30000][d_yHJ!$pdA~5][90000][0_90]"
    assert a == hashlib.md5(raw.encode("utf-8")).hexdigest()


def test_build_report_url_keeps_field_order():
    url = build_report_url(
        report_url=REPORT_BASE + "/",
        dtoken="DT",
        clazz_id="222",
        user_id="98765",
        jobid="job-1",
        object_id="obj-1",
        duration=300,
        other_info="nodeId_444",
        enc="e" * 32,
        rt=0.75,
        stamp_ms=1700000000123,
    )
    assert url.startswith(REPORT_BASE + "/DT?")
    assert _param(url, "clazzId") == "222"
    assert _param(url, "playingTime") == "300"
    assert _param(url, "duration") == "300"
    assert _param(url, "clipTime") == "0_300"
    assert _param(url, "objectId") == "obj-1"
    assert _param(url, "otherInfo") == "nodeId_444"
    assert _param(url, "jobid") == "job-1"
    assert _param(url, "userid") == "98765"
    assert _param(url, "isdrag") == "4"
    assert _param(url, "view") == "pc"
    assert _param(url, "enc") == "e" * 32
    assert _param(url, "rt") == "0.75"
    assert _param(url, "dtype") == "Video"
    assert _param(url, "_t") == "1700000000123"


def test_build_report_url_honours_overrides():
    url = build_report_url(
        report_url=REPORT_BASE,
        dtoken="DT",
        clazz_id="2",
        user_id="3",
        jobid="4",
        object_id="5",
        duration=300,
        other_info="",
        enc="x",
        playing_time=59,
        isdrag=0,
        dtype="Audio",
    )
    assert _param(url, "playingTime") == "59"
    assert _param(url, "duration") == "300"
    assert _param(url, "isdrag") == "0"
    assert _param(url, "dtype") == "Audio"


def test_cookie_user_id_reads_underscore_uid_only():
    assert cookie_user_id(COOKIE) == "98765"
    assert cookie_user_id("UID=1; _uid=42 ;fid=2") == "42"
    assert cookie_user_id("UID=1; fid=2") == ""
    assert cookie_user_id("") == ""
    # UID 是另一个字段，不能当成 _uid 用。
    assert cookie_user_id("UID=12345") == ""


# --------------------------------------------------------------------------- #
# 白名单
# --------------------------------------------------------------------------- #


def test_write_prefixes_only_contain_chaoxing_write_endpoints():
    """写白名单必须逐项落在超星自己的写接口上，不能混进别的域名。"""
    assert WRITE_URL_PREFIXES == (
        "https://mooc1.chaoxing.com/mooc-ans/multimedia/log/a/",
        "https://mooc1.chaoxing.com/multimedia/log/a/",
        "https://mooc1-2.chaoxing.com/ananas/job?",
        "https://mooc1-2.chaoxing.com/ananas/job/readv2?",
        "https://mooc1-2.chaoxing.com/ananas/job/document?",
        "https://zhibo.chaoxing.com/saveTimePc?",
    )
    for prefix in WRITE_URL_PREFIXES:
        assert prefix.startswith("https://")
        host = prefix.split("/")[2]
        assert host.endswith(".chaoxing.com"), prefix
        # 只许落在「上报观看进度」或「任务点完成」这两类路径上。
        assert ("multimedia/log/a/" in prefix) or ("/ananas/job" in prefix) or (
            "saveTimePc" in prefix
        ), prefix


def test_read_prefixes_are_pinned():
    assert "https://mooc1-1.chaoxing.com/ananas/status/" in READ_URL_PREFIXES
    assert not any("multimedia/log" in item for item in READ_URL_PREFIXES)


def test_report_url_outside_whitelist_is_refused():
    writer = _writer(apply=True)
    bad = _target(report_url="https://evil.example.com/multimedia/log/a/1")
    with pytest.raises(WritebackError) as excinfo:
        writer.report(bad)
    assert "白名单" in str(excinfo.value)
    assert "multimedia/log" in excinfo.value.hint


def test_status_url_is_pinned_to_the_read_prefix():
    """状态地址由常量拼出来，object_id 塞什么都出不了这个前缀。"""
    writer = _writer()
    seen: list[str] = []

    def fake_get_json(url: str, *a: Any, **k: Any) -> Any:
        seen.append(url)
        return {"duration": 300, "dtoken": "DT"}

    writer.http.get_json = fake_get_json
    assert writer.status("../../evil") == {"duration": 300, "dtoken": "DT"}
    assert seen[0].startswith(READ_URL_PREFIXES[0])
    assert "_dc=" in seen[0]


# --------------------------------------------------------------------------- #
# apply=False 的隔离
# --------------------------------------------------------------------------- #


def test_apply_false_never_sends_a_request():
    writer = _writer(apply=False)
    sent: list[str] = []
    writer.http.get_json = lambda *a, **k: {"duration": 300, "dtoken": "DT"}
    writer.http.get = lambda url, *a, **k: sent.append(url) or FakeResponse({})

    result = writer.report(_target())
    assert sent == []
    assert result["applied"] is False
    assert result["ok"] is None
    assert result["requests"] == 0
    assert "演练" in result["reason"]


def test_apply_true_sends_exactly_one_request_when_not_paced():
    writer = _writer(apply=True)
    sent: list[str] = []
    writer.http.get_json = lambda *a, **k: {"duration": 300, "dtoken": "DT"}
    writer.http.get = lambda url, *a, **k: sent.append(url) or FakeResponse({"isPassed": True})

    result = writer.report(_target())
    assert len(sent) == 1
    assert result["requests"] == 1
    assert result["ok"] is True
    assert _param(sent[0], "playingTime") == "300"
    assert _param(sent[0], "isdrag") == "4"


def test_ispassed_false_is_not_reported_as_success():
    """``isPassed=false`` 就是任务点没通过，不能再记成功。

    以前把它解释成「没有剩余可报」一律记 ok=True，结果是界面上写「上报成功」、
    学习通的已完成任务点却一个不涨——谎报正是用户看到的那个 bug。
    """
    writer = _writer(apply=True)
    writer.http.get_json = lambda *a, **k: {"duration": 300, "dtoken": "DT"}
    writer.http.get = lambda url, *a, **k: FakeResponse(
        {"isPassed": False, "videoTimeLimit": False, "hasJobLimit": False}
    )

    result = writer.report(_target())
    assert result["ok"] is False
    assert result["requests"] == 1
    assert "isPassed=false" in result["reason"]


def test_stuck_video_is_skipped_without_sending():
    """播放位置已经满了但没判通过的任务点不再浪费请求。"""
    writer = _writer(apply=True)
    sent: list[str] = []
    writer.http.get_json = lambda *a, **k: {"duration": 300, "dtoken": "DT"}
    writer.http.get = lambda url, *a, **k: sent.append(url) or FakeResponse({"isPassed": True})

    result = writer.report(_target(play_time_ms=300_000, passed=False))
    assert result["ok"] is False
    assert result["requests"] == 0
    assert sent == []
    assert "重复上报无效" in result["reason"]


def test_stuck_video_is_marked_with_the_dead_state_label():
    """卡死态要能被界面单独统计，不能混进「未确认」。"""
    writer = _writer(apply=True)
    writer.http.get_json = lambda *a, **k: {"duration": 300, "dtoken": "DT"}
    writer.http.get = lambda url, *a, **k: FakeResponse({"isPassed": False})

    result = writer.report(_target(play_time_ms=300_000, passed=False, face_capture="FACE"))
    assert result["status"] == "stuck"
    assert "人脸抓拍" in result["reason"]


def test_video_with_face_capture_is_skipped_before_it_pollutes_its_progress():
    """开了人脸抓拍的视频一次跳满只会把播放位置占满，之后永远判不过，所以不发。"""
    writer = _writer(apply=True)
    sent: list[str] = []
    writer.http.get_json = lambda *a, **k: {"duration": 300, "dtoken": "DT"}
    writer.http.get = lambda url, *a, **k: sent.append(url) or FakeResponse({"isPassed": True})

    result = writer.report(_target(play_time_ms=0, face_capture="FACE"))
    assert result["ok"] is None
    assert result["status"] == "skipped"
    assert result["requests"] == 0
    assert sent == []
    assert "人脸抓拍" in result["reason"]


def test_force_lets_a_face_capture_video_through():
    """``force=True`` 是给「想拿还有救的课试 paced」留的逃生门。"""
    writer = _writer(apply=True, force=True)
    sent: list[str] = []
    writer.http.get_json = lambda *a, **k: {"duration": 300, "dtoken": "DT"}
    writer.http.get = lambda url, *a, **k: sent.append(url) or FakeResponse({"isPassed": True})

    result = writer.report(_target(play_time_ms=0, face_capture="FACE"))
    assert result["status"] == "passed"
    assert result["requests"] == 1
    assert len(sent) == 1


def test_judge_marks_status_so_callers_do_not_guess():
    writer = _writer(apply=True)
    passed: dict[str, Any] = {}
    writer._judge({"isPassed": True}, passed)
    assert (passed["ok"], passed["status"]) == (True, "passed")
    failed: dict[str, Any] = {}
    writer._judge({"isPassed": False, "videoTimeLimit": False, "hasJobLimit": False}, failed)
    assert (failed["ok"], failed["status"]) == (False, "unconfirmed")


def test_request_that_never_reaches_the_platform_is_a_failure():
    writer = _writer(apply=True)
    writer.http.get_json = lambda *a, **k: {"duration": 300, "dtoken": "DT"}
    writer.http.get = lambda url, *a, **k: FakeResponse(None)

    result = writer.report(_target())
    assert result["ok"] is False
    assert "没发成功" in result["reason"]


def test_apply_false_paced_still_sends_nothing_and_does_not_sleep():
    slept: list[float] = []
    writer = _writer(apply=False, sleep=slept.append)
    writer.http.get_json = lambda *a, **k: {"duration": 300, "dtoken": "DT"}
    writer.http.get = lambda *a, **k: pytest.fail("演练模式不许发请求")

    result = writer.report(_target(), paced=True)
    assert slept == []
    assert result["requests"] == 0
    assert "演练" in result["reason"]


# --------------------------------------------------------------------------- #
# 逐分钟上报
# --------------------------------------------------------------------------- #


def test_paced_sequence_is_three_then_zeroes_then_four():
    slept: list[float] = []
    writer = _writer(apply=True, sleep=slept.append, interval=0.01)
    sent: list[str] = []
    writer.http.get_json = lambda *a, **k: {"duration": 125, "dtoken": "DT"}
    writer.http.get = lambda url, *a, **k: sent.append(url) or FakeResponse({"isPassed": True})

    result = writer.report(_target(), paced=True)
    isdrags = [_param(u, "isdrag") for u in sent]
    playings = [_param(u, "playingTime") for u in sent]
    assert isdrags == ["3", "0", "0", "4"]
    assert playings == ["0", "59", "119", "125"]
    assert result["requests"] == 4
    assert result["ok"] is True
    assert len(slept) == 3


def test_paced_stops_when_a_middle_request_fails():
    writer = _writer(apply=True, sleep=lambda _s: None, interval=0.0)
    sent: list[str] = []
    calls = {"n": 0}

    def fake_get(url: str, *a: Any, **k: Any) -> FakeResponse:
        sent.append(url)
        calls["n"] += 1
        # 第二次（第一次 0 分钟上报）返回空，模拟中途断掉。
        return FakeResponse(None if calls["n"] == 2 else {"isPassed": True})

    writer.http.get_json = lambda *a, **k: {"duration": 300, "dtoken": "DT"}
    writer.http.get = fake_get

    result = writer.report(_target(), paced=True)
    assert len(sent) == 2
    assert result["ok"] is None
    assert result["reason"] == "中途上报失败"


# --------------------------------------------------------------------------- #
# 前置条件的失败要说话
# --------------------------------------------------------------------------- #


def test_missing_uid_is_reported_not_silently_sent():
    writer = ChaoxingVideoWriter(
        _account("UID=12345; fid=8888"),
        HttpSettings(delay=0.0, timeout=1.0, retries=0),
        apply=True,
        sleep=lambda _s: None,
    )
    writer.http.get = lambda *a, **k: pytest.fail("没有 _uid 就不该发上报")
    result = writer.report(_target())
    assert result["ok"] is None
    assert "_uid" in result["reason"]


def test_missing_duration_or_dtoken_aborts_before_writing():
    writer = _writer(apply=True)
    writer.http.get = lambda *a, **k: pytest.fail("状态不全就不该发上报")
    writer.http.get_json = lambda *a, **k: {"duration": 0, "dtoken": "DT"}
    result = writer.report(_target())
    assert result["ok"] is None
    assert "不完整" in result["reason"]

    writer.http.get_json = lambda *a, **k: {"duration": 300, "dtoken": ""}
    result = writer.report(_target())
    assert result["ok"] is None
    assert "不完整" in result["reason"]


def test_card_without_report_url_is_refused():
    writer = _writer(apply=True)
    writer.http.get = lambda *a, **k: pytest.fail("没有 reportUrl 就不该发上报")
    result = writer.report(_target(report_url=""))
    assert result["ok"] is None
    assert "reportUrl" in result["reason"]


# --------------------------------------------------------------------------- #
# 采集
# --------------------------------------------------------------------------- #


def test_iter_targets_keeps_known_kinds():
    writer = _writer()
    course = Course(id="111", name="高等数学", platform="chaoxing", extra={"class_id": "222"})
    writer.chapters = lambda _c: [("444", "第一章")]
    cards = {
        "reportUrl": REPORT_BASE,
        "defaults": {"reportUrl": REPORT_BASE},
        "attachments": [
            {"type": "video", "objectId": "obj-1", "jobid": "job-1",
             "property": {"name": "1.1", "rt": 0.75}},
            # 文档/阅读/图书/直播也算任务点，只有「作业/讨论」这类才跳过。
            {"type": "document", "objectId": "obj-2", "jobid": "job-2", "property": {}},
            # 内嵌音频在卡片里也是 type=video，靠 module 区分，dtype 要跟着改。
            {"type": "video", "objectId": "obj-3", "jobid": "job-3",
             "property": {"module": "insertaudio", "name": "听力"}},
            # 视频缺 objectId 就没法取 duration/dtoken，必须丢掉。
            {"type": "video", "objectId": "", "jobid": "job-4", "property": {}},
            {"type": "workid", "jobid": "job-5", "property": {}},
        ],
    }
    writer._cards = lambda _c, _k, _p: cards if _p == 0 else None

    targets = list(writer.iter_targets(course))
    # _walk_cards 用栈遍历，顺序不保证，按 object_id 排一下再断言。
    targets.sort(key=lambda t: t.object_id)
    assert [(t.object_id, t.kind) for t in targets] == [
        ("obj-1", "video"),
        ("obj-2", "document"),
        ("obj-3", "audio"),
    ]
    assert targets[0].chapter == "第一章"
    assert targets[0].clazz_id == "222"
    assert targets[0].rt == 0.75
    assert targets[0].dtype == "Video"
    assert targets[2].dtype == "Audio"
    assert targets[0].label == "高等数学 / 第一章 / 1.1"


def test_iter_targets_reads_report_url_from_defaults():
    """平台真实的 cards JSON 把 reportUrl/clazzId/cpi 放在 defaults 里，不在顶层。"""
    writer = _writer()
    course = Course(id="111", name="课", platform="chaoxing", extra={"class_id": "222"})
    writer.chapters = lambda _c: [("444", "第一章")]
    cards = {
        "defaults": {
            "reportUrl": "https://mooc1.chaoxing.com/mooc-ans/multimedia/log/a/406429824",
            "clazzId": "151567127",
            "cpi": "406429824",
        },
        "attachments": [{"type": "video", "objectId": "obj-1", "jobid": "job-1",
                         "otherInfo": "nodeId_1", "property": {"name": "1.1"}}],
    }
    writer._cards = lambda _c, _k, _p: cards if _p == 0 else None

    targets = list(writer.iter_targets(course))
    assert len(targets) == 1
    assert targets[0].report_url.endswith("/mooc-ans/multimedia/log/a/406429824")
    assert targets[0].clazz_id == "151567127"
    assert targets[0].cpi == "406429824"
    assert targets[0].other_info == "nodeId_1"


def test_iter_targets_stops_when_a_page_is_empty():
    writer = _writer()
    course = Course(id="111", name="课", platform="chaoxing", extra={"class_id": "222"})
    writer.chapters = lambda _c: [("444", "第一章")]
    pages: list[int] = []

    def fake_cards(_c: Course, _k: str, page: int) -> dict[str, Any] | None:
        pages.append(page)
        if page >= 2:
            return None
        return {"reportUrl": REPORT_BASE, "attachments": [{"type": "document"}]}

    writer._cards = fake_cards
    assert list(writer.iter_targets(course)) == []
    assert pages == [0, 1, 2]


# --------------------------------------------------------------------------- #
# 批量
# --------------------------------------------------------------------------- #


def test_run_respects_the_limit():
    writer = _writer(apply=False)
    course = Course(id="111", name="课", platform="chaoxing", extra={"class_id": "222"})
    targets = [_target(object_id=f"obj-{i}", jobid=f"job-{i}") for i in range(5)]
    writer.iter_targets = lambda _c: iter(targets)
    writer.http.get_json = lambda *a, **k: {"duration": 300, "dtoken": "DT"}
    writer.http.get = lambda *a, **k: pytest.fail("演练模式不许发请求")

    results = writer.run([course], limit=2)
    assert len(results) == 2
    assert all(r["ok"] is None for r in results)


def test_run_records_a_failure_and_keeps_going():
    writer = _writer(apply=True)
    course = Course(id="111", name="课", platform="chaoxing", extra={"class_id": "222"})
    targets = [
        _target(object_id="ok", jobid="j1"),
        _target(object_id="boom", jobid="j2"),
        _target(object_id="ok2", jobid="j3"),
    ]
    writer.iter_targets = lambda _c: iter(targets)

    def fake_get_json(url: str, *a: Any, **k: Any) -> Any:
        return {"duration": 300, "dtoken": "DT"}

    def fake_get(url: str, *a: Any, **k: Any) -> FakeResponse:
        if "objectId=boom" in url:
            from studydash.http import HttpError

            raise HttpError(url, "炸了")
        return FakeResponse({"isPassed": True})

    writer.http.get_json = fake_get_json
    writer.http.get = fake_get

    results = writer.run([course])
    assert [r["ok"] for r in results] == [True, False, True]
    assert results[1]["requests"] == 1
    assert "没发成功" in results[1]["reason"]


def test_headers_point_at_the_video_module():
    """Referer 不对平台会拒——这两个常量是抄前端请求头来的，别随手改域名。"""
    assert REFERER_STATUS.startswith("https://mooc1.chaoxing.com/ananas/modules/video/")
    assert REFERER_REPORT.startswith("https://mooc1-2.chaoxing.com/ananas/modules/video/")


# --------------------------------------------------------------------------- #
# 并发：workers > 1 时跑线程池，结果顺序、失败兜底、回滚路径都要保持原样。
# --------------------------------------------------------------------------- #


def _parallel_writer(slowest: float = 0.0) -> Any:
    """构造一个能并发的 writer，把主线程的 http 替换成线程安全的假货。

    真实实现里每个工作线程会走 :meth:`ChaoxingVideoWriter._client` 拿到自己的
    HttpClient（因为共享实例的限速是全局串行化点），测试里没有真 HttpClient，
    所以直接让 ``_client()`` 也返回这个假的。
    """
    writer = _writer(apply=True)
    import threading as _threading

    lock = _threading.Lock()
    seen: list[str] = []

    class FakeClient:
        def get_json(self, url: str, *a: Any, **k: Any) -> Any:
            return {"duration": 300, "dtoken": "DT"}

        def get(self, url: str, *a: Any, **k: Any) -> FakeResponse:
            import time as _time

            if slowest:
                _time.sleep(slowest)
            with lock:
                seen.append(url)
            if "objectId=boom" in url:
                from studydash.http import HttpError

                raise HttpError(url, "炸了")
            return FakeResponse({"isPassed": True})

    fake = FakeClient()
    writer.http = fake
    writer._client = lambda: fake  # type: ignore[method-assign]
    writer.seen_urls = seen  # type: ignore[attr-defined]
    return writer


def test_run_parallel_keeps_result_order_and_isolates_failures():
    """4 个线程跑 5 个点：一个炸了不能拖垮整批，结果仍按 todo 顺序排。"""
    writer = _parallel_writer()
    course = Course(id="111", name="课", platform="chaoxing", extra={"class_id": "222"})
    targets = [
        _target(object_id="a", jobid="j1"),
        _target(object_id="boom", jobid="j2"),
        _target(object_id="b", jobid="j3"),
        _target(object_id="c", jobid="j4"),
        _target(object_id="d", jobid="j5"),
    ]
    writer.iter_targets = lambda _c: iter(targets)

    results = writer.run([course], workers=4)
    assert [r["ok"] for r in results] == [True, False, True, True, True]
    assert len(writer.seen_urls) == 5
    assert "没发成功" in results[1]["reason"]


def test_run_parallel_reports_progress_without_losing_steps():
    """on_step 每个点回调一次，最后补一次 total/total 让进度条走到头。"""
    writer = _parallel_writer(slowest=0.01)
    course = Course(id="111", name="课", platform="chaoxing", extra={"class_id": "222"})
    targets = [_target(object_id=f"obj-{i}", jobid=f"job-{i}") for i in range(6)]
    writer.iter_targets = lambda _c: iter(targets)

    steps: list[tuple[int, int]] = []
    writer.run([course], workers=3, on_step=lambda d, t, _tg: steps.append((d, t)))

    assert steps[-1] == (6, 6)
    assert len(steps) == 7  # 6 个点各一次 + 收尾一次
    assert all(t == 6 for _d, t in steps)


def test_workers_are_clamped_and_paced_forces_serial():
    """workers 传 0/负数/巨大值都要安全；paced 时分片等待没意义，强制串行。"""
    writer = _parallel_writer()
    course = Course(id="111", name="课", platform="chaoxing", extra={"class_id": "222"})
    targets = [_target(object_id=f"obj-{i}", jobid=f"job-{i}") for i in range(3)]
    writer.iter_targets = lambda _c: iter(targets)

    assert len(writer.run([course], workers=0)) == 3
    assert len(writer.run([course], workers=-5)) == 3
    # paced + workers>1 会走回串行分支，仍然是 3 条结果、不出错
    paced_calls: list[float] = []
    writer.sleep = lambda s: paced_calls.append(s)
    assert len(writer.run([course], workers=8, paced=True)) == 3
    assert paced_calls  # 串行逐分钟路径确实走到了 sleep


# --------------------------------------------------------------------------- #
# 非视频任务点（阅读 / 文档 / 图书 / 直播）
# --------------------------------------------------------------------------- #


def _job_writer(kind: str, payload: Any = None, text: str = "") -> tuple[Any, Recorder]:
    """造一个能把任务点发出去的 writer，客户端换成记录器。"""
    writer = _writer(apply=True)
    recorder = Recorder(payload=payload if payload is not None else {"status": True}, text=text)
    writer._client = lambda: recorder  # type: ignore[method-assign]
    return writer, recorder


@pytest.mark.parametrize(
    ("kind", "path"),
    [
        ("read", "/ananas/job/readv2?"),
        ("document", "/ananas/job/document?"),
        ("book", "/ananas/job?"),
    ],
)
def test_report_job_uses_the_kind_endpoint(kind: str, path: str) -> None:
    writer, recorder = _job_writer(kind)
    target = _target(kind=kind, jtoken="JT")

    row = writer.report(target)

    assert recorder.calls, "应该真的发了一次请求"
    url, headers = recorder.calls[0]
    assert url.startswith("https://mooc1-2.chaoxing.com" + path)
    assert _param(url, "jobid") == "job-1"
    assert _param(url, "knowledgeid") == "444"
    assert _param(url, "courseid") == "111"
    assert _param(url, "clazzid") == "222"
    assert _param(url, "jtoken") == "JT"
    assert _param(url, "_dc")
    assert headers["X-Requested-With"] == "XMLHttpRequest"
    assert row["kind"] == kind
    assert row["ok"] is True
    assert row["requests"] == 1


def test_book_prefers_the_inner_document_referer() -> None:
    writer, recorder = _job_writer("book")
    writer.report(_target(kind="book", jtoken="JT"))
    assert "memorybook" in recorder.calls[0][1]["Referer"] or "innerbook" in (
        recorder.calls[0][1]["Referer"]
    )


def test_report_live_uses_savetimepc_and_accepts_text_success() -> None:
    writer, recorder = _job_writer("live", payload=None, text="@success")
    target = _target(kind="live", stream_name="STREAM-1", vdoid="VDO-1")

    row = writer.report(target)

    url, headers = recorder.calls[0]
    assert url.startswith("https://zhibo.chaoxing.com/saveTimePc?")
    assert _param(url, "streamName") == "STREAM-1"
    assert _param(url, "vdoid") == "VDO-1"
    assert _param(url, "userId") == "98765"
    assert _param(url, "courseId") == "111"
    assert headers["Referer"].startswith("https://mooc1.chaoxing.com/")
    assert row["ok"] is True


def test_report_live_without_ids_is_a_failure() -> None:
    writer, recorder = _job_writer("live", text="@success")
    row = writer.report(_target(kind="live"))
    assert row["ok"] is False
    assert "streamName" in row["reason"]
    assert recorder.calls == []


def test_report_job_without_jtoken_is_a_failure() -> None:
    writer, recorder = _job_writer("read")
    row = writer.report(_target(kind="read", jtoken=""))
    assert row["ok"] is False
    assert "jtoken" in row["reason"]
    assert recorder.calls == []


def test_report_job_marks_failure_when_platform_says_status_false() -> None:
    writer, _recorder = _job_writer("document", payload={"status": False})
    row = writer.report(_target(kind="document", jtoken="JT"))
    assert row["ok"] is False
    assert "没确认" in row["reason"]


def test_report_job_dry_run_never_sends() -> None:
    writer = _writer(apply=False)
    recorder = Recorder(payload={"status": True})
    writer._client = lambda: recorder  # type: ignore[method-assign]

    row = writer.report(_target(kind="read", jtoken="JT"))

    assert row["ok"] is None
    assert row["applied"] is False
    assert row["requests"] == 1
    assert "演练" in row["reason"]
    assert recorder.calls == []


def test_plan_filters_by_kind() -> None:
    writer = _writer()
    course = Course(id="111", name="课", platform="chaoxing", extra={"class_id": "222"})
    targets = [
        _target(object_id="obj-1", jobid="job-1", kind="video"),
        _target(object_id="obj-2", jobid="job-2", kind="document"),
        _target(object_id="obj-3", jobid="job-3", kind="read"),
    ]
    writer.iter_targets = lambda _c: iter(targets)

    assert [t.kind for _c, t in writer.plan([course])] == ["video", "document", "read"]
    only_docs = writer.plan([course], kinds=["document"])
    assert [t.kind for _c, t in only_docs] == ["document"]
    # 单个字符串也认，免得 CLI 传参时类型不稳。
    assert [t.kind for _c, t in writer.plan([course], kinds="read")] == ["read"]


def test_plan_rejects_unknown_kind() -> None:
    writer = _writer()
    course = Course(id="111", name="课", platform="chaoxing", extra={"class_id": "222"})
    writer.iter_targets = lambda _c: iter([])
    with pytest.raises(WritebackError) as excinfo:
        writer.plan([course], kinds=["quiz"])
    assert "不认识的任务点类型" in str(excinfo.value)


def test_run_passes_kinds_through_to_plan() -> None:
    writer = _writer(apply=False)
    course = Course(id="111", name="课", platform="chaoxing", extra={"class_id": "222"})
    targets = [
        _target(object_id="obj-1", jobid="job-1", kind="video"),
        _target(object_id="obj-2", jobid="job-2", kind="document"),
    ]
    writer.iter_targets = lambda _c: iter(targets)

    rows = writer.run([course], kinds=["document"])

    assert [row["kind"] for row in rows] == ["document"]


MISSION_HTML = """
<div class="mission">已完成任务点: <span class="orange">59</span>/ 1442 &nbsp;</div>
"""


def test_parse_mission_counts_reads_the_course_page_counter():
    assert parse_mission_counts(MISSION_HTML) == (59, 1442)
    assert parse_mission_counts("已完成任务点 3/77") == (3, 77)
    assert parse_mission_counts("<p>什么都没有</p>") is None
    assert parse_mission_counts("") is None


def test_mission_counts_hits_the_course_overview_with_a_cache_buster():
    writer = _writer(apply=False)
    seen: list[str] = []

    def fake_get_text(url: str, **_kw: Any) -> str:
        seen.append(url)
        return MISSION_HTML

    writer.http.get_text = fake_get_text
    course = Course(id="266688496", name="课", platform="chaoxing", extra={"class_id": "153", "cpi": "4"})

    assert writer.mission_counts(course) == (59, 1442)
    assert seen, "没读课程页"
    assert "studentcourse" in seen[0]
    assert "courseid=266688496" in seen[0]
    assert "clazzid=153" in seen[0]
    assert "&t=" in seen[0], "不带时间戳会被平台缓存，读到的是旧值"


def test_mission_counts_swallows_network_errors():
    writer = _writer(apply=False)
    writer.http.get_text = lambda url, **kw: ""
    course = Course(id="1", name="课", platform="chaoxing", extra={})
    assert writer.mission_counts(course) is None


def test_note_unsupported_counts_chapter_quizzes():
    writer = _writer(apply=False)
    course = Course(id="265948593", name="石文化", platform="chaoxing", extra={})

    writer._note_unsupported(course, {"jobid": "work-1", "type": "workid"})
    writer._note_unsupported(course, {"jobid": "work-2", "type": "workid"})
    writer._note_unsupported(course, {"jobid": "t-1", "type": "discussion"})
    # 没有 jobid 的节点根本不是任务点，不该计数
    writer._note_unsupported(course, {"type": "video", "objectId": "obj-1"})
    # 视频节点的 property 子对象有 jobid 和 type=".mp4"，不是任务点本身
    writer._note_unsupported(course, {"jobid": "v-1", "type": ".mp4", "name": "1.1.mp4"})
    # 同一个 jobid 出现两次只算一次
    writer._note_unsupported(course, {"jobid": "work-1", "type": "workid"})

    assert writer.unsupported[str(course.id)] == {"章节测验": 2, "讨论": 1}


def test_plan_resets_the_unsupported_counter():
    writer = _writer(apply=False)
    writer.unsupported = {"old": {"章节测验": 9}}
    writer.iter_targets = lambda _c: iter([])

    assert writer.plan([Course(id="1", name="课", platform="chaoxing", extra={})]) == []
    assert writer.unsupported == {}


def test_send_raw_reports_a_captcha_block_instead_of_a_network_failure():
    writer = _writer(apply=True)

    def boom(url: str, **_kw: Any) -> Any:
        raise HttpError(url, "forbidden", 403)

    writer.http.get = boom

    got = writer._send_raw(
        "https://mooc1.chaoxing.com/mooc-ans/multimedia/log/a/201009813/abc123",
        headers={},
    )

    assert got is None
    assert "403" in writer.blocked
    assert writer._send_failed_reason() != SEND_FAILED
    assert "验证码" in writer._send_failed_reason()

# --------------------------------------------------------------------------- #
# 递进上报（实测最优：三步递进比一次跳满的通过率高得多）
# --------------------------------------------------------------------------- #


def test_ramp_sends_three_increasing_points_then_passes():
    """30% → 70% → 100% 三步递进；前两步 false、最后一步 true 才算通过。"""
    writer = _writer(apply=True)
    sent: list[str] = []
    sleeps: list[float] = []
    writer.sleep = sleeps.append
    replies = iter([{"isPassed": False}, {"isPassed": False}, {"isPassed": True}])
    writer.http.get_json = lambda *a, **k: {"duration": 300, "dtoken": "DT"}
    writer.http.get = lambda url, *a, **k: sent.append(url) or FakeResponse(next(replies))

    result = writer.report(_target(), ramp=True)
    assert [_param(u, "playingTime") for u in sent] == ["90", "210", "300"]
    assert result["requests"] == 3
    assert result["ok"] is True
    assert result["status"] == "passed"
    assert "第 3 步" in result["reason"]
    assert sleeps == [1.5, 1.5]


def test_ramp_stops_as_soon_as_a_step_passes():
    """第一步就通过了就别再补两步——请求是钱，也是痕迹。"""
    writer = _writer(apply=True)
    sent: list[str] = []
    sleeps: list[float] = []
    writer.sleep = sleeps.append
    writer.http.get_json = lambda *a, **k: {"duration": 300, "dtoken": "DT"}
    writer.http.get = lambda url, *a, **k: sent.append(url) or FakeResponse({"isPassed": True})

    result = writer.report(_target(), ramp=True)
    assert len(sent) == 1
    assert result["requests"] == 1
    assert result["status"] == "passed"
    assert sleeps == []


def test_ramp_dry_run_sends_nothing_and_says_the_chain():
    writer = _writer(apply=False)
    sent: list[str] = []
    writer.http.get_json = lambda *a, **k: {"duration": 300, "dtoken": "DT"}
    writer.http.get = lambda url, *a, **k: sent.append(url) or FakeResponse({"isPassed": True})

    result = writer.report(_target(), ramp=True)
    assert sent == []
    assert result["requests"] == 0
    assert result["status"] == "dry"
    assert "90 → 210 → 300" in result["reason"]


def test_ramp_gives_up_and_says_so_when_all_steps_fail():
    writer = _writer(apply=True)
    sent: list[str] = []
    writer.sleep = lambda _s: None
    writer.http.get_json = lambda *a, **k: {"duration": 300, "dtoken": "DT"}
    writer.http.get = lambda url, *a, **k: sent.append(url) or FakeResponse({"isPassed": False})

    result = writer.report(_target(), ramp=True)
    assert len(sent) == 3
    assert result["ok"] is False
    assert result["status"] == "unconfirmed"
    assert "已递进 3 步" in result["reason"]


def test_run_passes_ramp_down_to_every_target():
    """``run(ramp=True)`` 必须一路传到 ``report``，不能只写在 CLI 上。"""
    course = Course(id="111", name="高等数学", platform="chaoxing")
    work = [(course, _target()), (course, _target(jobid="job-2", object_id="obj-2"))]
    writer = _writer(apply=True)
    sent: list[str] = []
    sleeps: list[float] = []
    writer.sleep = sleeps.append
    writer.http.get_json = lambda *a, **k: {"duration": 300, "dtoken": "DT"}
    writer.http.get = lambda url, *a, **k: sent.append(url) or FakeResponse({"isPassed": True})

    rows = writer.run([course], work=work, ramp=True)
    assert len(rows) == 2
    assert all(row["status"] == "passed" for row in rows)
    assert len(sent) == 2  # 每步都第一步就过，所以两点各一发

def test_plan_skips_targets_already_passed() -> None:
    """已经通过的任务点不再列——再报一次一个点数都不涨，只白花请求。"""
    writer = _writer()
    course = Course(id="111", name="课", platform="chaoxing", extra={"class_id": "222"})
    targets = [
        _target(object_id="obj-1", jobid="job-1", passed=True),
        _target(object_id="obj-2", jobid="job-2"),
    ]
    writer.iter_targets = lambda _c: iter(targets)

    got = [t.object_id for _c, t in writer.plan([course])]
    assert got == ["obj-2"]


def test_plan_puts_untouched_videos_before_dead_ones() -> None:
    """``--limit`` 截断时先砍没救的：没看过、没抓拍的排最前，死态排最后。"""
    writer = _writer()
    course = Course(id="111", name="课", platform="chaoxing", extra={"class_id": "222"})
    targets = [
        _target(object_id="dead", jobid="job-1", att_duration=300, play_time_ms=300_000),
        _target(object_id="capture", jobid="job-2", face_capture="ENC", att_duration=300),
        _target(object_id="half", jobid="job-3", att_duration=300, play_time_ms=120_000),
        _target(object_id="fresh", jobid="job-4", att_duration=300),
    ]
    writer.iter_targets = lambda _c: iter(targets)

    got = [t.object_id for _c, t in writer.plan([course])]
    assert got == ["fresh", "half", "dead", "capture"]


def test_plan_limit_counts_only_listed_targets() -> None:
    """跳过已通过的点之后，``--limit`` 数的是「真要报的」，不是被跳过的。"""
    writer = _writer()
    course = Course(id="111", name="课", platform="chaoxing", extra={"class_id": "222"})
    targets = [
        _target(object_id="done-1", jobid="job-1", passed=True),
        _target(object_id="obj-2", jobid="job-2"),
        _target(object_id="done-2", jobid="job-3", passed=True),
        _target(object_id="obj-4", jobid="job-4"),
    ]
    writer.iter_targets = lambda _c: iter(targets)

    got = [t.object_id for _c, t in writer.plan([course], limit=2)]
    assert got == ["obj-2", "obj-4"]
