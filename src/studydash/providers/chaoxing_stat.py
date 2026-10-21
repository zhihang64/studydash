"""超星学习通「学习统计」（stat2）适配器。

    ⚠️ 状态：**未验证**（unverified）

本适配器移植自第三方工具 `chaoxing_tool`（github.com/liuyunfz/chaoxing_tool）
里**只读**的那部分能力：学习次数、累计观看时长、任务点完成明细、``jobEnc``
令牌与章节进度。原工具里真正"跑起来"的功能（``do_finish()`` 伪造任务点完成、
明文密码登录 ``passport2.chaoxing.com/fanyalogin``、按分钟 sleep 刷时长）是写
操作，不进本项目——:class:`studydash.http.HttpClient` 会直接抛
:class:`studydash.http.ReadOnlyError` 把它们挡在门口。

和 ``chaoxing.py`` 的分工
------------------------
``chaoxing.py`` 读的是 **mooc2-ans / mooc1** 那一套（课程列表、课程页任务点、
作业列表），它是课程数据的来源；本模块读的是 **stat2-ans** 那一套（学习行为统计），
两者端点不同、字段不同，硬塞进一个适配器只会让两套正则互相绊住。因此这里
把 ``chaoxing.py`` 的课程列表解析**借来当输入**，输出的是"同一门课 + 统计字段"。

依赖注入说明：原工具用 ``requests`` + ``lxml``；本项目 ``pyproject.toml`` 的
``dependencies = []`` 是硬约束，所以这里用 ``self.http``（urllib）和
``..htmlx``（标准库 html.parser）等价替换。

验证方式：``studydash probe chaoxing_stat``，然后把 ``data/raw/`` 里的原始响应
和 :meth:`ChaoxingStatProvider._minutes` / :meth:`~._task_detail` 期望的字段
比对。
"""

from __future__ import annotations

import time
from typing import Any, ClassVar

from ..htmlx import parse, regex_first
from ..model import Course, Progress
from .base import AuthError, Provider, ProviderError, ProviderMeta, register
from .chaoxing import EP_COURSE_LIST, ChaoxingProvider

# --------------------------------------------------------------------------- #
# 端点（stat2-ans）
# --------------------------------------------------------------------------- #
#
# 这四个地址全部来自 chaoxing_tool/classis/Course/__init__.py，参数名一字未改：
#   学习次数   POST stat2/study-pv/chart     clazzid & courseid & cpi & ut & year & month
#   学习时长   GET  stat2/task/s/index       ... & pEnc=<jobEnc>
#   任务点明细 GET  stat2/task/s/progress/detail  ... & pEnc= & page & pageSize & status
#   令牌来源   GET  study-data/index         ... 页面里 <script> 有 jobEnc = '…';

EP_PV_CHART = "https://stat2-ans.chaoxing.com/stat2/study-pv/chart"
EP_TASK_INDEX = "https://stat2-ans.chaoxing.com/stat2/task/s/index"
EP_TASK_DETAIL = "https://stat2-ans.chaoxing.com/stat2/task/s/progress/detail"
EP_STUDY_DATA = "https://stat2-ans.chaoxing.com/study-data/index"

#: 唯一需要 POST 的端点。``stat2/study-pv/chart`` 里没有
#: ``http._FORBIDDEN_IN_POST_ENDPOINT`` 的任何写动作词，能通过启动期体检；
#: 测试 ``test_query_endpoints_are_readonly`` 也在卡这条。
QUERY_ENDPOINTS: tuple[str, ...] = (EP_PV_CHART,)

# --------------------------------------------------------------------------- #
# 正则
# --------------------------------------------------------------------------- #

#: ``jobEnc = 'xxxx';``。原工具写的是 ``r"jobEnc = '(.*)';"``，在 ``re.S`` 下
#: ``(.*)`` 贪婪，会把后面所有分号一起吞掉；这里收窄成 ``[^']*``——同一个令牌，
#: 但不会因为响应里多一段带引号的 JS 就整个错位。
RE_JOB_ENC = r"jobEnc\s*=\s*'([^']*)'"
#: 「总时长 1234 分钟」
RE_TOTAL_MINUTES = r"总时长\s*(\d+)"
#: 兜底：``<div class="fl min"><span>12.5</span>``。htmlx 解析不到时用。
RE_MINUTE_SPAN = (
    r"<div[^>]*class=[\"'][^\"']*\bmin\b[^\"']*[\"'][^>]*>\s*"
    r"<span[^>]*>\s*([\d.]+)"
)


@register
class ChaoxingStatProvider(Provider):
    meta = ProviderMeta(
        id="chaoxing_stat",
        name="超星学习统计",
        short="超星统计",
        status="unverified",
        color="#0ea5e9",
        auth="cookie",
        login=False,
        homepage="https://stat2-ans.chaoxing.com",
        verified="",
        note="学习次数、累计观看时长、任务点明细（stat2-ans）。"
        "移植自 chaoxing_tool 的只读查询部分，接口未经真实账号验证；"
        "改版后请用 probe 留档。",
    )

    endpoints: ClassVar[tuple[str, ...]] = QUERY_ENDPOINTS

    # ------------------------------------------------------------------ #
    # 与框架的接缝
    # ------------------------------------------------------------------ #

    def client_kwargs(self) -> dict[str, Any]:
        """交出 POST 白名单——没有它，学习次数那个接口根本发不出去。"""
        return {"query_endpoints": QUERY_ENDPOINTS}

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
        if self.looks_logged_out(raw):
            raise AuthError(
                "Cookie 已过期或未登录",
                hint=(
                    "重新在浏览器登录 https://i.chaoxing.com ，"
                    "复制 Cookie 整行覆盖配置文件里的 cookie 字段。"
                ),
            )

        courses = self._course_list(raw)
        if not courses:
            raise ProviderError(
                "课程列表解析结果为空",
                hint=(
                    "跑 `studydash probe chaoxing` 把原始响应留档下来（data/raw/），"
                    "确认列表页还是不是 chaoxing.py 认识的那套结构。"
                ),
            )

        self._fill_stats(courses)
        self.log.ok(f"{self.meta.name}: {len(courses)} 门课")
        return courses

    def probe_plan(self) -> list[tuple]:
        """课程列表 + 统计四件套。

        统计端点都要 ``clazzid``/``cpi``/``pEnc``，光有课程 id 打不通，所以默认
        只探课程列表；在配置里写 ``options.probe_course = "courseid:clazzid:cpi"``
        才会把后面三个也列进来（``pEnc`` 现场取，取不到就留空）。
        """
        plan: list[tuple] = [("课程列表", EP_COURSE_LIST)]
        extra = str(self.account.options.get("probe_course", "")).strip()
        if not extra:
            return plan

        course_id, _, rest = extra.partition(":")
        clazz_id, _, cpi = rest.partition(":")
        clazz_id = clazz_id or course_id
        common = {
            "courseid": course_id,
            "clazzid": clazz_id,
            "cpi": cpi,
            "ut": "s",
        }
        job_enc = self._job_enc(course_id, clazz_id, cpi)
        year, month = _this_month()
        plan.extend(
            [
                ("学习数据页（jobEnc 的来源）", f"{EP_STUDY_DATA}?{_query(common)}&t=0"),
                ("学习时长", f"{EP_TASK_INDEX}?{_query({**common, 'pEnc': job_enc})}"),
                (
                    "学习次数（POST 查询）",
                    EP_PV_CHART,
                    "POST",
                    {**common, "year": year, "month": month},
                ),
                (
                    "任务点明细",
                    f"{EP_TASK_DETAIL}?{_query({**common, 'pEnc': job_enc})}"
                    "&page=1&pageSize=16&status=0",
                ),
            ]
        )
        return plan

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #

    def _course_list(self, raw: str) -> list[Course]:
        """借 ``ChaoxingProvider`` 的列表解析。

        复制一份正则过来是最糟的选择：两套正则迟早各自漂移，改一次页面要改两处。
        这里只借用解析（不发请求），统计端点仍然走本适配器自己的 ``self.http``。
        """
        parser = ChaoxingProvider(self.account, self.settings, log=self.log)
        return parser._parse_course_list(raw)

    def _fill_stats(self, courses: list[Course]) -> None:
        cap = self._int_option("max_courses", 60)
        filled = 0
        for course in courses[:cap]:
            if self._fill_course(course):
                filled += 1
        self.log.debug(
            f"  统计：{filled}/{min(len(courses), cap)} 门课读到 stat2 数据"
        )

    def _fill_course(self, course: Course) -> bool:
        clazz_id = str(course.extra.get("class_id") or "").strip()
        cpi = str(course.extra.get("cpi") or "").strip()
        if not clazz_id:
            # 课程列表里偶尔有「已归档 / 跨校共享」条目不带 clazzid，
            # 没有它 stat2 全部端点都拼不出参数，直接跳过比打一串 404 好。
            self.log.debug(f"  {course.name}: 缺 clazzid，跳过统计")
            return False

        job_enc = self._job_enc(course.id, clazz_id, cpi)
        minutes = self._minutes(course.id, clazz_id, cpi, job_enc)
        count = self._study_count(course.id, clazz_id, cpi)
        tasks = self._task_detail(course.id, clazz_id, cpi, job_enc)

        if minutes is None and count is None and tasks is None:
            return False

        if minutes is not None:
            done, total = minutes
            if total > 0:
                course.progress = [
                    Progress(
                        label="视频时长",
                        done=round(done),
                        total=round(total),
                    )
                ]
            course.extra["minutes_done"] = round(done, 1)
            course.extra["minutes_total"] = round(total, 1)

        if count is not None:
            course.extra["study_count"] = count

        if tasks is not None:
            course.extra["task_count"] = len(tasks)
            if tasks:
                course.extra["tasks"] = tasks[:60]

        course.extra["stat2"] = True
        return True

    # ------------------------------------------------------------------ #
    # 四个查询
    # ------------------------------------------------------------------ #

    def _job_enc(self, course_id: str, clazz_id: str, cpi: str) -> str:
        """从学习数据页里抠 ``jobEnc``。它是后面三个端点的必需品。"""
        html = self.http.get_text(
            EP_STUDY_DATA,
            params={
                "courseid": course_id,
                "clazzid": clazz_id,
                "cpi": cpi,
                "ut": "s",
                "t": str(int(time.time() * 1000)),
            },
            note="学习数据页（jobEnc）",
        )
        if not html:
            return ""
        token = regex_first(RE_JOB_ENC, html)
        if not token:
            self.log.debug(f"  课程 {course_id}: 学习数据页里没有 jobEnc，可能未开统计")
        return token

    def _minutes(
        self, course_id: str, clazz_id: str, cpi: str, job_enc: str
    ) -> tuple[float, float] | None:
        """累计观看时长与总时长，单位分钟。

        原工具靠 ``//div[@class='fl min']/span/text()`` 取已完成、``总时长 (\\d+)``
        取总量。这里先走 htmlx 的结构化查找，找不到再退回正则。
        """
        html = self.http.get_text(
            EP_TASK_INDEX,
            params={
                "courseid": course_id,
                "cpi": cpi,
                "clazzid": clazz_id,
                "ut": "s",
                "pEnc": job_enc,
            },
            note="学习时长",
        )
        if not html:
            return None

        done_text = ""
        node = parse(html).find("div", cls="min")
        if node is not None:
            done_text = node.find_text("span")
        if not done_text:
            done_text = regex_first(RE_MINUTE_SPAN, html)

        done = _to_float(done_text)
        if done is None:
            return None
        total = _to_float(regex_first(RE_TOTAL_MINUTES, html)) or 0.0
        return (done, total)

    def _study_count(self, course_id: str, clazz_id: str, cpi: str) -> int | None:
        """本月学习次数。原工具把 ``year=2023&month=03`` 写死了，这里用当前月。"""
        year, month = _this_month()
        payload = self.http.post_query_json(
            EP_PV_CHART,
            {
                "clazzid": clazz_id,
                "courseid": course_id,
                "cpi": cpi,
                "ut": "s",
                "year": year,
                "month": month,
            },
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
            },
            note="学习次数",
            default=None,
        )
        if not isinstance(payload, dict):
            return None
        return _to_int(payload.get("total"))

    def _task_detail(
        self, course_id: str, clazz_id: str, cpi: str, job_enc: str
    ) -> list[dict[str, Any]] | None:
        """任务点明细（``data.results``）。字段名上游没文档，原样扁平化保留。"""
        payload = self.http.get_json(
            EP_TASK_DETAIL,
            params={
                "clazzid": clazz_id,
                "courseid": course_id,
                "cpi": cpi,
                "ut": "s",
                "pEnc": job_enc,
                "page": "1",
                "pageSize": "16",
                "status": "0",
            },
            headers={"Accept": "application/json, text/javascript, */*; q=0.01"},
            note="任务点明细",
            default=None,
        )
        if not isinstance(payload, dict):
            return None
        data = payload.get("data")
        if not isinstance(data, dict):
            return None
        rows = data.get("results")
        if not isinstance(rows, list):
            return None
        return [_flatten(row) for row in rows]

    # ------------------------------------------------------------------ #

    def _int_option(self, key: str, fallback: int) -> int:
        try:
            return max(1, int(self.account.options.get(key, fallback)))
        except (TypeError, ValueError):
            return fallback


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #

def _this_month() -> tuple[str, str]:
    """stat2 期望 ``year=2026`` / ``month=03``（月两位补零）。"""
    now = time.localtime()
    return (str(now.tm_year), f"{now.tm_mon:02d}")


def _query(params: dict[str, str]) -> str:
    return "&".join(f"{k}={v}" for k, v in params.items() if v != "")


def _flatten(row: Any) -> dict[str, Any]:
    """把一行响应压成「标量键值」，跳过多层嵌套。

    任务点明细的字段名各校版本不一样（``title`` / ``jobName`` / ``taskName``
    ……），在不掌握真实响应的前提下，按名字挑字段就是在猜；原样保留标量反而
    让用户在 JSON 里自己看得出来是什么。
    """
    if not isinstance(row, dict):
        return {"_value": str(row)[:120]}
    out: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, str):
            out[str(key)] = value[:120]
        elif value is None or isinstance(value, (int, float, bool)):
            out[str(key)] = value
        if len(out) >= 20:
            break
    return out


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    num = _to_float(value)
    return None if num is None else round(num)
