"""统一的 API 错误。

一个异常类型（``ApiError``）＋一个错误信封（``{"error": {...}}``），
外加一张把已有异常翻译成错误码的表。

之所以要这张翻译表：``studydash`` 的内部异常（``LoginError``、``ProviderError``、
``BrowserError``…）是为了**命令行**可读而设计的，前端拿到的必须是稳定的机器可读
错误码。把翻译集中在一处，前端就只需要认识 ``docs/web-api.md`` 里那张表。
"""

from __future__ import annotations

from typing import Any, Mapping

__all__ = ["ERROR_STATUS", "ApiError", "error_body", "to_api_error"]

#: 错误码 → HTTP 状态码。与 docs/web-api.md 第 2 节一一对应。
ERROR_STATUS: dict[str, int] = {
    "unauthorized": 401,
    "forbidden": 403,
    "invalid_request": 400,
    "not_found": 404,
    "auth_error": 401,
    "captcha_required": 409,
    "two_factor_required": 409,
    "provider_error": 502,
    "busy": 409,
    "conflict": 409,
    "internal": 500,
}


class ApiError(Exception):
    """一个带错误码、可直接变成响应的异常。"""

    def __init__(
        self,
        code: str = "internal",
        message: str = "",
        *,
        hint: str = "",
        detail: Mapping[str, Any] | None = None,
        status: int | None = None,
    ) -> None:
        self.code = code if code in ERROR_STATUS else "internal"
        self.message = message or self.code
        super().__init__(self.message)
        self.hint = hint
        self.detail: dict[str, Any] = dict(detail or {})
        self.status = status or ERROR_STATUS[self.code]

    def to_dict(self) -> dict[str, Any]:
        return {"error": self._body()}

    def _body(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.hint:
            body["hint"] = self.hint
        if self.detail:
            body["detail"] = self.detail
        return body

    def __repr__(self) -> str:                          # pragma: no cover - 调试用
        return f"ApiError({self.code!r}, {self.message!r})"


def error_body(code: str, message: str, *, hint: str = "", detail: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return ApiError(code, message, hint=hint, detail=detail).to_dict()


def to_api_error(exc: BaseException) -> ApiError:
    """把内部异常翻译成 ``ApiError``。

    顺序有讲究：先判子类再判基类。``CaptchaRequired`` 是 ``PasswordLoginError``
    的子类，反过来的话「请改用扫码」就会被压成一句没用的「登录失败」。
    导入都放在函数里，避免 ``studydash.web`` 与这些模块形成循环。
    """
    if isinstance(exc, ApiError):
        return exc

    from ..browsers import BrowserError
    from ..http import HttpError, ReadOnlyError
    from ..login import LoginError
    from ..passlogin import CaptchaRequired, PasswordLoginError, TwoFactorRequired
    from ..providers.base import AuthError, ProviderError
    from ..qrlogin import QrLoginError

    if isinstance(exc, CaptchaRequired):
        return ApiError(
            "captcha_required",
            "超星这次要求输入图形验证码",
            hint="验证码不猜。用扫码登录就行：它不用验证码，也不用把密码交给这个程序。",
        )
    if isinstance(exc, TwoFactorRequired):
        return ApiError(
            "two_factor_required",
            "这个账号开了二次验证，密码登录走不通",
            hint="用扫码登录：在手机 App 里确认一下即可。",
        )
    if isinstance(exc, QrLoginError):
        return ApiError("provider_error", str(exc), hint=_hint_of(exc))
    if isinstance(exc, HttpError):
        return ApiError("provider_error", f"请求 {exc.url} 失败：{exc.reason}")
    # AuthError 是 ProviderError 的子类，必须先判它 —— 反过来的话「Cookie 过期了，
    # 重新登录一下」会被压成 502「平台故障」，前端就不会引导用户去重新登录了。
    if isinstance(exc, AuthError):
        return ApiError("auth_error", str(exc))
    if isinstance(exc, ProviderError):
        return ApiError("provider_error", str(exc), hint=getattr(exc, "hint", "") or "")
    if isinstance(exc, PasswordLoginError):
        return ApiError("auth_error", str(exc), hint=_hint_of(exc))
    if isinstance(exc, LoginError):
        return ApiError("auth_error", str(exc), hint=_hint_of(exc))
    if isinstance(exc, BrowserError):
        # 浏览器开着 → 它独占 Cookie 库的句柄，这是本机状态冲突，不是平台故障
        return ApiError("conflict", str(exc), hint=_hint_of(exc))
    if isinstance(exc, ReadOnlyError):
        return ApiError(
            "internal",
            f"只读约束被触发了：{exc}",
            hint="这是程序缺陷，请提 issue。",
        )
    if isinstance(exc, (FileNotFoundError, PermissionError, OSError)):
        return ApiError("internal", f"{type(exc).__name__}: {exc}")
    return ApiError("internal", f"{type(exc).__name__}: {exc}")


def _hint_of(exc: BaseException) -> str:
    """内部异常的提示文本常写在消息里（换行 + 缩进的 `→` 行）。

    取出来单独放进 ``hint``，前端才能把「该怎么办」和「出了什么事」分开排版。
    """
    lines = [ln.strip() for ln in str(exc).splitlines()[1:] if ln.strip()]
    hints = [ln.lstrip("→ ").strip() for ln in lines if ln.startswith("→")]
    return " ".join(hints)
