"""章节学习次数（learning.py）的单元测试：计次解析、演练/真跑路径。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from studydash.http import HttpError
from studydash.learning import (
    EP_MONITOR,
    EP_STUDENT_STUDY,
    LearningCounter,
    VisitTarget,
    parse_setlog_url,
)
from studydash.model import Course
from studydash.providers.base import HttpSettings
from studydash.store import AccountConfig

SETLOG = "https://fystat-ans.chaoxing.com/log/setlog?courseid=1&clazzid=2&chapterid=3&fid=8888"

STUDY_HTML = f"""
<html><head>
<script type="text/javascript" src="{SETLOG}"></script>
</head><body>章节页</body></html>
"""


def _account() -> AccountConfig:
    return AccountConfig(platform="chaoxing", cookie="UID=1; fid=8888; _uid=10086")


def _counter(**kwargs) -> LearningCounter:
    kwargs.setdefault("sleep", lambda _s: None)
    return LearningCounter(_account(), HttpSettings(delay=0.0, retries=0), **kwargs)


class FakeHttp:
    def __init__(self, *, study: str = STUDY_HTML, status: int = 200, boom=None) -> None:
        self.study = study
        self.status = status
        self.boom = boom
        self.calls: list[str] = []

    def get(self, url, params=None, *, headers=None, note=""):
        self.calls.append(url)
        if self.boom is not None:
            raise self.boom
        body = self.study if url.startswith(EP_STUDENT_STUDY) else ""
        return SimpleNamespace(status=self.status, content=body.encode("utf-8"))


def _course() -> Course:
    return Course(
        id="266688496",
        name="课程 A",
        platform="chaoxing",
        extra={"class_id": "151567127", "cpi": "406429824"},
    )


def _target(**kwargs) -> VisitTarget:
    base = {
        "course_id": "266688496",
        "course_name": "课程 A",
        "clazz_id": "151567127",
        "cpi": "406429824",
        "chapter_id": "1001",
        "chapter_name": "第一章",
    }
    base.update(kwargs)
    return VisitTarget(**base)


# --------------------------------------------------------------------------- #
# 计次地址
# --------------------------------------------------------------------------- #


def test_parse_setlog_url_extracts_src() -> None:
    assert parse_setlog_url(STUDY_HTML) == SETLOG


def test_parse_setlog_url_handles_single_quotes() -> None:
    html = f"<script src='{SETLOG}'></script>"
    assert parse_setlog_url(html) == SETLOG


def test_parse_setlog_url_returns_empty_when_missing() -> None:
    assert parse_setlog_url("<html><body>没有计次脚本</body></html>") == ""


# --------------------------------------------------------------------------- #
# 单次访问
# --------------------------------------------------------------------------- #


def test_visit_dry_run_sends_nothing() -> None:
    counter = _counter()
    fake = FakeHttp()
    counter.http = fake
    result = counter.visit(_target())
    assert result["status"] == "dry"
    assert result["ok"] is None
    assert result["requests"] == 0
    assert fake.calls == []


def test_visit_apply_hits_count_and_heartbeats() -> None:
    counter = _counter(apply=True, interval=0.0)
    fake = FakeHttp()
    counter.http = fake
    result = counter.visit(_target())
    assert result["status"] == "passed"
    assert result["ok"] is True
    assert "已计次" in result["reason"]
    assert len(fake.calls) == 4  # 章节页 + 计次 + 两次心跳
    assert fake.calls[0].startswith(EP_STUDENT_STUDY)
    assert fake.calls[1] == SETLOG
    assert fake.calls[2].startswith(EP_MONITOR)
    assert fake.calls[3].startswith(EP_MONITOR)


def test_visit_without_setlog_fails() -> None:
    counter = _counter(apply=True, interval=0.0)
    counter.http = FakeHttp(study="<html><body>无脚本</body></html>")
    result = counter.visit(_target())
    assert result["status"] == "failed"
    assert result["ok"] is False
    assert "计次地址" in result["reason"]


def test_visit_bad_status_fails() -> None:
    counter = _counter(apply=True, interval=0.0)
    counter.http = FakeHttp(status=404)
    result = counter.visit(_target())
    assert result["status"] == "failed"
    assert "HTTP 404" in result["reason"]


def test_visit_reports_login_gate() -> None:
    counter = _counter(apply=True, interval=0.0)
    counter.blocked = "登录态失效"
    result = counter.visit(_target())
    assert result["status"] == "failed"
    assert result["reason"] == "登录态失效"


def test_heartbeat_failure_does_not_fail_the_round() -> None:
    counter = _counter(apply=True, interval=0.0)

    class HalfBroken(FakeHttp):
        def get(self, url, params=None, *, headers=None, note=""):
            if url.startswith(EP_MONITOR):
                self.calls.append(url)
                raise HttpError(url, "超时")
            return super().get(url, params, headers=headers, note=note)

    fake = HalfBroken()
    counter.http = fake
    result = counter.visit(_target())
    assert result["status"] == "passed"
    assert result["ok"] is True


def test_interval_is_slept_between_heartbeats() -> None:
    slept: list[float] = []
    counter = _counter(apply=True, interval=30.0, sleep=slept.append)
    counter.http = FakeHttp()
    counter.visit(_target())
    assert slept == [30.0]


# --------------------------------------------------------------------------- #
# 清点与批量
# --------------------------------------------------------------------------- #


def test_iter_targets_skips_non_numeric_chapter(monkeypatch) -> None:
    counter = _counter()
    monkeypatch.setattr(
        counter.provider,
        "_chapters_of",
        lambda _c: [("1001", "第一章"), ("abc", "坏章节"), ("1002", "第二章")],
    )
    targets = list(counter.iter_targets(_course()))
    assert [t.chapter_id for t in targets] == ["1001", "1002"]
    assert targets[0].clazz_id == "151567127"
    assert targets[0].cpi == "406429824"


def test_plan_respects_limit(monkeypatch) -> None:
    counter = _counter()
    monkeypatch.setattr(
        counter,
        "iter_targets",
        lambda _c: iter([_target(), _target(chapter_id="1002", chapter_name="第二章")]),
    )
    assert len(counter.plan([_course()], limit=1)) == 1
    assert len(counter.plan([_course()])) == 2


def test_run_repeats_times_and_reports_progress(monkeypatch) -> None:
    counter = _counter(apply=True, interval=0.0)
    counter.http = FakeHttp()
    steps: list[tuple[int, int, str]] = []
    results = counter.run(
        [_course()],
        times=2,
        work=[(_course(), _target())],
        on_step=lambda index, total, target: steps.append((index, total, target.chapter_id)),
    )
    assert len(results) == 2
    assert all(row["status"] == "passed" for row in results)
    assert steps == [(1, 2, "1001"), (2, 2, "1001")]


def test_run_stops_when_on_step_raises(monkeypatch) -> None:
    counter = _counter(apply=True, interval=0.0)
    counter.http = FakeHttp()

    def boom(_index, _total, _target):
        raise RuntimeError("取消")

    with pytest.raises(RuntimeError):
        counter.run([_course()], times=3, work=[(_course(), _target())], on_step=boom)
