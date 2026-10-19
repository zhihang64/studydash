"""账号：查看、添加、改、删。

这里只管理「账号条目」，登录动作全在 ``tasks`` 里以 ``login_*`` 任务跑。
分开的理由：登录要几十秒，账号列表要立刻显示；把两者混在一个接口里，
用户点一下「登录」就得干等一个不知道进度的请求。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel

from ...providers import AccountConfig, discover, ids
from ..errors import ApiError
from ..state import AppState
from .deps import account_view, get_state, _last_status

router = APIRouter(prefix="/api", tags=["accounts"])


def _list(state: AppState) -> list[dict[str, Any]]:
    statuses = _last_status(state.workspace)
    return [account_view(a, statuses) for a in state.config().accounts]


@router.get("/accounts", summary="所有账号")
def list_accounts(state: AppState = Depends(get_state)) -> dict[str, Any]:
    return {"accounts": _list(state)}


class AccountCreate(BaseModel):
    platform: str
    label: str = ""
    enabled: bool = True
    #: 可以顺手把 Cookie 一起带上。**必须有这个字段**：pydantic 默认丢掉模型里没有的
    #: 键，于是「贴一份 Cookie 建账号」会静默返回 201 并丢弃 Cookie，用户以为绑上了，
    #: 抓取时才发现是空会话。宁可在接口层收下它。
    cookie: str = ""
    options: dict[str, Any] = {}


@router.post("/accounts", status_code=201, summary="加一个账号条目")
def create_account(payload: AccountCreate, state: AppState = Depends(get_state)) -> dict[str, Any]:
    discover()
    platform = (payload.platform or "").strip()
    known = set(ids())
    if platform not in known:
        raise ApiError(
            "invalid_request",
            f"未知平台：{platform!r}",
            hint="可用的是：" + ", ".join(sorted(known)),
            detail={"field": "platform", "available": sorted(known)},
        )
    label = (payload.label or "").strip() or "默认"
    config = state.config()
    if any(a.platform == platform and (a.label or "默认") == label for a in config.accounts):
        raise ApiError(
            "conflict",
            f"{platform} 下已经有了一个叫「{label}」的账号",
            hint="换个名字，或者直接对它重新登录。",
            detail={"field": "label"},
        )
    config.accounts.append(
        AccountConfig(
            platform=platform,
            label=label,
            enabled=bool(payload.enabled),
            cookie=(payload.cookie or "").strip(),
            options=dict(payload.options or {}),
        )
    )
    state.save_config(config)
    return {"accounts": _list(state)}


class AccountPatch(BaseModel):
    enabled: bool | None = None
    label: str | None = None


@router.patch("/accounts/{platform}/{label}", summary="改账号（启用状态 / 改名）")
def patch_account(
    platform: str,
    label: str,
    payload: AccountPatch,
    state: AppState = Depends(get_state),
) -> dict[str, Any]:
    config = state.config()
    target = next(
        (a for a in config.accounts if a.platform == platform and (a.label or "默认") == label),
        None,
    )
    if target is None:
        raise ApiError("not_found", f"没有这个账号：{platform} / {label}")

    if payload.label is not None:
        new_label = (payload.label or "").strip() or "默认"
        clash = any(
            a is not target
            and a.platform == platform
            and (a.label or "默认") == new_label
            for a in config.accounts
        )
        if clash:
            raise ApiError(
                "conflict",
                f"{platform} 下已经有一个叫「{new_label}」的账号了",
                detail={"field": "label"},
            )
        target.label = new_label

    if payload.enabled is not None:
        target.enabled = bool(payload.enabled)

    state.save_config(config)
    return {"accounts": _list(state)}


@router.delete("/accounts/{platform}/{label}", status_code=204, summary="删账号（连同它存的 Cookie）")
def delete_account(
    platform: str,
    label: str,
    state: AppState = Depends(get_state),
) -> Response:
    config = state.config()
    before = len(config.accounts)
    config.accounts = [
        a
        for a in config.accounts
        if not (a.platform == platform and (a.label or "默认") == label)
    ]
    if len(config.accounts) == before:
        raise ApiError("not_found", f"没有这个账号：{platform} / {label}")
    state.save_config(config)
    return Response(status_code=204)
