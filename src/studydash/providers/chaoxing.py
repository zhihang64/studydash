"""超星学习通（chaoxing / 学习通）适配器。

**只读**：这里全部都是 GET。课程进度、章节任务点、作业与测验状态，都只读出来
展示，不做任何提交动作。

验证状态
--------
* 课程列表 + 课程进度：**已验证**（逻辑自 v1 迁移，长期在用）
* 章节任务点 / 作业 / 测验：**实验性**，默认关闭。用 ``options.chapters: true``
  打开。这段依赖 ``knowledge/cards`` 的响应结构，平台改版就会失效——失效时
  会明确报警告，而不是安静地返回空列表。怀疑失效时跑::

      studydash probe chaoxing

  把原始响应贴到 issue 里，改解析逻辑即可，不需要维护者的账号。
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..http import HttpError
from ..log import Log
from ..model import Assignment, Course, Progress, parse_dt
from .base import AuthError, Provider, ProviderError, ProviderMeta, register

# --------------------------------------------------------------------------- #
# 端点
# --------------------------------------------------------------------------- #

EP_LOGIN_CHECK = "https://i.chaoxing.com/base/settings"
EP_COURSE_LIST = (
    "https://mooc2-ans.chaoxing.com/mooc2-ans/visit/courses/list"
    "?rss=1&start=0&size=500&catalogId=0&searchname="
)
EP_COURSE_PAGE = (
    "https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/studentcourse"
    "?courseid={courseid}&clazzid={clazzid}&cpi={cpi}&ut=s&t={ts}"
)
EP_KNOWLEDGE_CARDS = (
    "https://mooc1.chaoxing.com/knowledge/cards"
    "?clazzid={clazzid}&courseid={courseid}&knowledgeid={knowledgeid}"
    "&num=0&ut=s&cpi={cpi}&v={ts}&mooc2=1"
)

# --------------------------------------------------------------------------- #
# 正则
# --------------------------------------------------------------------------- #

RE_COURSE_ID = re.compile(r"course[Ii]d=(\d+)")
RE_CLAZZ_ID = re.compile(r"cla[zs]{2}[Ii]d=(\d+)")
RE_CPI = re.compile(r"cpi=(\d+)")
RE_PERCENT = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")
RE_TAG = re.compile(r"<[^>]+>")
RE_LI_BLOCK = re.compile(r"<li\b[^>]*>(.*?)(?=<li\b|</ul>|\Z)", re.S | re.I)
RE_UL_COURSE_LIST = re.compile(
    r"<ul\b[^>]*id=[\"']courseList[\"'][^>]*>(.*?)</ul>", re.S | re.I
)
RE_MARG = re.compile(r"mArg\s*=\s*(\{[\s\S]*?\})\s*catch", re.I)
RE_CHAPTER_LINK = re.compile(
    r"<a\b[^>]*href=[\"']([^\"']*knowledgeid=(\d+)[^\"']*)[\"'][^>]*>(.*?)</a>", re.S | re.I
)
RE_CHAPTER_BLOCK = re.compile(
    r"<div\b[^>]*class=[\"'][^\"']*fanyaChapterWhite[^\"']*[\"'][^>]*>(.*?)(?=<div\b[^>]*class=[\"'][^\"']*fanyaChapterWhite|\Z)",
    re.S | re.I,
)

_ENTITY = {
    "&nbsp;": " ",
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&#39;": "'",
}

#: 章节任务点里 attachment 的 module/type -> 统一 kind
_KIND_MAP = (
    ("insertaudio", "video"),
    ("audio", "video"),
    ("video", "video"),
    ("read", "read"),
    ("book", "read"),
    ("document", "read"),
    ("doc", "read"),
    ("live", "live"),
    ("discuss", "discussion"),
    ("bbs", "discussion"),
    ("work", "homework"),
    ("homework", "homework"),
    ("home", "homework"),
    ("exam", "exam"),
    ("quiz", "quiz"),
    ("test", "quiz"),
)


def _strip_tags(html: str) -> str:
    text = RE_TAG.sub(" ", html or "")
    for entity, char in _ENTITY.items():
        text = text.replace(entity, char)
    return re.sub(r"\s+", " ", text).strip()


def _clean_name(text: str) -> str:
    text = _strip_tags(text)
    text = re.sub(r"^(课程|课程名称)[:：]\s*", "", text)
    return text.strip(" -|·")[:120]


def _kind_of(*values: Any) -> str:
    blob = " ".join(str(v).lower() for v in values if v)
    for needle, kind in _KIND_MAP:
        if needle in blob:
            return kind
    return "other"


@register
class ChaoxingProvider(Provider):
    meta = ProviderMeta(
        id="chaoxing",
        name="超星学习通",
        short="超星",
        status="beta",
        color="#3b82f6",
        auth="cookie",
        homepage="https://i.chaoxing.com",
        verified="2026-03-14",
        note="课程与进度已验证；章节任务点/作业为实验性功能（options.chapters）。",
    )

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #

    def fetch(self) -> list[Course]:
        self.require_cookie()
        self._check_login()

        raw = self.http.get_text(
            EP_COURSE_LIST,
            headers={"Accept": "text/html,application/json,*/*"},
            note="课程列表",
        )
        if not raw:
            raise ProviderError(
                "课程列表页没有返回内容",
                hint="常见原因：Cookie 已过期，或被校园网/防火墙拦截。",
            )

        courses = self._parse_course_list(raw)
        if not courses:
            raise ProviderError(
                "课程列表解析结果为空",
                hint=(
                    "平台可能改版了。请运行 `studydash probe chaoxing` 并把原始响应"
                    "贴到 issue，解析逻辑在 providers/chaoxing.py。"
                ),
            )

        if self.account.options.get("chapters"):
            self._enrich_with_chapters(courses)

        self.log.ok(f"{self.meta.name}: {len(courses)} 门课")
        return courses

    def probe_plan(self) -> list[tuple[str, str]]:
        plan = [("登录态自检", EP_LOGIN_CHECK), ("课程列表", EP_COURSE_LIST)]
        extra = str(self.account.options.get("probe_course", "")).strip()
        if extra:
            cid, _, clz = extra.partition(":")
            plan.append(
                (
                    "课程页（用于核对 token / 章节结构）",
                    EP_COURSE_PAGE.format(courseid=cid, clazzid=clz or cid, cpi="", ts=0),
                )
            )
        return plan

    # ------------------------------------------------------------------ #
    # 登录态
    # ------------------------------------------------------------------ #

    def _check_login(self) -> None:
        text = self.http.get_text(EP_LOGIN_CHECK, note="登录态自检")
        if not text:
            raise AuthError(
                "登录态自检没有响应",
                hint="Cookie 可能已失效，或网络无法访问 i.chaoxing.com。",
            )
        if self.looks_logged_out(text):
            raise AuthError(
                "Cookie 已过期或未登录",
                hint=(
                    "重新在浏览器登录 https://i.chaoxing.com ，"
                    "复制 Cookie 整行覆盖配置文件里的 cookie 字段。"
                ),
            )

    # ------------------------------------------------------------------ #
    # 课程列表解析
    # ------------------------------------------------------------------ #

    def _parse_course_list(self, text: str) -> list[Course]:
        items = self._parse_json_course_list(text)
        if items:
            self.log.debug(f"课程列表按 JSON 解析，{len(items)} 条")
            return self._courses_from_json(items)
        return self._parse_html_course_list(text)

    @staticmethod
    def _parse_json_course_list(text: str) -> list[dict] | None:
        """有些部署直接返回 JSON；递归找出形似课程对象的字典。"""
        stripped = text.lstrip()
        if not stripped.startswith(("{", "[")):
            return None
        try:
            obj = json.loads(text)
        except (ValueError, TypeError):
            return None

        found: list[dict] = []

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                keys = {str(k).lower() for k in node}
                if keys & {"courseid", "course_id", "clazzid", "clazz_id"}:
                    found.append(node)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(obj)
        return found or None

    def _courses_from_json(self, items: list[dict]) -> list[Course]:
        courses: list[Course] = []
        seen: set[str] = set()
        for item in items:
            lowered = {str(k).lower(): v for k, v in item.items()}

            def pick(*names: str, default: str = "") -> Any:
                for name in names:
                    value = lowered.get(name.lower())
                    if value not in (None, ""):
                        return value
                return default

            cid = str(pick("courseId", "course_id", "id"))
            if not cid or cid in seen:
                continue
            seen.add(cid)

            percent = self._to_percent(pick("progress", "percent", "schedule", default=0))
            courses.append(
                Course(
                    id=cid,
                    name=_clean_name(str(pick("courseName", "course_name", "name", "title"))),
                    platform=self.meta.id,
                    teacher=_clean_name(str(pick("teacher", "teacherName", "personName"))),
                    term=_clean_name(str(pick("term", "termName", "semesterName"))),
                    url=str(pick("url", "courseUrl", default="")),
                    progress=[Progress("课程进度", round(percent * 100), 100)] if percent else [],
                    extra={
                        "class_id": str(pick("clazzid", "clazzId", "class_id")),
                        "cpi": str(pick("cpi")),
                        "platform_name": self.meta.name,
                    },
                )
            )
        return courses

    def _parse_html_course_list(self, text: str) -> list[Course]:
        scope = RE_UL_COURSE_LIST.search(text)
        body = scope.group(1) if scope else text
        if not scope:
            self.log.debug("未找到 <ul id='courseList'>，回退到全文扫描 <li>")

        courses: list[Course] = []
        seen: set[tuple[str, str]] = set()

        for match in RE_LI_BLOCK.finditer(body):
            block = match.group(1)
            cid = RE_COURSE_ID.search(block)
            if not cid:
                continue
            clz = RE_CLAZZ_ID.search(block)
            cpi = RE_CPI.search(block)

            course_id = cid.group(1)
            class_id = clz.group(1) if clz else ""
            if (course_id, class_id) in seen:
                continue
            seen.add((course_id, class_id))

            title = ""
            for pattern in (
                r"<a\b[^>]*title=[\"']([^\"']+)[\"']",
                r"<span\b[^>]*class=[\"'][^\"']*course[^\"']*name[^\"']*[\"'][^>]*>(.*?)</span>",
                r"<h3\b[^>]*>(.*?)</h3>",
            ):
                found = re.search(pattern, block, re.S | re.I)
                if found:
                    title = found.group(1)
                    break

            teacher = ""
            found = re.search(
                r"(?:讲师|教师|授课教师)\s*[:：]?\s*([^<\s][^<]{0,40}?)\s*<", block
            )
            if found:
                teacher = found.group(1)

            percent_match = RE_PERCENT.search(block)
            percent = self._to_percent(percent_match.group(1)) if percent_match else 0.0

            courses.append(
                Course(
                    id=course_id,
                    name=_clean_name(title) or f"课程 {course_id}",
                    platform=self.meta.id,
                    teacher=_clean_name(teacher),
                    url=EP_COURSE_PAGE.format(
                        courseid=course_id,
                        clazzid=class_id,
                        cpi=cpi.group(1) if cpi else "",
                        ts=0,
                    ).split("&t=")[0],
                    progress=[Progress("课程进度", round(percent * 100), 100)] if percent else [],
                    extra={
                        "class_id": class_id,
                        "cpi": cpi.group(1) if cpi else "",
                        "platform_name": self.meta.name,
                    },
                )
            )

        self.log.debug(f"课程列表按 HTML 解析，{len(courses)} 条")
        return courses

    @staticmethod
    def _to_percent(value: Any) -> float:
        """把各种写法的进度统一成 0~1。"""
        try:
            number = float(str(value).replace("%", "").strip())
        except (TypeError, ValueError):
            return 0.0
        if number > 0.999 and number <= 100:
            number /= 100.0
        return min(max(number, 0.0), 1.0)

    # ------------------------------------------------------------------ #
    # 章节任务点（实验性）
    # ------------------------------------------------------------------ #

    def _enrich_with_chapters(self, courses: list[Course]) -> None:
        cap = int(self.account.options.get("max_chapters", 40))
        failed = 0
        for course in courses:
            try:
                chapters = self._chapters_of(course)
            except (HttpError, ProviderError) as exc:
                failed += 1
                self.log.debug(f"  {course.name}: 章节读取失败 {exc}")
                continue
            for knowledge_id, chapter_name in chapters[:cap]:
                try:
                    self._attach_cards(course, knowledge_id, chapter_name)
                except (HttpError, ValueError) as exc:
                    self.log.debug(f"  {course.name}/{chapter_name}: 任务点解析失败 {exc}")

        if failed and failed == len(courses):
            self.log.warn(
                "章节任务点全部读取失败——该功能可能已被平台改版。"
                "跑 `studydash probe chaoxing` 取原始响应后再修 providers/chaoxing.py。"
            )

    def _chapters_of(self, course: Course) -> list[tuple[str, str]]:
        url = EP_COURSE_PAGE.format(
            courseid=course.id,
            clazzid=course.extra.get("class_id", ""),
            cpi=course.extra.get("cpi", ""),
            ts="",
        ).replace("&t=", "&t=&")
        html = self.http.get_text(url, note=f"{course.name} 章节目录")
        if not html:
            return []
        if self.looks_logged_out(html):
            raise AuthError("读取章节目录时被重定向到登录页，Cookie 可能已过期")

        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        for block in RE_CHAPTER_BLOCK.finditer(html):
            for _href, kid, label in RE_CHAPTER_LINK.findall(block.group(1)):
                if kid in seen:
                    continue
                seen.add(kid)
                out.append((kid, _clean_name(label)))
        if not out:                                    # 回退：全文扫 knowledgeid
            for _href, kid, label in RE_CHAPTER_LINK.findall(html):
                if kid not in seen:
                    seen.add(kid)
                    out.append((kid, _clean_name(label)))
        return out

    def _attach_cards(self, course: Course, knowledge_id: str, chapter_name: str) -> None:
        url = EP_KNOWLEDGE_CARDS.format(
            clazzid=course.extra.get("class_id", ""),
            courseid=course.id,
            knowledgeid=knowledge_id,
            cpi=course.extra.get("cpi", ""),
            ts="",
        ).replace("&v=", "&v=&")
        html = self.http.get_text(url, note=f"{course.name}/{chapter_name} 任务点")
        if not html:
            return

        match = RE_MARG.search(html)
        if not match:
            raise ValueError("响应里没有 mArg JSON")
        try:
            data = json.loads(match.group(1))
        except ValueError as exc:
            raise ValueError(f"mArg 不是合法 JSON: {exc}") from exc

        for node in _walk_cards(data):
            kind = _kind_of(node.get("property", {}).get("module"), node.get("type"))
            job_id = str(node.get("jobid") or node.get("jobId") or "").strip()
            if not job_id:
                continue
            name = str(
                node.get("property", {}).get("name")
                or node.get("name")
                or chapter_name
            ).strip()
            due = parse_dt(
                node.get("property", {}).get("endTime")
                or node.get("endTime")
                or node.get("dueDate")
            )
            course.assignments.append(
                Assignment(
                    id=job_id,
                    title=f"{chapter_name} · {name}" if name != chapter_name else chapter_name,
                    platform=self.meta.id,
                    course_id=course.id,
                    course=course.name,
                    kind=kind,
                    status="unknown",
                    due=due,
                    detail=chapter_name,
                    platform_name=self.meta.name,
                )
            )


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #

def _walk_cards(data: Any):
    """从 knowledge/cards 的 mArg JSON 里挖出所有带 jobid 的任务对象。"""
    stack: list[Any] = [data]
    seen: set[int] = set()
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if id(node) in seen:
                continue
            seen.add(id(node))
            if node.get("jobid") or node.get("jobId"):
                yield node
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
