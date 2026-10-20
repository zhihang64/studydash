"""中国大学 MOOC（icourse163.org）适配器。

    ✅ 状态：**测试中**（beta，2026-10-20 用真实账号验证）

慕课是一套 RPC 接口：所有查询都是 POST 到 ``*.rpc``，参数放表单，``csrfKey``
拼在查询串上。这里的三个端点都已经用真实账号跑通：

===========================================  ==========================================
端点                                          用途
===========================================  ==========================================
``learnerCourseRpcBean.getMyLearnedCoursePanelList.rpc``
                                              「我的课程」面板，一次给出 MOOC + SPOC
                                              全部课程、``learnedCount`` / ``lessonsCount``
``courseBean.getMocTermDto.rpc``              单个学期的章节树，作业/测验/考试都挂在
                                              章节上（``test.deadline`` 是截止时间）
===========================================  ==========================================

两个容易踩的坑（都踩过了，别再回去）：

* ``csrfKey`` 取的是 **``NTESSTUDYSI`` 这个 Cookie 的值**，不是某个叫
  ``csrfKey`` 的 Cookie。缺了会被判「非法跨域请求」。
* 面板接口的参数是 ``type``（30 = 我学过的课），**不是** ``pageIndex`` /
  ``pageSize``；传了没声明的参数会被 ``-1007 不含有该参数 :xxx`` 顶回来。

``probe`` 的留档功能仍然有用：接口再改版时，把响应结构贴出来就能照着改。
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from ..model import Assignment, Course, Progress, iso, now, parse_dt
from .base import AuthError, ProviderError, ProviderMeta, register
from .rpc import RpcProvider, find_lists

BASE = "https://www.icourse163.org"

#: 「我的课程」面板。``type=30`` 是页面自己用的取值。
COURSE_RPC = BASE + "/web/j/learnerCourseRpcBean.getMyLearnedCoursePanelList.rpc"
#: 学期详情：章节 + 作业 + 测验 + 考试。
TERM_RPC = BASE + "/web/j/courseBean.getMocTermDto.rpc"

#: 面板类型。30 会同时返回 MOOC 与 SPOC；DWR 老接口把同样的 30 作为第三个参数。
PANEL_TYPE = 30

#: ``test.type`` → 作业类型。实测 3=作业，2=考试/测试，1=测验。
_KIND_BY_TEST_TYPE = {1: "quiz", 2: "exam", 3: "homework"}

#: 兜底用的列表键名（接口外面多包一层时也找得到）。
COURSE_KEYS = ("result", "list", "coursePanels", "coursePanelList")
TERM_KEYS = ("chapters", "homeworks", "quizs", "exams")


@register
class Icourse163Provider(RpcProvider):
    meta = ProviderMeta(
        id="icourse163",
        name="中国大学 MOOC",
        short="慕课",
        status="beta",
        color="#0ea5e9",
        auth="cookie",
        login=True,
        homepage=BASE,
        verified="2026-10-20",
        note="课程、进度、作业/测验/考试已用真实账号验证；"
             "成绩单与证书暂未接入。接口改版时用 `studydash probe icourse163` 留档。",
    )

    endpoints: ClassVar[tuple[str, ...]] = (COURSE_RPC, TERM_RPC)

    queries: ClassVar[tuple[tuple[str, str], ...]] = (
        ("课程面板", COURSE_RPC),
    )

    def query_form(self, url: str) -> dict[str, Any]:
        """只给面板接口补参数；``csrfKey`` 由 :meth:`_with_csrf` 统一拼到查询串。"""
        if url == COURSE_RPC:
            return {"type": PANEL_TYPE}
        return {}

    def probe_plan(self) -> list[tuple[str, str, str, dict[str, Any]]]:
        """留档时也要带上 ``csrfKey``，否则只会拿到「非法跨域请求」。"""
        return [
            ("课程面板", self._with_csrf(COURSE_RPC), "POST", self.query_form(COURSE_RPC)),
        ]

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

        self._attach_assignments(courses)
        total = sum(len(c.assignments) for c in courses)
        self.log.ok(f"{self.meta.name}: {len(courses)} 门课 / {total} 条作业")
        return courses

    # ------------------------------------------------------------------ #

    def _fetch_courses(self) -> list[Course]:
        payload = self.http.post_query_json(
            self._with_csrf(COURSE_RPC),
            self.query_form(COURSE_RPC),
            note="课程面板",
            default=None,
        )
        if payload is None:
            raise ProviderError(
                f"{self.meta.name}: 课程面板没有返回 JSON",
                hint="Cookie 可能已失效；重新扫码或粘贴 Cookie 再试。",
            )
        self._check_rpc(payload, "课程面板")
        return self.parse_courses(payload)

    def parse_courses(self, payload: Any) -> list[Course]:
        out: list[Course] = []
        seen: set[str] = set()
        for raw in _course_items(payload):
            course = self._course(raw)
            if course is None:
                continue
            # 同一门课可能出现在多个学期，``term_id`` 才是唯一键。
            key = f"{course.id}:{course.extra.get('term_id') or ''}"
            if key in seen:
                continue
            seen.add(key)
            out.append(course)
        return out

    def _course(self, raw: dict[str, Any]) -> Course | None:
        term = raw.get("termPanel") if isinstance(raw.get("termPanel"), dict) else {}
        name = str(raw.get("name") or term.get("courseName") or "").strip()
        if not name:
            return None

        cid = str(raw.get("id") or term.get("courseId") or "")
        term_id = str(term.get("id") or raw.get("termId") or "")
        school = raw.get("schoolPanel") if isinstance(raw.get("schoolPanel"), dict) else {}

        total = _int(term.get("lessonsCount"))
        learned = _int(raw.get("learnedCount"))
        progress: list[Progress] = []
        if total and learned is not None:
            progress.append(Progress(label="已学小节", done=learned, total=total))

        mode = raw.get("mode")
        end = parse_dt(term.get("endTime"))
        return Course(
            id=cid,
            name=name,
            platform=self.meta.id,
            teacher=str(school.get("name") or ""),
            term=_term_label(term),
            url=self._course_url(cid, term_id),
            progress=progress,
            extra={
                "platform_name": self.meta.name,
                "source": "rpc",
                "term_id": term_id,
                "mode": "spoc" if _int(mode) == 20 else "mooc",
                "start": iso(parse_dt(term.get("startTime"))),
                "end": iso(parse_dt(term.get("endTime"))),
                # 已结课：页面上会灰掉，聚合层也能据此把「逾期」算得客气一点。
                "ended": bool(end and end <= now()),
                "learned": learned,
                "lessons": total,
                "cover": str(raw.get("imgUrl") or ""),
            },
        )

    # ------------------------------------------------------------------ #

    def _attach_assignments(self, courses: list[Course]) -> None:
        for course in courses:
            term_id = str(course.extra.get("term_id") or "")
            if not term_id:
                continue
            payload = self.http.post_query_json(
                self._with_csrf(TERM_RPC),
                {"termId": term_id, "isDraft": 0, "excludeUnrelease": 0},
                note=f"学期 {course.name}",
                default=None,
            )
            if payload is None:
                self.log.warn(f"{self.meta.name}: {course.name} 的学期详情没取到")
                continue
            try:
                self._check_rpc(payload, f"{course.name} 学期详情")
            except AuthError:
                raise                                     # 会话失效，整轮都别继续了
            except ProviderError as exc:
                self.log.warn(str(exc))
                continue
            course.assignments.extend(self.parse_term(payload, course))

    def parse_term(self, payload: Any, course: Course) -> list[Assignment]:
        result = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(result, dict):
            return []

        out: list[Assignment] = []
        seen: set[str] = set()

        for chapter in result.get("chapters") or []:
            if not isinstance(chapter, dict):
                continue
            chapter_name = str(chapter.get("name") or "")
            for bucket in ("homeworks", "quizs"):
                for raw in chapter.get(bucket) or []:
                    item = self._assignment(raw, course, chapter_name)
                    if item is not None and item.id not in seen:
                        seen.add(item.id)
                        out.append(item)
            exam = chapter.get("exam")
            if isinstance(exam, dict):
                item = self._exam(exam, course, chapter_name)
                if item is not None and item.id not in seen:
                    seen.add(item.id)
                    out.append(item)

        # ``exams`` 常与章节里的 ``exam`` 重复，靠 id 去重。
        for exam in result.get("exams") or []:
            if not isinstance(exam, dict):
                continue
            item = self._exam(exam, course, "")
            if item is not None and item.id not in seen:
                seen.add(item.id)
                out.append(item)

        return out

    def _assignment(
        self, raw: dict[str, Any], course: Course, chapter: str
    ) -> Assignment | None:
        if not isinstance(raw, dict):
            return None
        test = raw.get("test") if isinstance(raw.get("test"), dict) else {}
        title = str(test.get("name") or raw.get("name") or "").strip()
        if not title:
            return None

        aid = str(test.get("id") or raw.get("contentId") or raw.get("id") or title)
        type_id = _int(test.get("type"))
        return Assignment(
            id=f"t{aid}",
            title=title,
            platform=self.meta.id,
            course_id=course.id,
            course=course.name,
            kind=_KIND_BY_TEST_TYPE.get(type_id, "other"),
            status=_status(test),
            due=parse_dt(test.get("deadline")),
            score=_float(test.get("userScore")),
            max_score=_float(test.get("totalScore")),
            url=self._course_url(course.id, str(course.extra.get("term_id") or "")),
            detail=chapter,
            platform_name=self.meta.name,
        )

    def _exam(
        self, raw: dict[str, Any], course: Course, chapter: str
    ) -> Assignment | None:
        title = str(raw.get("name") or "").strip()
        if not title:
            return None

        # 同一份考试在 exam / objectTest / objectTestVo 里各有一份，字段互补。
        object_test = raw.get("objectTest") if isinstance(raw.get("objectTest"), dict) else {}
        vo = raw.get("objectTestVo") if isinstance(raw.get("objectTestVo"), dict) else {}
        aid = str(raw.get("id") or object_test.get("id") or vo.get("id") or title)

        score = _float(raw.get("userScore"))
        if score is None:
            score = _float(vo.get("userScore"))
        tries = _int(vo.get("usedTryCount"))
        if tries is None:
            tries = _int(object_test.get("usedTryCount"))

        return Assignment(
            id=f"e{aid}",
            title=title,
            platform=self.meta.id,
            course_id=course.id,
            course=course.name,
            kind="exam",
            status=_status({"userScore": score, "usedTryCount": tries}),
            due=parse_dt(raw.get("deadline") or object_test.get("deadline")),
            score=score,
            max_score=_float(raw.get("totalScore") or object_test.get("totalScore")),
            url=self._course_url(course.id, str(course.extra.get("term_id") or "")),
            detail=chapter or "考试",
            platform_name=self.meta.name,
        )

    # ------------------------------------------------------------------ #

    def _with_csrf(self, url: str) -> str:
        """所有 ``.rpc`` 都要把 ``csrfKey`` 拼在查询串上（页面自己就是这么干的）。"""
        key = self._csrf()
        if not key or "csrfKey=" in url:
            return url
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}csrfKey={key}"

    def _csrf(self) -> str:
        """``csrfKey`` = ``NTESSTUDYSI`` 这个 Cookie 的值。

        配置里的 ``csrf_key`` 允许覆盖（有些 Cookie 串被截断过，值还在别处）。
        注意**不能**去找名为 ``csrfKey`` 的 Cookie——慕课没有这个 Cookie，
        那样只会永远拿到空串，然后被服务端判「非法跨域请求」。
        """
        explicit = str(self.account.options.get("csrf_key") or "").strip()
        if explicit:
            return explicit
        cookie = self.account.cookie or ""
        match = re.search(r"(?:^|;\s*)NTESSTUDYSI=([^;]+)", cookie, re.I)
        return match.group(1).strip() if match else ""

    @staticmethod
    def _check_rpc(payload: Any, what: str) -> None:
        """``code != 0`` 时按语义抛错，别把失败伪装成「没有数据」。"""
        if not isinstance(payload, dict):
            return
        code = payload.get("code")
        if code in (None, 0):
            return
        message = str(payload.get("message") or "").strip()
        if "登录" in message or code == -1002:
            raise AuthError(
                f"中国大学 MOOC: {what}说会话不可用（{code} {message}）",
                hint="重新扫码登录，或粘贴一份新的 Cookie（要带上 NTESSTUDYSI）。",
            )
        raise ProviderError(
            f"中国大学 MOOC: {what}返回 {code} {message or '(没有说明)'}",
            hint="接口可能改版了；跑 `studydash probe icourse163` 留档后提 issue。",
        )

    @staticmethod
    def _course_url(cid: str, term_id: str) -> str:
        if term_id:
            return f"{BASE}/learn/{cid}?tid={term_id}"
        return f"{BASE}/learn/{cid}"


# --------------------------------------------------------------------------- #
# 解析小工具
# --------------------------------------------------------------------------- #

def _course_items(payload: Any) -> list[dict[str, Any]]:
    """面板响应是 ``{"result": {"result": [...], "pagination": {...}}}``。

    先按已知形状取；形状变了就退回「按名字找列表」，别因为多包一层就整个空掉。
    """
    if isinstance(payload, dict):
        result = payload.get("result")
        if isinstance(result, dict):
            items = result.get("result")
            if isinstance(items, list):
                return [x for x in items if isinstance(x, dict)]
        if isinstance(result, list):
            return [x for x in result if isinstance(x, dict)]

    out: list[dict[str, Any]] = []
    for bucket in find_lists(payload, COURSE_KEYS):
        out.extend(x for x in bucket if isinstance(x, dict))
    return out


def _term_label(term: dict[str, Any]) -> str:
    """``startTime`` → ``"2025 年秋季"``。没有时间就给空串，别编。"""
    start = parse_dt(term.get("startTime"))
    if start is None:
        return ""
    season = "春" if 2 <= start.month <= 7 else "秋"
    return f"{start.year} 年{season}季"


def _status(test: dict[str, Any]) -> str:
    """成绩出来就算批改完；做过但还没出分算已提交；否则待完成。"""
    if _float(test.get("userScore")) is not None:
        return "graded"
    tries = _int(test.get("usedTryCount"))
    if tries is not None and tries > 0:
        return "submitted"
    return "pending"


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
