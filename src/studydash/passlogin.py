"""账号密码登录超星——你在这里输，程序发出去，会话留在本地。

**密码怎么处理的（这是本模块最要紧的一段说明）：**

* 密码只在内存里待着。不写配置文件、不进日志、不碰命令行参数（所以不会出现在
  进程列表和 shell 历史里，用 ``input`` 读）。
* 按超星登录页自己的做法封一层再发：``AES-128-CBC``，key 与 IV 都是页面里写死的
  ``u2oh6Vu^HWe4_AES``，PKCS7 补齐，取密文的 Base64。这**不是**什么加密保障——
  密钥就写在网页源码里，谁都能看见；它只是超星要求的封装格式，避免密码明文过线。
  真正保护你密码的是 HTTPS。
* 落盘的是登录成功之后服务端下发的会话 Cookie，不是密码。

**做不到的事，说在前面：**

* 服务端这次要求图形/滑块验证码时（页面上的 ``needVcode=1``），程序停下并说明，
  不会去猜验证码。这条路的正解是改扫码登录。
* 账号开了二次验证时同理——需要你在浏览器里过掉那一关。

这两条不是缺陷，是边界。写在这里，免得有人以为是 bug 而去改。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any

from . import aes
from .http import HttpClient
from .login import (
    LoginError,
    Session,
    check as check_session,
)
from .qrlogin import (
    BASE,
    LOGIN_URL,
    REFER,
    AJAX_HEADERS,
    session_cookie,
    hidden_value,
)

__all__ = [
    "LOGIN_PATH",
    "TRANSFER_KEY",
    "PasswordLoginError",
    "CaptchaRequired",
    "TwoFactorRequired",
    "fetch_form",
    "encrypt_password",
    "submit",
    "login",
]

LOGIN_PATH = "/fanyalogin"
LOGIN_API = BASE + LOGIN_PATH

#: 登录页里写死的 AES 传输密钥（同一个字符串既当 key 又当 IV）
TRANSFER_KEY = "u2oh6Vu^HWe4_AES"

#: 页面自己会带上的隐藏字段；缺了也发空串，别因为改版就整个崩掉
_OPTIONAL_FIELDS = (
    "validate", "doubleFactorLogin", "independentId", "independentNameId",
)


class PasswordLoginError(LoginError):
    """密码这条路走不通。"""


class CaptchaRequired(PasswordLoginError):
    """服务端要求图形验证码——这条路的正解是改用扫码。"""


class TwoFactorRequired(PasswordLoginError):
    """账号开了二次验证，需要人在浏览器里过。"""


@dataclass
class LoginForm:
    """登录页上那些「跟账号无关但必须原样回传」的字段。"""

    fid: str = ""
    refer: str = REFER
    t: str = ""
    forbid_other: str = ""
    need_vcode: str = ""
    optional: dict[str, str] = dataclasses.field(default_factory=dict)

    @property
    def wants_captcha(self) -> bool:
        return self.need_vcode.strip() == "1"

    @property
    def encrypts(self) -> bool:
        """页面逻辑：``t == "true"`` 时才用 AES 封密码。"""
        return self.t.strip().lower() == "true"


def fetch_form(client: HttpClient, *, log=None) -> LoginForm:
    """打开登录页，把提交时要用到的隐藏字段抄下来。"""
    html = client.get(
        LOGIN_URL, {"fid": "", "refer": REFER},
        headers={"Upgrade-Insecure-Requests": "1"},
        note="密码登录页",
    ).text

    fid = hidden_value(html, "fid")
    if not fid:
        raise PasswordLoginError(
            "登录页里没找到 fid 字段，超星大概改版了。\n"
            "  → 改用 `studydash login` 扫码登录（更稳，也不用输密码）"
        )

    form = LoginForm(
        fid=fid,
        refer=hidden_value(html, "refer") or REFER,
        t=hidden_value(html, "t"),
        forbid_other=hidden_value(html, "forbidotherlogin"),
        need_vcode=hidden_value(html, "needVcode"),
        optional={name: hidden_value(html, name) for name in _OPTIONAL_FIELDS},
    )
    if log:
        log.debug(f"  登录页字段：fid={form.fid} t={form.t or '(空)'} "
                  f"needVcode={form.need_vcode or '(空)'}")
    return form


def encrypt_password(password: str, form: LoginForm | None = None) -> str:
    """按超星的要求把密码封成 Base64 密文（``form`` 说不用封就原样返回）。"""
    if form is not None and not form.encrypts:
        return password
    return aes.encrypt_cbc_b64(TRANSFER_KEY, password)


def submit(
    client: HttpClient,
    form: LoginForm,
    username: str,
    password: str,
    *,
    log=None,
) -> dict[str, Any]:
    """POST 出去。返回服务端 JSON；密码错误之类以 :class:`PasswordLoginError` 抛出。"""
    payload: dict[str, str] = {
        "fid": form.fid,
        "uname": username,
        "password": encrypt_password(password, form),
        "refer": form.refer,
        "t": form.t,
        "forbidotherlogin": form.forbid_other,
    }
    payload.update(form.optional)

    try:
        data = client.post_query(
            LOGIN_API, payload, headers=AJAX_HEADERS, note="密码登录",
        ).json(None)
    except ValueError as exc:                            # 不是 JSON
        raise PasswordLoginError(f"登录接口没返回 JSON：{exc}") from exc

    if not isinstance(data, dict):
        raise PasswordLoginError(
            "登录接口返回的不是 JSON 对象，超星大概改版了。\n"
            "  → 改用 `studydash login` 扫码登录"
        )

    if data.get("containTwoFactorLogin"):
        url = data.get("twoFactorLoginPCUrl") or ""
        raise TwoFactorRequired(
            "这个账号开了二次验证，密码这一关过了也还差一道。\n"
            "  → 用 `studydash login --source live` 在浏览器里过掉，"
            "再用 `--source browser` 读回来\n"
            + (f"  → 二次验证页面：{url}" if url else "")
        )

    if not data.get("status"):
        message = str(data.get("msg") or data.get("message") or "").strip()
        raise PasswordLoginError(
            "登录被拒绝了" + (f"：{message}" if message else "（服务端没说原因）")
            + "\n  → 账号或密码不对；也可能是这次要求验证码，请改用 `studydash login` 扫码"
        )
    return data


def login(
    username: str,
    password: str,
    *,
    timeout: float = 15.0,
    log=None,
) -> Session:
    """走完密码登录，返回**已验证**的会话。

    这里不弹 ``input`` —— 由调用方（命令行）负责问，本函数只处理登录。这样它
    可以被测试直接调，也可以在别的界面里复用。
    """
    if not username or not password:
        raise PasswordLoginError("账号和密码都不能为空")

    client = HttpClient(
        timeout=timeout, delay=0.25, referer=LOGIN_URL,
        headers={"Origin": BASE}, query_endpoints=(LOGIN_API,),
        logger=(log.debug if log else (lambda _m: None)),
    )

    form = fetch_form(client, log=log)
    if form.wants_captcha:
        raise CaptchaRequired(
            "超星这次要求图形验证码，程序不猜验证码——这条路的正解是扫码。\n"
            "  → 跑 `studydash login`（默认就是扫码），十秒钟的事"
        )

    submit(client, form, username, password, log=log)

    cookie = session_cookie(client)
    if not cookie:
        raise PasswordLoginError(
            "登录接口说成功，但没下发会话 Cookie。\n"
            "  → 改用 `studydash login` 扫码登录"
        )

    session = check_session(cookie, log=log)
    return dataclasses.replace(session, source="password")
