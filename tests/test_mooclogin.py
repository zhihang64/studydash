"""中国大学 MOOC 扫码登录的协议解析与接线测试（全部不联网）。"""

from __future__ import annotations

import json

import pytest

from studydash import mooclogin
from studydash.login import Session


class FakeResponse:
    def __init__(self, payload=None, content=b"", status: int = 200):
        self._payload = payload
        self.content = content
        self.status = status

    def json(self, default=None):
        return self._payload if self._payload is not None else default


class FakeClient:
    """按 URL 回放的假客户端，记录每次请求。"""

    def __init__(self, handler):
        self.handler = handler
        self.calls: list[tuple[str, dict | None]] = []
        self.jar: list[tuple[str, str]] = []

    def get(self, url, params=None, **_kw):
        self.calls.append((url, params))
        return self.handler(url, params)

    def cookie_header(self, _url=""):
        return "; ".join(f"{k}={v}" for k, v in self.jar)

    def jar_cookies(self):
        return list(self.jar)


# --------------------------------------------------------------------------- #
# 取码 / 轮询
# --------------------------------------------------------------------------- #

def test_request_code_reads_poll_key_and_upgrades_cdn_to_https():
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 100
    client = FakeClient(
        lambda url, _p: FakeResponse(
            {"code": 0, "result": {"pollKey": "K1", "codeUrl": "http://cdn/x-qrcode-logon.png"}}
        )
        if "code.do" in url
        else FakeResponse(content=png)
    )

    ticket = mooclogin.request_code(client)

    assert ticket.poll_key == "K1"
    assert ticket.code_url == "https://cdn/x-qrcode-logon.png"
    assert ticket.png == png
    # code.do 要带尺寸参数，图片从服务端给的 https 地址取
    assert client.calls[0][1] == {"width": 182, "height": 182}
    assert client.calls[1][0] == "https://cdn/x-qrcode-logon.png"


def test_request_code_surfaces_server_error_instead_of_a_dead_qr():
    client = FakeClient(
        lambda _u, _p: FakeResponse({"code": -1003, "message": "服务端不高兴"})
    )
    with pytest.raises(mooclogin.MoocLoginError) as caught:
        mooclogin.request_code(client)
    assert "服务端不高兴" in str(caught.value)


def test_request_code_rejects_a_non_png_response():
    client = FakeClient(
        lambda url, _p: FakeResponse(
            {"code": 0, "result": {"pollKey": "K", "codeUrl": "https://cdn/x.png"}}
        )
        if "code.do" in url
        else FakeResponse(content=b"<html>blocked</html>")
    )
    with pytest.raises(mooclogin.MoocLoginError):
        mooclogin.request_code(client)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0, mooclogin.NOT_SCAN),
        (1, mooclogin.SCAN_NOT_CONFIRM),
        (2, mooclogin.CONFIRMED),
        (3, mooclogin.EXPIRED),
        (4, mooclogin.SCAN_BY_OTHER_APP),
        (None, mooclogin.NOT_SCAN),
    ],
)
def test_poll_once_maps_code_status(raw, expected):
    client = FakeClient(
        lambda _u, _p: FakeResponse({"code": 0, "result": {"codeStatus": raw, "token": None}})
    )
    payload = mooclogin.poll_once(client, mooclogin.QrTicket("K", "https://cdn/x.png", b""))
    assert payload["codeStatus"] == expected
    assert payload["token"] == ""


def test_wait_for_scan_returns_as_soon_as_the_phone_confirms(monkeypatch):
    statuses = iter([mooclogin.NOT_SCAN, mooclogin.SCAN_NOT_CONFIRM, mooclogin.CONFIRMED])
    client = FakeClient(
        lambda _u, _p: FakeResponse(
            {"code": 0, "result": {"codeStatus": next(statuses), "token": "T"}}
        )
    )
    monkeypatch.setattr(
        mooclogin, "_finish", lambda _c, token, **_kw: Session(cookie=token, source="qr", ok=True)
    )
    seen: list[int] = []
    session = mooclogin.wait_for_scan(
        client,
        mooclogin.QrTicket("K", "https://cdn/x.png", b""),
        timeout=10.0,
        interval=0.0,
        on_tick=lambda payload: seen.append(payload["status"]),
    )
    assert session.cookie == "T"
    # 状态变化才回调，避免每 2 秒刷屏同一句「等待扫码」
    assert seen == [mooclogin.NOT_SCAN, mooclogin.SCAN_NOT_CONFIRM]


def test_wait_for_scan_raises_a_timeout_specific_error():
    client = FakeClient(
        lambda _u, _p: FakeResponse({"code": 0, "result": {"codeStatus": 0}})
    )
    with pytest.raises(mooclogin.MoocTimeoutError):
        mooclogin.wait_for_scan(
            client,
            mooclogin.QrTicket("K", "https://cdn/x.png", b""),
            timeout=0.0,
            interval=0.0,
        )


def test_wait_for_scan_replaces_an_expired_code(monkeypatch):
    """codeStatus=3 说明这张码作废了，继续轮询它只会白等。"""
    fresh = mooclogin.QrTicket("K2", "https://cdn/x2.png", b"\x89PNG")
    monkeypatch.setattr(mooclogin, "request_code", lambda _c, **_k: fresh)
    statuses = iter([mooclogin.EXPIRED, mooclogin.CONFIRMED])
    client = FakeClient(
        lambda _u, _p: FakeResponse(
            {"code": 0, "result": {"codeStatus": next(statuses), "token": "T2"}}
        )
    )
    monkeypatch.setattr(
        mooclogin, "_finish", lambda _c, token, **_kw: Session(cookie=token, source="qr", ok=True)
    )
    redrawn: list[str] = []
    session = mooclogin.wait_for_scan(
        client,
        mooclogin.QrTicket("K1", "https://cdn/x1.png", b"\x89PNG"),
        timeout=10.0,
        interval=0.0,
        on_code=lambda ticket: redrawn.append(ticket.poll_key),
    )
    assert session.cookie == "T2"
    assert redrawn == ["K2"]
    # 换码后轮询用的是新 pollKey
    assert client.calls[-1][1] == {"pollKey": "K2"}


# --------------------------------------------------------------------------- #
# 换 Cookie
# --------------------------------------------------------------------------- #

def test_finish_exchanges_token_and_returns_a_verified_session(monkeypatch):
    client = FakeClient(lambda _u, _p: FakeResponse(payload={}))
    client.jar = [("NTESSTUDYSI", "abc")]
    monkeypatch.setattr(
        mooclogin,
        "check",
        lambda cookie, **_kw: Session(cookie=cookie, source="qr", uid="9", ok=True, message="会话可用"),
    )

    session = mooclogin._finish(client, "TOKEN")

    assert session.ok and session.uid == "9"
    assert session.source == "qr"
    assert session.cookie == "NTESSTUDYSI=abc"
    url, params = client.calls[0]
    assert url == mooclogin.EXCHANGE_URL
    assert params["token"] == "TOKEN"
    assert params["returnUrl"]  # base64 过的回跳地址


def test_finish_refuses_to_pretend_success_without_a_token():
    with pytest.raises(mooclogin.MoocLoginError):
        mooclogin._finish(FakeClient(lambda _u, _p: FakeResponse()), "")


# --------------------------------------------------------------------------- #
# 登录态自检
# --------------------------------------------------------------------------- #

class FakeHttp:
    """顶替 studydash.http.HttpClient。"""

    last: "FakeHttp | None" = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.cookie = kwargs.get("cookie", "")
        self.jar = [("NTESSTUDYSI", "from-jar")]
        self.calls: list[tuple[str, dict]] = []
        FakeHttp.last = self

    def get(self, url, params=None, **_kw):
        self.calls.append((url, dict(params or {})))
        return FakeResponse(content=b"<html></html>")

    def jar_cookies(self):
        return list(self.jar)

    def post_query_json(self, url, data=None, **_kw):
        self.calls.append((url, dict(data or {})))
        return {
            "code": 0,
            "result": {"id": 7788, "nickName": "小明", "userId": 7788},
        }


def test_check_uses_the_user_info_rpc_and_reads_uid(monkeypatch):
    monkeypatch.setattr(mooclogin, "HttpClient", FakeHttp)
    session = mooclogin.check("NTESSTUDYSI=csrf; EDUWEBDEVICE=d")
    assert session.ok
    assert session.uid == "7788"
    assert "小明" in session.message
    url, data = FakeHttp.last.calls[-1]
    assert url.startswith(mooclogin.USERINFO_URL)
    assert "csrfKey=csrf" in url
    assert data == {"csrfKey": "csrf"}


def test_check_seeds_csrf_from_the_server_when_the_cookie_lacks_it(monkeypatch):
    monkeypatch.setattr(mooclogin, "HttpClient", FakeHttp)
    session = mooclogin.check("EDUWEBDEVICE=d")
    assert session.ok
    # 先访问一次登录页拿到 NTESSTUDYSI，再用它问用户信息
    assert FakeHttp.last.calls[0][0] == mooclogin.LOGIN_HOME
    assert "csrfKey=from-jar" in FakeHttp.last.calls[-1][0]


def test_check_treats_an_empty_user_object_as_not_logged_in(monkeypatch):
    class NullUser(FakeHttp):
        def post_query_json(self, url, data=None, **_kw):
            self.calls.append((url, dict(data or {})))
            return {"code": 0, "result": None}

    monkeypatch.setattr(mooclogin, "HttpClient", NullUser)
    session = mooclogin.check("NTESSTUDYSI=csrf")
    assert not session.ok
    assert "还没登录" in session.message


def test_check_reports_a_server_error_instead_of_saying_expired(monkeypatch):
    class Boom(FakeHttp):
        def post_query_json(self, url, data=None, **_kw):
            return {"code": -1003, "message": "Url里面的类名出错"}

    monkeypatch.setattr(mooclogin, "HttpClient", Boom)
    session = mooclogin.check("NTESSTUDYSI=csrf")
    assert not session.ok
    assert "Url里面的类名出错" in session.message


# --------------------------------------------------------------------------- #
# CLI 接线
# --------------------------------------------------------------------------- #

def test_cli_login_qr_for_icourse163_uses_the_mooc_protocol(tmp_path, monkeypatch):
    from studydash import cli

    seen = {}

    def fake_login(**kwargs):
        seen.update(kwargs)
        return Session(cookie="NTESSTUDYSI=abc", source="qr", ok=True)

    monkeypatch.setattr(mooclogin, "login", fake_login)
    home = tmp_path / "ws"
    rc = cli.main(
        [
            "--home", str(home),
            "login", "--platform", "icourse163",
            "--source", "qr", "--no-fetch",
        ]
    )
    assert rc == 0
    # 关键：走的是 MOOC 的二维码，而不是超星的
    assert callable(seen["on_code"]) and callable(seen["on_tick"])
    data = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert data["accounts"][0]["platform"] == "icourse163"
    assert data["accounts"][0]["cookie"] == "NTESSTUDYSI=abc"


def test_cli_login_password_for_icourse163_fails_with_a_useful_message(tmp_path, monkeypatch, capsys):
    from studydash import cli

    monkeypatch.setattr("builtins.input", lambda *_a: "13800000000")
    rc = cli.main(
        [
            "--home", str(tmp_path / "ws"),
            "login", "--platform", "icourse163",
            "--source", "password", "--no-fetch",
        ]
    )
    assert rc == 1
    out = capsys.readouterr().out
    assert "还不支持账号密码登录" in out
    assert "扫码" in out


def test_mooc_timeout_maps_to_login_timeout_not_a_platform_failure():
    """没扫码是用户没动作，不是平台挂了；错误码必须和超星那条路一致。"""
    from studydash.web.errors import to_api_error

    err = to_api_error(mooclogin.MoocTimeoutError("等了 240 秒也没扫"))
    assert err.code == "login_timeout"


def test_web_login_qr_picks_the_mooc_protocol_for_icourse163(tmp_path, monkeypatch):
    """回归：选了 MOOC 却弹超星的码，MOOC App 扫不了。"""
    from studydash.web import operations, state as state_mod, tasks as tasks_mod

    called = {}

    def fake_mooc(**kwargs):
        called["yes"] = True
        kwargs["on_code"](mooclogin.QrTicket("K", "https://cdn/x.png", b"\x89PNG"))
        return Session(cookie="NTESSTUDYSI=x", source="qr", ok=True)

    monkeypatch.setattr(operations.mooclogin, "login", fake_mooc)

    def wrong(**_kw):
        raise AssertionError("icourse163 不该调用超星的扫码协议")

    monkeypatch.setattr(operations, "qr_login", wrong)

    app_state = state_mod.AppState.create(home=tmp_path / "ws")
    mgr = tasks_mod.TaskManager()
    task = mgr.create("login_qr", {}, None)
    rep = tasks_mod.Reporter(mgr, task)

    result = operations.op_login_qr(app_state, rep, {"platform": "icourse163"})

    assert called.get("yes")
    assert result["account"]["platform"] == "icourse163"


def test_web_password_login_refuses_mooc_instead_of_calling_chaoxing(tmp_path):
    from studydash.web import operations, state as state_mod, tasks as tasks_mod
    from studydash.web.errors import ApiError

    app_state = state_mod.AppState.create(home=tmp_path / "ws")
    mgr = tasks_mod.TaskManager()
    task = mgr.create("login_password", {}, None)
    rep = tasks_mod.Reporter(mgr, task)

    with pytest.raises(ApiError) as caught:
        operations.op_login_password(
            app_state, rep, {"platform": "icourse163", "username": "u", "password": "p"}
        )
    assert "账号密码" in caught.value.message
    assert "扫码" in caught.value.hint


def test_each_platform_declares_whether_login_is_really_implemented():
    """没有实现的平台必须显式写 False，界面才不会把它们放进登录下拉框。"""
    from studydash.providers import metas

    flags = {meta.id: meta.login for meta in metas()}
    assert flags["chaoxing"] is True
    assert flags["icourse163"] is True
    assert flags["zhihuishu"] is False
    assert flags["demo"] is False


def test_web_login_qr_refuses_a_platform_without_login_support(tmp_path):
    """回归：选智慧树也不该偷偷跑超星的扫码。"""
    from studydash.web import operations, state as state_mod, tasks as tasks_mod
    from studydash.web.errors import ApiError

    app_state = state_mod.AppState.create(home=tmp_path / "ws")
    mgr = tasks_mod.TaskManager()
    task = mgr.create("login_qr", {}, None)
    rep = tasks_mod.Reporter(mgr, task)

    with pytest.raises(ApiError) as caught:
        operations.op_login_qr(app_state, rep, {"platform": "zhihuishu"})
    assert caught.value.code == "invalid_request"
    assert "登录" in caught.value.message

