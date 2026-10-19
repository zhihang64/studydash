"""任务：建、查、取消、订阅进度。

前端有两条路读进度，读的是同一份数据：轮询 ``GET /api/tasks/{id}``，或者订阅
``GET /api/tasks/{id}/events`` 这条 SSE 流。SSE 快、省请求，但公司网络里偶尔会被
代理掐掉；轮询慢但几乎哪都能用。两条都提供，前端按情况选。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import operations
from ..state import AppState
from .deps import get_state

router = APIRouter(prefix="/api", tags=["tasks"])

#: SSE 心跳间隔（秒）。中间层（反代、杀软的网络过滤）常在 30~60 秒无数据时断流。
KEEPALIVE_SECONDS = 15.0
#: SSE 快照比对间隔（秒）
POLL_SECONDS = 0.4


class TaskRequest(BaseModel):
    kind: str = Field(description="任务类型，见 GET /api/platforms 的 task_kinds")
    options: dict[str, Any] = Field(default_factory=dict)


@router.post("/tasks", status_code=202, summary="建一个任务")
def create_task(payload: TaskRequest, state: AppState = Depends(get_state)) -> dict[str, Any]:
    runner = operations.resolve(payload.kind)
    task = state.tasks.create(payload.kind, payload.options, runner, state=state)
    return {"task": task.to_dict(full=False)}


@router.get("/tasks", summary="最近的任务")
def list_tasks(state: AppState = Depends(get_state)) -> dict[str, Any]:
    return {"tasks": [t.to_dict(full=False) for t in state.tasks.list()]}


@router.get("/tasks/{task_id}", summary="单个任务")
def get_task(task_id: str, state: AppState = Depends(get_state)) -> dict[str, Any]:
    return state.tasks.snapshot(task_id)


@router.post("/tasks/{task_id}/cancel", summary="取消任务")
def cancel_task(task_id: str, state: AppState = Depends(get_state)) -> dict[str, Any]:
    return state.tasks.cancel(task_id)


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get("/tasks/{task_id}/events", summary="任务进度（SSE）")
def task_events(task_id: str, state: AppState = Depends(get_state)) -> StreamingResponse:
    task = state.tasks.require(task_id)

    async def stream():
        seen = 0
        last_head: dict[str, Any] | None = None
        idle = 0.0
        while True:
            snapshot = task.to_dict(full=True)
            logs = snapshot.pop("logs", [])
            while seen < len(logs):
                yield _sse("log", logs[seen])
                seen += 1
                idle = 0.0

            head = {k: v for k, v in snapshot.items() if k != "result"}
            if head != last_head:
                yield _sse("task", snapshot)
                last_head = head
                idle = 0.0

            if task.finished:
                yield _sse("done", snapshot)
                return

            await asyncio.sleep(POLL_SECONDS)
            idle += POLL_SECONDS
            if idle >= KEEPALIVE_SECONDS:
                # 注释行不触发事件，只为了让连接别被中间层当成空闲掐掉
                yield ": keep-alive\n\n"
                idle = 0.0

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
