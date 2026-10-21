"""章节测验自动答题（把上游 A 能力搬进 studydash）。

流程（照抄 ``Samueli924/chaoxing`` 实测过的协议，参数一字不改）：

1. 从知识卡页里挑 ``type == "workid"`` 的任务点（这类就是章节测验）；
2. ``GET https://mooc1.chaoxing.com/mooc-ans/api/work`` 取整页题目；
3. 解析题面——学习通把题干和选项用换形字体加密，:mod:`studydash.cxfont`
   按字形指纹还原；
4. 逐题查题库（:mod:`studydash.tiku`），把答案文本换成平台要的形态
   （单选/多选给字母、判断给 ``true``/``false``、填空给原文）；
5. 覆盖率到阈值才提交：``POST …/work/addStudentWorkNew``。

两条硬规矩，和写回模块一致：

* **默认演练**。``apply=False`` 时一个请求都不往提交接口发，只把「哪门课哪道
  测验、几道题、题库覆盖几道」摆出来。
* **不谎报**。``status`` 为假就记失败；覆盖率不够就不提交（平台会记一次低分
  作答，比不提交更糟），原因写清楚。
"""

from __future__ import annotations

import difflib
import json
import re
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import cxfont
from .htmlx import Node, parse
from .http import HttpClient, HttpError
from .model import Course
from .providers.base import HttpSettings, ProviderError
from .providers.chaoxing import ChaoxingProvider, extract_marg
from .writeback import (
    _assert_prefix,
    _walk_cards,
)

__all__ = [
    "EP_SUBMIT",
    "EP_WORK",
    "Question",
    "QuizError",
    "QuizPage",
    "QuizRunner",
    "QuizTarget",
    "answer_to_field",
    "parse_page",
]

#: 取题目（整页 HTML，题面在里面）
EP_WORK = "https://mooc1.chaoxing.com/mooc-ans/api/work"

#: 交卷 / 保存答案
EP_SUBMIT = "https://mooc1.chaoxing.com/mooc-ans/work/addStudentWorkNew"

#: 章节测验的题型代码 -> 名字（平台就这么编的号）
TYPE_NAMES = {
    "0": "single",
    "1": "multiple",
    "2": "completion",
    "3": "judgement",
    "4": "shortanswer",
}

_TYPE_LABEL = {
    "single": "单选",
    "multiple": "多选",
    "completion": "填空",
    "judgement": "判断",
    "shortanswer": "简答",
    "unknown": "未知",
}

#: 判断题的两套说法（题库回什么都认）
JUDGE_TRUE = frozenset({"true", "t", "1", "对", "正确", "√", "是", "yes", "y", "a"})
JUDGE_FALSE = frozenset({"false", "f", "0", "错", "错误", "×", "否", "no", "n", "不对", "不正确", "b"})

_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

#: 多选答案里用来切分的分隔符
_SPLIT = re.compile(r"[\n、,，;；|/]+")

#: 交卷时带的头（平台会看 X-Requested-With 与 Origin）
_SUBMIT_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Origin": "https://mooc1.chaoxing.com",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
}

_WORK_HEADERS = {"X-Requested-With": "XMLHttpRequest", "Accept": "*/*"}


class QuizError(Exception):
    """测验用不了（页面没题目、题库没配、平台回的不是 JSON）。"""


@dataclass(slots=True)
class Question:
    """一道题。"""

    #: 平台给的题号，答案字段名就是 ``answer{id}``
    id: str
    #: 题型代码（"0" 单选 / "1" 多选 / "2" 填空 / "3" 判断 / "4" 简答）
    type_code: str
    #: 题干（已解字体加密）
    title: str = ""
    #: 选项，形如 ``[("A", "计算机"), ("B", "统计学")]``
    options: list[tuple[str, str]] = field(default_factory=list)

    @property
    def type_name(self) -> str:
        return TYPE_NAMES.get(self.type_code, "unknown")

    @property
    def type_label(self) -> str:
        return _TYPE_LABEL[self.type_name]

    @property
    def options_text(self) -> str:
        return "\n".join(f"{letter}. {text}" for letter, text in self.options)


@dataclass(slots=True)
class QuizPage:
    """一页测验：表单里的隐藏字段 + 题目列表。"""

    fields: dict[str, str] = field(default_factory=dict)
    questions: list[Question] = field(default_factory=list)

    def submit_data(self, answers: dict[str, str], *, save_only: bool) -> dict[str, str]:
        """拼提交表单。``save_only`` 时 ``pyFlag="1"``（只存不交）。"""
        data = dict(self.fields)
        for question in self.questions:
            data[f"answer{question.id}"] = answers.get(question.id, "")
            data[f"answertype{question.id}"] = question.type_code
        data["answerwqbid"] = ",".join(q.id for q in self.questions) + ","
        data["pyFlag"] = "1" if save_only else ""
        return data


@dataclass(slots=True)
class QuizTarget:
    """一个待做的章节测验。"""

    course_id: str
    course_name: str
    clazz_id: str
    cpi: str
    knowledge_id: str
    ktoken: str
    jobid: str
    enc: str
    chapter: str = ""
    title: str = ""

    @property
    def label(self) -> str:
        name = self.title or self.jobid
        return f"{self.course_name} / {self.chapter} / {name}" if self.chapter else f"{self.course_name} / {name}"

    @property
    def work_id(self) -> str:
        return self.jobid.replace("work-", "")


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #

def _clean_text(node: Node | None) -> str:
    if node is None:
        return ""
    return re.sub(r"[\r\t\n]+", "", node.all_text).strip()


def _options_of(question_div: Node, html: str) -> list[tuple[str, str]]:
    ul = question_div.find("ul")
    if ul is None:
        return []
    out: list[tuple[str, str]] = []
    for index, li in enumerate(ul.findall("li")):
        letter = _LETTERS[index] if index < len(_LETTERS) else str(index + 1)
        # 平台把选项文字塞在 aria-label 里，回退到整段文本
        raw = str(li.attrs.get("aria-label") or "").strip() or _clean_text(li)
        text = _decode(html, raw)
        # 有的课程 aria-label 是「A 太湖石」（字母+空格），有的是「A、太湖石」
        text = re.sub(r"^[A-Z][\s、.．:：]{1,3}\s*", "", text).strip()
        if text.endswith("选择"):
            text = text[:-2].strip()
        out.append((letter, text))
    return out


def _decode(html: str, text: str) -> str:
    """字体重命名解密；没加密或解不开时原样返回。"""
    if not text:
        return ""
    try:
        return cxfont.decode_text(html, text)
    except cxfont.FontDecodeError:
        return text


def parse_page(html: str) -> QuizPage:
    """解析测验页：隐藏表单字段 + 全部题目（题干与选项都已解密）。"""
    root = parse(html)
    page = QuizPage()
    form = root.find("form") or root
    for input_tag in form.findall("input"):
        name = str(input_tag.attrs.get("name") or "").strip()
        if not name or "answer" in name.lower():
            continue
        page.fields[name] = str(input_tag.attrs.get("value") or "")
    for div in root.findall("div", cls="singleQuesId"):
        qid = str(div.attrs.get("data") or "").strip()
        if not qid:
            continue
        ti_mu = div.find("div", cls="TiMu")
        type_code = str((ti_mu.attrs.get("data") if ti_mu is not None else "") or "").strip()
        title = _decode(html, _clean_text(div.find("div", cls="Zy_TItle")))
        page.questions.append(
            Question(
                id=qid,
                type_code=type_code or "0",
                title=title,
                options=_options_of(div, html),
            )
        )
    return page


# --------------------------------------------------------------------------- #
# 答案映射
# --------------------------------------------------------------------------- #

def _normalize(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", text or "").lower()


def _pick_option(answer: str, options: Sequence[tuple[str, str]]) -> str:
    """把答案文本落到某个选项字母上；对不上返回空串。"""
    if not options:
        return ""
    raw = (answer or "").strip()
    if not raw:
        return ""
    # 1) 题库直接给字母（"A" / "A." / "A、"）；"AC" 这种多选整串留到多选分支处理
    letters = {letter for letter, _text in options}
    head = re.sub(r"[\s、.．:：)）]+$", "", raw)
    if len(head) == 1 and head.upper() in letters:
        return head.upper()
    # 2) 字面命中：答案就是选项原文（去掉序号后）
    want = _normalize(raw)
    if not want:
        return ""
    for letter, text in options:
        if _normalize(text) == want:
            return letter
    # 3) 包含关系：答案短，选项长（"计算机" ⊂ "计算机科学与技术"）
    for letter, text in options:
        norm = _normalize(text)
        if norm and (want in norm or norm in want):
            return letter
    # 4) 相似度兜底，阈值压得比较保守，宁可判「不知道」也别乱选
    best_letter = ""
    best_ratio = 0.0
    for letter, text in options:
        ratio = difflib.SequenceMatcher(None, want, _normalize(text)).ratio()
        if ratio > best_ratio:
            best_letter, best_ratio = letter, ratio
    return best_letter if best_ratio >= 0.6 else ""


def answer_to_field(question: Question, answer: str) -> str:
    """把题库的答案原文换成平台要的字符串。换不出来返回空串。"""
    raw = (answer or "").strip()
    if not raw:
        return ""
    kind = question.type_name
    if kind == "judgement":
        low = raw.lower()
        if low in JUDGE_TRUE:
            return "true"
        if low in JUDGE_FALSE:
            return "false"
        return ""
    if kind == "single":
        return _pick_option(raw, question.options)
    if kind == "multiple":
        picked: list[str] = []
        for chunk in _SPLIT.split(raw):
            chunk = chunk.strip()
            if not chunk:
                continue
            letter = _pick_option(chunk, question.options)
            if letter and letter not in picked:
                picked.append(letter)
        if picked:
            return "".join(sorted(picked))
        # 题库把多选写成一整串（"ABD"）
        compact = re.sub(r"[\s\W_]+", "", raw).upper()
        if compact and all(ch in {letter for letter, _ in question.options} for ch in compact):
            return "".join(sorted(set(compact)))
        return ""
    if kind == "completion":
        return raw
    if kind == "shortanswer":
        return raw
    return ""


# --------------------------------------------------------------------------- #
# 执行
# --------------------------------------------------------------------------- #

class QuizRunner:
    """把一门课的章节测验做掉。

    和 :class:`studydash.writeback.ChaoxingVideoWriter` 一样，HTTP 出口按线程
    隔离：CookieJar 与限速标记都不是线程安全的，共享会把并发变成互相踩。
    """

    def __init__(
        self,
        account: Any,
        settings: HttpSettings | None = None,
        *,
        log: Any = None,
        apply: bool = False,
        tiku: Any = None,
        cover_rate: float = 0.8,
        max_pages: int = 20,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.account = account
        self.settings = settings or HttpSettings()
        self.log = log
        self.apply = bool(apply)
        self.tiku = tiku
        self.cover_rate = max(0.0, min(1.0, float(cover_rate)))
        self.max_pages = max(1, int(max_pages))
        self.sleep = sleep
        self.user_id = ""
        self.http = HttpClient(
            cookie=account.cookie,
            timeout=self.settings.timeout,
            delay=self.settings.delay,
            user_agent=self.settings.user_agent,
            retries=self.settings.retries,
            logger=self._debug,
            query_endpoints=(EP_SUBMIT,),
        )
        self.provider = ChaoxingProvider(account, self.settings, log=log)
        self.blocked = ""
        #: 每题查询之间歇多久，给题库留余地
        self.query_delay = 1.0

    # -- 小工具 ------------------------------------------------------------- #

    def _debug(self, message: str) -> None:
        if self.log is not None:
            self.log.debug(message)

    def _warn(self, message: str) -> None:
        if self.log is not None:
            self.log.warn(message)

    def _info(self, message: str) -> None:
        if self.log is not None:
            self.log.info(message)

    # -- 采集 --------------------------------------------------------------- #

    def chapters(self, course: Course) -> list[tuple[str, str]]:
        try:
            return self.provider._chapters_of(course)
        except (HttpError, ProviderError) as exc:
            self._warn(f"  {course.name}: 章节读取失败 {exc}")
            return []

    def _cards(self, course: Course, knowledge_id: str, page: int) -> dict[str, Any] | None:
        from .writeback import EP_CARDS, READ_URL_PREFIXES

        url = EP_CARDS.format(
            clazzid=course.extra.get("class_id", ""),
            courseid=course.id,
            knowledgeid=knowledge_id,
            num=page,
            cpi=course.extra.get("cpi", ""),
        )
        _assert_prefix(url, READ_URL_PREFIXES, "知识卡页")
        html = self.http.get_text(url, note=f"卡片页 {knowledge_id}#{page}")
        if not html:
            return None
        raw = extract_marg(html)
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def iter_targets(self, course: Course) -> Iterator[QuizTarget]:
        """列出这门课的章节测验（只读，不发任何写请求）。"""
        clazz_id = str(course.extra.get("class_id", ""))
        cpi = str(course.extra.get("cpi", ""))
        for knowledge_id, chapter_name in self.chapters(course):
            for page in range(self.max_pages):
                data = self._cards(course, knowledge_id, page)
                if data is None:
                    break
                defaults = data.get("defaults")
                if not isinstance(defaults, dict):
                    defaults = {}
                clazz_id = str(defaults.get("clazzId") or clazz_id)
                cpi = str(defaults.get("cpi") or cpi)
                ktoken = str(defaults.get("ktoken") or "")
                kid = str(defaults.get("knowledgeid") or knowledge_id)
                for node in _walk_cards(data):
                    if str(node.get("type") or "").lower() != "workid":
                        continue
                    jobid = str(node.get("jobid") or node.get("jobId") or "")
                    if not jobid:
                        continue
                    prop = node.get("property") if isinstance(node.get("property"), dict) else {}
                    yield QuizTarget(
                        course_id=str(course.id),
                        course_name=course.name,
                        clazz_id=clazz_id,
                        cpi=cpi,
                        knowledge_id=kid,
                        ktoken=ktoken,
                        jobid=jobid,
                        enc=str(node.get("enc") or ""),
                        chapter=chapter_name,
                        title=str(prop.get("title") or prop.get("name") or ""),
                    )

    def plan(self, courses: Sequence[Course], *, limit: int = 0) -> list[tuple[Course, QuizTarget]]:
        """清点所有章节测验，返回 ``[(course, target), …]``。"""
        out: list[tuple[Course, QuizTarget]] = []
        for course in courses:
            for target in self.iter_targets(course):
                out.append((course, target))
                if limit and len(out) >= limit:
                    return out
        return out

    # -- 取题 --------------------------------------------------------------- #

    def fetch_page(self, target: QuizTarget) -> str:
        """取测验整页 HTML。"""
        params = {
            "api": "1",
            "workId": target.work_id,
            "jobid": target.jobid,
            "originJobId": target.jobid,
            "needRedirect": "true",
            "skipHeader": "true",
            "knowledgeid": target.knowledge_id,
            "ktoken": target.ktoken,
            "cpi": target.cpi,
            "ut": "s",
            "clazzId": target.clazz_id,
            "type": "",
            "enc": target.enc,
            "mooc2": "1",
            "courseid": target.course_id,
        }
        url = EP_WORK + "?" + "&".join(f"{k}={_quote(v)}" for k, v in params.items())
        html = self.http.get_text(url, headers=_WORK_HEADERS, note=f"测验 {target.jobid}")
        return html or ""

    # -- 作答 --------------------------------------------------------------- #

    def solve(self, target: QuizTarget) -> dict[str, Any]:
        """做一份测验。返回一行结果（结构同写回模块）。"""
        result: dict[str, Any] = {
            "label": target.label,
            "jobid": target.jobid,
            "course_id": target.course_id,
            "kind": "workid",
            "applied": self.apply,
            "ok": None,
            "status": "",
            "reason": "",
            "requests": 0,
            "total": 0,
            "answered": 0,
            "submitted": False,
        }
        if self.blocked:
            result["ok"] = False
            result["status"] = "failed"
            result["reason"] = self.blocked
            return result
        html = self.fetch_page(target)
        result["requests"] += 1
        if not html:
            result["ok"] = False
            result["status"] = "failed"
            result["reason"] = "取不到测验页（Cookie 过期或被拦）"
            return result
        if "教师未创建完成该测验" in html:
            result["ok"] = None
            result["status"] = "skipped"
            result["reason"] = "平台说「教师未创建完成该测验」，跳过"
            return result
        page = parse_page(html)
        result["total"] = len(page.questions)
        if not page.questions:
            result["ok"] = False
            result["status"] = "failed"
            result["reason"] = "页面上没有解析到题目（可能已做过或页面结构变了）"
            return result

        answers, hits = self._answers(page)
        result["answered"] = hits
        coverage = hits / len(page.questions) if page.questions else 0.0
        # 覆盖率不够就不交卷：平台会把低分作答记进成绩，比不交更糟
        save_only = coverage < self.cover_rate
        data = page.submit_data(answers, save_only=save_only)
        if not self.apply:
            result["ok"] = None
            result["status"] = "dry"
            result["reason"] = (
                f"演练：{len(page.questions)} 道题，题库覆盖 {hits} 道"
                f"（{coverage:.0%}），{'会只保存' if save_only else '会交卷'}"
            )
            return result
        return self._submit(target, data, result, coverage=coverage, save_only=save_only)

    def _answers(self, page: QuizPage) -> tuple[dict[str, str], int]:
        """逐题查题库并换成平台答案，返回 ``({题号: 答案}, 命中数)``。"""
        answers: dict[str, str] = {}
        hits = 0
        for index, question in enumerate(page.questions):
            field_value = ""
            if self.tiku is not None:
                if index and self.query_delay:
                    self.sleep(self.query_delay)
                reply = self.tiku.query(question.title)
                if reply.answer:
                    field_value = answer_to_field(question, reply.answer)
                    if not field_value:
                        self._debug(f"    题库答案对不上选项：{reply.answer!r} / {question.title[:20]}")
                elif reply.message:
                    self._debug(f"    题库没答案：{reply.message}")
            if field_value:
                hits += 1
                self._info(f"  {question.type_label} {question.title[:24]} -> {field_value}")
            else:
                self._debug(f"  {question.type_label} {question.title[:24]} -> 没有答案")
            answers[question.id] = field_value
        return answers, hits

    def _submit(
        self,
        target: QuizTarget,
        data: dict[str, str],
        result: dict[str, Any],
        *,
        coverage: float,
        save_only: bool,
    ) -> dict[str, Any]:
        del target
        try:
            resp = self.http.post_query(EP_SUBMIT, data, headers=_SUBMIT_HEADERS, note="交卷")
        except HttpError as exc:
            result["ok"] = False
            result["status"] = "failed"
            result["reason"] = self.blocked or f"交卷请求失败：{exc.reason}"
            return result
        result["requests"] += 1
        result["submitted"] = not save_only
        try:
            payload = json.loads(resp.content.decode("utf-8", "replace"))
        except ValueError:
            text = resp.content.decode("utf-8", "replace")[:200]
            result["ok"] = False
            result["status"] = "failed"
            result["reason"] = f"平台没回 JSON：{text}"
            return result
        status = bool(payload.get("status")) if isinstance(payload, dict) else False
        message = str(payload.get("msg") or "") if isinstance(payload, dict) else ""
        if save_only:
            result["ok"] = False
            result["status"] = "unconfirmed"
            result["reason"] = (
                f"题库只覆盖 {coverage:.0%}，没交卷（只保存了答案）：{message or '平台没说明'}"
            )
            return result
        if status:
            result["ok"] = True
            result["status"] = "passed"
            result["reason"] = f"平台已收卷：{message or 'ok'}"
        else:
            result["ok"] = False
            result["status"] = "unconfirmed"
            result["reason"] = f"平台没确认：{message or payload}"
        return result


# --------------------------------------------------------------------------- #
# 杂项
# --------------------------------------------------------------------------- #

def _quote(text: str) -> str:
    from urllib.parse import quote

    return quote(str(text), safe="")
