"""统一数据模型。

所有平台的原始响应都先被各自的 Provider 规整成这里的数据结构，
聚合层与渲染层只认这一套模型，不再关心平台差异。

设计约束
--------
* 纯 stdlib，可直接序列化成 JSON。
* 时间一律带时区（解析不到时区时按本机时区补上），存储用 ISO 8601 字符串。
* ``state`` / ``kind`` / ``status`` 的枚举值是前端契约的一部分，改动等于破坏性变更。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

SCHEMA_VERSION = 2

#: 作业/任务类型
KINDS: tuple[str, ...] = (
    "video",
    "read",
    "quiz",
    "homework",
    "exam",
    "discussion",
    "live",
    "other",
)

#: 提交状态
STATUSES: tuple[str, ...] = ("pending", "submitted", "graded", "overdue", "unknown")

#: 紧急度（由聚合层计算，Provider 不必填）
STATES: tuple[str, ...] = ("overdue", "urgent", "soon", "later", "none", "done")

KIND_LABEL: dict[str, str] = {
    "video": "视频",
    "read": "阅读",
    "quiz": "测验",
    "homework": "作业",
    "exam": "考试",
    "discussion": "讨论",
    "live": "直播",
    "other": "其他",
}

STATE_LABEL: dict[str, str] = {
    "overdue": "已逾期",
    "urgent": "即将截止",
    "soon": "一周内",
    "later": "以后",
    "none": "无截止",
    "done": "已完成",
}

#: 已提交/已评分的状态集合，用于判断任务是否还需要处理
FINISHED_STATUSES = frozenset({"submitted", "graded"})


# --------------------------------------------------------------------------- #
# 时间工具
# --------------------------------------------------------------------------- #

def now() -> datetime:
    """带本机时区的当前时间。"""
    return datetime.now().astimezone()


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.isoformat(timespec="seconds")


#: 「月-日 [时:分[:秒]]」，没写年份。解析时按当年补。
_RE_MONTH_DAY = re.compile(r"^(\d{1,2})-(\d{1,2})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?$")

#: 「时:分[:秒]」，没写日期。解析时按今天补。
_RE_TIME_ONLY = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")


def parse_dt(value: Any) -> datetime | None:
    """尽量把五花八门的时间表示解析成带时区的 datetime。

    接受 datetime、unix 秒/毫秒时间戳、以及常见字符串格式。
    解析不出来返回 ``None``（绝不抛异常——抓取到脏数据不该让整轮崩掉）。
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.astimezone()

    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e11:            # 毫秒
            ts /= 1000.0
        if ts <= 0:
            return None
        return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()

    text = str(value).strip()
    if not text:
        return None

    # 纯数字时间戳
    if text.isdigit():
        return parse_dt(int(text))

    norm = (
        text.replace("年", "-")
        .replace("月", "-")
        .replace("日", " ")
        .replace("/", "-")
        .replace("T", " ")
        .strip()
    )
    # 绝对日期（带年份）走 strptime：这些格式没有歧义，安全。
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(norm, fmt).astimezone()
        except ValueError:
            continue

    # 不带年份的写法得自己解析。strptime 碰到「有月日、没年份」会发
    # DeprecationWarning（CPython 3.13 起），在 -W error 下直接变成异常。
    # 语义上这些值本来就是「今年的某月某日」，自己补年份比让 strptime 默认
    # 成 1900 年再 replace 更清楚。
    m = _RE_MONTH_DAY.match(norm)
    if m:
        month, day, hh, mm, ss = (int(g) if g else 0 for g in m.groups())
        try:
            return datetime(now().year, month, day, hh, mm, ss).astimezone()
        except ValueError:
            return None

    m = _RE_TIME_ONLY.match(norm)
    if m:
        hh, mm, ss = (int(g) if g else 0 for g in m.groups())
        today = now()
        try:
            return datetime(today.year, today.month, today.day, hh, mm, ss).astimezone()
        except ValueError:
            return None

    # 最后试 ISO 解析（能吃掉带偏移量的写法）
    try:
        dt = datetime.fromisoformat(norm)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.astimezone()


def days_left(due: datetime | None, *, ref: datetime | None = None) -> float | None:
    if due is None:
        return None
    ref = ref or now()
    return round((due - ref).total_seconds() / 86400.0, 2)


# --------------------------------------------------------------------------- #
# 数据类
# --------------------------------------------------------------------------- #

@dataclass
class Progress:
    """一条进度条：``done / total`` 的完成情况。"""

    label: str = ""
    done: int = 0
    total: int = 0

    @property
    def ratio(self) -> float | None:
        if self.total <= 0:
            return None
        return round(min(max(self.done / self.total, 0.0), 1.0), 4)

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "done": self.done,
            "total": self.total,
            "ratio": self.ratio,
        }


@dataclass
class Assignment:
    """一条作业/任务节点。"""

    id: str
    title: str
    platform: str
    course_id: str = ""
    course: str = ""
    kind: str = "other"
    status: str = "unknown"
    due: datetime | None = None
    score: float | None = None
    max_score: float | None = None
    url: str = ""
    detail: str = ""

    #: 由聚合层填充
    state: str = "none"
    platform_name: str = ""

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            self.kind = "other"
        if self.status not in STATUSES:
            self.status = "unknown"
        if not self.course:
            self.course = self.course_id

    @property
    def uid(self) -> str:
        """全局唯一标识，跨平台不会撞。"""
        return f"{self.platform}:{self.course_id}:{self.id}"

    @property
    def finished(self) -> bool:
        return self.status in FINISHED_STATUSES

    def to_dict(self, *, ref: datetime | None = None) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "id": self.id,
            "title": self.title,
            "course": self.course,
            "course_id": self.course_id,
            "platform": self.platform,
            "platform_name": self.platform_name or self.platform,
            "kind": self.kind,
            "kind_label": KIND_LABEL.get(self.kind, self.kind),
            "status": self.status,
            "due": iso(self.due),
            "days_left": days_left(self.due, ref=ref),
            "state": self.state,
            "state_label": STATE_LABEL.get(self.state, self.state),
            "score": self.score,
            "max_score": self.max_score,
            "url": self.url,
            "detail": self.detail,
        }


@dataclass
class Course:
    """一门课。"""

    id: str
    name: str
    platform: str
    teacher: str = ""
    term: str = ""
    url: str = ""
    progress: list[Progress] = field(default_factory=list)
    assignments: list[Assignment] = field(default_factory=list)
    last_active: datetime | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def platform_name(self) -> str:
        return str(self.extra.get("platform_name") or self.platform)

    @property
    def ratio(self) -> float:
        """课程总进度。

        优先用 ``progress`` 里的加权平均；没有进度条时退回「作业完成率」。
        两者都没有就返回 0.0。
        """
        parts = [p.ratio for p in self.progress if p.ratio is not None]
        if parts:
            return round(sum(parts) / len(parts), 4)
        if self.assignments:
            done = sum(1 for a in self.assignments if a.finished)
            return round(done / len(self.assignments), 4)
        return 0.0

    def to_dict(self, *, ref: datetime | None = None) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "platform_name": self.platform_name,
            "id": self.id,
            "name": self.name,
            "teacher": self.teacher,
            "term": self.term,
            "url": self.url,
            "progress": self.ratio,
            "progress_parts": [p.to_dict() for p in self.progress],
            "pending": self.count("pending"),
            "overdue": self.count("overdue"),
            "done": self.count("done"),
            "next_due": iso(self.next_due(ref=ref)),
            "next_due_title": self.next_due_title(ref=ref),
            "last_active": iso(self.last_active),
            "assignments": [a.to_dict(ref=ref) for a in self.assignments],
        }

    # -- 统计辅助 ---------------------------------------------------------- #

    def count(self, state: str) -> int:
        return sum(1 for a in self.assignments if a.state == state)

    def next_due(self, *, ref: datetime | None = None) -> datetime | None:
        pending = [a for a in self.assignments if not a.finished and a.due is not None]
        if not pending:
            return None
        return min(a.due for a in pending)          # type: ignore[type-var]

    def next_due_title(self, *, ref: datetime | None = None) -> str:
        due = self.next_due(ref=ref)
        if due is None:
            return ""
        for a in self.assignments:
            if a.due == due and not a.finished:
                return a.title
        return ""


@dataclass
class Account:
    """一个平台账号的抓取结果。"""

    platform: str
    label: str = ""
    ok: bool = True
    message: str = ""
    fetched_at: datetime | None = None
    course_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "label": self.label or self.platform,
            "ok": self.ok,
            "message": self.message,
            "fetched_at": iso(self.fetched_at),
            "course_count": self.course_count,
        }


# --------------------------------------------------------------------------- #
# 紧急度计算
# --------------------------------------------------------------------------- #

def classify(
    assignment: Assignment,
    *,
    alert_days: float = 3.0,
    ref: datetime | None = None,
) -> str:
    """给一条作业算 ``state``。"""
    if assignment.finished:
        return "done"
    if assignment.due is None:
        return "none"
    left = days_left(assignment.due, ref=ref)
    if left is None:
        return "none"
    if left < 0:
        return "overdue"
    if left <= alert_days:
        return "urgent"
    if left <= 7:
        return "soon"
    return "later"


def classify_all(
    courses: Iterable[Course],
    *,
    alert_days: float = 3.0,
    ref: datetime | None = None,
) -> None:
    """就地给所有课程的所有作业填上 ``state`` / ``platform_name``。"""
    ref = ref or now()
    for course in courses:
        for a in course.assignments:
            if not a.platform_name:
                a.platform_name = course.platform_name
            if not a.course:
                a.course = course.name
            a.state = classify(a, alert_days=alert_days, ref=ref)


def sort_assignments(items: list[Assignment]) -> list[Assignment]:
    """待办排序：紧急度优先，其次按截止时间升序，无截止的垫底。"""
    order = {"overdue": 0, "urgent": 1, "soon": 2, "later": 3, "none": 4, "done": 5}

    def key(a: Assignment):
        far = datetime.max.replace(tzinfo=timezone.utc)
        return (order.get(a.state, 9), a.due or far, a.course, a.title)

    return sorted(items, key=key)


def pct(value: float | None, digits: int = 1) -> str:
    """0.6742 -> '67.4%'"""
    if value is None:
        return "—"
    return f"{value * 100:.{digits}f}%"
