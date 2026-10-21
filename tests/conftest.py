"""测试公共夹具。

原则：**测试里不出现真实网络**。所有适配器相关的断言要么跑 demo 适配器，
要么往 ``aggregate(providers=...)`` 里注入假的 Provider。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from studydash.model import Assignment, Course, Progress
from studydash.providers import AccountConfig, HttpSettings, Provider, ProviderMeta
from studydash.store import Config, Workspace

#: 固定参考时间。所有涉及「还剩几天」的断言都钉在这个时刻上，
#: 否则测试会在跨零点时随机失败。
REF = datetime(2026, 3, 14, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
def ref() -> datetime:
    return REF


@pytest.fixture
def recent() -> datetime:
    """真实「现在」。

    聚合层内部自己调 ``now()``（``build_payload`` 不收 ``ref``），所以凡是
    经它的用例都必须以当下为基准。用固定的 ``REF`` 写这类断言等于埋雷：
    写用例那天「2 天后到期」是逾期之外的正常项，几个月后同一份数据
    全都变成已逾期，测试开始无理由地红。
    每个用例取一次，跨零点也不会在同一用例内漂移。
    """
    return datetime.now().astimezone()


@pytest.fixture
def workspace(tmp_path) -> Workspace:
    return Workspace(tmp_path).ensure()


@pytest.fixture
def config() -> Config:
    return Config(
        accounts=[
            AccountConfig(platform="chaoxing", label="我的超星", cookie="uid=1; _d=2"),
            AccountConfig(platform="icourse163", label="我的慕课", cookie="NTESSTUDYSI=3"),
            AccountConfig(platform="zhihuishu", label="已停用", enabled=False),
        ],
        http=HttpSettings(delay=0.0, timeout=1.0, retries=0),
        ui={"theme": "auto", "alert_days": 3},
    )


def make_assignment(
    aid: str,
    *,
    title: str = "",
    course: str = "",
    platform: str = "chaoxing",
    kind: str = "homework",
    status: str = "pending",
    due: datetime | None = None,
    score: float | None = None,
    max_score: float | None = None,
) -> Assignment:
    return Assignment(
        id=aid,
        title=title or f"作业 {aid}",
        platform=platform,
        course_id="c1",
        course=course or f"课程 {platform}",
        kind=kind,
        status=status,
        due=due,
        score=score,
        max_score=max_score,
    )


def make_course(
    cid: str,
    *,
    name: str = "",
    platform: str = "chaoxing",
    progress: list[Progress] | None = None,
    assignments: list[Assignment] | None = None,
) -> Course:
    return Course(
        id=cid,
        name=name or f"课程 {cid}",
        platform=platform,
        teacher="某老师",
        term="2025-2026-2",
        progress=progress if progress is not None else [Progress("任务点", 3, 4)],
        assignments=assignments or [],
    )


class FakeProvider(Provider):
    """可控的假适配器：用来验证聚合层的容错，而不碰网络。"""

    meta = ProviderMeta(id="faketest", name="假平台", short="假", status="stable")

    def __init__(
        self,
        courses=None,
        *,
        error: Exception | None = None,
        cookie: str = "",
        settings: HttpSettings | None = None,
    ):
        super().__init__(AccountConfig(platform="faketest", cookie=cookie), settings)
        self._courses = courses or []
        self._error = error

    def fetch(self) -> list[Course]:
        if self._error is not None:
            raise self._error
        return list(self._courses)


@pytest.fixture
def days(ref):
    """``days(1.5)`` -> 相对参考时间 1.5 天后的时刻。"""

    def _make(offset: float) -> datetime:
        return ref + timedelta(days=offset)

    return _make


@pytest.fixture
def payload_json(workspace):
    """跑一次真实 demo 流程，返回落盘的 payload（不联网）。"""
    import json

    from studydash.aggregate import build_payload
    from studydash.providers.demo import DemoProvider

    provider = DemoProvider(AccountConfig(platform="demo"), HttpSettings(delay=0.0))
    courses = provider.fetch()
    payload = build_payload(courses, [], Config(), mode="demo")
    path = workspace.save_progress(payload)
    return json.loads(path.read_text(encoding="utf-8"))
