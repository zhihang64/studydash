"""智慧树（zhihuishu.com）适配器。

    ⚠️ 状态：**未验证**（unverified）

和慕课适配器一样，这是按接口形状写出来的骨架，没有用真实账号跑通过。
智慧树的前端是个 SPA，页面本身没有课程数据，数据全在 XHR 里，因此这里
只能直接打那些接口。

智慧树近年把「翻转课 / 共享课」拆成了两套后端（``onlineweb`` 和
``studyservice``），返回结构不一样，所以这里用候选列表逐个试，谁先返回
能解析的数据就用谁——这是在没有稳定字段文档的前提下唯一现实的做法。

验证方式：``studydash probe zhihuishu``，然后照
:meth:`ZhihuishuProvider.parse_courses` 里期望的字段名比对原始响应。
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from ..model import Course, Progress
from .base import ProviderMeta, register
from .rpc import RpcProvider, find_lists

ONLINE_CACHE = "https://onlineweb.zhihuishu.com/onlinestuh5/getMyCourseList"
STUDY_SERVICE = "https://studyservice.zhihuishu.com/learning/getMyCourseList"

COURSE_KEYS = ("rt", "result", "list", "courseList", "data", "courses")
TASK_KEYS = ("taskList", "list", "result", "courseTaskList", "homeworkList")


@register
class ZhihuishuProvider(RpcProvider):
    meta = ProviderMeta(
        id="zhihuishu",
        name="智慧树",
        short="智慧树",
        status="unverified",
        color="#22c55e",
        auth="cookie",
        homepage="https://www.zhihuishu.com",
        verified="",
        note="接口未经真实账号验证；智慧树 SPA 页面无课程数据，只能直直接口，改版后请用 probe 留档。",
    )

    endpoints: ClassVar[tuple[str, ...]] = (ONLINE_CACHE, STUDY_SERVICE)

    queries: ClassVar[tuple[tuple[str, str], ...]] = (
        ("共享课课程列表", ONLINE_CACHE),
        ("翻转课课程列表", STUDY_SERVICE),
    )

    def query_form(self, url: str) -> dict[str, Any]:
        form: dict[str, Any] = {"pageIndex": 1, "pageSize": str(self._page_size())}
        uuid = self._uuid()
        if uuid:
            form["uuid"] = uuid
        return form

    # ------------------------------------------------------------------ #

    def parse(self, note: str, url: str, payload: Any) -> list[Course]:
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
        # 有些接口把课程埋在 {"course": {...}} 里
        node = raw.get("course") if isinstance(raw.get("course"), dict) else raw
        assert isinstance(node, dict)

        name = str(
            node.get("courseName")
            or node.get("name")
            or node.get("title")
            or raw.get("courseName")
            or ""
        ).strip()
        if not name:
            return None

        cid = str(
            node.get("courseId")
            or node.get("id")
            or node.get("recruitId")
            or raw.get("courseId")
            or name
        )
        recruit_id = str(raw.get("recruitId") or node.get("recruitId") or "")

        progress: list[Progress] = []
        for label, keys in (
            ("视频", ("videoProgress", "videoPercent")),
            ("作业", ("homeworkProgress", "workProgress")),
        ):
            value = _percent(_pick(raw, node, keys))
            if value is not None:
                progress.append(Progress(label=label, done=round(value), total=100))

        overall = _percent(
            _pick(raw, node, ("progress", "studyProgress", "learnProgress", "schedule"))
        )
        if overall is not None:
            progress.append(Progress(label="总体", done=round(overall), total=100))

        return Course(
            id=cid,
            name=name,
            platform=self.meta.id,
            teacher=str(_pick(raw, node, ("teacherName", "teacher", "schoolName")) or ""),
            term=str(_pick(raw, node, ("termName", "seasonName", "term")) or ""),
            url=self._course_url(cid, recruit_id),
            progress=progress,
            extra={
                "platform_name": self.meta.name,
                "source": "rpc",
                "recruit_id": recruit_id,
                "raw_keys": sorted(str(k) for k in raw.keys())[:40],
            },
        )

    # ------------------------------------------------------------------ #

    def _uuid(self) -> str:
        explicit = str(self.account.options.get("uuid") or "").strip()
        if explicit:
            return explicit
        cookie = self.account.cookie or ""
        for name in ("uuid", "CASLOGC", "SESSIONID", "hlod"):
            m = re.search(rf"(?:^|;\s*){name}=([^;]+)", cookie, re.I)
            if m and name == "uuid":
                return m.group(1)
        return ""

    def _page_size(self) -> int:
        try:
            return max(1, min(200, int(self.account.options.get("page_size", 50))))
        except (TypeError, ValueError):
            return 50

    def probe_plan(self) -> list[tuple]:
        plan = super().probe_plan()
        # 顺手看一眼登录页形态，方便判断 Cookie 到底有没有生效
        plan.append(("登录后首页（HTML，用于确认登录态）", "https://onlineweb.zhihuishu.com/onlinestuh5"))
        return plan

    @staticmethod
    def _course_url(cid: str, recruit_id: str) -> str:
        if recruit_id:
            return f"https://onlineweb.zhihuishu.com/onlinestuh5/main?recruitId={recruit_id}"
        return f"https://onlineweb.zhihuishu.com/onlinestuh5?courseId={cid}"


# --------------------------------------------------------------------------- #

def _pick(primary: dict[str, Any], fallback: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        for source in (fallback, primary):
            value = source.get(key)
            if value not in (None, ""):
                return value
    return None


def _percent(value: Any) -> float | None:
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
