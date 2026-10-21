"""抓取层并发的守门测试：``map_concurrent`` / ``fetch_workers`` / 账号级并发。

并发最容易踩的两类坑，这里的用例就是围着它们转的：

1. **顺序**：结果必须按传入顺序回填。平台卡的顺序变了，用户会以为课程换班了。
2. **失败**：一个工作线程抛异常，不能让已经拿到的结果全部作废，也不能让异常
   静默消失。

另外守一条倒退：``fetch_workers <= 1`` 时必须走**同线程串行**分支，否则
``provider.http.get_text = fake`` 这类 monkeypatch 会在工作线程里失效。
"""

from __future__ import annotations

import threading
import time

import pytest

from studydash.aggregate import aggregate
from studydash.providers import AccountConfig, HttpSettings, ProviderError
from studydash.providers.base import (
    FETCH_WORKERS_MAX,
    FETCH_WORKERS_MIN,
    _clamp_workers,
    map_concurrent,
)
from studydash.store import Config

from .conftest import FakeProvider, make_course

# --------------------------------------------------------------------------- #
# map_concurrent
# --------------------------------------------------------------------------- #

def test_map_concurrent_keeps_input_order():
    """反着睡的任务也要按原序返回，否则平台卡顺序会随机漂移。"""

    def work(n: int) -> int:
        time.sleep((5 - n) * 0.02)          # 越靠前睡得越久
        return n * 10

    assert map_concurrent(work, [1, 2, 3, 4], workers=4) == [10, 20, 30, 40]


def test_map_concurrent_returns_none_for_a_failed_item_and_keeps_the_rest():
    """单个任务抛异常只挖掉自己那一格，不连累同批的其它结果。"""
    seen: list[tuple[int, BaseException]] = []

    def work(n: int) -> int:
        if n == 3:
            raise ValueError("第三条坏了")
        return n

    out = map_concurrent(work, [1, 2, 3, 4], workers=4, on_error=lambda i, e: seen.append((i, e)))

    assert out == [1, 2, None, 4]
    assert len(seen) == 1
    assert seen[0][0] == 3
    assert isinstance(seen[0][1], ValueError)


def test_map_concurrent_reraises_without_an_error_handler():
    def work(n: int) -> int:
        raise RuntimeError("炸了")

    with pytest.raises(RuntimeError):
        map_concurrent(work, [1], workers=2)


def test_map_concurrent_runs_serially_on_the_calling_thread_when_workers_is_one():
    """workers<=1 必须同线程跑完——单测把 ``self.http`` 换成假对象靠的就是这点。"""
    main = threading.get_ident()
    seen: list[int] = []

    def work(n: int) -> int:
        seen.append(threading.get_ident())
        return n

    assert map_concurrent(work, [1, 2, 3], workers=1) == [1, 2, 3]
    assert seen == [main, main, main]


def test_map_concurrent_handles_an_empty_iterable():
    assert map_concurrent(lambda n: n, [], workers=4) == []


def test_map_concurrent_really_uses_several_threads():
    main = threading.get_ident()
    lock = threading.Lock()
    threads: set[int] = set()

    def work(n: int) -> int:
        with lock:
            threads.add(threading.get_ident())
        time.sleep(0.02)
        return n

    map_concurrent(work, list(range(6)), workers=3)

    assert len(threads) > 1
    assert main not in threads


# --------------------------------------------------------------------------- #
# fetch_workers 配置
# --------------------------------------------------------------------------- #

def test_fetch_workers_defaults_to_serial():
    assert HttpSettings().fetch_workers == FETCH_WORKERS_MIN == 1


def test_fetch_workers_is_clamped_both_ways():
    assert HttpSettings(fetch_workers=0).fetch_workers == FETCH_WORKERS_MIN
    assert HttpSettings(fetch_workers=-9).fetch_workers == FETCH_WORKERS_MIN
    assert HttpSettings(fetch_workers=99).fetch_workers == FETCH_WORKERS_MAX
    assert _clamp_workers(4) == 4


def test_fetch_workers_survives_a_dict_round_trip():
    """``patch_settings`` 靠 to_dict/from_dict 合并，字段漏一处就会被静默吃掉。"""
    blob = HttpSettings(delay=0.1, fetch_workers=4).to_dict()

    assert blob["fetch_workers"] == 4
    assert HttpSettings.from_dict(blob).fetch_workers == 4


def test_from_dict_tolerates_junk_fetch_workers():
    assert HttpSettings.from_dict({"fetch_workers": "abc"}).fetch_workers == 1
    assert HttpSettings.from_dict({"fetch_workers": "3"}).fetch_workers == 3
    assert HttpSettings.from_dict({}).fetch_workers == 1


def test_provider_client_is_shared_on_the_owner_thread_only():
    """owner 线程拿到 ``self.http``（单测的 monkeypatch 才生效），别线程另起一份。"""
    provider = FakeProvider([make_course("c1")], settings=HttpSettings(fetch_workers=2))

    assert provider._client() is provider.http

    others: list[object] = []

    def grab() -> None:
        others.append(provider._client())

    map_concurrent(lambda _n: grab(), [0, 1], workers=2)

    assert others and all(c is not provider.http for c in others)


# --------------------------------------------------------------------------- #
# aggregate 的账号级并发
# --------------------------------------------------------------------------- #

class _SleepyProvider(FakeProvider):
    """记下自己在哪个线程被调用、睡了多久。"""

    def __init__(self, courses, *, delay: float, trace: list) -> None:
        super().__init__(courses)
        self._delay = delay
        self._trace = trace

    def fetch(self):
        self._trace.append(threading.get_ident())
        time.sleep(self._delay)
        return super().fetch()


def _cfg(*platforms: str, workers: int) -> Config:
    cfg = Config(accounts=[AccountConfig(platform=p, cookie="x=1") for p in platforms])
    cfg.http = HttpSettings(delay=0.0, timeout=1.0, retries=0, fetch_workers=workers)
    return cfg


def test_aggregate_keeps_platform_order_when_accounts_run_concurrently():
    """并发完成后必须按 config 里的账号顺序回填，不是按谁先跑完。"""
    trace: list[int] = []
    providers = {
        "chaoxing": _SleepyProvider([make_course("c1", platform="chaoxing")],
                                    delay=0.12, trace=trace),
        "icourse163": _SleepyProvider([make_course("c2", platform="icourse163")],
                                      delay=0.0, trace=trace),
        "educoder": _SleepyProvider([make_course("c3", platform="educoder")],
                                    delay=0.0, trace=trace),
    }

    courses, results = aggregate(
        _cfg("chaoxing", "icourse163", "educoder", workers=3),
        providers=providers,
    )

    assert [r.platform for r in results] == ["chaoxing", "icourse163", "educoder"]
    assert [c.name for c in courses] == ["课程 c1", "课程 c2", "课程 c3"]
    assert all(r.ok for r in results)
    assert len(set(trace)) > 1


def test_aggregate_serial_branch_stays_on_one_thread():
    """fetch_workers=1 时行为必须和改造之前逐字一致：同一线程、原序。"""
    trace: list[int] = []
    providers = {
        "chaoxing": _SleepyProvider([make_course("c1", platform="chaoxing")],
                                    delay=0.0, trace=trace),
        "icourse163": _SleepyProvider([make_course("c2", platform="icourse163")],
                                      delay=0.0, trace=trace),
    }

    _, results = aggregate(_cfg("chaoxing", "icourse163", workers=1), providers=providers)

    assert len(set(trace)) == 1
    assert [r.platform for r in results] == ["chaoxing", "icourse163"]


def test_aggregate_isolates_a_failure_inside_the_concurrent_branch():
    """一个平台在并发分支里炸掉，其余平台的课必须照常出来。"""
    providers = {
        "chaoxing": FakeProvider(error=ProviderError("接口改版了")),
        "icourse163": FakeProvider([make_course("c2", platform="icourse163", name="网络")]),
        "educoder": FakeProvider([make_course("c3", platform="educoder", name="实训")]),
    }

    courses, results = aggregate(
        _cfg("chaoxing", "icourse163", "educoder", workers=3), providers=providers
    )

    assert [c.name for c in courses] == ["网络", "实训"]
    assert {r.platform: r.ok for r in results} == {
        "chaoxing": False,
        "icourse163": True,
        "educoder": True,
    }


def test_aggregate_concurrent_branch_reports_an_unexpected_exception_per_account():
    providers = {
        "chaoxing": FakeProvider(error=ZeroDivisionError("适配器写错了个除法")),
        "manual": FakeProvider([make_course("m1", platform="manual", name="线下课")]),
    }

    courses, results = aggregate(_cfg("chaoxing", "manual", workers=2), providers=providers)

    assert [c.name for c in courses] == ["线下课"]
    assert {r.platform: r.ok for r in results} == {"chaoxing": False, "manual": True}


def test_aggregate_concurrent_workers_are_capped_by_the_account_count():
    """2 个账号配 fetch_workers=8，不能真的开 8 条线程去抢。"""
    providers = {
        "chaoxing": FakeProvider([make_course("c1", platform="chaoxing")]),
        "icourse163": FakeProvider([make_course("c2", platform="icourse163")]),
    }

    _, results = aggregate(_cfg("chaoxing", "icourse163", workers=8), providers=providers)

    assert [r.platform for r in results] == ["chaoxing", "icourse163"]


# --------------------------------------------------------------------------- #
# 适配器内部的并发也保序
# --------------------------------------------------------------------------- #

def test_provider_map_concurrent_defaults_to_the_configured_workers():
    """``Provider.map_concurrent`` 不传 workers 时读的就是配置里的值。"""
    provider = FakeProvider([], settings=HttpSettings(fetch_workers=4))
    main = threading.get_ident()
    threads: set[int] = set()
    lock = threading.Lock()

    def work(n: int) -> int:
        with lock:
            threads.add(threading.get_ident())
        time.sleep(0.02)
        return n

    assert provider.fetch_workers == 4
    assert provider.map_concurrent(work, [0, 1, 2, 3]) == [0, 1, 2, 3]
    assert len(threads) > 1
    assert main not in threads


def test_provider_map_concurrent_workers_one_matches_the_old_serial_behaviour():
    provider = FakeProvider([], settings=HttpSettings(fetch_workers=1))
    main = threading.get_ident()

    threads = [threading.get_ident() for _ in provider.map_concurrent(lambda n: n, [0, 1, 2])]

    assert threads == [main, main, main]


def test_every_builtin_provider_exposes_the_client_seam():
    """适配器只要还用 ``self.http`` 直连，并发就白改了——这里把口子钉住。"""
    from studydash.providers import discover

    for pid, cls in discover().items():
        assert hasattr(cls, "_client"), f"{pid} 缺少线程隔离的 _client()"
        assert hasattr(cls, "map_concurrent"), f"{pid} 缺少 map_concurrent()"
