"""Provider 契约。

一个 Provider = 一个平台适配器。新平台的支持方式就是往 ``providers/`` 丢一个
文件，继承 :class:`Provider`、用 ``@register`` 注册，就完事了。

三条设计原则
------------
1. **只读**：Provider 只能通过 ``self.http`` 发 GET（见 ``http.py``），
   想提交作业在接口层面就做不到。
2. **Cookie 鉴权**：不接受账号密码。用户在自己浏览器里登录，把 Cookie 贴进
   配置即可。项目因此永远不碰你的密码，也不需要写登录加密。
3. **失败要响**：平台改版了就让 ``fetch`` 抛 :class:`ProviderError`，
   在 ``meta.status`` 里标出来、在报告里显示警告，而不是悄悄返回空列表
   让你以为「这学期没课」。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar, Iterable, Mapping

from ..http import DEFAULT_UA, HttpClient, HttpTrace
from ..log import Log
from ..model import Course, Account, iso, now

__all__ = [
    "ProviderMeta",
    "AccountConfig",
    "HttpSettings",
    "Provider",
    "ProviderError",
    "AuthError",
    "RedirectedError",
    "PROVIDER_STATUSES",
    "STATUS_LABEL",
    "LOGIN_PAGE_SIGNATURES",
    "login_page_evidence",
    "REGISTRY",
    "register",
]

#: 判定「这是登录页」的结构证据：同一组里的特征**同时**出现才算命中。
#:
#: 刻意不用 ``passport`` / ``用户登录`` / ``login`` 这类字符串。实测超星的登录页
#: 前 8000 字里 ``passport2`` 出现 **13 次**——它是个头部到处都在引用的资源域，
#: 登录之后的页面同样满屏都是。拿它当判据，等于把每一个响应都判成登录页。
#:
#: 具体到超星登录页（``GET https://i.chaoxing.com/base/settings`` 未登录时
#: 返回 200 + 33286 字节），可靠的结构特征只有三处：``<title>用户登录</title>``
#: （整页标题，登录之后的任何页面都不会是这样）、``id="pwd"`` 的密码框（偏移
#: 13517，落在旧实现 8000 字窗口之外）配 ``name="fid"`` 表单域、以及扫码登录的
#: ``createqr`` / ``getauthstatus`` 接口。``fanyalogin`` 在该页里根本不存在
#: （表单由脚本驱动），拿它当判据会永远不命中。
#:
#: 为什么不单用 ``type="password"`` + ``id="pwd"``：我们检查的正是「设置」页，
#: 用户信息/修改密码那一块完全可能放一个密码输入框。那样一来真实账号登录成功
#: 也会被判定成登录页——即把用户本来就在报的这个 bug 换个形式留着。
LOGIN_PAGE_SIGNATURES: tuple[tuple[str, ...], ...] = (
    ("<title>用户登录</title>",),           # 整页标题就是登录页
    ('type="password"', 'name="fid"'),      # 登录表单：密码框 + 学校 id 隐藏域
    ("createqr", "getauthstatus"),          # 扫码登录的取码 + 轮询接口
)


def login_page_evidence(html: str) -> str:
    """返回命中的那组结构特征（用 ``+`` 连起来）；不像登录页就返回空串。

    返回证据而不是布尔，是为了让报错能说清「凭什么认为这是登录页」——否则用户
    拿着「会话不管用」这句话没法判断是该重新登录还是程序判错了。
    """
    if not html:
        return ""
    low = html.lower()
    for group in LOGIN_PAGE_SIGNATURES:
        if all(marker in low for marker in group):
            return " + ".join(group)
    return ""


PROVIDER_STATUSES = ("stable", "beta", "unverified", "broken")

STATUS_LABEL = {
    "stable": "已验证",
    "beta": "测试中",
    "unverified": "未验证",
    "broken": "已失效",
}

#: platform id -> Provider 子类。放在 base 里，而不是 ``__init__``：
#: 适配器模块要 ``from .base import register``，如果注册表在包 ``__init__``
#: 里就会形成「包 → 适配器 → 包」的循环导入。
REGISTRY: dict[str, type["Provider"]] = {}


def register(cls: type["Provider"]) -> type["Provider"]:
    """类装饰器：把适配器登记进注册表。"""
    meta = getattr(cls, "meta", None)
    if meta is None or not getattr(meta, "id", ""):
        raise ValueError(f"{cls.__name__} 缺少 meta.id，无法注册")
    if meta.id.startswith("_"):
        raise ValueError(f"{cls.__name__} 的 meta.id 不能以 _ 开头（保留给抽象基类）")
    existing = REGISTRY.get(meta.id)
    if existing is not None and existing is not cls:
        raise ValueError(
            f"平台 id 冲突: {meta.id!r} 同时被 {existing.__name__} 和 {cls.__name__} 注册"
        )
    REGISTRY[meta.id] = cls
    return cls


# --------------------------------------------------------------------------- #
# 元数据与配置
# --------------------------------------------------------------------------- #

_FALSE_WORDS = frozenset({"0", "false", "no", "off", "n", "f", "否", "不是"})
_TRUE_WORDS = frozenset({"1", "true", "yes", "on", "y", "t", "是"})


def _as_bool(value: Any, default: bool = True) -> bool:
    """把配置里各种写法的布尔读成 ``bool``。

    ``bool("false")`` 是 ``True``——人手写成字符串 ``"false"`` 时，
    用户以为关掉了某个账号，程序其实照抓不误。所以这里专门认一遍。
    """
    if value is None:
        return default
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _FALSE_WORDS:
            return False
        if text in _TRUE_WORDS:
            return True
        return bool(text)
    return bool(value)


def _as_float(value: Any, default: float) -> float:
    """宽容的数字读取：畸形值退回默认，而不是让整个配置加载失败。"""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class ProviderMeta:
    """适配器的静态自我介绍。``status`` 与 ``verified`` 是给用户看的诚实标签。"""

    id: str
    name: str
    short: str
    status: str = "beta"
    color: str = "#64748b"
    auth: str = "cookie"
    #: 网页版 / 命令行是否真的实现了这个平台的登录流程。False 表示只能手工导入
    #: Cookie——不要拿别的平台的协议冒充它（MOOC 与超星就是完全两套）。
    login: bool = False
    homepage: str = ""
    #: 最近一次由真人用真实账号验证通过的日期（YYYY-MM-DD）。空 = 从没验证过。
    verified: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        """把 ``id`` 归一化并在构造时就拦下明显的错。

        frozen dataclass 里改字段只能走 ``object.__setattr__``。放在这里而不是
        ``register`` 里，是为了让**任何**手写的 meta 都受同一套规矩——注册表只是
        其中一个使用者。``id`` 里的空白尤其阴险：``"  "`` 是真值，能一路混进
        ``REGISTRY``，然后在配置文件里变成一个永远匹配不上的平台名。
        """
        id_ = str(self.id or "").strip()
        if not id_.strip("_"):
            raise ValueError("ProviderMeta.id 不能为空")
        # 允许前导下划线：抽象基类 Provider 自己就带着 id="_abstract" 这个标记。
        # 「不许注册 _ 开头的 id」是注册表的事，由 register() 把关，不在这里拦——
        # 否则基类的类属性定义一执行就炸，整个包 import 不进来。
        if id_ != id_[:1].lower() + id_[1:] or not id_.replace("-", "").replace("_", "").isalnum():
            raise ValueError(f"ProviderMeta.id 只允许小写字母/数字/连字符/下划线: {id_!r}")
        if self.status not in STATUS_LABEL:
            raise ValueError(
                f"ProviderMeta.status 取值非法: {self.status!r}；"
                f"可选 {'/'.join(sorted(STATUS_LABEL))}"
            )
        if not str(self.name or "").strip() or not str(self.short or "").strip():
            raise ValueError(f"{id_}: name/short 不能为空（界面上要显示它们）")
        object.__setattr__(self, "id", id_)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "short": self.short,
            "status": self.status,
            "status_label": STATUS_LABEL.get(self.status, self.status),
            "color": self.color,
            "auth": self.auth,
            "login": self.login,
            "homepage": self.homepage,
            "verified": self.verified,
            "note": self.note,
        }


@dataclass
class HttpSettings:
    """抓取行为。默认值刻意保守：慢一点，别给人家服务器添麻烦。"""

    timeout: float = 8.0
    delay: float = 0.4
    user_agent: str = DEFAULT_UA
    retries: int = 2

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "HttpSettings":
        data = dict(data or {})
        return cls(
            timeout=_as_float(data.get("timeout"), 8.0),
            delay=_as_float(data.get("delay"), 0.4),
            user_agent=str(data.get("user_agent") or DEFAULT_UA),
            retries=_as_int(data.get("retries"), 2),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "timeout": self.timeout,
            "delay": self.delay,
            "user_agent": self.user_agent,
            "retries": self.retries,
        }


@dataclass
class AccountConfig:
    """配置文件里的一条账号。"""

    platform: str
    label: str = ""
    enabled: bool = True
    cookie: str = ""
    options: dict[str, Any] = field(default_factory=dict)

    @property
    def display(self) -> str:
        return self.label or self.platform

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any] | str,
        *,
        default_platform: str = "",
    ) -> "AccountConfig":
        # 允许把一条账号写成裸字符串："accounts": ["chaoxing"]
        if isinstance(data, str):
            return cls(platform=data.strip() or default_platform)
        if not isinstance(data, Mapping):
            raise TypeError(
                f"账号配置应该是对象或字符串，拿到的是 {type(data).__name__}"
            )
        return cls(
            platform=str(data.get("platform") or default_platform),
            label=str(data.get("label") or ""),
            enabled=_as_bool(data.get("enabled", True)),
            cookie=str(data.get("cookie") or ""),
            options=dict(data.get("options") or {}),
        )

    def to_dict(self, *, redact: bool = True) -> dict[str, Any]:
        cookie = self.cookie or ""
        if redact and cookie:
            cookie = f"<已设置 {len(cookie)} 字符>"
        return {
            "platform": self.platform,
            "label": self.label,
            "enabled": self.enabled,
            "cookie": cookie,
            "options": self.options,
        }


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #

class ProviderError(Exception):
    """平台适配器出问题了（改版、字段变了、响应不是预期结构）。"""

    def __init__(self, message: str, *, hint: str = "") -> None:
        self.hint = hint
        super().__init__(message)


class AuthError(ProviderError):
    """Cookie 缺失、过期或被判定为未登录。"""


class RedirectedError(AuthError):
    """请求被重定向到登录页——最常见的一种「Cookie 过期」表现。"""


# --------------------------------------------------------------------------- #
# 基类
# --------------------------------------------------------------------------- #

class Provider(ABC):
    """所有平台适配器的基类。"""

    #: 子类必须覆盖
    meta: ClassVar[ProviderMeta] = ProviderMeta(
        id="_abstract", name="抽象适配器", short="抽象", status="unverified"
    )

    def __init__(
        self,
        account: AccountConfig,
        settings: HttpSettings | None = None,
        *,
        log: Log | None = None,
    ) -> None:
        self.account = account
        self.settings = settings or HttpSettings()
        self.log = log or Log(quiet=True)
        kwargs = dict(self.client_kwargs())
        self.http = HttpClient(
            cookie=self.account.cookie,
            timeout=self.settings.timeout,
            delay=self.settings.delay,
            user_agent=self.settings.user_agent,
            retries=self.settings.retries,
            logger=self.log.debug,
            **kwargs,
        )

    # -- 需要子类实现 ------------------------------------------------------ #

    @abstractmethod
    def fetch(self) -> list[Course]:
        """抓取该账号下的全部课程（含作业）。只读。"""

    def client_kwargs(self) -> dict[str, Any]:
        """额外的 :class:`HttpClient` 参数。

        RPC 风格平台（慕课、智慧树）在这里交出 ``query_endpoints``，
        声明自己允许 POST 到哪些**查询**端点。不覆盖 = 只能 GET。
        """
        return {}

    def probe_plan(self) -> list[tuple[str, str]]:
        """声明本适配器依赖的端点，``probe`` 命令会逐个请求并留档。

        返回 ``[(说明, url), ...]``，也可以是 ``(说明, url, "POST", {表单})``
        四元组——POST 端点必须同时在 ``client_kwargs()`` 的白名单里。
        社区贡献者改版后就是靠这个命令把原始响应贴到 issue 里，维护者不用
        自己的账号也能修。
        """
        return []

    # -- 通用工具 ---------------------------------------------------------- #

    def require_cookie(self) -> str:
        cookie = (self.account.cookie or "").strip()
        if not cookie:
            raise AuthError(
                f"{self.meta.name} 未配置 Cookie",
                hint=(
                    "在浏览器里登录该平台，打开开发者工具 → Network → 任选一个请求，"
                    "复制 Request Headers 里的 Cookie 整行，填到配置文件的 cookie 字段。"
                ),
            )
        return cookie

    @staticmethod
    def looks_logged_out(html: str) -> bool:
        """页面里长出登录表单，就说明被踢回登录页了。"""
        return bool(login_page_evidence(html))

    def account_result(
        self,
        courses: Iterable[Course],
        *,
        ok: bool = True,
        message: str = "",
    ) -> Account:
        items = list(courses)
        return Account(
            platform=self.meta.id,
            label=self.account.display,
            ok=ok,
            message=message,
            fetched_at=now(),
            course_count=len(items),
        )

    # -- probe ------------------------------------------------------------- #

    def probe(self) -> list[HttpTrace]:
        """按 ``probe_plan`` 逐个请求，返回留痕（不抛异常）。"""
        for entry in self.probe_plan():
            note = str(entry[0])
            url = str(entry[1]) if len(entry) > 1 else ""
            method = str(entry[2]).upper() if len(entry) > 2 else "GET"
            data = entry[3] if len(entry) > 3 else None
            if not url:
                continue
            self.log.step(f"{note} [{method}]: {url}")
            try:
                if method == "POST":
                    self.http.post_query(url, data, note=note)
                else:
                    self.http.get(url, note=note)
            except Exception as exc:                       # noqa: BLE001 - probe 就是要吃到所有错
                self.log.warn(f"  {note} 失败: {exc}")
        return self.http.trace

    # -- 展示 -------------------------------------------------------------- #

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} {self.meta.id} account={self.account.display!r}>"
