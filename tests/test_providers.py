"""适配器注册表与各内置适配器的静态约束。

这里不联网，只检查「接口层面的承诺」是否被守住：每个平台都声明了只读查询
端点、没有哪个适配器偷偷接受账号密码、注册表不会被坏 meta 污染。
"""

from __future__ import annotations

import pytest

from studydash.providers import (
    REGISTRY,
    AccountConfig,
    AuthError,
    HttpSettings,
    Provider,
    ProviderError,
    ProviderMeta,
    build,
    discover,
    get,
    ids,
    metas,
    register,
)
from studydash.http import validate_query_endpoints

BUILTIN = {"chaoxing", "icourse163", "educoder", "zhihuishu", "manual", "demo"}
VALID_STATUS = {"stable", "beta", "unverified", "broken", "planned"}


# --------------------------------------------------------------------------- #
# 注册表
# --------------------------------------------------------------------------- #

def test_all_builtin_providers_are_discovered():
    discover(reload=True)
    assert BUILTIN <= set(ids())


def test_each_provider_meta_is_self_consistent():
    for platform in ids():
        cls = get(platform)
        assert cls is not None, platform
        meta = cls.meta
        assert meta.id == platform, f"{platform} 的 meta.id 与注册键不一致"
        assert meta.name and meta.short, platform
        assert meta.status in VALID_STATUS, f"{platform} 的 status 取值非法: {meta.status}"
        assert meta.color.startswith("#") and len(meta.color) == 7, platform


def test_provider_ids_match_their_module_names():
    """约定：模块名 = 平台 id。看文件名就能猜到怎么配，也让 discover 的
    「扫包内所有模块」策略不会漏东西。"""
    import pathlib

    from studydash import providers

    pkg = pathlib.Path(providers.__file__).parent
    modules = {
        p.stem
        for p in pkg.glob("*.py")
        if p.stem not in {"__init__", "base", "rpc"}
    }
    assert modules == BUILTIN


def test_unknown_platform_is_none_or_raises():
    discover()
    assert get("根本没有这个平台") is None
    assert "根本没有这个平台" not in ids()
    # get() 给 None，build() 必须拦下来。抛 ProviderError 是最希望的那条路
    # （带上可读信息，CLI 直接打给用户看），KeyError/ValueError 也算拦住了。
    with pytest.raises((KeyError, ValueError, ProviderError)):
        build(AccountConfig(platform="根本没有这个平台"))


def test_metas_sorted_stable_first():
    discover(reload=True)
    order = [m.status for m in metas()]
    # stable 必须全部排在 beta/unverified 前面
    assert order == sorted(order, key=lambda s: 0 if s == "stable" else 1)


def test_metas_enabled_only_filters_planned():
    for meta in metas(enabled_only=True):
        assert meta.status != "planned"


# --------------------------------------------------------------------------- #
# 注册守卫
# --------------------------------------------------------------------------- #

class _Probe(Provider):
    """注册守卫的试验品。meta 由 fixture 按用例定制，跑完立刻从表里删掉。"""

    meta = ProviderMeta(id="guardprobe", name="守卫探针", short="探针", status="beta")

    def fetch(self):  # pragma: no cover - 守卫用例不会调到这儿
        return []


@pytest.fixture
def probe_registry():
    """注册守卫会往全局 REGISTRY 里写，用例之间必须擦干净。"""
    before = dict(REGISTRY)
    yield REGISTRY
    REGISTRY.clear()
    REGISTRY.update(before)


def test_register_rejects_an_empty_or_underscore_id(probe_registry):
    for bad in ("", "  ", "_private", "__dup"):
        with pytest.raises(ValueError):
            register(
                type(
                    "P",
                    (Provider,),
                    {
                        "meta": ProviderMeta(id=bad, name="x", short="x"),
                        "fetch": lambda self: [],
                    },
                )
            )


def test_register_rejects_a_duplicate_id(probe_registry):
    """两个适配器抢同一个平台 id，只会让用户拿到随机的那一个。"""
    with pytest.raises(ValueError) as exc:
        register(
            type(
                "P",
                (Provider,),
                {
                    "meta": ProviderMeta(id="chaoxing", name="冒充超星", short="冒"),
                    "fetch": lambda self: [],
                },
            )
        )
    assert "chaoxing" in str(exc.value)


def test_register_returns_the_class_so_it_can_be_used_as_a_decorator(probe_registry):
    got = register(_Probe)
    assert got is _Probe
    assert probe_registry["guardprobe"] is _Probe


# --------------------------------------------------------------------------- #
# 各适配器的只读承诺
# --------------------------------------------------------------------------- #

def test_every_provider_declares_valid_query_endpoints():
    """适配器声明的 POST 白名单必须过 http 层那套「看着像不像查询」的卡。"""
    for platform in ids():
        cls = get(platform)
        declared = list(getattr(cls, "QUERY_ENDPOINTS", ()) or ())
        # 能过校验就是好的；校验本身会抛 ReadOnlyError
        validate_query_endpoints(declared)


def test_no_provider_accepts_a_password():
    """全项目的策略是「只用 Cookie，不收账号密码」。

    收密码意味着要替用户保管凭据，也意味着可以自动化登录——两条都被刻意
    排除掉了。这里连同名参数一起禁掉。
    """
    import inspect

    for platform in ids():
        cls = get(platform)
        assert cls.meta.auth in {"cookie", "none"}, f"{platform} 的 auth 不是 cookie/none"
        for name, param in inspect.signature(cls.__init__).parameters.items():
            assert "password" not in name.lower(), f"{platform}.__init__ 收了一个密码参数"
            assert "passwd" not in name.lower(), f"{platform}.__init__ 收了一个密码参数"


def test_providers_that_need_auth_fail_politely_without_a_cookie():
    discover(reload=True)
    for platform in ids():
        cls = get(platform)
        if cls.meta.auth != "cookie":
            continue
        provider = cls(AccountConfig(platform=platform, cookie=""))
        with pytest.raises((AuthError, ProviderError)):
            provider.fetch()


def test_demo_provider_needs_no_auth_and_returns_data():
    from studydash.providers.demo import DemoProvider

    provider = DemoProvider(AccountConfig(platform="demo"), HttpSettings(delay=0))
    courses = provider.fetch()
    assert courses
    assert {c.platform for c in courses} >= {"chaoxing", "icourse163", "manual"}
    assert all(c.name for c in courses)


def test_demo_provider_is_stable_across_runs():
    """演示数据的截止时间是相对当下算的，不该出现「第二天打开全过期」。"""
    from studydash.providers.demo import DemoProvider

    provider = DemoProvider(AccountConfig(platform="demo"), HttpSettings(delay=0))
    first = {a.title for c in provider.fetch() for a in c.assignments}
    second = {a.title for c in provider.fetch() for a in c.assignments}
    assert first == second


def test_manual_provider_reads_a_json_file(tmp_path):
    """手动导入是目前唯一 stable 的作业来源，值得钉住。"""
    import json

    from studydash.providers.manual import ManualProvider

    path = tmp_path / "manual.json"
    path.write_text(
        json.dumps(
            {
                "courses": [
                    {
                        "id": "m1",
                        "name": "形势与政策（线下）",
                        "platform": "school",
                        "platform_name": "教务系统",
                        "assignments": [
                            {"id": "w1", "title": "课程论文提纲", "kind": "homework",
                             "due": "2026-10-24 18:00", "status": "pending"}
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    courses = ManualProvider(
        AccountConfig(platform="manual", options={"path": str(path)}),
        HttpSettings(delay=0),
    ).fetch()

    assert len(courses) == 1
    course = courses[0]
    assert course.name == "形势与政策（线下）"
    assert course.platform == "school"
    assert course.platform_name == "教务系统"
    assert course.assignments[0].title == "课程论文提纲"
    assert course.assignments[0].due is not None


def test_manual_provider_reports_a_missing_file_clearly(tmp_path):
    from studydash.providers.manual import ManualProvider

    provider = ManualProvider(
        AccountConfig(platform="manual", options={"path": str(tmp_path / "没有.json")}),
        HttpSettings(delay=0),
    )
    with pytest.raises(ProviderError) as exc:
        provider.fetch()
    assert "没有.json" in str(exc.value)


def test_manual_provider_rejects_garbage_json_clearly(tmp_path):
    from studydash.providers.manual import ManualProvider

    path = tmp_path / "manual.json"
    path.write_text("{坏掉的", encoding="utf-8")
    provider = ManualProvider(
        AccountConfig(platform="manual", options={"path": str(path)}), HttpSettings(delay=0)
    )
    with pytest.raises(ProviderError) as exc:
        provider.fetch()
    assert "JSON" in str(exc.value)


# --------------------------------------------------------------------------- #
# 账号配置的脱敏
# --------------------------------------------------------------------------- #

def test_account_to_dict_redacts_the_cookie():
    import json

    account = AccountConfig(platform="chaoxing", label="我的", cookie="UID=真凭据")
    blob = json.dumps(account.to_dict(), ensure_ascii=False)
    assert "真凭据" not in blob
    assert "已设置" in blob
    assert account.to_dict(redact=False)["cookie"] == "UID=真凭据"


def test_account_display_falls_back_to_the_platform():
    assert AccountConfig(platform="chaoxing", label="").display == "chaoxing"
    assert AccountConfig(platform="chaoxing", label="小号").display == "小号"


def test_account_from_dict_tolerates_junk():
    assert AccountConfig.from_dict({}).platform == ""
    assert AccountConfig.from_dict("chaoxing").platform == "chaoxing"
    assert AccountConfig.from_dict({"platform": "chaoxing", "enabled": "no"}).enabled is False
