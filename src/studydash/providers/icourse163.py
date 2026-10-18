"""中国大学 MOOC（icourse163.org）适配器。

    ⚠️ 状态：**未验证**（unverified）

这个适配器是按公开可见的接口形状写的，但**没有用真实账号验证过**。原因是
验证它需要一个能登录的慕课账号，而本项目不接受账号密码、只能由你自己贴
Cookie —— 维护者手上没有测试账号。

所以请把它当成一份**起点**而不是成品：

1. 先配好 Cookie，跑 ``studydash probe icourse163``；
2. 把 ``data/raw/probe-icourse163.json`` 里的原始响应打开看；
3. 如果字段名和这里写的对不上，改本文件的 :meth:`Icourse163Provider.parse_courses`
   和 :meth:`Icourse163Provider.parse_homework`，然后提个 PR。

``probe`` 的留档功能就是为这件事准备的——你不需要把你的 Cookie 发给任何人，
只要把响应里的结构（去掉个人信息后）贴出来，维护者就能改代码。

慕课的接口是 RPC（POST 到 ``*.rpc``），白名单见 :attr:`Icourse163Provider.endpoints`。
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from ..model import Assignment, Course, Progress, parse_dt
from .base import ProviderMeta, register
from .rpc import RpcProvider, find_lists

COURSE_RPC = "https://www.icourse163.org/web/j/mocUserRpcBean.getUserOwnCourse.rpc"
HOMEWORK_RPC = "https://www.icourse163.org/web/j/mocHomeworkBean.getHomeworkList.rpc"

#: 列表里可能出现的键名（不同接口的包装层不一样，全试一遍）
COURSE_KEYS = ("result", "list", "mocCourseDtos", "userOwnCourseDtos", "courseSummaryDtos")
HOMEWORK_KEYS = ("result", "list", "homeworkList", "homeworkDtos", "paperList")


@register
class Icourse163Provider(RpcProvider):
    meta = ProviderMeta(
        id="icourse163",
        name="中国大学 MOOC",
        short="慕课",
        status="unverified",
        color="#0ea5e9",
        auth="cookie",
        homepage="https://www.icourse163.org",
        verified="",
        note="RPC 接口未经真实账号验证；取不到数据时请用 `studydash probe icourse163` 留档提 issue。",
    )

    endpoints: ClassVar[tuple[str, ...]] = (COURSE_RPC, HOMEWORK_RPC)

    queries: ClassVar[tuple[tuple[str, str], ...]] = (
        (f"课程列表 {COURSE_RPC.rsplit('/', 1)[-1]}", COURSE_RPC),
    )

    def query_form(self, url: str) -> dict[str, Any]:
        form = {"pageIndex": 1, "pageSize": str(self._page_size())}
        key = self._csrf()
        if key:
            form["csrfKey"] = key
        return form

    # ------------------------------------------------------------------ #

    def fetch(self) -> list[Course]:
        # 不捕 AuthError：Cookie 过期必须红着报出来，不能变成「0 门课」。
        # 理由同 RpcProvider.fetch（那条注释有详细说明）。
        self.require_cookie()

        courses = self._fetch_courses()
        if not courses:
            self.log.warn(
                f"{self.meta.name}: 课程列表为空。接口可能已改版，"
                f"或这次登录确实没选课；跑 `studydash probe {self.meta.id}` 看一眼原始响应。"
            )
            return []

        self._attach_homework(courses)
        total = sum(len(c.assignments) for c in courses)
        self.log.ok(f"{self.meta.name}: {len(courses)} 门课 / {total} 条作业")
        return courses

    # ------------------------------------------------------------------ #

    def _fetch_courses(self) -> list[Course]:
        payload = self.http.post_query_json(
            COURSE_RPC, self.query_form(COURSE_RPC), note="course-list", default=None
        )
        if payload is None:
            return []
        return self.parse_courses(payload)

    def parse_courses(self, payload: Any) -> list[Course]:
        out: list[Course] = []
        seen: set[str] = set()
        for bucket in find_lists(payload, COURSE_KEYS):
            for raw in bucket:
                if not isinstance(raw, dict):
                    continue
                course = self._course(raw)
                if course is None or course.id in seen:
                    continue
                seen.add(course.id)
                out.append(course)
        return out

    def _course(self, raw: dict[str, Any]) -> Course | None:
        name = str(
            raw.get("name") or raw.get("courseName") or raw.get("title") or ""
        ).strip()
        if not name:
            return None

        cid = str(
            raw.get("id")
            or raw.get("courseId")
            or raw.get("mocCourseId")
            or raw.get("termId")
            or name
        )
        term_id = str(raw.get("termId") or raw.get("term_id") or "")

        progress: list[Progress] = []
        percent = _percent(
            raw.get("progress")
            or raw.get("learnProgress")
            or raw.get("passPercent")
            or raw.get("studyProgress")
        )
        if percent is not None:
            progress.append(Progress(label="已完成", done=round(percent), total=100))

        teacher = str(
            raw.get("teacherName")
            or raw.get("schoolName")
            or raw.get("teacher")
            or ""
        )

        return Course(
            id=cid,
            name=name,
            platform=self.meta.id,
            teacher=teacher,
            term=str(raw.get("termName") or raw.get("term") or ""),
            url=self._course_url(cid, term_id),
            progress=progress,
            extra={
                "platform_name": self.meta.name,
                "source": "rpc",
                "term_id": term_id,
                "raw_keys": sorted(str(k) for k in raw.keys())[:40],
            },
        )

    def _attach_homework(self, courses: list[Course]) -> None:
        for course in courses:
            term_id = str(course.extra.get("term_id") or "")
            if not term_id:
                continue
            payload = self.http.post_query_json(
                HOMEWORK_RPC,
                {"courseId": course.id, "termId": term_id, "csrfKey": self._csrf()},
                note=f"homework {course.name}",
                default=None,
            )
            if payload is None:
                continue
            course.assignments.extend(self.parse_homework(payload, course))

    def parse_homework(self, payload: Any, course: Course) -> list[Assignment]:
        out: list[Assignment] = []
        for bucket in find_lists(payload, HOMEWORK_KEYS):
            for raw in bucket:
                if not isinstance(raw, dict):
                    continue
                item = self._homework(raw, course)
                if item is not None:
                    out.append(item)
        return out

    def _homework(self, raw: dict[str, Any], course: Course) -> Assignment | None:
        title = str(
            raw.get("name") or raw.get("title") or raw.get("homeworkName") or ""
        ).strip()
        if not title:
            return None
        hid = str(raw.get("id") or raw.get("homeworkId") or raw.get("paperId") or title)
        due = parse_dt(
            raw.get("deadline")
            or raw.get("deadLine")
            or raw.get("endTime")
            or raw.get("submitEndTime")
        )
        return Assignment(
            id=hid,
            title=title,
            platform=self.meta.id,
            course_id=course.id,
            course=course.name,
            kind=_kind_from(raw),
            status=_status_from(raw),
            due=due,
            score=_float(raw.get("score") or raw.get("userScore")),
            max_score=_float(raw.get("totalScore") or raw.get("maxScore")),
            url=f"https://www.icourse163.org/learn/{course.id}",
            detail=str(raw.get("chapterName") or raw.get("type") or ""),
            platform_name=self.meta.name,
        )

    # ------------------------------------------------------------------ #

    def _csrf(self) -> str:
        """csrfKey 可能藏在配置里，也可能就在 Cookie 里。"""
        explicit = str(self.account.options.get("csrf_key") or "").strip()
        if explicit:
            return explicit
        cookie = self.account.cookie or ""
        m = re.search(r"(?:^|;\s*)csrfKey=([^;]+)", cookie, re.I)
        if m:
            return m.group(1)
        return ""

    def _page_size(self) -> int:
        try:
            return max(1, min(200, int(self.account.options.get("page_size", 50))))
        except (TypeError, ValueError):
            return 50

    @staticmethod
    def _course_url(cid: str, term_id: str) -> str:
        if term_id:
            return f"https://www.icourse163.org/learn/{cid}?tid={term_id}"
        return f"https://www.icourse163.org/learn/{cid}"


# --------------------------------------------------------------------------- #

def _percent(value: Any) -> float | None:
    """把 ``0.42`` / ``42`` / ``"42%"`` 都归一到 0-100。"""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        value = value.strip().rstrip("%")
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if 0 <= num <= 1:
        num *= 100
    return min(max(num, 0.0), 100.0)


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _kind_from(raw: dict[str, Any]) -> str:
    text = " ".join(
        str(raw.get(k) or "") for k in ("type", "typeName", "homeworkType", "name")
    ).lower()
    if any(w in text for w in ("quiz", "test", "测验", "测试")):
        return "quiz"
    if any(w in text for w in ("exam", "考试")):
        return "exam"
    if any(w in text for w in ("discuss", "讨论")):
        return "discussion"
    return "homework"


def _status_from(raw: dict[str, Any]) -> str:
    if _truthy(raw.get("submitted")) or _truthy(raw.get("hasSubmit")):
        return "submitted"
    score = _float(raw.get("score") or raw.get("userScore"))
    if score is not None:
        return "graded"
    text = str(raw.get("status") or raw.get("submitStatus") or "").lower()
    if text in {"1", "submitted", "finished", "done"}:
        return "submitted"
    if text in {"2", "graded", "checked"}:
        return "graded"
    if text in {"0", "pending", "notsubmit", "unsubmitted"}:
        return "pending"
    return "unknown"


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}
