"""把学习通的任务点写回平台（实验性，显式开关，默认只演练）。

覆盖六种任务点，与第三方工具 ``chaoxing_tool`` 的 ``classis/Media/`` 一一对应：

===========  ==========================================================
``video``    视频，取状态拿 ``duration``/``dtoken`` 后拼 md5 签名上报观看位置
``audio``    视频的一种（``property.module == "insertaudio"``），协议同上，``dtype=Audio``
``read``     ``GET /ananas/job/readv2``，平台回 ``{"status": true}``
``document`` ``GET /ananas/job/document``，判定同上
``book``     ``GET /ananas/job``（图书），判定同上
``live``     ``GET zhibo.chaoxing.com/saveTimePc``，平台回纯文本 ``@success``
===========  ==========================================================


这个模块**违背**了项目开头的只读承诺，所以它被单独隔离出来：

* 只有 ``studydash writeback --apply`` 才会真的发出上报，不加 ``--apply`` 一律只演练；
* 上报地址被钉死在 :data:`WRITE_URL_PREFIXES`，别的地址一律拒绝发送；
* 它不出现在 ``fetch`` / ``all`` / ``web`` 的任何调用链里，只在你亲手敲命令时运行；
* 它仍然只走 :class:`studydash.http.HttpClient`，网络出口还是那一个，
  ``assert_readonly()`` 依旧返回空——没有第二个 socket 出口。

链路搬自第三方工具 ``chaoxing_tool`` 的 ``classis/Media/Video.py``（作者 liuyunfz，
config.yml 声明开源免费），只保留「取视频状态 → 拼 md5 签名 → 上报观看位置」这一段。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from .http import HttpClient, HttpError
from .log import Log
from .model import Course
from .providers.base import AccountConfig, HttpSettings, ProviderError
from .providers.chaoxing import ChaoxingProvider, _walk_cards, extract_marg

__all__ = [
    "EP_JOB",
    "EP_JOB_DOCUMENT",
    "EP_JOB_READ",
    "EP_LIVE_TIME",
    "EP_VIDEO_STATUS",
    "RAMP_STEPS",
    "TASK_KINDS",
    "WRITE_URL_PREFIXES",
    "ChaoxingVideoWriter",
    "VideoTarget",
    "build_enc",
    "build_report_url",
    "cookie_user_id",
]

#: 取视频状态（读）。返回裸 JSON，含 ``duration`` 与 ``dtoken``。
EP_VIDEO_STATUS = (
    "https://mooc1-1.chaoxing.com/ananas/status/{object_id}?k=&flag=normal&_dc={ms}"
)

#: 知识卡页（读），``num`` 是页码。与 ``providers/chaoxing.py`` 的
#: :data:`EP_KNOWLEDGE_CARDS` 同一地址，这里因为要翻页，把 ``num`` 也做成占位符。
EP_CARDS = (
    "https://mooc1.chaoxing.com/knowledge/cards?clazzid={clazzid}&courseid={courseid}"
    "&knowledgeid={knowledgeid}&num={num}&ut=s&cpi={cpi}&v=&mooc2=1"
)

#: 阅读任务点（读+写）。平台把它挂在 ``mooc1-2`` 上，与视频的 ``mooc1`` 不同域。
EP_JOB_READ = (
    "https://mooc1-2.chaoxing.com/ananas/job/readv2?jobid={jobid}&knowledgeid={knowledgeid}"
    "&courseid={courseid}&clazzid={clazzid}&jtoken={jtoken}&_dc={ms}"
)

#: 文档任务点。
EP_JOB_DOCUMENT = (
    "https://mooc1-2.chaoxing.com/ananas/job/document?jobid={jobid}&knowledgeid={knowledgeid}"
    "&courseid={courseid}&clazzid={clazzid}&jtoken={jtoken}&_dc={ms}"
)

#: 图书任务点——注意没有 ``/document`` 这类后缀，端点就是 ``/ananas/job``。
EP_JOB = (
    "https://mooc1-2.chaoxing.com/ananas/job?jobid={jobid}&knowledgeid={knowledgeid}"
    "&courseid={courseid}&clazzid={clazzid}&jtoken={jtoken}&_dc={ms}"
)

#: 直播任务点。走 zhibo 子域，回的是纯文本 ``@success``，不是 JSON。
EP_LIVE_TIME = (
    "https://zhibo.chaoxing.com/saveTimePc?streamName={stream}&vdoid={vdoid}"
    "&userId={userid}&isStart=0&t={ms}&courseId={courseid}"
)

#: 本模块认得的全部任务点种类。``kind`` 字段的取值就在这里。
TASK_KINDS: tuple[str, ...] = ("video", "audio", "read", "document", "book", "live")

#: 种类 → 中文名，只用于日志与提示。
KIND_LABEL: dict[str, str] = {
    "video": "视频",
    "audio": "音频",
    "read": "阅读",
    "document": "文档",
    "book": "图书",
    "live": "直播",
}

#: 允许真正「写」的地址前缀。**只有这些**，加别的先改这里再改测试。
WRITE_URL_PREFIXES: tuple[str, ...] = (
    "https://mooc1.chaoxing.com/mooc-ans/multimedia/log/a/",
    "https://mooc1.chaoxing.com/multimedia/log/a/",
    "https://mooc1-2.chaoxing.com/ananas/job?",
    "https://mooc1-2.chaoxing.com/ananas/job/readv2?",
    "https://mooc1-2.chaoxing.com/ananas/job/document?",
    "https://zhibo.chaoxing.com/saveTimePc?",
)

#: 本模块会读的地址前缀。写死的白名单，防止卡片页里塞个外站 reportUrl 就把
#: Cookie 带出去。
READ_URL_PREFIXES: tuple[str, ...] = (
    "https://mooc1-1.chaoxing.com/ananas/status/",
    "https://mooc1.chaoxing.com/knowledge/cards",
    "https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/studentcourse",
)

REFERER_STATUS = "https://mooc1.chaoxing.com/ananas/modules/video/index.html?v=2022-0329-1945"
REFERER_REPORT = "https://mooc1-2.chaoxing.com/ananas/modules/video/index.html?v=2023-0512-1953"
REFERER_PDF = "https://mooc1-2.chaoxing.com/ananas/modules/pdf/index.html?v=2020-1103-1706"
REFERER_BOOK = "https://mooc1-2.chaoxing.com/ananas/modules/innerbook/index.html?v=2018-0126-1905"
REFERER_LIVE = "https://mooc1.chaoxing.com/ananas/modules/live/index.html?v=2022-1214-1139"

#: 课程任务点总览页，页面上写着「已完成任务点 X/Y」——这是唯一能证明上报真生效的判据。
EP_MISSION = (
    "https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/studentcourse"
    "?courseid={courseid}&clazzid={clazzid}&cpi={cpi}&ut=s&t={ms}"
)
RE_MISSION = re.compile(r"已完成任务点:\s*<span[^>]*>(\d+)</span>\s*/\s*(\d+)")
RE_MISSION_LOOSE = re.compile(r"已完成任务点[^0-9]{0,80}(\d+)\s*/\s*(\d+)")

#: 超星前端签名里的固定盐值。换掉它上报必然失败，所以只能硬编码。
_ENC_SALT = "d_yHJ!$pdA~5"

#: 请求本身没成（网络挂了，或平台没回 JSON）——只有这种才算上报失败。
#: 注意 ``isPassed=false`` **不算**网络失败，但它意味着任务点没完成，
#: 见 :meth:`ChaoxingVideoWriter._judge`。
SEND_FAILED = "上报请求没发成功（网络中断或平台没回 JSON）"

_STATUS_HEADERS = {"Referer": REFERER_STATUS}
_REPORT_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Content-Type": "application/json",
    "Referer": REFERER_REPORT,
}
#: 阅读/文档/图书/直播的公共头。平台靠 ``X-Requested-With`` 认定这是页面自己发的。
_JOB_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
}


class WritebackError(ProviderError):
    """写回被拒绝或上报失败。"""


def cookie_user_id(cookie: str) -> str:
    """从 Cookie 里取 ``_uid``——上报签名要用它当 userid。"""
    for chunk in (cookie or "").split(";"):
        name, _, value = chunk.strip().partition("=")
        if name.strip() == "_uid":
            return value.strip()
    return ""


def parse_mission_counts(html: str) -> tuple[int, int] | None:
    """从课程页里抠出「已完成任务点 X/Y」。抠不到返回 ``None``。

    这是唯一能证明上报真的生效的判据——平台对上报请求回的 ``isPassed`` 与页面上
    显示的完成数并不总是同步，而完成数就是用户眼睛看到的那个数。
    """
    text = html or ""
    match = RE_MISSION.search(text) or RE_MISSION_LOOSE.search(text)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def build_enc(
    *,
    clazz_id: str,
    user_id: str,
    jobid: str,
    object_id: str,
    playing_time: int,
    duration: int,
) -> str:
    """复刻前端的 ``enc`` 签名。

    ``[clazzId][userid][jobid][objectId][playingTime*1000][盐][duration*1000][0_duration]``
    的 md5 十六进制串。``playing_time`` 与 ``duration`` 用秒，乘 1000 后再拼。
    """
    raw = (
        f"[{clazz_id}][{user_id}][{jobid}][{object_id}]"
        f"[{int(playing_time) * 1000}][{_ENC_SALT}]"
        f"[{int(duration) * 1000}][0_{int(duration)}]"
    )
    # md5 是平台前端自己的签名算法，不是这里选的——换成别的平台直接不收。
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def build_report_url(
    *,
    report_url: str,
    dtoken: str,
    clazz_id: str,
    user_id: str,
    jobid: str,
    object_id: str,
    duration: int,
    other_info: str,
    enc: str,
    rt: float = 0.9,
    dtype: str = "Video",
    playing_time: int | None = None,
    isdrag: int = 4,
    stamp_ms: int | None = None,
) -> str:
    """拼出上报地址。参数顺序与平台前端保持一致，别随手重排。"""
    playing = int(duration if playing_time is None else playing_time)
    ts = int(time.time() * 1000) if stamp_ms is None else int(stamp_ms)
    tail = (
        f"/{dtoken}?clazzId={clazz_id}&playingTime={playing}&duration={int(duration)}"
        f"&clipTime=0_{int(duration)}&objectId={object_id}&otherInfo={other_info}"
        f"&jobid={jobid}&userid={user_id}&isdrag={isdrag}&view=pc&enc={enc}"
        f"&rt={rt}&dtype={dtype}&_t={ts}"
    )
    return report_url.rstrip("/") + tail


@dataclass
class VideoTarget:
    """一个待写回的任务点。

    名字为了向后兼容保留了 ``Video``，但 :attr:`kind` 可能是 :data:`TASK_KINDS`
    里的任意一种。视频/音频之外的任务点不需要 ``object_id``，也不需要签名。
    """

    course_id: str
    course_name: str
    clazz_id: str
    cpi: str
    knowledge_id: str
    chapter: str
    jobid: str
    object_id: str
    title: str = ""
    other_info: str = ""
    rt: float = 0.9
    dtype: str = "Video"
    module: str = ""
    report_url: str = ""
    kind: str = "video"
    jtoken: str = ""
    stream_name: str = ""
    vdoid: str = ""
    #: 卡片页里平台已经记下的播放位置（毫秒）。已经等于片长时说明这个任务点卡在
    #: 「播放位置满了但没判通过」，再上报也不会带来增量。
    play_time_ms: int = 0
    #: 卡片页里 ``isPassed`` 是否已经为真（已完成的任务点不该再报）。
    passed: bool = False
    #: 卡片页给的片长（秒，``attDuration``）。有了它就能在发任何请求之前判断死态。
    att_duration: int = 0
    #: 卡片页给的 ``videoFaceCaptureEnc``。非空说明这个视频开了人脸抓拍/防代刷，
    #: 实测这类视频一次跳满之后平台只记 ``playTime``，``isPassed`` 永远是 false。
    face_capture: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.course_name} / {self.chapter} / {self.title or self.object_id or self.jobid}"

    @property
    def kind_label(self) -> str:
        return KIND_LABEL.get(self.kind, self.kind)


def classify_node(node: dict[str, Any]) -> str:
    """判断一个知识卡节点属于哪种任务点；不认识的返回空串。

    判定顺序照抄 ``chaoxing_tool`` 的 ``deal_course.py``：先看 ``type``，音视频
    共用 ``type=video`` 靠 ``property.module`` 区分；图书没有自己的 ``type``，
    只能靠 ``property.bookname`` 认；剩下的（作业、讨论、测验）一律跳过。
    """
    property_ = node.get("property")
    prop = property_ if isinstance(property_, dict) else {}
    type_ = str(node.get("type") or "").strip().lower()
    if type_ == "video":
        return "audio" if str(prop.get("module") or "") == "insertaudio" else "video"
    if type_ in ("read", "document", "live"):
        return type_
    if "bookname" in prop:
        return "book"
    return ""


def _assert_prefix(url: str, prefixes: tuple[str, ...], what: str) -> None:
    if not url.startswith(prefixes):
        raise WritebackError(
            f"{what}地址不在白名单里：{url[:120]}",
            hint="只允许 " + "、".join(prefixes) + "。要放宽先改 writeback.py 与它的测试。",
        )


#: 递进上报的节奏（占视频总长的比例）。实测把播放位置**一次性**送到片尾，5 个任务点
#: 平台只认 1 个，还把另外 4 个标上 ``videoFaceCaptureEnc`` 变成死态；改成
#: 30% → 70% → 100% 三步发，4 个全部通过。代价是 3 次请示点，但不需要真的等时长。
RAMP_STEPS: tuple[float, ...] = (0.3, 0.7, 1.0)
RAMP_DELAY = 1.5

def _plan_rank(target: VideoTarget) -> int:
    """``plan`` 的排序键：越小越该先报。

    0 = 没看过、也没开人脸抓拍（最可能一次过）
    1 = 有进度但没满（递进上报还救得回来）
    2 = 开了抓拍或播放位置已满（死态，报了也白报，排到最后让 ``--limit`` 先砍它）
    """
    full = bool(target.att_duration) and target.play_time_ms >= target.att_duration * 1000
    if target.face_capture or full:
        return 2
    return 0 if not target.play_time_ms else 1


class ChaoxingVideoWriter:
    """把视频观看位置上报给超星。

    参数
    ----
    account   ``platform=chaoxing`` 的那条账号，Cookie 必填
    settings  超时/限速/重试，与抓取共用一份
    log       日志
    apply     **默认 False**。False 只演练（不发出任何上报）
    sleep     可注入的 sleep，测试用
    interval  逐分钟上报时的间隔秒数（平台接受约 60 秒一跳）
    max_pages 一个章节最多翻多少页知识卡
    workers   并发上报的线程数（:meth:`run` 用）。1 = 串行，与旧版行为完全一致。

    线程安全：:class:`studydash.http.HttpClient` 里既有 CookieJar 也有一个
    「距上次请求不足 ``delay`` 就 sleep」的限速标记，多个线程共用一份会互相排队，
    并发就白开了。所以构造它的那个线程继续用 ``self.http``（测试替换
    ``writer.http.get`` 仍然有效），其它线程各自拿一份独立的 ``HttpClient``，
    由 :meth:`_client` 按线程缓存。
    """

    def __init__(
        self,
        account: AccountConfig,
        settings: HttpSettings | None = None,
        *,
        log: Log | None = None,
        apply: bool = False,
        sleep: Any = time.sleep,
        interval: float = 59.8,
        max_pages: int = 20,
        force: bool = False,
        ramp_delay: float = RAMP_DELAY,
    ) -> None:
        self.account = account
        self.settings = settings or HttpSettings()
        self.log = log or Log(quiet=True)
        self.apply = bool(apply)
        self.sleep = sleep if sleep is not None else time.sleep
        self.interval = float(interval)
        self.max_pages = max(1, int(max_pages))
        #: 忽略「人脸抓拍」这道保险。抓拍视频实测报不出通过，但拿一门还有救的课
        #: 试 ``--paced`` 时，加 ``--force`` 就能让它真的发。
        self.force = bool(force)
        #: 递进上报每步之间的间隔（秒）。实测 1.5 秒就够，不需要等真实时长。
        self.ramp_delay = max(0.0, float(ramp_delay))
        #: 上一次 403/验证码拦截的说明。空串表示没被拦过。上报层的 reason 会带上它，
        #: 免得把「平台让我过验证码」报成「网络中断」。
        self.blocked = ""
        #: ``plan`` 清点时顺手记下的「有 jobid 但我们不支持」的任务点，
        #: 形如 ``{course_id: {种类: 个数}}``。章节测验就在这里面。
        self.unsupported: dict[str, dict[str, int]] = {}
        self._unsupported_seen: set[str] = set()
        self.user_id = cookie_user_id(account.cookie)
        self.http = HttpClient(
            cookie=account.cookie,
            timeout=self.settings.timeout,
            delay=self.settings.delay,
            user_agent=self.settings.user_agent,
            retries=self.settings.retries,
            logger=self.log.debug,
        )
        # 章节树解析复用 chaoxing 适配器，不另起一份 HTML 逻辑。
        self.provider = ChaoxingProvider(account, self.settings, log=self.log)
        self._owner = threading.get_ident()
        self._local = threading.local()

    # -- 线程隔离的 HTTP 出口 ------------------------------------------------ #

    def _client(self) -> HttpClient:
        """当前线程该用的 HttpClient。

        构造这个 writer 的线程（测试里就是主线程）继续用它自己的 ``self.http``，
        这样测试里 ``writer.http.get = fake`` 依旧生效；工作线程各自新建一份，
        免得共享 CookieJar 与限速标记。
        """
        if threading.get_ident() == self._owner:
            return self.http
        client = getattr(self._local, "http", None)
        if client is None:
            client = HttpClient(
                cookie=self.account.cookie,
                timeout=self.settings.timeout,
                delay=self.settings.delay,
                user_agent=self.settings.user_agent,
                retries=self.settings.retries,
                logger=self.log.debug,
            )
            self._local.http = client
        return client

    # -- 采集 --------------------------------------------------------------- #

    def chapters(self, course: Course) -> list[tuple[str, str]]:
        try:
            return self.provider._chapters_of(course)
        except (HttpError, ProviderError) as exc:
            self.log.warn(f"  {course.name}: 章节读取失败 {exc}")
            return []

    def mission_counts(self, course: Course) -> tuple[int, int] | None:
        """读课程页的「已完成任务点 X/Y」。读不到返回 ``None``（不抛）。"""
        url = EP_MISSION.format(
            courseid=course.id,
            clazzid=course.extra.get("class_id", ""),
            cpi=course.extra.get("cpi", ""),
            ms=int(time.time() * 1000),
        )
        _assert_prefix(url, READ_URL_PREFIXES, "任务点总览")
        html = self._client().get_text(url, note="任务点总览") or ""
        return parse_mission_counts(html)

    def _cards(self, target_course: Course, knowledge_id: str, page: int) -> dict[str, Any] | None:
        url = EP_CARDS.format(
            clazzid=target_course.extra.get("class_id", ""),
            courseid=target_course.id,
            knowledgeid=knowledge_id,
            num=page,
            cpi=target_course.extra.get("cpi", ""),
        )
        _assert_prefix(url, READ_URL_PREFIXES, "知识卡页")
        html = self.http.get_text(url, note=f"卡片页 {knowledge_id}#{page}")
        if not html:
            return None
        raw_json = extract_marg(html)
        if not raw_json:
            return None
        try:
            data = json.loads(raw_json)
        except ValueError as exc:
            self.log.debug(f"  卡片页 JSON 解析失败 {knowledge_id}#{page}: {exc}")
            return None
        return data if isinstance(data, dict) else None

    def iter_targets(self, course: Course) -> Iterator[VideoTarget]:
        """列出这门课所有待写回的任务点（不发送任何写请求）。

        :data:`TASK_KINDS` 里的六种都会产出，调用方可以按 ``target.kind`` 过滤。
        """
        clazz_id = str(course.extra.get("class_id", ""))
        cpi = str(course.extra.get("cpi", ""))
        for knowledge_id, chapter_name in self.chapters(course):
            seen_page = False
            for page in range(self.max_pages):
                data = self._cards(course, knowledge_id, page)
                if data is None:
                    break
                seen_page = True
                defaults = data.get("defaults")
                if not isinstance(defaults, dict):
                    defaults = {}
                # 平台把上报地址与班级信息放在 defaults 里，不在顶层。
                report_url = str(defaults.get("reportUrl") or data.get("reportUrl") or "")
                clazz_id = str(defaults.get("clazzId") or clazz_id)
                cpi = str(defaults.get("cpi") or cpi)
                # 阅读/文档/图书的 jobid 端点带的是 defaults 里的 knowledgeid，
                # 没给才回退到本章节的 knowledgeid。
                job_knowledge_id = str(defaults.get("knowledgeid") or knowledge_id)
                for node in _walk_cards(data):
                    kind = classify_node(node)
                    if not kind:
                        self._note_unsupported(course, node)
                        continue
                    jobid = str(node.get("jobid") or node.get("jobId") or "")
                    if not jobid:
                        continue
                    object_id = str(node.get("objectId") or "")
                    # 音视频要靠 objectId 去 /ananas/status 取 duration 与 dtoken，
                    # 其余四种只要 jobid 就能发。
                    if kind in ("video", "audio") and not object_id:
                        continue
                    property_ = node.get("property")
                    prop = property_ if isinstance(property_, dict) else {}
                    yield VideoTarget(
                        course_id=str(course.id),
                        course_name=course.name,
                        clazz_id=clazz_id,
                        cpi=cpi,
                        knowledge_id=job_knowledge_id,
                        chapter=chapter_name,
                        jobid=jobid,
                        object_id=object_id,
                        title=str(prop.get("name") or ""),
                        other_info=str(node.get("otherInfo") or ""),
                        rt=_as_float(prop.get("rt"), 0.9),
                        dtype="Audio" if kind == "audio" else "Video",
                        module=str(prop.get("module") or ""),
                        report_url=report_url,
                        kind=kind,
                        jtoken=str(node.get("jtoken") or ""),
                        stream_name=str(prop.get("streamName") or ""),
                        vdoid=str(prop.get("vdoid") or ""),
                        play_time_ms=_as_int(node.get("playTime"), 0),
                        att_duration=_as_int(node.get("attDuration"), 0),
                        passed=bool(node.get("isPassed")),
                        face_capture=str(node.get("videoFaceCaptureEnc") or ""),
                    )
            if not seen_page:
                self.log.debug(f"  {chapter_name}: 没有可读的知识卡页")

    def _note_unsupported(self, course: Course, node: dict[str, Any]) -> None:
        """记下「有 jobid 但这套协议覆盖不到」的任务点，尤其是章节测验。

        平台的「已完成任务点 0/98」把视频和章节测验算在一起，我们只做前者，
        不把这个差说清楚，用户只会看到「报完了还是 0」。
        """
        jobid = str(node.get("jobid") or node.get("jobId") or "")
        if not jobid:
            return
        # 递归遍历会把节点的子对象也捞出来：视频节点的 ``property`` 里就有一份
        # ``jobid`` 和 ``type=".mp4"``。只认同任务点自身的 type，其余一律不算，
        # 免得凭空多出几十个「其他」。
        node_type = str(node.get("type") or "").lower()
        labels = {
            "workid": "章节测验",
            "work": "章节测验",
            "discussion": "讨论",
            "topic": "讨论",
            "exam": "考试",
            "test": "考试",
        }
        label = labels.get(node_type)
        if label is None and jobid.lower().startswith("work-"):
            label = "章节测验"
        if label is None:
            return
        mark = f"{course.id}:{jobid}"
        if mark in self._unsupported_seen:
            return
        self._unsupported_seen.add(mark)
        bucket = self.unsupported.setdefault(str(course.id), {})
        bucket[label] = bucket.get(label, 0) + 1

    # -- 上报 --------------------------------------------------------------- #

    def status(self, object_id: str) -> dict[str, Any] | None:
        """读视频状态，拿 ``duration`` 与 ``dtoken``。失败返回 None。"""
        url = EP_VIDEO_STATUS.format(object_id=object_id, ms=int(time.time() * 1000))
        _assert_prefix(url, READ_URL_PREFIXES, "视频状态")
        data = self._client().get_json(
            url, headers=_STATUS_HEADERS, note="视频状态", default=None
        )
        return data if isinstance(data, dict) else None

    def _send_raw(
        self,
        url: str,
        *,
        headers: dict[str, str],
        note: str = "上报",
    ) -> Any | None:
        """发一次写请求并原样返回响应；演练模式返回 ``None``（一个请求都不发）。

        阅读/文档/图书回 JSON、直播回纯文本，所以不能像视频那样在这里就
        ``.json()`` 解析掉，只能把响应交回调用方，由它按种类判定。
        """
        _assert_prefix(url, WRITE_URL_PREFIXES, "上报")
        if not self.apply:
            return None
        try:
            return self._client().get(url, headers=headers, note=note)
        except HttpError as exc:
            status = getattr(exc, "status", None)
            if status == 403 or "验证码" in str(exc) or "captcha" in str(exc).lower():
                self.blocked = (
                    f"平台回了 403（验证码/风控拦截），先在浏览器里打开学习通把验证码过一遍再重试（{exc.reason}）"
                )
                self.log.error(f"  {self.blocked}")
            else:
                self.log.warn(f"  上报请求失败：{exc}")
            return None

    def _send_failed_reason(self) -> str:
        """上报没成时该写进 reason 的话：被拦了就说被拦了。"""
        return self.blocked or SEND_FAILED

    def _send(self, url: str) -> dict[str, Any] | None:
        resp = self._send_raw(url, headers=_REPORT_HEADERS, note="上报观看时长")
        if resp is None:
            return None
        data = resp.json(default=None)
        return data if isinstance(data, dict) else None

    def report(
        self,
        target: VideoTarget,
        *,
        paced: bool = False,
        playing_time: int | None = None,
        ramp: bool = False,
    ) -> dict[str, Any]:
        """上报一个视频任务点。

        ``paced=False``（默认）直接把观看位置推到片尾，一次请求。
        ``paced=True`` 复刻原工具行为：先报 0（isdrag=3），再每 ``interval`` 秒
        报一分钟，最后一次报满（isdrag=4）。慢，但更像真的在看。
        ``ramp=True`` 是实测最优：把播放位置按 :data:`RAMP_STEPS` 分三步递进上报，
        每步间隔 ``ramp_delay`` 秒。三个请求，通过率明显高于一次性跳满。
        """
        result: dict[str, Any] = {
            "label": target.label,
            "jobid": target.jobid,
            "object_id": target.object_id,
            "kind": target.kind,
            "applied": self.apply,
            "ok": None,
            "status": "",
            "reason": "",
            "requests": 0,
        }
        if target.kind not in ("video", "audio"):
            return self._report_job(target, result)
        if not self.user_id:
            result["reason"] = "Cookie 里没有 _uid，无法拼签名"
            self.log.warn(f"  {target.label}: {result['reason']}")
            return result
        if not target.report_url:
            result["reason"] = "卡片页没给 reportUrl"
            self.log.warn(f"  {target.label}: {result['reason']}")
            return result
        _assert_prefix(target.report_url, WRITE_URL_PREFIXES, "上报")

        hint = target.att_duration
        if hint > 0:
            early = self._dead_state(target, hint, result)
            if early is not None:
                return early

        info = self.status(target.object_id)
        if not info:
            result["reason"] = "取不到视频状态（duration/dtoken）"
            self.log.warn(f"  {target.label}: {result['reason']}")
            return result
        duration = _as_int(info.get("duration"), 0)
        dtoken = str(info.get("dtoken") or "")
        if duration <= 0 or not dtoken:
            result["reason"] = f"视频状态不完整（duration={duration!r}, dtoken={bool(dtoken)}）"
            self.log.warn(f"  {target.label}: {result['reason']}")
            return result

        early = self._dead_state(target, duration, result)
        if early is not None:
            return early

        if ramp:
            return self._report_ramp(target, duration, dtoken, result)
        if paced:
            return self._report_paced(target, duration, dtoken, result)
        return self._report_once(target, duration, dtoken, result, playing_time=playing_time)

    def _report_ramp(
        self,
        target: VideoTarget,
        duration: int,
        dtoken: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        """按 :data:`RAMP_STEPS` 递进上报播放位置，最后一步送到片尾。

        平台不是「你说看完就算看完」的：一次性把位置从 0 推到片尾，它倾向于
        只记 ``playTime`` 而不给 ``isPassed``，甚至顺手挂上 ``videoFaceCaptureEnc``。
        分三步、每步间隔一两秒，平台就当成正常观看逐段记账，最后一步回
        ``isPassed=true``。所以「快」不等于「一次跳到底」，而是「少等、多报几次」。
        """
        steps = [max(1, round(duration * frac)) for frac in RAMP_STEPS]
        steps[-1] = duration
        steps = sorted(set(steps))
        if not self.apply:
            chain = " → ".join(str(s) for s in steps)
            result["ok"] = None
            result["status"] = "dry"
            result["reason"] = f"演练：{duration} 秒分 {len(steps)} 步递进上报（{chain}）"
            self.log.info(f"  [演练] {target.label}：{chain} 秒")
            return result
        for index, point in enumerate(steps, 1):
            # 中间几步不出声：一个任务点刷三行 ✓ 只会把日志读成三个任务点。
            self._report_once(
                target, duration, dtoken, result, playing_time=point, quiet=index < len(steps)
            )
            if result.get("status") == "failed":
                return result
            if result.get("status") == "passed":
                if index > 1:
                    result["reason"] += f"（第 {index} 步递进到 {point} 秒时通过）"
                return result
            if index < len(steps) and self.ramp_delay > 0:
                self.sleep(self.ramp_delay)
        result["reason"] += f"（已递进 {len(steps)} 步送到片尾仍未通过）"
        return result

    def _dead_state(
        self, target: VideoTarget, duration: int, result: dict[str, Any]
    ) -> dict[str, Any] | None:
        """发请求之前先认两种情况，能省则省、能拦则拦。

        一是**开了人脸抓拍**的视频（``videoFaceCaptureEnc`` 非空）：实测一次跳满只把
        ``playTime`` 记上，``isPassed`` 恒 false，而且这个状态回不去了——86 个任务点
        就这样被推到「有进度但不算完成」。宁可跳过并说清楚，也不要再制造死态。

        二是**已经是死态**的（播放位置满了却没判通过）：再发任何变体（换 ``isdrag``、
        加 ``videoFaceCaptureEnc``、``randomCaptureTime``、逐分钟上报）平台都回 false，
        全额浪费请求。直接标 ``stuck``，让界面单独统计，不混进「未确认」。
        """
        if duration <= 0 or target.passed:
            return None
        full = target.play_time_ms >= duration * 1000
        if target.face_capture and not full and not self.force:
            result["ok"] = None
            result["status"] = "skipped"
            result["reason"] = (
                "该视频开了人脸抓拍（videoFaceCaptureEnc 非空），自动上报拿不到通过，"
                "需要手动播放；继续上报只会把播放位置占满变成「有进度但不算完成」"
            )
            self.log.warn(f"  {target.label}: {result['reason']}")
            return result
        if full:
            result["ok"] = False
            result["status"] = "stuck"
            result["reason"] = (
                f"平台记的播放位置已满（{target.play_time_ms // 1000}/{duration} 秒）"
                "但任务点没通过，重复上报无效"
            )
            if target.face_capture:
                result["reason"] += "（该视频开了人脸抓拍，需手动重看）"
            self.log.warn(f"  {target.label}: {result['reason']}")
            return result
        return None

    def _report_once(
        self,
        target: VideoTarget,
        duration: int,
        dtoken: str,
        result: dict[str, Any],
        *,
        playing_time: int | None = None,
        quiet: bool = False,
    ) -> dict[str, Any]:
        enc = build_enc(
            clazz_id=target.clazz_id,
            user_id=self.user_id,
            jobid=target.jobid,
            object_id=target.object_id,
            playing_time=duration if playing_time is None else playing_time,
            duration=duration,
        )
        url = build_report_url(
            report_url=target.report_url,
            dtoken=dtoken,
            clazz_id=target.clazz_id,
            user_id=self.user_id,
            jobid=target.jobid,
            object_id=target.object_id,
            duration=duration,
            other_info=target.other_info,
            enc=enc,
            rt=target.rt,
            dtype=target.dtype,
            playing_time=playing_time,
        )
        if not self.apply:
            result["reason"] = f"演练：{duration} 秒一次送到片尾"
            result["ok"] = None
            result["status"] = "dry"
            self.log.info(f"  [演练] {target.label}：{duration} 秒 → 片尾")
            return result
        data = self._send(url)
        result["requests"] += 1
        if data is None:
            result["ok"] = False
            result["status"] = "failed"
            result["reason"] = self._send_failed_reason()
            self.log.warn(f"  {target.label}: {result['reason']}")
            return result
        self._judge(data, result)
        if not quiet:
            self.log.ok(f"  {target.label}: {duration} 秒")
        return result

    def _judge(self, data: dict[str, Any], result: dict[str, Any]) -> None:
        """按平台回的 ``isPassed`` 判这个任务点到底有没有完成。

        这是唯一可信的判据：``isPassed=true`` 表示平台已把任务点标成通过，
        实测课程页「已完成任务点」计数会同步 +1。``isPassed=false`` 就是没完成——
        以前把它当成「没有剩余可报」一律记成功，用户看到「上报成功」但学习通点数
        不动，就是因为这里谎报了。
        """
        if data.get("isPassed"):
            result["ok"] = True
            result["status"] = "passed"
            result["reason"] = "平台回 isPassed=true，任务点已标记完成"
        else:
            result["ok"] = False
            result["status"] = "unconfirmed"
            result["reason"] = (
                "平台回 isPassed=false，任务点没被标成通过"
                f"（videoTimeLimit={data.get('videoTimeLimit')}, "
                f"hasJobLimit={data.get('hasJobLimit')}）"
            )

    def _report_job(self, target: VideoTarget, result: dict[str, Any]) -> dict[str, Any]:
        """阅读/文档/图书/直播任务点：一次 GET，看平台回的文本判定成败。

        这四种不需要签名，也不碰 ``/ananas/status``——`:class:`VideoTarget` 里的
        ``jtoken`` / ``streamName`` / ``vdoid`` 是唯一的额外凭据。
        """
        ms = int(time.time() * 1000)
        if target.kind == "live":
            if not target.stream_name or not target.vdoid or not self.user_id:
                result["ok"] = False
                result["reason"] = "直播任务点缺 streamName/vdoid/用户 id，构造不出上报地址"
                return result
            url = EP_LIVE_TIME.format(
                stream=target.stream_name,
                vdoid=target.vdoid,
                userid=self.user_id,
                ms=ms,
                courseid=target.course_id,
            )
            headers = {**_JOB_HEADERS, "Referer": REFERER_LIVE}
        else:
            if not target.jobid or not target.jtoken:
                result["ok"] = False
                result["reason"] = f"{target.kind_label}任务点缺 jobid/jtoken，构造不出上报地址"
                return result
            template = {"read": EP_JOB_READ, "document": EP_JOB_DOCUMENT, "book": EP_JOB}[target.kind]
            url = template.format(
                jobid=target.jobid,
                knowledgeid=target.knowledge_id,
                courseid=target.course_id,
                clazzid=target.clazz_id,
                jtoken=target.jtoken,
                ms=ms,
            )
            referer = REFERER_BOOK if target.kind == "book" else REFERER_PDF
            headers = {**_JOB_HEADERS, "Referer": referer}

        result["requests"] = 1
        if not self.apply:
            result["ok"] = None
            result["status"] = "dry"
            result["reason"] = f"演练：只算了{target.kind_label}上报地址，没发请求"
            self.log.info(f"  [演练] {target.kind_label} {target.label}")
            return result

        resp = self._send_raw(url, headers=headers, note=f"上报{target.kind_label}")
        if resp is None:
            result["ok"] = False
            result["status"] = "failed"
            result["reason"] = self._send_failed_reason()
            return result
        self._judge_job(target, resp, result)
        return result

    def _judge_job(self, target: VideoTarget, resp: Any, result: dict[str, Any]) -> None:
        """判定非视频任务点是否上报成功。

        阅读/文档/图书回 JSON ``{"status": true}``；直播回纯文本 ``@success``。
        先按 JSON 判 ``status``，拿不到 JSON 再退回文本匹配——平台的返回格式
        随模块版本变，不能只认一种。
        """
        text = _response_text(resp)
        data = resp.json(default=None)
        if isinstance(data, dict) and "status" in data:
            ok = bool(data.get("status"))
        else:
            compact = text.replace(" ", "")
            ok = "@success" in text or '"status":true' in compact
        result["ok"] = bool(ok)
        result["status"] = "passed" if ok else "unconfirmed"
        if ok:
            result["reason"] = f"平台确认完成（{text[:60].strip()!r}）"
        else:
            result["reason"] = f"平台没确认：{text[:120].strip()!r}"
        self.log.ok(f"  {target.kind_label} {target.label}: {result['reason']}")

    def _report_paced(
        self,
        target: VideoTarget,
        duration: int,
        dtoken: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        total_minutes = duration // 60
        rest = duration - total_minutes * 60
        if not self.apply:
            self.log.info(
                f"  [演练] {target.label}：{duration} 秒，逐分钟上报 "
                f"{total_minutes + (1 if rest else 0)} 次（约 {duration} 秒）"
            )
            result["reason"] = "演练：逐分钟上报"
            return result

        data = self._send(
            build_report_url(
                report_url=target.report_url,
                dtoken=dtoken,
                clazz_id=target.clazz_id,
                user_id=self.user_id,
                jobid=target.jobid,
                object_id=target.object_id,
                duration=duration,
                other_info=target.other_info,
                enc=build_enc(
                    clazz_id=target.clazz_id,
                    user_id=self.user_id,
                    jobid=target.jobid,
                    object_id=target.object_id,
                    playing_time=0,
                    duration=duration,
                ),
                rt=target.rt,
                dtype=target.dtype,
                playing_time=0,
                isdrag=3,
            )
        )
        result["requests"] += 1
        self.log.debug(f"  {target.label}: 起点已报 {bool(data)}")

        for minute in range(total_minutes):
            self.sleep(self.interval)
            playing = minute * 60 + 59
            data = self._send(
                build_report_url(
                    report_url=target.report_url,
                    dtoken=dtoken,
                    clazz_id=target.clazz_id,
                    user_id=self.user_id,
                    jobid=target.jobid,
                    object_id=target.object_id,
                    duration=duration,
                    other_info=target.other_info,
                    enc=build_enc(
                        clazz_id=target.clazz_id,
                        user_id=self.user_id,
                        jobid=target.jobid,
                        object_id=target.object_id,
                        playing_time=playing,
                        duration=duration,
                    ),
                    rt=target.rt,
                    dtype=target.dtype,
                    playing_time=playing,
                    isdrag=0,
                )
            )
            result["requests"] += 1
            self.log.debug(f"  {target.label}: {playing}/{duration}")
            if data is None:
                result["reason"] = "中途上报失败"
                return result
        if rest:
            self.sleep(min(self.interval, rest))
        data = self._send(
            build_report_url(
                report_url=target.report_url,
                dtoken=dtoken,
                clazz_id=target.clazz_id,
                user_id=self.user_id,
                jobid=target.jobid,
                object_id=target.object_id,
                duration=duration,
                other_info=target.other_info,
                enc=build_enc(
                    clazz_id=target.clazz_id,
                    user_id=self.user_id,
                    jobid=target.jobid,
                    object_id=target.object_id,
                    playing_time=duration,
                    duration=duration,
                ),
                rt=target.rt,
                dtype=target.dtype,
                playing_time=duration,
                isdrag=4,
            )
        )
        result["requests"] += 1
        if data is None:
            result["ok"] = False
            result["reason"] = "中途上报失败"
            self.log.warn(f"  {target.label}: {result['reason']}")
            return result
        self._judge(data, result)
        self.log.ok(f"  {target.label}: {duration} 秒（逐分钟）")
        return result

    # -- 批量 --------------------------------------------------------------- #

    def plan(
        self,
        courses: list[Course],
        *,
        limit: int = 0,
        kinds: Sequence[str] | None = None,
    ) -> list[tuple[Course, VideoTarget]]:
        """先清点一遍待写回的任务点，好让调用方知道总数（进度条要用）。

        ``kinds`` 限定种类（``video`` / ``audio`` / ``read`` / ``document`` /
        ``book`` / ``live``），不传就是全都要；给了不认识的种类直接报错，
        免得用户以为自己限定了范围而实际全跑了。
        清点只读章节页与卡片页，不会发出任何上报；结果直接喂给 :meth:`run`，
        所以不会因此多请求一次。

        两个刻意的取舍：

        * **已经通过的任务点不列。** 再报一次一个点数都不会涨，只是白花请求
          （以前不清，``--limit 4`` 前四个常常全是早就通过的旧视频）。
        * **同一门课里「没看过」的排在前面。** 卡片页给的 ``playTime`` 为 0 且没有
          人脸抓拍的，是最有希望一次通过的；有进度、带抓拍、已经死态的排在后面，
          这样 ``--limit`` 截断时先砍掉的是没救的。
        """
        wanted = _normalize_kinds(kinds)
        out: list[tuple[Course, VideoTarget]] = []
        skipped_passed = 0
        self.unsupported = {}
        self._unsupported_seen = set()
        for course in courses:
            self.log.step(f"{course.name}")
            batch: list[tuple[Course, VideoTarget]] = []
            for target in self.iter_targets(course):
                if wanted is not None and target.kind not in wanted:
                    continue
                if target.passed:
                    skipped_passed += 1
                    continue
                batch.append((course, target))
            batch.sort(key=lambda pair: _plan_rank(pair[-1]))
            for pair in batch:
                out.append(pair)
                if limit and len(out) >= limit:
                    self.log.info(f"  已达上限 {limit}，停下")
                    if skipped_passed:
                        self.log.info(f"  跳过 {skipped_passed} 个已经通过的任务点")
                    return out
        if skipped_passed:
            self.log.info(f"  跳过 {skipped_passed} 个已经通过的任务点")
        return out

    def run(
        self,
        courses: list[Course],
        *,
        limit: int = 0,
        paced: bool = False,
        on_step: Callable[[int, int, VideoTarget], None] | None = None,
        work: list[tuple[Course, VideoTarget]] | None = None,
        workers: int = 1,
        kinds: Sequence[str] | None = None,
        ramp: bool = False,
    ) -> list[dict[str, Any]]:
        """跑一批课程。``limit`` > 0 时最多处理这么多个任务点。

        ``work`` 是 :meth:`plan` 的结果（传了就省掉重新清点）；
        ``on_step(done, total, target)`` 在每个任务点开跑前回调一次，给进度条用。

        ``workers`` > 1 时用线程池并发上报。每个任务点都是「读一次状态 + 发一次上报」
        的独立 GET，彼此没有先后依赖，所以并发在语义上是安全的；真正要小心的是
        HTTP 出口（见 :meth:`_client`）和回调的顺序（``results`` 始终按 ``todo``
        顺序排，不按完成顺序）。
        逐分钟模式（``paced``）每一步之间要 sleep 几十秒，并发没有收益，强制串行；
        逐分钟只对音视频有意义，非音视频任务点会按一次性上报处理。
        """
        todo = work if work is not None else self.plan(courses, limit=limit, kinds=kinds)
        total = len(todo)
        if not total:
            return []
        workers = max(1, int(workers))
        if paced and workers > 1:
            self.log.info("逐分钟上报本来就是慢的，并发没有收益，按串行跑")
            workers = 1
        if workers == 1:
            return self._run_serial(todo, paced=paced, on_step=on_step, ramp=ramp)
        return self._run_parallel(todo, paced=paced, on_step=on_step, workers=workers, ramp=ramp)

    def _run_serial(
        self,
        todo: list[tuple[Course, VideoTarget]],
        *,
        paced: bool,
        on_step: Callable[[int, int, VideoTarget], None] | None,
        ramp: bool = False,
    ) -> list[dict[str, Any]]:
        total = len(todo)
        results: list[dict[str, Any]] = []
        for done, (_course, target) in enumerate(todo):
            if on_step is not None:
                on_step(done, total, target)
            try:
                results.append(self.report(target, paced=paced, ramp=ramp))
            except (HttpError, WritebackError) as exc:
                self.log.warn(f"  {target.label}: {exc}")
                results.append(self._fail_row(target, str(exc)))
        if on_step is not None:
            on_step(total, total, todo[-1][1])
        return results

    def _run_parallel(
        self,
        todo: list[tuple[Course, VideoTarget]],
        *,
        paced: bool,
        on_step: Callable[[int, int, VideoTarget], None] | None,
        workers: int,
        ramp: bool = False,
    ) -> list[dict[str, Any]]:
        total = len(todo)
        self.log.info(f"并发上报：{workers} 个线程，共 {total} 个任务点")
        lock = threading.Lock()
        state = {"done": 0, "stopped": False}
        slots: list[dict[str, Any] | None] = [None] * total

        def one(index: int, target: VideoTarget) -> None:
            with lock:
                if state["stopped"]:
                    return
                done = state["done"]
            if on_step is not None:
                try:
                    on_step(done, total, target)
                except Exception:  # 取消信号来自上层，别再报一次
                    with lock:
                        state["stopped"] = True
                    return
            try:
                row = self.report(target, paced=paced, ramp=ramp)
            except (HttpError, WritebackError) as exc:
                self.log.warn(f"  {target.label}: {exc}")
                row = self._fail_row(target, str(exc))
            except Exception as exc:  # 一个点炸了不能拖垮整批
                self.log.warn(f"  {target.label}: 意外错误 {exc}")
                row = self._fail_row(target, str(exc))
            with lock:
                slots[index] = row
                state["done"] += 1

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="studydash-wb") as pool:
            futures = [pool.submit(one, i, target) for i, (_c, target) in enumerate(todo)]
            for future in futures:
                future.result()
        # 并发下每个点只在「开跑前」回调，最后一次可能停在 total-1（比如 3 线程里
        # 第 6 个点的 on_step 拿到的 done 是 5）。补一次收尾，进度条才能走到头。
        if on_step is not None and not state["stopped"]:
            with contextlib.suppress(Exception):
                on_step(total, total, todo[-1][1])
        return [row for row in slots if row is not None]

    def _fail_row(self, target: VideoTarget, reason: str) -> dict[str, Any]:
        return {
            "label": target.label,
            "jobid": target.jobid,
            "object_id": target.object_id,
            "kind": target.kind,
            "applied": self.apply,
            "ok": False,
            "status": "failed",
            "reason": reason,
            "requests": 0,
        }


def _normalize_kinds(kinds: Sequence[str] | None) -> set[str] | None:
    """把 ``kinds`` 选项规整成集合；没给返回 None（表示全都要）。"""
    if not kinds:
        return None
    raw = [kinds] if isinstance(kinds, str) else list(kinds)
    out = {str(k).strip().lower() for k in raw if str(k).strip()}
    unknown = out - set(TASK_KINDS)
    if unknown:
        raise WritebackError(
            f"不认识的任务点类型：{', '.join(sorted(unknown))}（可选 {', '.join(TASK_KINDS)}）"
        )
    return out or None


def _response_text(resp: Any) -> str:
    """把响应体解成文本，给直播的 ``@success`` 之类做匹配。"""
    content = getattr(resp, "content", b"")
    if isinstance(content, bytes):
        return content.decode("utf-8", "replace")
    if isinstance(content, str):
        return content
    return str(content)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out > 0 else default
