"""系统与设置。"""

from __future__ import annotations

import sys
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ... import __version__
from ... import diagnostics, login
from ...model import SCHEMA_VERSION
from ...providers import HttpSettings, discover, metas
from .. import operations
from ..state import AppState
from .deps import get_state, settings_view

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health", summary="存活检查")
def health(state: AppState = Depends(get_state)) -> dict[str, Any]:
    return {
        "ok": True,
        "version": __version__,
        "schema": SCHEMA_VERSION,
        "uptime_s": state.uptime_s,
    }


@router.get("/version", summary="版本信息")
def version(state: AppState = Depends(get_state)) -> dict[str, Any]:
    return {
        "version": __version__,
        "schema": SCHEMA_VERSION,
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "frozen": bool(getattr(sys, "frozen", False)),
    }


@router.get("/platforms", summary="支持的平台")
def platforms(state: AppState = Depends(get_state)) -> dict[str, Any]:
    discover()
    return {
        "platforms": [m.to_dict() for m in metas()],
        "sources": list(login.SOURCES),
        "task_kinds": operations.kinds(),
    }


@router.get("/doctor", summary="环境自检")
def doctor(state: AppState = Depends(get_state)) -> dict[str, Any]:
    items = diagnostics.checks(state.workspace, config=state.config())
    return {"checks": items, "failed": len(diagnostics.failed_names(items))}


@router.get("/settings", summary="读设置")
def read_settings(state: AppState = Depends(get_state)) -> dict[str, Any]:
    return settings_view(state, state.config())


class SettingsPatch(BaseModel):
    """局部更新。只给想改的字段，其余保持不动。"""

    http: dict[str, Any] | None = Field(default=None, description="timeout / delay / retries / user_agent")
    ui: dict[str, Any] | None = Field(default=None, description="theme / alert_days")
    keep_raw: bool | None = None


@router.patch("/settings", summary="改设置")
def patch_settings(patch: SettingsPatch, state: AppState = Depends(get_state)) -> dict[str, Any]:
    config = state.config()
    if patch.http:
        merged = config.http.to_dict()
        merged.update({k: v for k, v in patch.http.items() if v is not None})
        config.http = HttpSettings.from_dict(merged)
    if patch.ui:
        config.ui.update({k: v for k, v in patch.ui.items() if v is not None})
    if patch.keep_raw is not None:
        config.keep_raw = bool(patch.keep_raw)
    state.save_config(config)
    return settings_view(state, config)
