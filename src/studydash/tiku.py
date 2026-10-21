"""题库查询：给章节测验找答案。

只实现一个题库——**言溪题库**（``https://tk.enncy.cn/query``）。它不需要注册
密钥，一个 token 走 query 参数就能查，响应里 ``data.answer`` 就是答案文本。
token 由用户自己在设置页填（``account.options["tiku_token"]``），本模块不内置
任何 token，也不替用户申请。

为什么不内置更多题库：上游项目接了一串（言溪/GO题/LIKE/…），接口各不相同且
经常改，把一堆脆弱的适配器一次性搬进来不划算。这里的 :class:`TikuClient`
接口留得很薄——以后要加题库，实现 ``query()`` 就行。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .http import HttpClient, HttpError

__all__ = ["DEFAULT_API", "TikuClient", "TikuError", "TikuReply", "TikuSettings"]

#: 言溪题库查询地址
DEFAULT_API = "https://tk.enncy.cn/query"

#: 一次查询之间至少隔多久（秒）——题库会限流，太快会被当成爬
DEFAULT_DELAY = 1.0


class TikuError(Exception):
    """题库用不了（没 token、token 过期、次数用完）。"""


@dataclass(slots=True)
class TikuReply:
    """一次查询的结果。"""

    #: 题库给的答案原文（没有答案时是空串）
    answer: str = ""
    #: 剩余次数（题库愿意给才给，``None`` 表示没说）
    times: int | None = None
    #: 题库的说明（失败时是错误原因）
    message: str = ""


class TikuClient:
    """言溪题库客户端。

    ``query()`` 永远不抛网络异常——查不到就返回空 :class:`TikuReply`，
    调用方按「这题没答案」处理，不至于因为一次超时把整页题目丢掉。
    """

    def __init__(
        self,
        token: str,
        *,
        api: str = DEFAULT_API,
        timeout: float = 8.0,
        log: Callable[[str], None] | None = None,
        http: HttpClient | None = None,
    ) -> None:
        token = (token or "").strip()
        if not token:
            raise TikuError("没配置题库 token（设置页「题库 token」留空了）")
        self.token = token
        self.api = api
        self.log = log or (lambda _msg: None)
        self.http = http or HttpClient(
            cookie="",
            timeout=timeout,
            delay=0.0,
            retries=1,
            logger=lambda _msg: None,
        )
        self.used = 0

    # -- 查询 --------------------------------------------------------------- #

    def query(self, question: str) -> TikuReply:
        """查一道题。``question`` 是题干原文（不含选项）。"""
        question = (question or "").strip()
        if not question:
            return TikuReply(message="题干是空的")
        url = f"{self.api}?question={_quote(question)}&token={_quote(self.token)}"
        try:
            resp = self.http.get(url, note="题库查询")
        except HttpError as exc:
            self.log(f"  ! 题库请求失败：{exc}")
            return TikuReply(message=f"题库请求失败：{exc.reason}")
        self.used += 1
        try:
            payload = json.loads(resp.content.decode("utf-8", "replace"))
        except ValueError:
            text = resp.content.decode("utf-8", "replace")[:200]
            self.log(f"  ! 题库回了非 JSON：{text}")
            return TikuReply(message="题库回了非 JSON")
        return _parse_reply(payload)


def _parse_reply(payload: Any) -> TikuReply:
    """把题库的 JSON 翻成 :class:`TikuReply`。"""
    if not isinstance(payload, dict):
        return TikuReply(message="题库响应不是对象")
    data = payload.get("data")
    message = str(payload.get("message") or "")
    times: int | None = None
    answer = ""
    if isinstance(data, dict):
        raw_times = data.get("times")
        if isinstance(raw_times, int):
            times = raw_times
        answer = str(data.get("answer") or "").strip()
    if not payload.get("code"):
        # code 假 ⇒ 查询失败；常见原因是 token 错或次数用完
        return TikuReply(answer=answer, times=times, message=message or answer or "题库拒绝了这次查询")
    return TikuReply(answer=answer, times=times, message=message)


def _quote(text: str) -> str:
    from urllib.parse import quote

    return quote(text, safe="")


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #

#: 回给前端时，已配置的 token 只露尾号
MASK_PREFIX = "已配置"


@dataclass
class TikuSettings:
    """``config.json`` 里的 ``tiku`` 段。

    token 是用户自己的题库账号凭证，只存在本机配置文件里，接口回给前端一律
    打码（``已配置（尾号 abcd）``），前端留空就表示「不改」。
    """

    token: str = ""
    api: str = DEFAULT_API
    #: 题库覆盖率低于这个值就不交卷（避免低分作答被记成绩）
    cover_rate: float = 0.8

    @classmethod
    def from_dict(cls, data: Any) -> TikuSettings:
        if not isinstance(data, Mapping):
            return cls()
        token = str(data.get("token") or "").strip()
        if token.startswith(MASK_PREFIX):
            token = ""                       # 前端把打码值回传了，视为「不改」
        api = str(data.get("api") or DEFAULT_API).strip() or DEFAULT_API
        try:
            cover = float(data.get("cover_rate", 0.8))
        except (TypeError, ValueError):
            cover = 0.8
        return cls(token=token, api=api, cover_rate=max(0.0, min(1.0, cover)))

    def to_dict(self) -> dict[str, Any]:
        return {"token": self.token, "api": self.api, "cover_rate": self.cover_rate}

    def masked(self) -> dict[str, Any]:
        """给前端看的样子：token 只留尾号。"""
        token = self.token.strip()
        shown = f"{MASK_PREFIX}（尾号 {token[-4:]}）" if len(token) >= 4 else (MASK_PREFIX if token else "")
        return {"token": shown, "token_set": bool(token), "api": self.api, "cover_rate": self.cover_rate}

    @property
    def enabled(self) -> bool:
        return bool(self.token.strip())

    def client(self, *, log: Callable[[str], None] | None = None) -> TikuClient | None:
        """建客户端；没配 token 返回 ``None``（调用方按「不答题」处理）。"""
        if not self.enabled:
            return None
        try:
            return TikuClient(self.token, api=self.api, log=log)
        except TikuError:
            return None
