"""全项目唯一的网络出口。

只读承诺怎么落地
----------------
* **GET 到任何地址**：GET 天然是读操作。
* **POST 只允许打到适配器显式声明的查询端点**（``query_endpoints``）。
  慕课、智慧树这类平台的接口是 RPC 风格，查询也用 POST，一刀切禁用 GET
  之外的动词就等于放弃这些平台。所以这里改成「白名单 + 审计」：
  没在白名单里的地址，POST 直接抛 :class:`ReadOnlyError`；白名单本身由测试
  卡住（端点里不许出现 submit / save / answer / commit / upload 这类词）。

项目里其它任何模块都不应该直接 import ``urllib.request``（有测试守着）。

因此 studydash 能做的事只有一件：把你的数据读出来看看。
它不会交作业、不会刷视频、不会替你点任何按钮。
"""

from __future__ import annotations

import gzip
import json as _json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

__all__ = ["HttpClient", "Response", "HttpError", "ReadOnlyError", "DEFAULT_UA"]

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

#: 读操作允许的动词
READ_METHODS: tuple[str, ...] = ("GET", "POST")

#: POST 白名单里禁止出现的词——出现就说明这个端点像是在「写」
_FORBIDDEN_IN_POST_ENDPOINT = re.compile(
    r"(submit|save|answer|commit|upload|create|delete|update|modify|"
    r"doWork|doHomeWork|finish|report|sign|checkin|signin|submitWork)",
    re.I,
)

_CHARSET = re.compile(r"charset\s*=\s*[\"']?([\w-]+)", re.I)


class ReadOnlyError(RuntimeError):
    """试图发起写操作，或 POST 到没被声明的地址。"""


class HttpError(Exception):
    """一次请求失败。"""

    def __init__(self, url: str, reason: str, status: int | None = None) -> None:
        self.url = url
        self.reason = reason
        self.status = status
        super().__init__(f"{url} 失败: {reason}" + (f" (HTTP {status})" if status else ""))


@dataclass
class Response:
    url: str
    status: int
    headers: dict[str, str]
    content: bytes

    _encoding: str = ""

    @property
    def encoding(self) -> str:
        if self._encoding:
            return self._encoding
        ctype = self.headers.get("content-type", "")
        m = _CHARSET.search(ctype)
        if m:
            return m.group(1)
        return "utf-8"

    @property
    def text(self) -> str:
        for enc in (self.encoding, "utf-8", "gb18030", "latin-1"):
            try:
                return self.content.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return self.content.decode("utf-8", "replace")

    def json(self, default: Any = None) -> Any:
        try:
            return _json.loads(self.text)
        except (ValueError, TypeError):
            return default

    def __len__(self) -> int:
        return len(self.content)


@dataclass
class HttpTrace:
    """一次请求的留痕，``probe`` 子命令靠它输出端点原始响应。"""

    method: str
    url: str
    status: int = 0
    bytes: int = 0
    ms: int = 0
    note: str = ""
    body: str = ""

    def to_dict(self, *, with_body: bool = False) -> dict[str, Any]:
        out = {
            "method": self.method,
            "url": self.url,
            "status": self.status,
            "bytes": self.bytes,
            "ms": self.ms,
            "note": self.note,
        }
        if with_body:
            out["body"] = self.body
        return out


class HttpClient:
    """带 Cookie、限速、重试与留痕的只读客户端。

    参数
    ----
    cookie      整个 Cookie 请求头的字符串（``a=1; b=2``），空则不发送
    timeout     单次请求超时秒数
    delay       两次请求之间的最小间隔秒数（礼貌抓取，别把人家站点打挂）
    user_agent  伪装 UA；有些站点对空 UA 直接 403
    retries     失败重试次数（只对超时/5xx/连接错误重试）
    trace_limit 最多保留多少条请求留痕
    query_endpoints
        允许 POST 的地址前缀白名单。留空 = 这个客户端完全不能 POST。
        前缀必须通过 :func:`validate_query_endpoints` 检查（不含写动作词）。
    """

    def __init__(
        self,
        *,
        cookie: str = "",
        timeout: float = 8.0,
        delay: float = 0.0,
        user_agent: str = DEFAULT_UA,
        referer: str = "",
        headers: Mapping[str, str] | None = None,
        retries: int = 2,
        trace_limit: int = 200,
        query_endpoints: Iterable[str] = (),
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self.cookie = (cookie or "").strip()
        self.timeout = float(timeout)
        self.delay = max(0.0, float(delay))
        self.user_agent = user_agent or DEFAULT_UA
        self.referer = referer
        self.extra_headers = {k: v for k, v in (headers or {}).items()}
        self.retries = max(0, int(retries))
        self.trace_limit = max(1, int(trace_limit))
        self.query_endpoints = validate_query_endpoints(query_endpoints)
        self.log = logger or (lambda _msg: None)

        self.trace: list[HttpTrace] = []
        self._last_at = 0.0
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(),
            urllib.request.HTTPRedirectHandler(),
        )
        # 不跟随 gzip 的自动解压留给 Content-Encoding 处理，禁用 brotli
        self._opener.addheaders = []

    # -- 对外 API ---------------------------------------------------------- #

    def get(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        note: str = "",
    ) -> Response:
        """发一次 GET。失败抛 :class:`HttpError`。"""
        full = _with_params(url, params)
        return self._request(full, headers=headers, note=note)

    def get_text(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        note: str = "",
        default: str = "",
    ) -> str:
        """GET 并返回文本；失败时返回 ``default`` 而不是抛异常。

        抓取器里绝大多数调用都该用这个——一个页面的失败不该让整轮聚合崩掉。
        """
        try:
            return self.get(url, params, headers=headers, note=note).text
        except HttpError as exc:
            self.log(f"  ! {exc}")
            return default

    def get_json(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        note: str = "",
        default: Any = None,
    ) -> Any:
        try:
            return self.get(url, params, headers=headers, note=note).json(default)
        except HttpError as exc:
            self.log(f"  ! {exc}")
            return default

    def post_query(
        self,
        url: str,
        data: Mapping[str, Any] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        note: str = "",
    ) -> Response:
        """往**已声明的查询端点**发一次 POST，用来取数据。

        这不是「放开 POST」——地址必须命中 ``query_endpoints`` 白名单，
        否则抛 :class:`ReadOnlyError`。白名单端点由各适配器声明，并被测试
        卡住（端点里不许出现 submit/save/answer/commit 等词）。
        """
        if not self._post_allowed(url):
            raise ReadOnlyError(
                f"拒绝 POST 到未声明的地址: {url}\n"
                f"  已声明的查询端点: {', '.join(self.query_endpoints) or '(无)'}\n"
                "  只读模式：只能 POST 到适配器显式声明的查询端点。"
            )
        return self._request(url, method="POST", data=data, headers=headers, note=note)

    def post_query_json(
        self,
        url: str,
        data: Mapping[str, Any] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        note: str = "",
        default: Any = None,
    ) -> Any:
        try:
            return self.post_query(url, data, headers=headers, note=note).json(default)
        except (HttpError, ReadOnlyError) as exc:
            self.log(f"  ! {exc}")
            return default

    def _post_allowed(self, url: str) -> bool:
        """地址是否命中声明的查询端点。

        纯 ``startswith`` 是不够的：声明 ``…/getMyCourseRpc.rpc`` 会把
        ``…/getMyCourseRpc.rpcEVIL`` 一起放行。所以前缀命中后还要求紧跟一个
        URL 分隔符（或就此结束）。尾部 ``/`` 先归一化，免得 ``…/api/``
        反而放不过 ``…/api/items``。
        """
        for prefix in self.query_endpoints:
            base = prefix.rstrip("/")
            if url == base or (url.startswith(base) and url[len(base)] in "/?&#;"):
                return True
        return False

    # -- 内部 -------------------------------------------------------------- #

    def _wait(self) -> None:
        if self.delay <= 0:
            return
        gap = time.monotonic() - self._last_at
        if gap < self.delay:
            time.sleep(self.delay - gap)
        self._last_at = time.monotonic()

    def _headers(self, extra: Mapping[str, str] | None) -> dict[str, str]:
        head = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
        }
        if self.cookie:
            head["Cookie"] = self.cookie
        if self.referer:
            head["Referer"] = self.referer
        head.update(self.extra_headers)
        head.update(extra or {})
        return head

    def _request(
        self,
        url: str,
        *,
        method: str = "GET",
        data: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        note: str = "",
    ) -> Response:
        if method not in READ_METHODS:
            raise ReadOnlyError(f"method {method!r} 不在只读范围内，本项目不接受")
        body_bytes = _encode_body(data) if data is not None else None
        if body_bytes is not None and method != "POST":
            raise ReadOnlyError("请求体只能跟着 POST 发送，而 POST 需要查询端点白名单")
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            self._wait()
            started = time.monotonic()
            request = urllib.request.Request(url, data=body_bytes, method=method)
            head = self._headers(headers)
            if body_bytes is not None:
                head.setdefault("Content-Type", "application/x-www-form-urlencoded")
            for key, value in head.items():
                request.add_header(key, value)
            try:
                with self._opener.open(request, timeout=self.timeout) as resp:
                    raw = resp.read()
                    body = _decompress(raw, resp.headers.get("Content-Encoding", ""))
                    status = int(getattr(resp, "status", 200) or 200)
                    response = Response(
                        url=resp.geturl() or url,
                        status=status,
                        headers={k.lower(): v for k, v in resp.headers.items()},
                        content=body,
                    )
                    self._record(
                        HttpTrace(
                            method=method,
                            url=url,
                            status=status,
                            bytes=len(body),
                            ms=int((time.monotonic() - started) * 1000),
                            note=note,
                            body=response.text[:20000],
                        )
                    )
                    return response
            except urllib.error.HTTPError as exc:            # 4xx / 5xx
                body = b""
                try:
                    body = exc.read()
                except Exception:                             # pragma: no cover
                    pass
                last = HttpError(url, exc.reason or "HTTP error", exc.code)
                self._record(
                    HttpTrace(
                        method=method,
                        url=url,
                        status=int(exc.code or 0),
                        bytes=len(body),
                        ms=int((time.monotonic() - started) * 1000),
                        note=note or "error",
                    )
                )
                # 4xx 重试没有意义，直接放弃
                if 400 <= int(exc.code or 0) < 500:
                    raise last from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = HttpError(url, str(getattr(exc, "reason", exc)))
                self._record(
                    HttpTrace(
                        method=method,
                        url=url,
                        status=0,
                        ms=int((time.monotonic() - started) * 1000),
                        note=note or "error",
                    )
                )
            if attempt < self.retries:
                time.sleep(0.4 * (attempt + 1))
        raise last or HttpError(url, "未知错误")

    def _record(self, item: HttpTrace) -> None:
        self.trace.append(item)
        if len(self.trace) > self.trace_limit:
            del self.trace[: len(self.trace) - self.trace_limit]

    # -- 自检 -------------------------------------------------------------- #

    @staticmethod
    def methods() -> tuple[str, ...]:
        """本客户端支持的 HTTP 动词。没有 PUT/DELETE/PATCH，这是故意的。"""
        return READ_METHODS

    def readonly_report(self) -> dict[str, Any]:
        return {
            "readonly": True,
            "methods": list(READ_METHODS),
            "post_endpoints": list(self.query_endpoints),
            "requests": len(self.trace),
        }


def _encode_body(data: Mapping[str, Any]) -> bytes:
    clean = {
        k: ("1" if v is True else "0" if v is False else str(v))
        for k, v in data.items()
        if v is not None
    }
    return urllib.parse.urlencode(clean).encode("utf-8")


def validate_query_endpoints(endpoints: Iterable[str]) -> tuple[str, ...]:
    """检查 POST 白名单里的地址看着像不像「查询」。

    端点里出现 submit/save/answer/commit/upload… 直接拒绝——那种地址一旦
    进白名单，只读承诺就名存实亡了。纯字符串启发式，不能证明什么，
    但能把明显的错误挡在启动阶段。
    """
    out: list[str] = []
    for raw in endpoints:
        url = str(raw).strip()
        if not url:
            continue
        if not url.startswith(("http://", "https://")):
            raise ReadOnlyError(f"查询端点必须是绝对 URL: {url!r}")
        hit = _FORBIDDEN_IN_POST_ENDPOINT.search(url)
        if hit:
            raise ReadOnlyError(
                f"拒绝把 {url} 当查询端点：地址里含有写动作词 {hit.group(0)!r}。"
                "只读模式不允许 POST 到这类接口。"
            )
        out.append(url)
    return tuple(out)


def _with_params(url: str, params: Mapping[str, Any] | None) -> str:
    if not params:
        return url
    clean = {
        k: ("1" if v is True else "0" if v is False else str(v))
        for k, v in params.items()
        if v is not None
    }
    sep = "&" if urllib.parse.urlparse(url).query else "?"
    return url + sep + urllib.parse.urlencode(clean)


def _decompress(raw: bytes, encoding: str) -> bytes:
    enc = (encoding or "").lower()
    try:
        if "gzip" in enc:
            return gzip.decompress(raw)
        if "deflate" in enc:
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
    except (OSError, zlib.error):
        return raw
    return raw


#: 扫描「谁在私自联网」时用的特征词。
_NETWORK_LIB = re.compile(r"\b(urllib\.request|requests\.(get|post|put|delete|patch)|http\.client)\b")

#: 允许绕开上面的扫描、自己开 socket 的模块——但只准连本机（下面两条正则会核）。
#: 目前只有 ``cdp.py``：它要跟本机的浏览器调试端口讲 DevTools 协议，用裸 socket
#: 手写 WebSocket 帧，正因为这样它才不必引入 HTTP 客户端库。
LOCAL_ONLY_MODULES = frozenset({"cdp.py"})

#: 被放行的模块必须显式把主机定死在回环地址上。
_LOCAL_HOST = re.compile(r"""HOST\s*=\s*["'](127\.0\.0\.1|localhost|::1)["']""")

#: 被放行的模块里不许出现别的主机名。
_URL_HOST = re.compile(r"https?://([A-Za-z0-9._\-]+)")


def _is_local_only(text: str) -> bool:
    """被放行的模块是否确实只连本机。"""
    if not _LOCAL_HOST.search(text):
        return False
    return all(host in ("127.0.0.1", "localhost", "::1") for host in _URL_HOST.findall(text))


def assert_readonly(paths: Iterable[str] | None = None) -> list[str]:
    """扫描源码，找出除本模块外还在私自联网的文件。

    两条规则：

    1. 谁都不许直接用 ``urllib.request`` / ``requests`` / ``http.client``——
       网络出口只能是本模块。
    2. 少数模块（见 :data:`LOCAL_ONLY_MODULES`）可以自己开 socket，但主机必须
       钉死在回环地址上，且文件里不许出现别的主机名。

    返回违规文件列表（空表示干净）。CI 里跑这个，防止有人偷偷加回写操作。
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent
    myself = pathlib.Path(__file__).resolve()
    offenders: list[str] = []
    for path in paths or [str(p) for p in root.rglob("*.py")]:
        p = pathlib.Path(path)
        if p.resolve() == myself:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        if p.name in LOCAL_ONLY_MODULES:
            if not _is_local_only(text):
                offenders.append(str(p))
        elif _NETWORK_LIB.search(text):
            offenders.append(str(p))
    return offenders
