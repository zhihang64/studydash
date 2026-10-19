"""学业数据：最近一次抓取的 payload，以及演示数据。

抓取本身是任务（``POST /api/tasks`` 的 ``fetch``），这里只负责把**结果**发出去。
把「跑」和「读」分开，前端才能在抓取还在跑的时候继续显示上一次的看板，
而不是整页变成加载中。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from ...aggregate import build_payload, demo_courses
from ..errors import ApiError
from ..state import AppState
from .deps import get_state

router = APIRouter(prefix="/api", tags=["data"])


def _require_payload(state: AppState) -> dict[str, Any]:
    payload = state.workspace.load_progress()
    if not isinstance(payload, dict) or not payload:
        raise ApiError(
            "not_found",
            "还没有抓取过任何数据",
            hint="先登录一个账号并抓取，或者点「先看演示数据」。",
        )
    return payload


@router.get("/payload", summary="最近一次的看板数据")
def get_payload(state: AppState = Depends(get_state)) -> dict[str, Any]:
    return _require_payload(state)


@router.get("/payload/meta", summary="看板数据的元信息（首屏用）")
def get_payload_meta(state: AppState = Depends(get_state)) -> dict[str, Any]:
    payload = _require_payload(state)
    meta = payload.get("meta") or {}
    return {
        "generated_at": meta.get("generated_at"),
        "mode": meta.get("mode"),
        "warnings": meta.get("warnings") or [],
        "overview": payload.get("overview") or {},
    }


@router.post("/payload/demo", summary="用内置演示数据生成一份看板")
def make_demo(state: AppState = Depends(get_state)) -> dict[str, Any]:
    config = state.config()
    courses, accounts = demo_courses(alert_days=config.alert_days())
    payload = build_payload(courses, accounts, config, mode="demo")
    state.workspace.save_progress(payload)
    overview = payload.get("overview") or {}
    return {
        "mode": "demo",
        "summary": {
            "courses": int(overview.get("courses") or 0),
            "pending": int(overview.get("pending") or 0),
            "overdue": int(overview.get("overdue") or 0),
            "avg_progress": overview.get("avg_progress", 0),
        },
    }
