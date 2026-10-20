"""头歌（educoder.net）适配器。

    ✅ 状态：**测试中**（beta，2026-10-20 用真实账号验证）

头歌的网页是 React 单页应用，数据全在 ``/api/*.json``。它的接口不像超星那样
直出 HTML，也不像慕课那样用表单 POST，而是 **GET + PC 端签名**：

* 每个请求都要带 ``X-EDU-Type: pc``、``X-EDU-Timestamp``（毫秒）和
  ``X-EDU-Signature``；
* 签名 = ``md5(base64("method=GET&ak=<ak>&sk=<sk>&time=<毫秒>"))``，
  ``ak`` / ``sk`` 就写在前端的 umi bundle 里（这里的两条常量就是从中解出来的）；
* 少了签名会拿到 ``{"status":-102,"message":"服务器时间与您的设备时间不匹配…"}``
  ——**这句报错是幌子**，真实原因是签名没带或本机时间偏差太大。

用到的端点（都已用真实账号跑通）：

==========================================  =======================================
``/api/users/get_user_info.json``           当前登录用户；游客返回 ``login=""``、
                                            ``username="游客"``，正好当登录判据
``/api/courses/mine.json``                  「我的课程」，``data[]`` 里是
                                            ``id`` / ``name`` / ``end_date``
``/api/courses/{id}/homework_commons.json`` 课程任务，**靠 ``type`` 区分两类**：
                                            ``type=4`` 实训/作业（任务点、截止时间、
                                            完成状态），``type=1`` 图文作业
==========================================  =======================================

几个容易踩的坑：

* ``homework_commons.json`` **默认只回 ``type=4``（实训）**，图文作业得另发一次
  ``type=1``。不传 ``type`` 时工程实践课看起来「一条作业都没有」。
* ``type`` 是数字枚举：传 ``"normal"`` / ``5`` 之类会拿到 ``-1 type参数有误``。
* ``homework_commons.json`` **默认每页 20 条**，不传 ``per_page`` 会把 51 条任务
  当成 20 条。``published_count`` 才是总数，``unpublished_count`` 是没发布的，
  不该算进进度；``homeworks[]`` 里也只会出现已发布的那批。
* 进度用 **已完成 / 已发布**（任务粒度）。实训看 ``finished_task_count``，
  图文作业看有多少条 ``un_commit_work == false``。
  ``online_learning.json`` 里有 ``learned`` / ``challenges_count``，但实测对
  「实训型」课程全是 0，不能拿来当进度。
* 实训是否完成看 ``shixun_finished_status == 1``（或 ``student_passed_time`` 非空），
  **不能看 ``status``**：``["提交中"]`` 表示「提交窗口开着」，不是「已提交」。
  图文作业同理，看 ``un_commit_work``。
* 作业链接要用**教室里的作业页**：``/classrooms/{课程id}/shixun_homework/{作业id}/detail``
  和 ``/classrooms/{课程id}/common_homework/{作业id}/detail``。头歌自己的
  ``task_operation`` 给的是 ``/shixuns/{identifier}/shixun_exec``，但那个地址
  是「进入实战」的动作页，直接打开会渲染成 404（实测）。
"""

from __future__ import annotations

import base64
import hashlib
import time
import uuid
from typing import Any, ClassVar

from ..model import Assignment, Course, Progress, parse_dt
from .base import AuthError, Provider, ProviderError, ProviderMeta, register

BASE = "https://www.educoder.net"

#: 当前用户。游客（未登录/会话失效）返回 ``login=""``、``username="游客"``。
USER_RPC = BASE + "/api/users/get_user_info.json"
#: 「我的课程」。匿名也能调，但只会返回空数组——所以登录判据必须用 USER_RPC。
COURSES_RPC = BASE + "/api/courses/mine.json"
#: 课程任务列表。``{cid}`` 是数字课程 id。**必须带 ``type``**，否则只回实训。
HOMEWORK_RPC = BASE + "/api/courses/{cid}/homework_commons.json"

#: ``homework_commons.json`` 的 ``type`` 参数（枚举，传别的值回 ``-1 type参数有误``）。
#: 实测：``4`` = 实训/作业（``is_shixun: true``，带任务点），``1`` = 图文作业
#: （``is_shixun: false``，带 ``work_status`` / ``un_commit_work``）。``3`` 这个
#: 教室里全是 0 条，先不接。
TYPE_SHIXUN = 4
TYPE_SUBJECT = 1

#: PC 端签名用的 ak/sk，取自前端 umi bundle（``527951`` 模块，双重 base64）。
SIGN_AK = "e9dd5b4322f9f7d83d009de9bfa100c3"
SIGN_SK = "2e3da06ae26ba9f76a5d8d355746f2fe"

#: 一次取全任务。实测 ``per_page=100`` 能拿到 51 条；给 200 留余量。
PER_PAGE = 200

#: 已完成的文案。头歌的 ``status`` 是给老师看的「提交窗口」状态，
#: 学生侧完成与否要看 ``shixun_finished_status``。
_FINISHED_WORDS = ("已提交", "已批阅", "已评分", "已完成", "已通过")


def signed_headers() -> dict[str, str]:
    """构造一次 GET 所需的 PC 端签名头。

    签名 = ``md5(base64("method=GET&ak=<ak>&sk=<sk>&time=<毫秒>"))``。它只跟
    HTTP 方法有关，不带路径——但时间戳有有效期，所以每次请求现算，别缓存。
    """
    stamp = str(int(time.time() * 1000))
    raw = f"method=GET&ak={SIGN_AK}&sk={SIGN_SK}&time={stamp}"
    signature = hashlib.md5(base64.b64encode(raw.encode())).hexdigest()
    return {
        "Accept": "application/json",
        "X-EDU-Type": "pc",
        "X-EDU-Timestamp": stamp,
        "X-EDU-Signature": signature,
        "X-Request-Id": str(uuid.uuid4()),
        "X-Original-Protocol": "https:",
        "X-Original-Host": "www.educoder.net",
        "X-Original-Origin": BASE,
    }


@register
class EducoderProvider(Provider):
    meta = ProviderMeta(
        id="educoder",
        name="头歌",
        short="头歌",
        status="beta",
        color="#f97316",
        auth="cookie",
        login=True,
        homepage=BASE,
        verified="2026-10-20",
        note="课程、实训/图文作业、截止时间与完成状态已用真实账号验证；"
        "登录支持「粘贴 Cookie」「读本机浏览器」「独立窗口登录」，"
        "暂不支持扫码与账号密码。接口改版时用 `studydash probe educoder` 留档。",
    )

    endpoints: ClassVar[tuple[str, ...]] = (USER_RPC, COURSES_RPC, HOMEWORK_RPC)

    # ------------------------------------------------------------------ #
    # 请求
    # ------------------------------------------------------------------ #

    def _sign_headers(self) -> dict[str, str]:
        return signed_headers()

    def _get_json(
        self, url: str, params: dict[str, Any] | None = None, *, note: str = ""
    ) -> dict[str, Any]:
        payload = self.http.get_json(
            url, params, headers=self._sign_headers(), note=note, default=None
        )
        if not isinstance(payload, dict):
            raise ProviderError(
                f"{self.meta.name}: {note or url} 返回的不是 JSON",
                hint="换一个网络环境再试；如果一直这样，用 `studydash probe educoder` 留档。",
            )
        return payload

    def _check(self, payload: dict[str, Any], what: str) -> None:
        """头歌把错误码放在 ``status`` 里（正常是 0）。"""
        status = payload.get("status")
        if status in (None, 0):
            return
        message = str(payload.get("message") or "").strip()
        if status in (401, 403) or "登录" in message:
            raise AuthError(
                f"{self.meta.name}：{message or '请先登录'}",
                hint="在浏览器里重新登录 educoder.net，再把 Cookie 贴进来。",
            )
        if status == -102:
            raise ProviderError(
                "头歌拒绝了请求：PC 端签名缺失或本机时间偏差太大",
                hint="先校准系统时间；如果时间是对的，说明签名算法改版了，"
                "用 `studydash probe educoder` 留档后更新适配器。",
            )
        raise ProviderError(
            f"{what} 失败（{status}）：{message or '平台没有说明原因'}",
            hint="接口可能改版了；用 `studydash probe educoder` 留档原始响应。",
        )

    # ------------------------------------------------------------------ #
    # 抓取
    # ------------------------------------------------------------------ #

    def fetch(self) -> list[Course]:
        self.require_cookie()

        info = self._get_json(USER_RPC, note="用户信息")
        self._check(info, "用户信息")
        if not str(info.get("login") or "").strip():
            # 匿名请求 courses/mine.json 同样是 status=0 + 空数组，
            # 不先判登录就会把「会话过期」显示成「0 门课」。
            raise AuthError(
                f"{self.meta.name} 的 Cookie 无效或已过期",
                hint="重新登录 educoder.net 后粘贴 Cookie；"
                "游客身份下 `courses/mine.json` 也会返回成功和空列表，别被它骗了。",
            )
        who = str(info.get("username") or info.get("login") or "")

        listing = self._get_json(COURSES_RPC, note="我的课程")
        self._check(listing, "我的课程")
        raw_courses = [c for c in listing.get("data") or [] if isinstance(c, dict)]

        courses: list[Course] = []
        total_tasks = 0
        for raw in raw_courses:
            course = self._course(raw)
            if course is None:
                continue
            shixun = self._homeworks(course, TYPE_SHIXUN, "实训")
            subject = self._homeworks(course, TYPE_SUBJECT, "图文作业")
            tasks = self._attach_tasks(course, shixun, subject)
            total_tasks += tasks
            courses.append(course)

        self.log.info(
            f"{self.meta.name}: {who} · {len(courses)} 门课 / {total_tasks} 条任务"
        )
        return courses

    def _homeworks(
        self, course: Course, kind: int, label: str
    ) -> dict[str, Any]:
        """拿一门课某一类的任务列表。拿不到不该让整门课消失，只警告。"""
        try:
            return self._get_json(
                HOMEWORK_RPC.format(cid=course.id),
                {"per_page": PER_PAGE, "type": kind},
                note=f"{course.name} 的{label}",
            )
        except (AuthError, ProviderError) as exc:
            # 一门课的任务拿不到不该让整门课消失：课程本身仍然有名字和结束时间。
            self.log.warn(f"{self.meta.name}: {course.name} 的{label}没取到：{exc}")
            return {}

    def _course(self, raw: dict[str, Any]) -> Course | None:
        cid = raw.get("id")
        name = str(raw.get("name") or "").strip()
        if cid in (None, "") or not name:
            return None
        return Course(
            id=str(cid),
            name=name,
            platform=self.meta.id,
            last_active=parse_dt(raw.get("updated_at")),
            extra={
                "platform_name": self.meta.name,
                "term_id": "",          # 头歌的课程 id 本身就是一次开课，没有额外学期 id
                "end_date": str(raw.get("end_date") or ""),
                "created_at": str(raw.get("created_at") or ""),
            },
        )

    def _attach_tasks(
        self,
        course: Course,
        shixun: dict[str, Any],
        subject: dict[str, Any],
    ) -> int:
        """把实训和图文作业都挂到课程上，返回解析到的任务条数。"""
        self._check(shixun, f"{course.name} 的实训")
        self._check(subject, f"{course.name} 的图文作业")

        raw_shixun = [h for h in shixun.get("homeworks") or [] if isinstance(h, dict)]
        raw_subject = [h for h in subject.get("homeworks") or [] if isinstance(h, dict)]

        # ``published_count`` 是发布总数（``homeworks[]`` 只会给已发布的），
        # ``unpublished_count`` 不能算进进度。
        published_shixun = _published(shixun, raw_shixun)
        published_subject = _published(subject, raw_subject)
        finished_shixun = min(
            max(_int(shixun.get("finished_task_count")), 0), published_shixun
        )
        # 图文作业没有 ``finished_task_count``，只能数作品交没交。
        finished_subject = sum(1 for raw in raw_subject if _subject_done(raw))

        total = published_shixun + published_subject
        if total > 0:
            course.progress.append(
                Progress("已完成任务", finished_shixun + finished_subject, total)
            )

        count = 0
        for raw in raw_shixun:
            assignment = self._task(raw, course, subject=False)
            if assignment is not None:
                course.assignments.append(assignment)
                count += 1
        for raw in raw_subject:
            assignment = self._task(raw, course, subject=True)
            if assignment is not None:
                course.assignments.append(assignment)
                count += 1
        return count

    def _task(
        self, raw: dict[str, Any], course: Course, *, subject: bool
    ) -> Assignment | None:
        title = str(raw.get("name") or raw.get("shixun_name") or "").strip()
        if not title:
            return None
        hid = raw.get("homework_id") or raw.get("id")
        if hid in (None, ""):
            return None

        if subject:
            kind_label = "图文作业"
            detail = f"{kind_label} · {'已提交' if _subject_done(raw) else '待提交'}"
            # 图文作业进的是教室的作业详情页；`/shixuns/...` 那套是实训的。
            url = f"{BASE}/classrooms/{course.id}/common_homework/{hid}/detail"
            task_id = f"s{hid}"
        else:
            challenges = _int(raw.get("challenge_count"))
            done_challenges = _int(raw.get("finished_challenge_count"))
            detail = (
                f"实训 · 任务点 {done_challenges}/{challenges}" if challenges else "实训"
            )
            # 用教室里的作业详情页，不要用 ``task_operation`` 里那个
            # ``/shixuns/{identifier}/shixun_exec``——那是「进入实战」的动作地址，
            # 直接打开会渲染成 404（2026-10-20 实测）。
            url = f"{BASE}/classrooms/{course.id}/shixun_homework/{hid}/detail?tabs=0"
            task_id = f"h{hid}"

        return Assignment(
            id=task_id,
            title=title,
            platform=self.meta.id,
            course_id=course.id,
            course=course.name,
            kind="homework",
            status=_status(raw, subject=subject),
            due=parse_dt(raw.get("end_time")),
            url=url,
            detail=detail,
            platform_name=self.meta.name,
        )

    # ------------------------------------------------------------------ #
    # probe
    # ------------------------------------------------------------------ #

    def probe(self):  # type: ignore[override]
        """``Provider.probe`` 不会带签名头，这里自己按平台要求请求。"""
        plan: list[tuple[str, str, dict[str, Any] | None]] = [
            ("用户信息", USER_RPC, None),
            ("我的课程", COURSES_RPC, None),
        ]
        try:
            listing = self._get_json(COURSES_RPC, note="我的课程")
            first = (listing.get("data") or [{}])[0]
            cid = first.get("id") if isinstance(first, dict) else None
            if cid:
                for kind, label in ((TYPE_SHIXUN, "实训"), (TYPE_SUBJECT, "图文作业")):
                    plan.append(
                        (
                            f"课程{label}",
                            HOMEWORK_RPC.format(cid=cid),
                            {"per_page": PER_PAGE, "type": kind},
                        )
                    )
        except Exception as exc:                            # noqa: BLE001 - probe 不该崩
            self.log.warn(f"  取课程 id 失败: {exc}")

        for note, url, params in plan:
            self.log.step(f"{note} [GET]: {url}")
            try:
                payload = self._get_json(url, params, note=note)
                self._check(payload, note)
            except Exception as exc:                        # noqa: BLE001
                self.log.warn(f"  {note} 失败: {exc}")
        return self.http.trace


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #

def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _published(payload: dict[str, Any], items: list[dict[str, Any]]) -> int:
    """已发布任务数。``published_count`` 缺失时退回「列表里真有几条」。"""
    published = _int(payload.get("published_count"))
    return published if published > 0 else len(items)


def _subject_done(raw: dict[str, Any]) -> bool:
    """图文作业交没交。

    只认 ``un_commit_work``：``work_status`` 在没提交的作业上可能是空数组，
    ``student_work_id`` 更是没交也有值。``False`` 表示作品已提交（可能还能改）。
    """
    return raw.get("un_commit_work") is False


def _status(raw: dict[str, Any], *, subject: bool = False) -> str:
    """把头歌的两种状态合成模型要的 ``pending/submitted/graded``。

    实训只认 ``shixun_finished_status`` / ``student_passed_time``：``status:
    ["提交中"]`` 是「提交窗口开着」，不是「已经交了」。图文作业认
    ``un_commit_work``，列表接口不给成绩，所以「已交」就记成 ``submitted``。
    """
    if subject:
        return "submitted" if _subject_done(raw) else "pending"
    if _int(raw.get("shixun_finished_status")) == 1 or str(
        raw.get("student_passed_time") or ""
    ).strip():
        return "graded"
    labels = raw.get("status") or []
    text = " ".join(str(item) for item in labels) if isinstance(labels, list) else str(labels)
    if any(word in text for word in _FINISHED_WORDS):
        return "submitted"
    return "pending"
