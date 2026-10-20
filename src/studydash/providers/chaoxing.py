"""超星学习通（chaoxing / 学习通）适配器。

**只读**：这里全部都是 GET。课程进度、章节任务点、作业与测验状态，都只读出来
展示，不做任何提交动作。

验证状态
--------
* 课程列表 + 课程进度：**已验证**（逻辑自 v1 迁移，长期在用）
* **作业 / 待办列表：已验证**（2026-10-19，真实账号 21 条跨 9 门课）。数据在
  手机版 ``/mooc-ans/work/stu-work`` 的服务端直出 HTML 里，一次 GET 就拿到全账号
  的作业；未提交项再逐条读详情页拿**绝对截止时间**（列表页只给「剩余 X 小时」）。
* 章节任务点：**实验性**，默认关闭。用 ``options.chapters: true`` 打开。这段依赖
  ``knowledge/cards`` 的响应结构，平台改版就会失效——失效时会明确报警告，而不是
  安静地返回空列表。怀疑失效时跑::

      studydash probe chaoxing

  把原始响应贴到 issue 里，改解析逻辑即可，不需要维护者的账号。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from html import unescape as _unescape
from typing import Any

from ..http import HttpError
from ..log import Log
from ..model import Assignment, Course, Progress, now, parse_dt
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
#: 作业列表（手机版）。**不带任何课程参数**也返回全账号的作业，一次 GET 就够：
#: 传 courseId 反而只是被忽略（实测三种写法响应完全相同）。列表里同时有课程 id、
#: 标题、提交状态和「剩余 X 小时」，未提交项的绝对截止时间在详情页。
EP_WORK_LIST = "https://mooc1.chaoxing.com/mooc-ans/work/stu-work?ut=s"

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

# --------------------------------------------------------------------------- #
# 作业列表（手机版 /work/stu-work）
# --------------------------------------------------------------------------- #
#
# 真实结构（2026-10 实测）::
#
#     <li onclick="goTask(this);" data="https://mooc1.chaoxing.com/mooc-ans/
#         android/mtaskmsgspecial?taskrefId=56258341&...&courseId=266688496
#         &clazzId=153613588&type=work&enc_task=…">
#       <div role="option">
#         <p aria-hidden="true">第5章 参数估计</p>
#         <span class="status" aria-hidden="true">未提交</span>
#         <span aria-hidden="true">《大数据统计分...》</span>
#         <span class="fr" aria-hidden="true">剩余995小时51分钟</span>
#       </div>
#     </li>
#
# 已完成/待批阅的条目没有 ``class="status"``，状态直接写成
# ``<span aria-hidden="true">已完成</span>``，而且**没有**「剩余…」。
# 列表里的课程名被截断成「大数据统计分...」，所以课程名一律按 courseId 回查课程
# 列表，别信这段显示文本。
RE_WORK_ITEM = re.compile(
    r"<li\b[^>]*\bonclick=[\"']goTask\(this\);[\"'][^>]*>", re.I
)
RE_WORK_DATA = re.compile(r"\bdata=[\"']([^\"']+)[\"']", re.I)
RE_WORK_TITLE = re.compile(r"<p\b[^>]*>(.*?)</p>", re.S | re.I)
RE_WORK_STATUS = re.compile(
    r"<span\b[^>]*\bclass=[\"'][^\"']*\bstatus\b[^\"']*[\"'][^>]*>(.*?)</span>",
    re.S | re.I,
)
RE_WORK_STATUS_TEXT = re.compile(
    r"<span\b[^>]*\baria-hidden=[\"']true[\"'][^>]*>\s*"
    r"(未提交|待批阅|已批阅|已完成|已过期|未开始)\s*</span>",
    re.I,
)
RE_WORK_COURSE_DISPLAY = re.compile(r"《([^》]*)》")
RE_WORK_REMAIN_HM = re.compile(r"剩余\s*(\d+)\s*小时\s*(\d+)\s*分钟")
RE_WORK_REMAIN_M = re.compile(r"剩余\s*(\d+)\s*分钟")
RE_WORK_REMAIN_D = re.compile(r"剩余\s*(\d+)\s*天(?:\s*(\d+)\s*小时)?")
RE_WORK_DUE_TEXT = re.compile(
    r"截止时间\s*[：:]\s*(\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2})"
)
RE_WORK_DUE_ARIA = re.compile(
    r"截止时间\s*(\d{1,2})月(\d{1,2})日"
    r"(\d{1,2})时(\d{2})分(?:(\d{2})秒)?"
)
#: 已完成作业详情页顶部的分数：``<p class="Number"> 100 <span>分</span>``
RE_WORK_SCORE = re.compile(
    r"class=[\"'][^\"']*\bNumber\b[^\"']*[\"'][^>]*>\s*(\d+(?:\.\d+)?)\s*(?:<|分)", re.I
)

#: 列表里的状态文案 -> (模型 status, 是否已完成)。
#: ``已完成``／``待批阅`` 都算完成（一个已出分、一个等老师批），
#: 这样 ``classify_all`` 会把它们归到 ``done``，不会跑进待办里催人。
_WORK_STATUS_MAP: dict[str, tuple[str, bool]] = {
    "未提交": ("pending", False),
    "未开始": ("pending", False),
    "已过期": ("pending", False),
    "待批阅": ("submitted", True),
    "已批阅": ("graded", True),
    "已完成": ("submitted", True),
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
        login=True,
        homepage="https://i.chaoxing.com",
        verified="2026-10-19",
        note="课程列表、任务点进度（已完成任务点 X/Y）与作业/待办列表都已用真实账号"
        "验证；作业带截止时间与提交状态。章节任务点明细可开（options.chapters）。",
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

        if self.account.options.get("works", True):
            self._fill_works(courses)

        if self.account.options.get("chapters"):
            self._enrich_with_chapters(courses)

        self.log.ok(f"{self.meta.name}: {len(courses)} 门课")
        return courses

    def probe_plan(self) -> list[tuple[str, str]]:
        plan = [("课程列表", EP_COURSE_LIST), ("作业列表", EP_WORK_LIST)]
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
    # 作业 / 待办
    # ------------------------------------------------------------------ #

    def _fill_works(self, courses: list[Course]) -> None:
        """读作业列表并挂到对应课程上。

        列表页一次 GET 返回全账号的作业（2026-10 实测 21 条 / 9 门课），
        所以这里不是「每门课一次请求」。列表本身只写「剩余 X 小时」，
        未提交项的**绝对截止时间**要到详情页拿；已提交/已完成的项不需要
        截止时间（``classify_all`` 会按状态直接归到 ``done``），因此只对
        还没交的项发详情请求，用 ``max_work_details`` 封顶。
        """
        html = self.http.get_text(EP_WORK_LIST, note="作业列表")
        if not html:
            self.log.warn(
                "作业列表没有返回内容，本次待办会是空的（不代表没有作业）。"
                "稍后重试；若持续失败，跑 `studydash probe chaoxing` 留档排查。"
            )
            return

        items = self._parse_work_list(html)
        if not items:
            # 真的没有作业，和「页面结构变了」在这里长得一样。用结构特征区分：
            # 列表页必然有 goTask 点击绑定或「作业列表」标题。
            if "goTask" not in html and "作业列表" not in html:
                self.log.warn(
                    "作业列表页结构无法识别（可能是平台改版），本次待办为空。"
                    "跑 `studydash probe chaoxing` 取原始响应后修 providers/chaoxing.py。"
                )
            return

        by_id = {course.id: course for course in courses}
        cap = int(self.account.options.get("max_work_details", 60))
        details = 0
        attached = 0

        for item in items:
            course = by_id.get(item["course_id"])
            if course is None:
                # 课程列表里没有这门课（课程解析漏了/选了别的学期）。不造假课程，
                # 但留一条 debug，免得以后排查时以为作业凭空消失。
                self.log.debug(
                    f"  作业 {item['title']!r} 的课程 {item['course_id']}"
                    f"（{item['course_display'] or '显示名未知'}）不在课程列表里，跳过"
                )
                continue

            status, finished = _WORK_STATUS_MAP.get(
                item["status"], ("unknown", False)
            )
            due: datetime | None = None
            score: float | None = None

            if not finished:
                if details < cap:
                    detail = self._work_detail(item["url"])
                    details += 1
                    due = detail["due"]
                    score = detail["score"]
                if due is not None:
                    # 详情页的截止时间没写年份（``11-30 10:04``）。用列表里的
                    # 「剩余 X 小时」当锚点挑年份，跨年（12 月看到 1 月）也不会
                    # 把明年的 DDL 算成今年已逾期。
                    due = _align_deadline_year(due, item["remaining_hours"])
                elif item["remaining_hours"] is not None:
                    due = now() + timedelta(hours=item["remaining_hours"])

            if score is not None and status == "submitted":
                status = "graded"

            # 「待批阅」和「已完成」都算 done，但前者还没出分，值得在卡片上
            # 留一句——否则用户看到「已完成」会以为已经批完了。
            detail = item["status"] if item["status"] in {"待批阅", "已批阅"} else ""

            course.assignments.append(
                Assignment(
                    id=item["id"],
                    title=item["title"],
                    platform=self.meta.id,
                    course_id=course.id,
                    course=course.name,
                    kind="homework",
                    status=status,
                    due=due,
                    score=score,
                    url=item["url"],
                    detail=detail,
                    platform_name=self.meta.name,
                )
            )
            attached += 1

        self.log.debug(
            f"  作业：列表 {len(items)} 条，挂到 {attached} 门课的明细上"
            f"（读了 {details} 个详情页）"
        )

    def _parse_work_list(self, html: str) -> list[dict[str, Any]]:
        """解析手机版作业列表，返回普通 dict（方便单测，不直接造 Assignment）。

        按 ``<li onclick="goTask(this)">`` 的开口锚点切块，和课程列表同一套思路：
        不拿 ``</li>`` 当右边界，避免被内层结构截断。
        """
        opens = list(RE_WORK_ITEM.finditer(html))
        items: list[dict[str, Any]] = []

        for index, match in enumerate(opens):
            attrs = match.group(0)
            end = opens[index + 1].start() if index + 1 < len(opens) else len(html)
            block = html[match.end():end]

            data_match = RE_WORK_DATA.search(attrs)
            url = _unescape(data_match.group(1)) if data_match else ""
            if url.startswith("//"):
                url = "https:" + url
            course_id = _query_value(url, "courseId")
            taskref = _query_value(url, "taskrefId")

            title_match = RE_WORK_TITLE.search(block)
            title = _clean_name(title_match.group(1)) if title_match else ""
            if not title:
                continue

            status = ""
            status_match = RE_WORK_STATUS.search(block)
            if status_match:
                status = _strip_tags(status_match.group(1))
            if not status:
                status_match = RE_WORK_STATUS_TEXT.search(block)
                if status_match:
                    status = status_match.group(1)
            status = status.strip()

            display_match = RE_WORK_COURSE_DISPLAY.search(block)
            course_display = _clean_name(display_match.group(1)) if display_match else ""
            course_display = course_display.rstrip("…. ")

            items.append(
                {
                    "id": taskref or url,
                    "title": title,
                    "course_id": course_id,
                    "course_display": course_display,
                    "status": status,
                    "url": url,
                    "remaining_hours": _remaining_hours(block),
                }
            )
        return items

    def _work_detail(self, url: str) -> dict[str, Any]:
        """读一条作业的详情页，取绝对截止时间与分数；失败就当没读到。"""
        if not url:
            return {"due": None, "score": None}
        # ``get_text`` 已经把 HttpError 吞成空串并写进日志，这里只判断有没有内容。
        html = self.http.get_text(url, note="作业详情")
        if not html:
            return {"due": None, "score": None}
        return self._parse_work_detail(html)

    @staticmethod
    def _parse_work_detail(html: str) -> dict[str, Any]:
        """从详情页解析截止时间与分数。

        结构（2026-10 实测）::

            <h4 aria-hidden="true">截止时间：11-30 10:04</h4>
            <span aria-label="截止时间11月30日10时04分46秒"></span>
            ...
            <p class="Number"> 100 <span>分</span> </p>

        ``截止时间`` 不带年份，年份由 :func:`_align_deadline_year` 结合列表里的
        剩余时间补齐；这里只做纯解析，方便单测。
        """
        due: datetime | None = None
        due_match = RE_WORK_DUE_TEXT.search(html)
        if due_match:
            due = parse_dt(due_match.group(1))
        if due is None:
            aria = RE_WORK_DUE_ARIA.search(html)
            if aria:
                month, day, hour, minute = (int(g) for g in aria.groups()[:4])
                due = parse_dt(f"{month}-{day} {hour:02d}:{minute:02d}")

        score: float | None = None
        score_match = RE_WORK_SCORE.search(html)
        if score_match:
            try:
                score = float(score_match.group(1))
            except ValueError:
                score = None

        return {"due": due, "score": score}

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

def _query_value(url: str, key: str) -> str:
    """从 URL 里取一个查询参数（只处理作业列表用到的简单场景）。"""
    match = re.search(rf"[?&]{re.escape(key)}=([^&#]*)", url or "")
    return _unescape(match.group(1)) if match else ""


def _remaining_hours(text: str) -> float | None:
    """把「剩余995小时51分钟」换成小时数。解析不到返回 ``None``。"""
    match = RE_WORK_REMAIN_D.search(text)
    if match:
        days = int(match.group(1))
        hours = int(match.group(2) or 0)
        return days * 24 + hours
    match = RE_WORK_REMAIN_HM.search(text)
    if match:
        return int(match.group(1)) + int(match.group(2)) / 60.0
    match = RE_WORK_REMAIN_M.search(text)
    if match:
        return int(match.group(1)) / 60.0
    return None


def _align_deadline_year(
    due: datetime | None, remaining_hours: float | None
) -> datetime | None:
    """给「月-日 时:分」形式的截止时间补年份。

    ``parse_dt`` 按当年补年份。跨年时这会错：12 月看到「01-05 23:59」会被补成
    今年 1 月（已经过去近一年），实际是明年 1 月。列表里的「剩余 X 小时」就是
    一个靠谱的锚点——在 ``due.year-1/0/+1`` 里挑离 ``now + 剩余`` 最近的那个。
    """
    if due is None or remaining_hours is None:
        return due
    target = now() + timedelta(hours=remaining_hours)
    best = due
    for delta in (-1, 0, 1):
        try:
            candidate = due.replace(year=due.year + delta)
        except ValueError:                      # 2 月 29 日落到平年
            continue
        if abs((candidate - target).total_seconds()) < abs(
            (best - target).total_seconds()
        ):
            best = candidate
    return best


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
