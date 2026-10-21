"""刷章节学习次数（把上游 B 能力搬进 studydash）。

学习通算「学习次数」看的是三个东西，缺一个都不计数：

1. ``GET …/mycourse/studentstudyAjax`` —— 让服务端把这一章标记为「本次访问」，
   响应里带一个**当场签名**的统计地址；
2. ``GET https://fystat-ans.chaoxing.com/log/setlog?…`` —— 这才是真正的计次
   接口，``encode`` 参数由上面那步给出，自己拼不出来；
3. ``GET https://detect.chaoxing.com/api/monitor`` —— 心跳。上游实测：计次后
   立刻发一次，等 30 秒再发一次，这一轮才算落库。

所以本模块只能按「访问 → setlog → 心跳 → 等 30 秒 → 心跳」的顺序老老实实走，
并发没有意义（服务端按时间窗口算次数）。默认仍然是**演练**：不发请求，只列出
将要刷哪些章节、要刷几轮、预计多久。
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from .http import HttpClient, HttpError
from .model import Course
from .providers.base import HttpSettings, ProviderError
from .providers.chaoxing import ChaoxingProvider

__all__ = [
    "EP_MONITOR",
    "EP_STUDENT_STUDY",
    "RE_SETLOG",
    "LearningCounter",
    "LearningError",
    "VisitTarget",
    "parse_setlog_url",
]

#: 让服务端生成本次访问（响应里带 setlog 签名地址）
EP_STUDENT_STUDY = "https://mooc1.chaoxing.com/mooc-ans/mycourse/studentstudyAjax"

#: 心跳
EP_MONITOR = "https://detect.chaoxing.com/api/monitor"

#: 从 studentstudyAjax 的 HTML 里抠出计次地址
RE_SETLOG = re.compile(r"""<script[^>]+src=["'](https://fystat-ans\.chaoxing\.com/log/setlog[^"']*)["']""")

#: 上游实测的等待：计次后 30 秒再补一次心跳
HEARTBEAT_WAIT = 30.0

_STUDY_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "text/html, */*; q=0.01",
}


class LearningError(Exception):
    """刷次数用不了（没章节、Cookie 过期）。"""


@dataclass(slots=True)
class VisitTarget:
    """一次「访问某一章」的目标。"""

    course_id: str
    course_name: str
    clazz_id: str
    cpi: str
    chapter_id: str
    chapter_name: str

    @property
    def label(self) -> str:
        return f"{self.course_name} / {self.chapter_name}"


def parse_setlog_url(html: str) -> str:
    """从 studentstudyAjax 的响应里取计次地址；没有就返回空串。"""
    if not html:
        return ""
    match = RE_SETLOG.search(html)
    return match.group(1) if match else ""


class LearningCounter:
    """按章节刷学习次数。"""

    def __init__(
        self,
        account: Any,
        settings: HttpSettings | None = None,
        *,
        log: Any = None,
        apply: bool = False,
        interval: float = HEARTBEAT_WAIT,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.account = account
        self.settings = settings or HttpSettings()
        self.log = log
        self.apply = bool(apply)
        self.interval = max(0.0, float(interval))
        self.sleep = sleep
        self.http = HttpClient(
            cookie=account.cookie,
            timeout=self.settings.timeout,
            delay=self.settings.delay,
            user_agent=self.settings.user_agent,
            retries=self.settings.retries,
            logger=self._debug,
        )
        self.provider = ChaoxingProvider(account, self.settings, log=log)
        self.fid = _cookie_value(account.cookie, "fid")
        self.blocked = ""

    # -- 小工具 ------------------------------------------------------------- #

    def _debug(self, message: str) -> None:
        if self.log is not None:
            self.log.debug(message)

    def _warn(self, message: str) -> None:
        if self.log is not None:
            self.log.warn(message)

    # -- 采集 --------------------------------------------------------------- #

    def chapters(self, course: Course) -> list[tuple[str, str]]:
        try:
            return self.provider._chapters_of(course)
        except (HttpError, ProviderError) as exc:
            self._warn(f"  {course.name}: 章节读取失败 {exc}")
            return []

    def iter_targets(self, course: Course) -> Iterator[VisitTarget]:
        clazz_id = str(course.extra.get("class_id", ""))
        cpi = str(course.extra.get("cpi", ""))
        for chapter_id, chapter_name in self.chapters(course):
            chapter_id = str(chapter_id)
            if not chapter_id.isdigit():
                continue
            yield VisitTarget(
                course_id=str(course.id),
                course_name=course.name,
                clazz_id=clazz_id,
                cpi=cpi,
                chapter_id=chapter_id,
                chapter_name=chapter_name,
            )

    def plan(self, courses: Sequence[Course], *, limit: int = 0) -> list[tuple[Course, VisitTarget]]:
        """清点要刷的章节。"""
        out: list[tuple[Course, VisitTarget]] = []
        for course in courses:
            for target in self.iter_targets(course):
                out.append((course, target))
                if limit and len(out) >= limit:
                    return out
        return out

    # -- 一轮 --------------------------------------------------------------- #

    def visit(self, target: VisitTarget) -> dict[str, Any]:
        """刷一次。返回一行结果（结构同其他写回模块）。"""
        result: dict[str, Any] = {
            "label": target.label,
            "course_id": target.course_id,
            "chapter_id": target.chapter_id,
            "kind": "learning-count",
            "applied": self.apply,
            "ok": None,
            "status": "",
            "reason": "",
            "requests": 0,
        }
        if not self.apply:
            result["ok"] = None
            result["status"] = "dry"
            result["reason"] = "演练：会访问这一章、发一次计次、补两次心跳"
            return result
        if self.blocked:
            result["ok"] = False
            result["status"] = "failed"
            result["reason"] = self.blocked
            return result
        html = self._study_ajax(target, result)
        if html is None:
            return result
        setlog = parse_setlog_url(html)
        if not setlog:
            result["ok"] = False
            result["status"] = "failed"
            result["reason"] = "服务端没给计次地址（章节可能没开放，或 Cookie 已过期）"
            return result
        if not self._hit(setlog, result, note="计次"):
            return result
        self._heartbeat(target, result)
        if self.interval:
            self.sleep(self.interval)
        self._heartbeat(target, result)
        result["ok"] = True
        result["status"] = "passed"
        result["reason"] = "已计次并补了两次心跳"
        return result

    def _study_ajax(self, target: VisitTarget, result: dict[str, Any]) -> str | None:
        params = {
            "courseId": target.course_id,
            "clazzid": target.clazz_id,
            "chapterId": target.chapter_id,
            "cpi": target.cpi,
            "verificationcode": "",
            "mooc2": "1",
            "microTopicId": "0",
            "editorPreview": "0",
        }
        url = EP_STUDENT_STUDY + "?" + "&".join(f"{k}={_quote(v)}" for k, v in params.items())
        try:
            resp = self.http.get(url, headers=_STUDY_HEADERS, note=f"访问章节 {target.chapter_id}")
        except HttpError as exc:
            self._fail(result, exc.reason)
            return None
        result["requests"] += 1
        if getattr(resp, "status", 200) != 200:
            self._fail(result, f"访问章节回了 HTTP {resp.status}")
            return None
        return resp.content.decode("utf-8", "replace")

    def _hit(self, url: str, result: dict[str, Any], *, note: str) -> bool:
        try:
            resp = self.http.get(url, headers=_STUDY_HEADERS, note=note)
        except HttpError as exc:
            self._fail(result, exc.reason)
            return False
        result["requests"] += 1
        if getattr(resp, "status", 200) != 200:
            self._fail(result, f"{note}回了 HTTP {resp.status}")
            return False
        return True

    def _heartbeat(self, target: VisitTarget, result: dict[str, Any]) -> None:
        stamp = int(time.time() * 1000)
        params = {
            "version": str(stamp),
            "refer": "http://i.mooc.chaoxing.com",
            "from": "",
            "fid": self.fid,
            "jsoncallback": "jsonp" + uuid.uuid4().int.__str__()[:20],
            "t": str(stamp),
        }
        url = EP_MONITOR + "?" + "&".join(f"{k}={_quote(v)}" for k, v in params.items())
        referer = (
            "https://mooc1.chaoxing.com/mycourse/studentstudy"
            f"?chapterId={target.chapter_id}&courseId={target.course_id}"
            f"&clazzid={target.clazz_id}&cpi={target.cpi}&mooc2=1"
        )
        try:
            self.http.get(url, headers={"Referer": referer}, note="心跳")
        except HttpError as exc:
            # 心跳失败不推翻整轮：计次已经发出去了
            self._warn(f"  {target.label}: 心跳失败 {exc.reason}")
            return
        result["requests"] += 1

    def _fail(self, result: dict[str, Any], reason: str) -> None:
        result["ok"] = False
        result["status"] = "failed"
        result["reason"] = self.blocked or reason

    # -- 批量 --------------------------------------------------------------- #

    def run(
        self,
        courses: Sequence[Course],
        *,
        times: int = 1,
        limit: int = 0,
        on_step: Callable[[int, int, VisitTarget], None] | None = None,
        work: Sequence[tuple[Course, VisitTarget]] | None = None,
    ) -> list[dict[str, Any]]:
        """按章节刷 ``times`` 轮。串行——服务端按时间窗口计次，并发没有意义。"""
        todo = list(work) if work is not None else self.plan(courses, limit=limit)
        times = max(1, int(times))
        total = len(todo) * times
        results: list[dict[str, Any]] = []
        done = 0
        for _round in range(times):
            for _course, target in todo:
                if not self.apply:
                    results.append(self.visit(target))
                else:
                    try:
                        results.append(self.visit(target))
                    except Exception as exc:
                        results.append(
                            {
                                "label": target.label,
                                "course_id": target.course_id,
                                "chapter_id": target.chapter_id,
                                "kind": "learning-count",
                                "applied": True,
                                "ok": False,
                                "status": "failed",
                                "reason": f"这一章出错了：{exc}",
                                "requests": 0,
                            }
                        )
                done += 1
                if on_step is not None:
                    on_step(done, total, target)
        return results


def _cookie_value(cookie: str, name: str) -> str:
    """从 ``a=1; b=2`` 里取某个 Cookie 值。"""
    for part in (cookie or "").split(";"):
        key, _, value = part.partition("=")
        if key.strip() == name:
            return value.strip()
    return ""


def _quote(text: str) -> str:
    from urllib.parse import quote

    return quote(str(text), safe="")
