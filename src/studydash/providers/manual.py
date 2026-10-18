"""手动导入适配器：把「平台没有公开接口」的课程也拉进同一个看板。

这是本项目里**唯一一个不依赖任何平台改版**的数据来源，也是当下最实用的
「作业进度」来源：线下课的论文、教务系统的 DDL、老师群里发的通知，写进一个
JSON 或 CSV 就能和超星、慕课的数据并排显示、一起排序、统一预警。

JSON 结构（``studydash import --init`` 会生成一份带注释的模板）::

    {
      "courses": [
        {
          "id": "morning-class",
          "name": "形势与政策（线下）",
          "platform": "manual",          // 可填任意平台名，比如 zhihuishu
          "platform_name": "手动导入",    // 可选，展示用
          "teacher": "陈老师",
          "term": "2025-2026-2",
          "url": "",
          "progress": [{"label": "出勤", "done": 4, "total": 9}],
          "assignments": [
            {
              "id": "100",
              "title": "课程论文提纲",
              "kind": "homework",        // video/read/quiz/homework/exam/discussion/live/other
              "status": "pending",       // pending/submitted/graded/overdue/unknown
              "due": "2026-03-20 18:00", // 支持 "2026-03-20"、"2026/3/20 18:00"、时间戳
              "score": null,
              "max_score": 100,
              "url": "",
              "detail": "课堂通知"
            }
          ]
        }
      ]
    }

也接受扁平 CSV（日期/时间列名随意，见 :data:`CSV_COLUMNS`）。
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from ..model import Assignment, Course, Progress, parse_dt
from .base import Provider, ProviderError, ProviderMeta, register

DEFAULT_FILE = "data/manual.json"
DEFAULT_CSV = "data/manual.csv"

CSV_COLUMNS = (
    "platform",
    "course_id",
    "course_name",
    "title",
    "kind",
    "status",
    "due",
    "score",
    "max_score",
    "url",
    "detail",
)

TEMPLATE: dict[str, Any] = {
    "courses": [
        {
            "id": "morning-class",
            "name": "形势与政策（线下）",
            "platform": "manual",
            "platform_name": "手动导入",
            "teacher": "陈老师",
            "term": "2025-2026-2",
            "url": "",
            "progress": [{"label": "出勤", "done": 4, "total": 9}],
            "assignments": [
                {
                    "id": "100",
                    "title": "课程论文提纲",
                    "kind": "homework",
                    "status": "pending",
                    "due": "2026-03-20 18:00",
                    "score": None,
                    "max_score": 100,
                    "url": "",
                    "detail": "课堂通知",
                }
            ],
        }
    ]
}


@register
class ManualProvider(Provider):
    meta = ProviderMeta(
        id="manual",
        name="手动导入",
        short="手动",
        status="stable",
        color="#8b5cf6",
        auth="none",
        homepage="",
        verified="2026-03-14",
        note="从本地 JSON/CSV 读取，不联网。任何平台的课程和 DDL 都能这样进看板。",
    )

    def fetch(self) -> list[Course]:
        path = self._resolve_path()
        if path is None:
            self.log.warn(
                f"没找到手动数据文件（{DEFAULT_FILE} 或 {DEFAULT_CSV}）。"
                "跑 `studydash import --init` 生成模板。"
            )
            return []

        text = path.read_text(encoding="utf-8-sig")
        if path.suffix.lower() == ".csv":
            courses = self._from_csv(text, path.name)
        else:
            courses = self._from_json(text, path)

        total = sum(len(c.assignments) for c in courses)
        self.log.ok(f"{self.meta.name}: {len(courses)} 门课 / {total} 条作业（{path.name}）")
        return courses

    # ------------------------------------------------------------------ #

    def _resolve_path(self) -> Path | None:
        raw = str(self.account.options.get("path") or "").strip()
        if raw:
            path = Path(raw).expanduser()
            if not path.is_absolute():
                path = self.root() / path
            if not path.exists():
                raise ProviderError(
                    f"配置里指定的手动数据文件不存在: {path}",
                    hint="检查配置文件里 manual 账号的 options.path。",
                )
            return path

        for name in (DEFAULT_FILE, DEFAULT_CSV):
            candidate = self.root() / name
            if candidate.exists():
                return candidate
        return None

    @staticmethod
    def root() -> Path:
        """数据目录。打包成 exe 时是 exe 所在目录，源码运行时是仓库根目录。"""
        import sys

        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve().parent
        return Path(__file__).resolve().parents[3]

    # ------------------------------------------------------------------ #

    def _from_json(self, text: str, path: Path) -> list[Course]:
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise ProviderError(
                f"{path.name} 不是合法 JSON：{exc}",
                hint="用 `studydash import --init` 生成的模板对照一下括号和逗号。",
            ) from exc

        if isinstance(data, list):                       # 允许顶层直接是课程数组
            data = {"courses": data}
        if not isinstance(data, dict) or not isinstance(data.get("courses"), list):
            raise ProviderError(
                f"{path.name} 缺少 courses 数组",
                hint='顶层应为 {"courses": [ ... ]}。',
            )

        courses: list[Course] = []
        for index, item in enumerate(data["courses"]):
            if not isinstance(item, dict):
                self.log.warn(f"  第 {index + 1} 个课程不是对象，已跳过")
                continue
            course = self._course_from_dict(item, index)
            if course is not None:
                courses.append(course)
        return courses

    def _course_from_dict(self, item: dict[str, Any], index: int) -> Course | None:
        name = str(item.get("name") or item.get("course_name") or "").strip()
        if not name:
            self.log.warn(f"  第 {index + 1} 个课程没有 name，已跳过")
            return None

        cid = str(item.get("id") or item.get("course_id") or f"manual-{index + 1}")
        platform = str(item.get("platform") or self.meta.id).strip() or self.meta.id

        progress: list[Progress] = []
        for part in item.get("progress") or []:
            if not isinstance(part, dict):
                continue
            progress.append(
                Progress(
                    label=str(part.get("label") or ""),
                    done=_int(part.get("done")),
                    total=_int(part.get("total")),
                )
            )

        assignments: list[Assignment] = []
        for j, raw in enumerate(item.get("assignments") or []):
            if not isinstance(raw, dict):
                continue
            assignments.append(
                Assignment(
                    id=str(raw.get("id") or f"{cid}-{j + 1}"),
                    title=str(raw.get("title") or "").strip() or "(未命名)",
                    platform=platform,
                    course_id=cid,
                    course=name,
                    kind=str(raw.get("kind") or "other"),
                    status=str(raw.get("status") or "unknown"),
                    due=parse_dt(raw.get("due")),
                    score=_float_or_none(raw.get("score")),
                    max_score=_float_or_none(raw.get("max_score")),
                    url=str(raw.get("url") or ""),
                    detail=str(raw.get("detail") or ""),
                    platform_name=str(item.get("platform_name") or self.meta.name),
                )
            )

        return Course(
            id=cid,
            name=name,
            platform=platform,
            teacher=str(item.get("teacher") or ""),
            term=str(item.get("term") or ""),
            url=str(item.get("url") or ""),
            progress=progress,
            assignments=assignments,
            last_active=parse_dt(item.get("last_active")),
            extra={
                "platform_name": str(item.get("platform_name") or self.meta.name),
                "source": self.meta.id,
            },
        )

    # ------------------------------------------------------------------ #

    def _from_csv(self, text: str, filename: str) -> list[Course]:
        rows = list(csv.DictReader(text.splitlines()))
        if not rows:
            return []

        buckets: dict[tuple[str, str], Course] = {}
        for i, row in enumerate(rows):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            course_name = row.get("course_name") or row.get("course") or ""
            if not course_name:
                self.log.warn(f"  {filename} 第 {i + 2} 行缺少 course_name，已跳过")
                continue
            platform = row.get("platform") or self.meta.id
            cid = row.get("course_id") or course_name
            key = (platform, cid)

            if key not in buckets:
                buckets[key] = Course(
                    id=cid,
                    name=course_name,
                    platform=platform,
                    teacher=row.get("teacher", ""),
                    term=row.get("term", ""),
                    url=row.get("course_url", ""),
                    extra={
                        "platform_name": row.get("platform_name") or self.meta.name,
                        "source": self.meta.id,
                    },
                )

            title = row.get("title") or row.get("assignment") or ""
            if not title:
                continue
            buckets[key].assignments.append(
                Assignment(
                    id=row.get("id") or f"{cid}-{i + 1}",
                    title=title,
                    platform=platform,
                    course_id=cid,
                    course=course_name,
                    kind=row.get("kind") or "other",
                    status=row.get("status") or "unknown",
                    due=parse_dt(row.get("due") or row.get("deadline")),
                    score=_float_or_none(row.get("score")),
                    max_score=_float_or_none(row.get("max_score")),
                    url=row.get("url", ""),
                    detail=row.get("detail", ""),
                    platform_name=row.get("platform_name") or self.meta.name,
                )
            )

        return list(buckets.values())


# --------------------------------------------------------------------------- #

def _int(value: Any) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return 0


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def write_template(path: Path) -> Path:
    """写一份带注释的 JSON 模板。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(TEMPLATE, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path
