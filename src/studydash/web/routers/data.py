"""学业数据：最近一次抓取的 payload，以及演示数据。

抓取本身是任务（``POST /api/tasks`` 的 ``fetch``），这里只负责把**结果**发出去。
把「跑」和「读」分开，前端才能在抓取还在跑的时候继续显示上一次的看板，
而不是整页变成加载中。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ...aggregate import build_payload, course_key, demo_courses, filter_payload
from ..errors import ApiError
from ..state import AppState
from .deps import get_state

router = APIRouter(prefix="/api", tags=["data"])


class HiddenCourseIn(BaseModel):
    """只收一个课程键（``平台:课程id:学期id``），课程信息以已抓取的数据为准。"""

    key: str


def _require_payload(state: AppState) -> dict[str, Any]:
    payload = state.workspace.load_progress()
    if not isinstance(payload, dict) or not payload:
        raise ApiError(
            "not_found",
            "还没有抓取过任何数据",
            hint="先登录一个账号并抓取，或者点「先看演示数据」。",
        )
    return payload


def _visible(state: AppState, payload: dict[str, Any]) -> dict[str, Any]:
    """按配置里的隐藏列表过滤。

    过滤发生在**读取**这一层：``progress.json`` 永远是完整的一份，所以取消隐藏
    不需要重新抓一遍 —— 平台那边可不会因为我们点了个按钮就去重下一遍数据。
    """
    return filter_payload(payload, state.config().hidden_courses())


def _hidden_reply(payload: dict[str, Any]) -> dict[str, Any]:
    hidden = payload.get("hidden") or []
    return {"hidden": hidden, "count": len(hidden)}


def _known_keys(payload: dict[str, Any]) -> set[str]:
    return {course_key(c) for c in payload.get("courses") or [] if isinstance(c, dict)}


@router.get("/payload", summary="最近一次的看板数据（已排除隐藏课程）")
def get_payload(state: AppState = Depends(get_state)) -> dict[str, Any]:
    return _visible(state, _require_payload(state))


@router.get("/payload/meta", summary="看板数据的元信息（首屏用）")
def get_payload_meta(state: AppState = Depends(get_state)) -> dict[str, Any]:
    payload = _visible(state, _require_payload(state))
    meta = payload.get("meta") or {}
    return {
        "generated_at": meta.get("generated_at"),
        "mode": meta.get("mode"),
        "warnings": meta.get("warnings") or [],
        "overview": payload.get("overview") or {},
        "hidden_count": meta.get("hidden_count", 0),
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


@router.get("/hidden", summary="已隐藏的课程")
def list_hidden(state: AppState = Depends(get_state)) -> dict[str, Any]:
    return _hidden_reply(_visible(state, _require_payload(state)))


@router.post("/hidden", summary="隐藏一门课程")
def hide_course(
    body: HiddenCourseIn, state: AppState = Depends(get_state)
) -> dict[str, Any]:
    key = (body.key or "").strip()
    if not key:
        raise ApiError("invalid_request", "缺少课程键", detail={"field": "key"})

    full = _require_payload(state)
    if key not in _known_keys(full):
        raise ApiError(
            "not_found",
            f"没有这门课：{key}",
            hint="先刷新一次数据；课程键由后端生成（点课程卡上的「隐藏」即可）。",
        )

    cfg = state.config()
    hidden = cfg.hidden_courses()
    if key not in hidden:
        hidden.append(key)
        cfg.set_hidden_courses(hidden)
        state.save_config(cfg)
    return _hidden_reply(_visible(state, full))


@router.delete("/hidden/{key:path}", summary="取消隐藏")
def unhide_course(key: str, state: AppState = Depends(get_state)) -> dict[str, Any]:
    full = _require_payload(state)
    cfg = state.config()
    hidden = cfg.hidden_courses()
    if key in hidden:
        hidden.remove(key)
        cfg.set_hidden_courses(hidden)
        state.save_config(cfg)
    # 已经不在隐藏列表里也返回 200：这是个幂等操作，前端不需要处理「重复取消」。
    return _hidden_reply(_visible(state, full))
