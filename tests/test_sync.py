"""刷课之后看板立刻同步：``aggregate.sync_payload``。

上报完看板还是旧数字，根因是 ``data/progress.json`` 只有 fetch 会重写。这里
验证「拿刚活读的任务点计数器就地改 payload」这条路：改数字、重算总览与平台卡、
但别的平台和别的课一个字节都不许动。
"""

from __future__ import annotations

from typing import Any

from studydash.aggregate import sync_payload


def _payload() -> dict[str, Any]:
    return {
        "meta": {"generated_at": "2026-10-20T10:00:00+08:00"},
        "overview": {"courses": 2, "avg_progress": 0.5, "has_progress": True, "pending": 1},
        "platforms": [
            {"id": "chaoxing", "courses": 1, "avg_progress": 0.5, "has_data": True, "pending": 0},
            {"id": "icourse163", "courses": 1, "avg_progress": 0.5, "has_data": True, "pending": 1},
        ],
        "courses": [
            {
                "platform": "chaoxing",
                "id": "258121704",
                "name": "形势与政策Ⅲ+Ⅴ",
                "progress": 0.5,
                "progress_parts": [
                    {"label": "任务点", "done": 5, "total": 10, "ratio": 0.5},
                ],
                "has_data": True,
                "pending_points": 5,
                "assignments": [],
            },
            {
                "platform": "icourse163",
                "id": "999",
                "name": "大学英语",
                "progress": 0.5,
                "progress_parts": [{"label": "任务点", "done": 1, "total": 2, "ratio": 0.5}],
                "has_data": True,
                "assignments": [],
            },
        ],
        "timeline": [],
    }


def test_sync_updates_bar_and_ratio() -> None:
    payload = _payload()
    touched = sync_payload(payload, {"258121704": {"done": 8, "total": 10}})
    assert touched == 1
    card = payload["courses"][0]
    assert card["progress"] == 0.8
    assert card["progress_parts"][0] == {"label": "任务点", "done": 8, "total": 10, "ratio": 0.8}
    assert card["has_data"] is True
    # pending_points 是「还差多少」，得跟着 done 走，不能还写着 5
    assert card["pending_points"] == 2


def test_sync_recounts_overview_and_platforms() -> None:
    payload = _payload()
    sync_payload(payload, {"258121704": {"done": 10, "total": 10}})
    # 两门课：超星 1.0，中国大学 0.5 -> 平均 0.75
    assert payload["overview"]["avg_progress"] == 0.75
    assert payload["overview"]["courses"] == 2
    assert payload["overview"]["platforms"] == 2
    by_id = {card["id"]: card for card in payload["platforms"]}
    assert by_id["chaoxing"]["avg_progress"] == 1.0
    assert by_id["icourse163"]["avg_progress"] == 0.5


def test_sync_leaves_other_platform_alone() -> None:
    payload = _payload()
    untouched = dict(payload["courses"][1]["progress_parts"][0])
    sync_payload(payload, {"999": {"done": 2, "total": 2}}, platform="chaoxing")
    assert payload["courses"][1]["progress"] == 0.5
    assert payload["courses"][1]["progress_parts"][0] == untouched


def test_sync_ignores_unknown_and_broken_counts() -> None:
    payload = _payload()
    before = payload["courses"][0]["progress"]
    assert sync_payload(payload, {}) == 0
    assert sync_payload(payload, {"258121704": {"done": 3, "total": 0}}) == 0
    assert sync_payload(payload, {"999999": {"done": 3, "total": 10}}) == 0
    assert payload["courses"][0]["progress"] == before


def test_sync_clamps_to_total_and_creates_missing_bar() -> None:
    payload = _payload()
    payload["courses"][0]["progress_parts"] = []
    sync_payload(payload, {"258121704": {"done": 99, "total": 10}})
    card = payload["courses"][0]
    assert card["progress"] == 1.0
    assert card["progress_parts"] == [{"label": "任务点", "done": 10, "total": 10, "ratio": 1.0}]
