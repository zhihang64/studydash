"""一次 ``studydash web`` 运行期的全部状态。

配置每次读盘，不做缓存：网页上刚登录完，紧接着的抓取必须看见那个新账号，
而把 ``Config`` 缓存在内存里正是这类「改了没生效」bug 的经典来源。配置只有几 KB，
重读的代价可以忽略。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import __version__
from ..store import Config, Workspace
from .errors import ApiError
from .security import new_token
from .tasks import TaskManager

__all__ = ["AppState", "boot_payload"]


def boot_payload(token: str, *, base_path: str = "") -> dict[str, Any]:
    """注入给前端的那一份启动参数。

    只有这三样东西：访问令牌、版本号、基路径。**不放任何账号信息**——
    它会原样出现在 HTML 源码里，用户随时可能截图分享。
    """
    return {"token": token, "version": __version__, "basePath": base_path}


@dataclass
class AppState:
    workspace: Workspace
    token: str = field(default_factory=new_token)
    started_at: float = field(default_factory=time.monotonic)
    tasks: TaskManager = field(default_factory=TaskManager)
    static_dir: Path | None = None
    requests: int = 0
    last_request: str = ""

    @classmethod
    def create(
        cls,
        home: str | Path | None = None,
        *,
        token: str = "",
        static_dir: str | Path | None = None,
    ) -> "AppState":
        return cls(
            workspace=Workspace.resolve(home),
            token=token or new_token(),
            static_dir=Path(static_dir) if static_dir else None,
        )

    # -- 配置 -------------------------------------------------------------- #

    def config(self) -> Config:
        """读配置。坏掉的配置当成空配置，而不是让整个服务报 500。"""
        try:
            return Config.load(self.workspace.config_path)
        except Exception:
            return Config()

    def save_config(self, cfg: Config) -> Path:
        path = self.workspace.config_path
        path.parent.mkdir(parents=True, exist_ok=True)
        cfg.save(path, redact=False)
        return path

    # -- 概览 -------------------------------------------------------------- #

    @property
    def uptime_s(self) -> float:
        return round(time.monotonic() - self.started_at, 3)

    # -- 前端资源 ---------------------------------------------------------- #

    def static_path(self) -> Path:
        """前端产物目录。

        ``--static-dir`` 指定的目录同样要过一遍检查：只把路径塞进字段、到用的时候
        才发现里面没有 ``index.html``，用户看到的会是一句「500 找不到文件」，而
        不是「你指错了目录」。宁可在第一次访问时就说清楚。
        """
        candidate = Path(self.static_dir) if self.static_dir is not None else (
            Path(__file__).resolve().parent / "static"
        )
        if (candidate / "index.html").is_file():
            return candidate
        raise ApiError(
            "internal",
            f"前端构建产物不存在：{candidate} 里没有 index.html",
            hint="在仓库根目录跑 `tools/build_web.ps1` 生成前端产物；"
                 "或者别传 `--static-dir`，用安装包自带的那份。",
            detail={"looked_in": str(candidate)},
        )

    def index_path(self) -> Path:
        return self.static_path() / "index.html"

    # -- 令牌 -------------------------------------------------------------- #

    def rotate_token(self) -> str:
        self.token = new_token()
        return self.token
