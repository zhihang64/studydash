"""工作区与配置：路径解析、读写、脱敏。

一个 **工作区** 就是你的个人数据目录，长这样::

    studydash/
      config.json        账号与抓取设置（含 Cookie，别提交）
      data/
        progress.json    聚合结果（可复现的中间产物）
        manual.json      手动登记的数据（可选）
        raw/*.json       probe 抓到的原始响应（排查用）
      out/
        dashboard.html   可以直接双击打开的自包含看板

工作区位置按这个优先级确定：``--home`` 参数 > ``STUDYDASH_HOME`` 环境变量 >
打包成 exe 时 exe 所在目录 > 当前目录。

**配置里只有 Cookie，没有账号密码。** 这是刻意的：项目不实现任何登录流程，
你在浏览器里登录完把 Cookie 贴过来即可。好处是这里永远不会保存你的密码，
也不需要跟着平台的登录加密算法改版。
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .model import SCHEMA_VERSION
from .providers import AccountConfig, HttpSettings

__all__ = ["Workspace", "Config", "CONFIG_NAME", "ENV_HOME", "atomic_write_json"]

CONFIG_NAME = "config.json"
ENV_HOME = "STUDYDASH_HOME"

#: Cookie 这类敏感字段在导出时的替换文本
REDACTED = "<已脱敏>"

_SENSITIVE_KEYS = ("cookie", "token", "password", "secret", "authorization")


# --------------------------------------------------------------------------- #
# 工作区
# --------------------------------------------------------------------------- #

@dataclass
class Workspace:
    """所有路径的唯一出口。"""

    root: Path

    @classmethod
    def resolve(cls, home: str | os.PathLike[str] | None = None) -> "Workspace":
        if home:
            return cls(Path(home).expanduser().resolve())
        env = os.environ.get(ENV_HOME)
        if env:
            return cls(Path(env).expanduser().resolve())
        if getattr(sys, "frozen", False):
            return cls(Path(sys.executable).resolve().parent)
        return cls(Path.cwd().resolve())

    # -- 目录 -------------------------------------------------------------- #

    @property
    def config_path(self) -> Path:
        return self.root / CONFIG_NAME

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def out_dir(self) -> Path:
        return self.root / "out"

    @property
    def progress_path(self) -> Path:
        return self.data_dir / "progress.json"

    @property
    def dashboard_path(self) -> Path:
        return self.out_dir / "dashboard.html"

    def ensure(self) -> "Workspace":
        for path in (self.data_dir, self.raw_dir, self.out_dir):
            path.mkdir(parents=True, exist_ok=True)
        return self

    def find_template(self, explicit: str | os.PathLike[str] | None = None) -> Path:
        """找渲染模板。

        查找顺序刻意区分「源码 / 已安装运行」和「打包运行」：模板随包分发
        （``src/studydash/templates``），所以先看包内；工作区里放一份同名
        文件即可覆盖皮肤；打包后包内那份落在 ``_MEIPASS`` 里，工作区仍在
        exe 旁边，两者必须分开找。
        """
        here = Path(__file__).resolve()
        candidates: list[Path] = []
        if explicit:
            candidates.append(Path(explicit))
        candidates.append(here.parent / "templates" / "dashboard.html")
        candidates.append(self.root / "templates" / "dashboard.html")
        candidates.append(here.parents[2] / "templates" / "dashboard.html")
        bundle = getattr(sys, "_MEIPASS", None)
        if bundle:
            candidates.append(Path(bundle) / "templates" / "dashboard.html")
        for path in candidates:
            if path.is_file():
                return path
        raise FileNotFoundError(
            "找不到 templates/dashboard.html。候选位置："
            + "、".join(str(c) for c in candidates)
        )

    # -- 进度文件 ---------------------------------------------------------- #

    def load_progress(self) -> dict[str, Any] | None:
        return read_json(self.progress_path)

    def save_progress(self, payload: Mapping[str, Any]) -> Path:
        self.ensure()
        atomic_write_json(self.progress_path, payload)
        return self.progress_path


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #

@dataclass
class Config:
    """``config.json`` 的内存表示。"""

    schema_version: int = SCHEMA_VERSION
    accounts: list[AccountConfig] = field(default_factory=list)
    http: HttpSettings = field(default_factory=HttpSettings)
    ui: dict[str, Any] = field(default_factory=dict)
    #: 抓取时自动写进 data/raw/ 的原始响应留档开关
    keep_raw: bool = True

    # -- 读取 -------------------------------------------------------------- #

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Config":
        raw_accounts = data.get("accounts") or []
        accounts: list[AccountConfig] = []
        for item in raw_accounts:
            if isinstance(item, str):            # 简写：直接用平台名
                accounts.append(AccountConfig(platform=item))
            elif isinstance(item, Mapping):
                accounts.append(AccountConfig.from_dict(item))
        raw_ui = data.get("ui")
        try:
            schema = int(data.get("schema_version", SCHEMA_VERSION))
        except (TypeError, ValueError):
            schema = SCHEMA_VERSION
        return cls(
            schema_version=schema,
            accounts=accounts,
            http=HttpSettings.from_dict(data.get("http")),
            ui=dict(raw_ui) if isinstance(raw_ui, Mapping) else {},
            keep_raw=bool(data.get("keep_raw", True)),
        )

    @classmethod
    def load(cls, path: Path) -> "Config":
        data = read_json(path)
        if data is None:
            return cls()
        return cls.from_dict(data)

    def save(self, path: Path, *, redact: bool = False) -> Path:
        atomic_write_json(path, self.to_dict(redact=redact))
        return path

    # -- 输出 -------------------------------------------------------------- #

    def to_dict(self, *, redact: bool = False) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "$schema": "https://raw.githubusercontent.com/zhihang64/studydash/main/config.schema.json",
            "accounts": [a.to_dict(redact=redact) for a in self.accounts],
            "http": self.http.to_dict(),
            "ui": dict(self.ui),
            "keep_raw": self.keep_raw,
        }

    def redacted(self) -> dict[str, Any]:
        """把 Cookie 之类换成占位符，用于 ``config --show`` 和分享排查。"""
        raw = self.to_dict(redact=False)
        return _scrub(raw)

    # -- 便捷 -------------------------------------------------------------- #

    def enabled_accounts(self, only: Iterable[str] | None = None) -> list[AccountConfig]:
        wanted = {p.strip() for p in (only or []) if p and p.strip()}
        out = []
        for account in self.accounts:
            if not account.enabled:
                continue
            if wanted and account.platform not in wanted:
                continue
            out.append(account)
        return out

    def alert_days(self) -> float:
        try:
            return float(self.ui.get("alert_days", 3))
        except (TypeError, ValueError):
            return 3.0

    def theme(self) -> str:
        theme = str(self.ui.get("theme") or "auto").lower()
        return theme if theme in {"auto", "light", "dark"} else "auto"


# --------------------------------------------------------------------------- #
# JSON 读写
# --------------------------------------------------------------------------- #

def read_json(path: Path) -> Any:
    """读 JSON；文件不存在返回 ``None``，内容坏了抛 ``ValueError``。"""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(f"读不了 {path}: {exc}") from exc
    if not text.strip():
        return None
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{path} 不是合法 JSON: {exc}") from exc


def atomic_write_json(path: Path, data: Any) -> None:
    """先写同目录临时文件再 ``os.replace``。

    中途崩了不会留下半截 JSON —— 看板读到一个截断的 progress.json 比读不到更糟。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if any(s in str(key).lower() for s in _SENSITIVE_KEYS):
                out[key] = REDACTED if item else ""
            else:
                out[key] = _scrub(item)
        return out
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value
