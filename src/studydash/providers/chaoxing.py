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
from html import unescape as _unescape
from typing import Any

from ..http import HttpError
from ..log import Log
from ..model import Assignment, Course, Progress, parse_dt
from .base import AuthError, Provider, ProviderError, ProviderMeta, register

# --------------------------------------------------------------------------- #
# 端点
# --------------------------------------------------------------------------- #

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
#: 同一个字段在两类响应里长得不一样：URL 里是 ``courseid=111``，JSON 里是
#: ``"courseId": 111``。诊断要说「数据在，只是没匹配上」就必须两种都认，
#: 否则一份含 courseId 的 JSON 会被误报成「页面结构变了」。
_RE_COURSE_ID_ANY = re.compile(r"course[Ii]d[\"']?\s*[:=]")
RE_CLAZZ_ID = re.compile(r"cla[zs]{2}[Ii]d=(\d+)")
RE_CPI = re.compile(r"cpi=(\d+)")
RE_PERCENT = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")
RE_TAG = re.compile(r"<[^>]+>")
#: 课程 ``<li>`` 的开口锚点：每个课程条目都带 ``id="c_<courseid>"``。
#:
#: 这里**不能**用一个 ``<li …>(.*?)</li>`` 或 ``(?=<li|</ul>)`` 式的正则——真实
#: 列表页里课程 ``<li>`` 内部嵌了 ``<ul class="hanlde-list">``（"移动到/退课"
#: 菜单），菜单里又是 ``<li>``。任何以 ``<li``/``</ul>`` 当右边界的写法都会停在
#: 第一个嵌套标签处：外层容器被第一个 ``</ul>`` 截断（22 门课只剩 1 门），条目
#: 块在 ``<li class="move-to">`` 处断掉（课名/教师全落在块外，退化成"课程 编号"）。
#: 改成以锚点切分后再按位置取相邻两锚点之间的正文，与嵌套深度无关。
RE_COURSE_ANCHOR = re.compile(r"<li\b[^>]*\bid=[\"']c_(\d+)[\"'][^>]*>", re.I)
#: ``<ul id="courseList">`` 只用来定位起点（不匹配到 ``</ul>``，避免被内层 ul 截断）
RE_UL_COURSE_LIST_OPEN = re.compile(
    r"<ul\b[^>]*id=[\"']courseList[\"'][^>]*>", re.I
)
#: 备用锚点：没有 ``id="c_<courseid>"`` 的老页面用类名 ``class="course …"`` 切分。
#: 这里只抓出 ``class`` 值，是否真是课程条目交给 :func:`_is_course_li` 判——
#: 不能用 ``\bcourse\b``：``class="quit-course"``（退课菜单）同样命中，退课按钮
#: 会被当成一门课，并把所属课程的正文从中间切断。
RE_LI_CLASS_ANCHOR = re.compile(r"<li\b[^>]*\bclass=[\"']([^\"']*)[\"'][^>]*>", re.I)


def _is_course_li(class_value: str) -> bool:
    """``class`` 里要有**独立**的 ``course`` 记号才算课程条目。

    ``course`` / ``course clearfix catalog_0`` / ``course-item`` 都算；
    ``quit-course``、``move-to``、``learnCourse`` 不算。
    """
    return any(
        token == "course" or token.startswith("course-")
        for token in class_value.split()
    )
#: 课名：``<span class="course-name overHidden2" title="…">…</span>``。``title``
#: 属性优先（正文里可能带 ``&nbsp;`` 与学期后缀，属性值是干净的课名）。
RE_COURSE_NAME_BLOCK = re.compile(
    r"<span\b[^>]*class=[\"'][^\"']*course-name[^\"']*[\"'][^>]*>(.*?)</span>", re.S | re.I
)
RE_COURSE_NAME_TITLE = re.compile(
    r"<span\b[^>]*class=[\"'][^\"']*course-name[^\"']*[\"'][^>]*?\btitle=[\"']([^\"']*)[\"']",
    re.I,
)
#: 教师：``<p class="line2 color3" title="尚俊平">尚俊平</p>``。页面里没有"教师"
#: 字样，靠前缀找是找不到的。
RE_COURSE_TEACHER_BLOCK = re.compile(
    r"<p\b[^>]*class=[\"'][^\"']*\bcolor3\b[^\"']*[\"'][^>]*>(.*?)</p>", re.S | re.I
)
RE_COURSE_TEACHER_TITLE = re.compile(
    r"<p\b[^>]*class=[\"'][^\"']*\bcolor3\b[^\"']*[\"'][^>]*?\btitle=[\"']([^\"']*)[\"']",
    re.I,
)
#: 课名兜底：老版本把课名放在链接的 ``title`` 或 ``<a>`` 正文 / ``<h3>`` 里。
#: 真实页命中不到上面那两条时才会走到这里，属于"别退化成占位名"的保险。
RE_LINK_TITLE = re.compile(r"<a\b[^>]*\btitle=[\"']([^\"']*)[\"']", re.I)
RE_H3 = re.compile(r"<h3\b[^>]*>(.*?)</h3>", re.S | re.I)
RE_LINK_TEXT = re.compile(r"<a\b[^>]*>(.*?)</a>", re.S | re.I)
#: 教师兜底：老版本写成 ``讲师：姓名`` / ``授课教师：姓名``。
RE_TEACHER_PREFIX = re.compile(
    r"(?:授课教师|教师|讲师|老师)\s*[：:]\s*([^<\n]{1,40})", re.I
)
RE_MARG = re.compile(r"mArg\s*=\s*(\{[\s\S]*?\})\s*catch", re.I)

#: 课程页头部：``已完成任务点: <span style="color:#00B368">0</span>/77``。
#: 数字常被包在 span/em 里，所以数字两侧都要允许穿标签。
RE_HEAD_TASK = re.compile(
    r"已完成任务点\s*[:：]?\s*(?:<[^<>]{0,200}>\s*)?(\d+)\s*(?:</[^<>]{0,60}>\s*)?"
    r"/\s*(?:<[^<>]{0,200}>\s*)?(\d+)",
    re.I,
)
#: 每个章节节点是一个 ``<div class="chapter_item" id="cur<knowledgeId>" onclick="toOld(...)">``
RE_CHAPTER_ITEM_OPEN = re.compile(
    r"<div\b([^>]*\bclass=[\"'][^\"']*\bchapter_item\b[^\"']*[\"'][^>]*)>", re.I
)
RE_ITEM_ID = re.compile(r"\bid=[\"']cur(\d+)[\"']", re.I)
RE_ITEM_TOOLD = re.compile(r"toOld\s*\(\s*[\"']?\d+[\"']?\s*,\s*[\"']?(\d+)", re.I)
RE_ITEM_SBAR = re.compile(r"class=[\"'][^\"']*\bcatalog_sbar\b[^\"']*[\"'][^>]*>([^<]{0,24})<", re.I)
#: 该节点还剩几个任务点没完成
RE_ITEM_PENDING = re.compile(
    r"class=[\"'][^\"']*\bcatalog_points_yi\b[^\"']*[\"'][^>]*>\s*(\d+)\s*<", re.I
)
#: 该节点挂了几份作业/测验
RE_ITEM_JOBS = re.compile(
    r"<input\b[^>]*\bclass=[\"'][^\"']*\bknowledgeJobCount\b[^\"']*[\"'][^>]*>", re.I
)
RE_ATTR = re.compile(r"\b([\w-]+)=[\"']([^\"']*)[\"']")

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
    # 用 html.unescape 而不是手写实体表：课名/教师里会混 ``&nbsp;``（如
    # ``刘俊娟&nbsp;樊为刚``）以及 ``&#160;`` 这类数字实体，手写表漏一个就会把
    # ``&nbsp;`` 原样印到看板上。
    text = _unescape(text)
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
        status="stable",
        color="#3b82f6",
        auth="cookie",
        homepage="https://i.chaoxing.com",
        verified="2026-10-19",
        note="课程列表与任务点进度（已完成任务点 X/Y）已用真实账号验证；"
        "章节任务点明细可开（options.chapters）。"
        "作业与待办尚未接入——待办显示 0 不代表没有作业，请到课程页确认。",
    )

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #

    def fetch(self) -> list[Course]:
        self.require_cookie()

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

        # 登录态就判这份响应本身：未登录时这个端点会把登录页当 200 直接回给，
        # 已登录才有 <ul id="courseList">。别单独再打一个「自检地址」——多一次
        # 请求是小事，关键是自检地址对**有效会话**也可能返回登录页
        # （i.chaoxing.com/base/settings 实测如此），会把真账号一律判成过期。
        if self.looks_logged_out(raw):
            raise AuthError(
                "Cookie 已过期或未登录",
                hint=(
                    "重新在浏览器登录 https://i.chaoxing.com ，"
                    "复制 Cookie 整行覆盖配置文件里的 cookie 字段。"
                ),
            )

        courses = self._parse_course_list(raw)
        if not courses:
            raise ProviderError(
                "课程列表解析结果为空",
                hint=(
                    f"{self._diagnose_list_page(raw)}\n"
                    "    → 跑 `studydash probe chaoxing` 把原始响应留档下来（data/raw/），"
                    f"解析逻辑在 providers/chaoxing.py。"
                ),
            )

        if self.account.options.get("progress", True):
            self._fill_progress(courses)

        if self.account.options.get("chapters"):
            self._enrich_with_chapters(courses)

        self.log.ok(f"{self.meta.name}: {len(courses)} 门课")
        return courses

    def probe_plan(self) -> list[tuple[str, str]]:
        plan = [("课程列表", EP_COURSE_LIST)]
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
    # 课程列表解析
    # ------------------------------------------------------------------ #

    def _diagnose_list_page(self, text: str) -> str:
        """解析为空时，把「这份响应到底长什么样」讲清楚。

        用户唯一能自己判断的事是「这是我的 Cookie 问题，还是平台改版」。只说一句
        「解析结果为空」等于把问题丢回给用户，所以这里把几个关键事实直接摊开：
        有多少字节、有没有那个 ``<ul id=courseList>`` 容器、容器里有几个 ``<li>``、
        正文像不像登录页。四句话就能把下一步该干什么指出来。
        """
        facts: list[str] = [f"响应 {len(text)} 字节"]
        if self.looks_logged_out(text):
            facts.append("正文看起来是登录页（Cookie 没生效）")

        # 数“有几门课”必须用锚点，不能用 ``<li>`` 总数：每个课程条目里还嵌着
        # 一层菜单 ``<li>``，数 ``<li>`` 会把 22 门课报成 60 多个条目。
        # 两种锚点同样要取并集（按位置去重），不能 ``or``——见 _parse_html_course_list。
        openers = RE_UL_COURSE_LIST_OPEN.search(text)
        starts = {m.start() for m in RE_COURSE_ANCHOR.finditer(text)}
        starts |= {
            m.start() for m in RE_LI_CLASS_ANCHOR.finditer(text) if _is_course_li(m.group(1))
        }
        count = len(starts)
        if openers:
            facts.append(f"找到了 <ul id=courseList>，识别到 {count} 门课")
            if not count:
                facts.append("容器里没有课程条目，可能是这个账号确实没有课程")
        else:
            facts.append("找不到 <ul id=courseList>，页面结构变了或这不是列表页")

        if RE_COURSE_ID.search(text) or _RE_COURSE_ID_ANY.search(text):
            facts.append("正文里出现过 courseId，说明数据在，是提取逻辑没匹配上")
        return "诊断：" + "；".join(facts) + "。"

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
        # 只截到 ``<ul id="courseList">`` 的**开口**，不切到 ``</ul>``：每个课程
        # 条目内部还有一层 ``<ul class="hanlde-list">``，匹配到第一个 ``</ul>``
        # 会把扫描范围砍成第一门课（22 门变 1 门的直接原因）。
        opener = RE_UL_COURSE_LIST_OPEN.search(text)
        if opener:
            body = text[opener.end():]
        else:
            body = text
            self.log.debug("未找到 <ul id='courseList'>，回退到全文扫描")

        # 两种锚点要**合并**着用，不能二选一：真实页面的 22 条同时带着
        # ``id="c_…"`` 和 ``class="course …"``（同一条命中两次，按位置去重），
        # 但老版本页面只有类名。写成 ``a or b`` 的话，只要页面里出现一条
        # ``id="c_…"``，其余只有类名的条目就会被整批丢掉——那正是"22 门课只出
        # 1 门"换了个马甲的样子。
        anchors: list[tuple[int, int, str]] = []
        seen_start: set[int] = set()
        for start, end, cid in (
            [(m.start(), m.end(), m.group(1)) for m in RE_COURSE_ANCHOR.finditer(body)]
            + [
                (m.start(), m.end(), "")
                for m in RE_LI_CLASS_ANCHOR.finditer(body)
                if _is_course_li(m.group(1))
            ]
        ):
            if start in seen_start:
                # 同一条 <li> 被两种锚点各命中一次：``id`` 那版带的 courseid 更可靠。
                if cid:
                    for pos, (s, _e, old) in enumerate(anchors):
                        if s == start and not old:
                            anchors[pos] = (s, end, cid)
                            break
                continue
            seen_start.add(start)
            anchors.append((start, end, cid))
        anchors.sort(key=lambda item: item[0])
        if anchors and all(not cid for _s, _e, cid in anchors):
            self.log.debug("课程条目没有 id='c_…'，改用 class 锚点切分")

        courses: list[Course] = []
        seen: set[tuple[str, str]] = set()

        for index, (_start, content_start, anchor_id) in enumerate(anchors):
            # 一门课的正文 = 本锚点之后到下一个锚点之前。按位置切，嵌套多深都行。
            end = anchors[index + 1][0] if index + 1 < len(anchors) else len(body)
            block = body[content_start:end]

            course_id = anchor_id
            if not course_id:
                found = RE_COURSE_ID.search(block)
                course_id = found.group(1) if found else ""
            if not course_id:
                continue

            clz = RE_CLAZZ_ID.search(block)
            cpi = RE_CPI.search(block)

            class_id = clz.group(1) if clz else ""
            if (course_id, class_id) in seen:
                continue
            seen.add((course_id, class_id))

            # 课名。真实列表页用 ``<span class="course-name" title="…">``；其他版本
            # 把课名放在链接的 ``title`` 或正文里，所以保留一串兜底——但顺序上
            # 一定要先试 ``course-name``，否则会先撞上侧栏那种无意义的标题。
            title = ""
            for extract in (
                RE_COURSE_NAME_TITLE.search,
                RE_COURSE_NAME_BLOCK.search,
                RE_LINK_TITLE.search,
                RE_H3.search,
                RE_LINK_TEXT.search,
            ):
                found = extract(block)
                if found and found.group(1).strip():
                    title = found.group(1)
                    break

            # 教师。真实页是 ``<p class="line2 color3" title="姓名">``，页面里没有
            # "教师"字样；旧版页面则写成 ``讲师：姓名``——两种都要认。
            teacher = ""
            for extract in (
                RE_COURSE_TEACHER_TITLE.search,
                RE_COURSE_TEACHER_BLOCK.search,
                RE_TEACHER_PREFIX.search,
            ):
                found = extract(block)
                if found and found.group(1).strip():
                    teacher = found.group(1)
                    break

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
    # 课程进度（任务点）
    # ------------------------------------------------------------------ #

    def _fill_progress(self, courses: list[Course]) -> None:
        """逐门课读课程页，取「已完成任务点 X/Y」当进度。

        这是唯一可靠的进度来源：课程列表页只给课程名与班级，没有百分比。
        每门课一次 GET，22 门课约 10 秒，所以设了上限 ``options.max_progress``。
        """
        cap = int(self.account.options.get("max_progress", 60))
        filled = 0
        for course in courses[:cap]:
            info = self._course_page_info(course)
            if info is None:
                continue
            total, done = info["total"], info["done"]
            if total > 0:
                course.progress = [
                    Progress(label="任务点", done=min(done, total), total=total)
                ]
                filled += 1
            course.extra["task_points_total"] = total
            course.extra["task_points_pending"] = info["pending"]
            course.extra["chapter_count"] = len(info["nodes"])
        self.log.debug(f"  进度：{filled}/{min(len(courses), cap)} 门课读到任务点数据")

    def _course_page_url(self, course: Course) -> str:
        return EP_COURSE_PAGE.format(
            courseid=course.id,
            clazzid=course.extra.get("class_id", ""),
            cpi=course.extra.get("cpi", ""),
            ts="",
        ).replace("&t=", "&t=&")

    def _course_page_info(self, course: Course) -> dict[str, Any] | None:
        """读一门课的课程页并解析；失败返回 None（登录失效则抛出）。"""
        url = self._course_page_url(course)
        try:
            html = self.http.get_text(url, note=f"{course.name} 课程页")
        except (HttpError, ProviderError) as exc:
            self.log.debug(f"  {course.name}: 课程页读取失败 {exc}")
            return None
        if not html:
            return None
        if "passport2.chaoxing.com" in html[:4000] or "用户登录" in html[:4000]:
            raise AuthError(
                "读取课程进度时被重定向到登录页，Cookie 可能已过期",
                hint="重新登录后复制整行 Cookie 覆盖配置。",
            )
        return self._parse_course_page(html)

    def _parse_course_page(self, html: str) -> dict[str, Any]:
        """从课程页解析总任务点进度与章节节点。

        结构（2026-10 实测）::

            <h2 class="xs_head_name">… 已完成任务点: <span>0</span>/77 …
            <div class="chapter_item" id="cur1236661559"
                 onclick="toOld('266688496','1236661559','153613588',0)"
                 title="统计学的性质与分类">
              <span class="catalog_sbar">1.1</span>
              <input type="hidden" value="1" class="knowledgeJobCount"/>
              <span class="catalog_points_yi">1</span><span class="bntHoverTips">1个待完成任务点</span>
        """
        head = RE_HEAD_TASK.search(html)
        info: dict[str, Any] = {
            "done": int(head.group(1)) if head else 0,
            "total": int(head.group(2)) if head else 0,
            "pending": 0,
            "nodes": [],
        }

        opens = list(RE_CHAPTER_ITEM_OPEN.finditer(html))
        for index, match in enumerate(opens):
            attrs = match.group(1)
            end = opens[index + 1].start() if index + 1 < len(opens) else len(html)
            body = html[match.end():end]

            kid = ""
            id_match = RE_ITEM_ID.search(attrs) or RE_ITEM_TOOLD.search(attrs)
            if id_match:
                kid = id_match.group(1)
            if not kid:                      # 一级节点（「第1章 …」）只是分组标题
                continue

            sbar_match = RE_ITEM_SBAR.search(body)
            sbar = _clean_name(sbar_match.group(1)) if sbar_match else ""
            title = dict(RE_ATTR.findall(attrs)).get("title", "")
            name = _clean_name(title) or sbar

            pending_match = RE_ITEM_PENDING.search(body)
            pending = int(pending_match.group(1)) if pending_match else 0
            jobs = 0
            jobs_match = RE_ITEM_JOBS.search(body)
            if jobs_match:
                value = dict(RE_ATTR.findall(jobs_match.group(0))).get("value", "")
                jobs = int(value) if value.isdigit() else 0

            info["pending"] += pending
            info["nodes"].append(
                {
                    "id": kid,
                    "name": name,
                    "sbar": sbar,
                    "pending": pending,
                    "jobs": jobs,
                }
            )
        return info

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
        """章节列表走课程页解析。

        旧实现按 ``knowledgeid=`` 扫链接，但 2026-10 的课程页里根本没这个参数
        （节点是 ``<div class="chapter_item" id="cur<kid>">``），所以一直是 0 条。
        """
        info = self._course_page_info(course)
        if info is None:
            return []
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        for node in info["nodes"]:
            kid = node["id"]
            if kid in seen:
                continue
            seen.add(kid)
            out.append((kid, node["name"]))
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
