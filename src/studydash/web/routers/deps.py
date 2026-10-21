"""路由共用的依赖与序列化辅助。"""

from __future__ import annotations

from typing import Any

from fastapi import Request

from ..errors import ApiError
from ..state import AppState

__all__ = ["account_view", "get_state", "settings_view"]


def get_state(request: Request) -> AppState:
    state = getattr(request.app.state, "studydash", None)
    if state is None:                                   # pragma: no cover - 装配错误
        raise ApiError("internal", "服务状态缺失")
    return state


def _last_status(workspace) -> dict[tuple[str, str], dict[str, Any]]:
    """从最近一次 payload 里取出各账号的抓取结果。

    网页上的账号列表要能显示「上次抓取成功了吗、抓到几门课」，这些信息在 payload
    里已经有了，没必要再存一份、更没必要为了显示它去重新抓一次。
    """
    payload = workspace.load_progress()
    if not isinstance(payload, dict):
        return {}
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for item in payload.get("accounts") or []:
        if not isinstance(item, dict):
            continue
        platform = str(item.get("platform") or "")
        label = str(item.get("label") or "")
        out[(platform, label)] = item
        out.setdefault((platform, ""), item)
    return out


def account_view(account: Any, statuses: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any]:
    label = account.label or "默认"
    last = statuses.get((account.platform, label)) or statuses.get((account.platform, "")) or {}
    return {
        "platform": account.platform,
        "label": label,
        "enabled": bool(account.enabled),
        # 只给长度提示，绝不返回 Cookie 值本身
        "cookie_hint": f"<已设置 {len(account.cookie)} 字符>" if account.cookie else "",
        "options": dict(account.options or {}),
        "ok": bool(last.get("ok", True)),
        "message": str(last.get("message") or ""),
        "fetched_at": last.get("fetched_at"),
        "course_count": int(last.get("course_count") or 0),
    }


def settings_view(state: AppState, config: Any) -> dict[str, Any]:
    return {
        "config_path": str(state.workspace.config_path),
        "home": str(state.workspace.root),
        "http": config.http.to_dict(),
        "tiku": config.tiku.masked(),
        "ui": {"theme": config.theme(), "alert_days": config.alert_days()},
        "keep_raw": bool(config.keep_raw),
    }
