"""Provider 注册表与发现。

内置适配器放在本包内，社区适配器可以通过 ``entry_points`` 以
``studydash.providers`` 组安装进来——也就是说第三方包只要在 pyproject 里声明

    [project.entry-points."studydash.providers"]
    my-university = "my_pkg.provider:CampusProvider"

就能被 studydash 直接识别，不需要改本仓库一行代码。
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Any, Iterable, Mapping

from .base import (  # noqa: F401 - 对外统一从这里导出
    AccountConfig,
    AuthError,
    HttpSettings,
    Provider,
    ProviderError,
    ProviderMeta,
    REGISTRY,
    RedirectedError,
    STATUS_LABEL,
    map_concurrent,
    register,
)

__all__ = [
    "REGISTRY",
    "register",
    "discover",
    "get",
    "metas",
    "build",
    "create",
    "AccountConfig",
    "HttpSettings",
    "Provider",
    "ProviderMeta",
    "ProviderError",
    "AuthError",
    "RedirectedError",
    "STATUS_LABEL",
    "map_concurrent",
    "ENTRY_POINT_GROUP",
]

ENTRY_POINT_GROUP = "studydash.providers"

_DISCOVERED = False


def discover(*, reload: bool = False) -> dict[str, type[Provider]]:
    """导入内置适配器 + 通过 entry_points 安装的第三方适配器。"""
    global _DISCOVERED
    if _DISCOVERED and not reload:
        return REGISTRY

    for _finder, name, _ispkg in pkgutil.iter_modules(__path__):
        if name in {"base", "__init__"} or name.startswith("_"):
            continue
        try:
            importlib.import_module(f"{__name__}.{name}")
        except Exception as exc:                            # noqa: BLE001
            # 单个适配器 import 失败不能拖垮整个程序
            print(f"! 适配器 {name} 载入失败: {exc}")

    try:
        from importlib.metadata import entry_points

        for ep in entry_points(group=ENTRY_POINT_GROUP):
            try:
                ep.load()
            except Exception as exc:                        # noqa: BLE001
                print(f"! 外部适配器 {ep.name} 载入失败: {exc}")
    except Exception:                                       # noqa: BLE001
        pass

    _DISCOVERED = True
    return REGISTRY


def get(platform: str) -> type[Provider] | None:
    discover()
    return REGISTRY.get(platform)


def metas(*, enabled_only: bool = False) -> list[ProviderMeta]:
    discover()
    out = [cls.meta for cls in REGISTRY.values()]
    if enabled_only:
        out = [m for m in out if m.status != "broken"]
    return sorted(out, key=lambda m: (m.status != "stable", m.id))


def build(
    account: AccountConfig,
    settings: HttpSettings | None = None,
    *,
    log: Any = None,
) -> Provider:
    """按账号配置实例化对应的适配器。"""
    discover()
    cls = REGISTRY.get(account.platform)
    if cls is None:
        known = ", ".join(sorted(REGISTRY)) or "（无）"
        raise ProviderError(
            f"未知平台 {account.platform!r}",
            hint=f"已装载的平台适配器: {known}",
        )
    return cls(account, settings, log=log)


def create(
    platform: str,
    *,
    cookie: str = "",
    label: str = "",
    options: Mapping[str, Any] | None = None,
    settings: HttpSettings | None = None,
    log: Any = None,
) -> Provider:
    return build(
        AccountConfig(
            platform=platform,
            label=label,
            cookie=cookie,
            options=dict(options or {}),
        ),
        settings,
        log=log,
    )


def ids() -> Iterable[str]:
    discover()
    return sorted(REGISTRY)


# 内置适配器在这里显式 import 一次，而不是只靠 discover() 里的 pkgutil 扫描。
#
# 两个原因，都是打包踩出来的：
#   1. PyInstaller 靠静态分析找依赖，扫不到 importlib.import_module(f"{__name__}.{name}")
#      拼出来的名字，这些模块不会被收进 exe；
#   2. 打包后 __path__ 在 FrozenImporter 上，pkgutil.iter_modules 拿不到文件列表，
#      扫描结果是空的 —— 表现就是 exe 里跑任何平台都报「未知平台 'demo'」。
#
# 显式 import 之后两条路都成立：源码运行照样能被 discover() 认到（模块已在
# sys.modules 里，不会重复注册），打包后注册表也是满的。
from . import (  # noqa: E402,F401
    chaoxing,
    chaoxing_stat,
    demo,
    icourse163,
    manual,
    zhihuishu,
)
