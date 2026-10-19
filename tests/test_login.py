"""``studydash.login`` 的测试。

这里**不联网**：要么只测纯解析函数，要么把 ``HttpClient`` 换掉，
把「登录页 / 已登录」两种响应喂进去看判定逻辑。
"""

from __future__ import annotations

import json

import pytest

from studydash import login
from studydash.login import LoginError, Session

# --------------------------------------------------------------------------- #
# normalize_cookie
# --------------------------------------------------------------------------- #

def test_normalize_plain_header():
    assert login.normalize_cookie("  _uid=1; _d=2  ") == "_uid=1; _d=2"


def test_normalize_strips_cookie_prefix():
    assert login.normalize_cookie("Cookie: _uid=1; _d=2") == "_uid=1; _d=2"
    assert login.normalize_cookie("cookie:_uid=1") == "_uid=1"


def test_normalize_keeps_equals_in_value():
    """Cookie 值里带 '=' 很常见（base64 尾巴），不能从第一个 '=' 之后全丢。"""
    header = login.normalize_cookie("token=YWJjZA==; x=1")
    assert header == "token=YWJjZA==; x=1"


def test_normalize_dedupes_last_wins():
    assert login.normalize_cookie("a=1; a=2; b=3") == "a=2; b=3"


def test_normalize_json_list_of_objects():
    raw = json.dumps([{"name": "a", "value": "1"}, {"name": "b", "value": "2"}])
    assert login.normalize_cookie(raw) == "a=1; b=2"


def test_normalize_json_accepts_key_alias():
    raw = json.dumps([{"key": "a", "value": "1"}])
    assert login.normalize_cookie(raw) == "a=1"


def test_normalize_json_wrapped_in_cookies():
    raw = json.dumps({"cookies": [{"name": "a", "value": "1"}]})
    assert login.normalize_cookie(raw) == "a=1"


def test_normalize_json_plain_mapping():
    assert login.normalize_cookie('{"a": "1", "b": 2}') == "a=1; b=2"


def test_normalize_netscape_cookies_txt():
    raw = "\n".join(
        [
            "# Netscape HTTP Cookie File",
            "#HttpOnly_.chaoxing.com\tTRUE\t/\tTRUE\t0\t_uid\t123",
            ".chaoxing.com\tTRUE\t/\tFALSE\t0\t_d\t456",
        ]
    )
    assert login.normalize_cookie(raw) == "_uid=123; _d=456"


def test_normalize_rejects_broken_json():
    with pytest.raises(LoginError, match="JSON"):
        login.normalize_cookie('[{"name": "a"')


def test_normalize_rejects_json_without_pairs():
    with pytest.raises(LoginError, match="没找到"):
        login.normalize_cookie("[1, 2, 3]")


def test_normalize_empty_is_empty():
    assert login.normalize_cookie("") == ""
    assert login.normalize_cookie("   ") == ""


# --------------------------------------------------------------------------- #
# from_file
# --------------------------------------------------------------------------- #

def test_from_file_reads(tmp_path):
    p = tmp_path / "cookie.txt"
    p.write_text("Cookie: _uid=7; _d=8", encoding="utf-8")
    assert login.from_file(p) == "_uid=7; _d=8"


def test_from_file_missing(tmp_path):
    with pytest.raises(LoginError, match="找不到文件"):
        login.from_file(tmp_path / "nope.txt")


def test_from_file_empty_content(tmp_path):
    p = tmp_path / "empty.txt"
    p.write_text("\n\n", encoding="utf-8")
    with pytest.raises(LoginError, match="没解析出"):
        login.from_file(p)


# --------------------------------------------------------------------------- #
# check：把 HttpClient 换掉，喂两种响应
# --------------------------------------------------------------------------- #

class _Resp:
    def __init__(self, text="", status=200, headers=None):
        self.text = text
        self.status = status
        self.headers = headers or {}


class _FakeClient:
    """替身：只记下被问的地址，回一份写死的响应。"""

    def __init__(self, response):
        self.response = response
        self.asked: list[str] = []

    def get(self, url, note=""):
        self.asked.append(url)
        return self.response


@pytest.fixture
def fake_http(monkeypatch):
    def _install(response):
        client = _FakeClient(response)
        monkeypatch.setattr(login, "HttpClient", lambda **kwargs: client)
        return client

    return _install


def test_check_empty_cookie_needs_no_network(fake_http):
    fake_http(_Resp())
    result = login.check("")
    assert result.ok is False
    assert "空" in result.message


def test_check_detects_login_page(fake_http):
    fake_http(_Resp(text="<html><title>用户登录</title></html>"))
    result = login.check("_uid=1")
    assert result.ok is False
    assert "登录页" in result.message


def test_check_detects_redirect_to_passport(fake_http):
    fake_http(_Resp(status=302, headers={"location": "https://passport2.chaoxing.com/login"}))
    result = login.check("_uid=1")
    assert result.ok is False


def test_check_accepts_uid_from_cookie(fake_http):
    fake_http(_Resp(text="<html>设置</html>"))
    result = login.check("_uid=123456; _d=abc")
    assert result.ok is True
    assert result.uid == "123456"
    assert result.cookie_count == 2


def test_check_accepts_uid_from_html(fake_http):
    """有些响应不带 Cookie、只在正文回显 _uid，这种情况也算登录上了。"""
    fake_http(_Resp(text="<script>var x='_uid=7654321;';</script>"))
    result = login.check("_d=abc")
    assert result.ok is True
    assert result.uid == "7654321"


def test_check_without_uid_is_not_ok(fake_http):
    fake_http(_Resp(text="<html>看不懂的页面</html>"))
    result = login.check("_d=abc")
    assert result.ok is False
    assert "_uid" in result.message


def test_check_queries_the_settings_url(fake_http):
    client = fake_http(_Resp(text="_uid=1"))
    login.check("_uid=1")
    assert client.asked == [login.LOGIN_CHECK]


# --------------------------------------------------------------------------- #
# save
# --------------------------------------------------------------------------- #

def test_save_creates_config(workspace):
    session = Session(cookie="_uid=1; _d=2", source="live", uid="1")
    path = login.save(workspace, session)
    assert path == workspace.config_path
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["accounts"][0]["platform"] == "chaoxing"
    assert data["accounts"][0]["cookie"] == "_uid=1; _d=2"
    assert data["accounts"][0]["enabled"] is True


def test_save_upserts_same_label(workspace):
    login.save(workspace, Session(cookie="old=1", source="live", uid="1"))
    login.save(workspace, Session(cookie="new=2", source="live", uid="1"))
    data = json.loads(workspace.config_path.read_text(encoding="utf-8"))
    assert len(data["accounts"]) == 1
    assert data["accounts"][0]["cookie"] == "new=2"


def test_save_appends_different_label(workspace):
    login.save(workspace, Session(cookie="a=1", source="live", uid="1"))
    login.save(workspace, Session(cookie="b=2", source="live", uid="2"), label="小号")
    data = json.loads(workspace.config_path.read_text(encoding="utf-8"))
    assert [a["label"] for a in data["accounts"]] == ["默认（1）", "小号"]


def test_save_keeps_unrelated_accounts(workspace):
    workspace.config_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "accounts": [
                    {"platform": "manual", "label": "线下课", "cookie": ""},
                ],
            }
        ),
        encoding="utf-8",
    )
    login.save(workspace, Session(cookie="a=1", source="live", uid="9"))
    data = json.loads(workspace.config_path.read_text(encoding="utf-8"))
    assert {a["platform"] for a in data["accounts"]} == {"manual", "chaoxing"}


def test_save_survives_broken_config(workspace):
    """配置文件坏了不该拦住登录——用户是来修数据的，不是来修配置的。"""
    workspace.config_path.write_text("{ 这不是 json", encoding="utf-8")
    login.save(workspace, Session(cookie="a=1", source="live", uid="1"))
    data = json.loads(workspace.config_path.read_text(encoding="utf-8"))
    assert data["accounts"][0]["cookie"] == "a=1"


def test_save_uses_explicit_label(workspace):
    login.save(workspace, Session(cookie="a=1", source="live", uid="1"), label="我的号")
    data = json.loads(workspace.config_path.read_text(encoding="utf-8"))
    assert data["accounts"][0]["label"] == "我的号"


# --------------------------------------------------------------------------- #
# 登录后接着抓取：CLI 层
# --------------------------------------------------------------------------- #

def test_cli_login_manual_no_check_then_stops(tmp_path):
    """``--no-check --no-fetch``：只把 Cookie 落盘，不碰网络。"""
    from studydash.cli import main

    home = tmp_path / "ws"
    rc = main(
        [
            "--home", str(home),
            "login",
            "--source", "manual",
            "--cookie", "_uid=1234; _d=x",
            "--no-check",
            "--no-fetch",
        ]
    )
    assert rc == 0
    data = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert data["accounts"][0]["cookie"] == "_uid=1234; _d=x"
    # 没验证也要能从 Cookie 里抠出 uid，账号名才分得清是谁的
    assert data["accounts"][0]["label"] == "默认（1234）"


def test_cli_login_no_check_respects_explicit_label(tmp_path):
    """显式 --label 不该被自动生成的账号名顶掉。"""
    from studydash.cli import main

    home = tmp_path / "ws"
    rc = main(
        [
            "--home", str(home),
            "login",
            "--source", "manual",
            "--cookie", "_uid=1234; _d=x",
            "--label", "学校的号",
            "--no-check",
            "--no-fetch",
        ]
    )
    assert rc == 0
    data = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert data["accounts"][0]["label"] == "学校的号"


def test_cli_login_two_accounts_get_distinct_labels(tmp_path):
    """同一个平台上两个账号，名字必须能区分开，否则配置里分不清看的是谁。"""
    from studydash.cli import main

    home = tmp_path / "ws"
    base = ["--home", str(home), "login", "--source", "manual", "--no-check", "--no-fetch"]
    assert main(base + ["--cookie", "_uid=1; _d=a"]) == 0
    assert main(base + ["--cookie", "_uid=2; _d=b"]) == 0
    data = json.loads((home / "config.json").read_text(encoding="utf-8"))
    labels = [a["label"] for a in data["accounts"]]
    assert labels == ["默认（1）", "默认（2）"]
    assert len(set(labels)) == 2


def test_uid_from_cookie():
    assert login.uid_from_cookie("_uid=998877; _d=x") == "998877"
    assert login.uid_from_cookie("_d=x") == ""
    assert login.uid_from_cookie("") == ""


def test_cookie_count_ignores_blanks():
    assert login.cookie_count("_uid=1; _d=a") == 2
    assert login.cookie_count("_uid=1;  ; _d=a; ") == 2
    assert login.cookie_count("") == 0


def test_session_summary_mentions_uid():
    assert "uid=42" in Session(cookie="x", source="manual", uid="42").summary
    assert "uid 未知" in Session(cookie="x", source="manual").summary


def test_cli_login_manual_requires_cookie(tmp_path):
    from studydash.cli import main

    rc = main(["--home", str(tmp_path / "ws"), "login", "--source", "manual"])
    assert rc == 2


def test_cli_login_file_requires_path(tmp_path):
    from studydash.cli import main

    rc = main(["--home", str(tmp_path / "ws"), "login", "--source", "file"])
    assert rc == 2


# --------------------------------------------------------------------------- #
# 只在纯函数层面覆盖 CDP 的 Cookie 归一
# --------------------------------------------------------------------------- #

def test_cdp_cookie_dicts_are_mapped():
    cookies = login._cdp_cookies_to_objects(
        [
            {"name": "a", "value": "1", "domain": ".chaoxing.com", "expires": -1, "secure": True},
            {"name": "", "value": "skip-me"},
        ]
    )
    assert len(cookies) == 1
    assert cookies[0].name == "a"
    assert cookies[0].domain == ".chaoxing.com"
    assert cookies[0].expires is None            # -1 是「会话 Cookie」
    assert cookies[0].secure is True


def test_chaoxing_domain_filter():
    items = [
        {"name": "keep1", "value": "1", "domain": ".chaoxing.com"},
        {"name": "keep2", "value": "2", "domain": "i.chaoxing.com"},
        {"name": "keep3", "value": "3", "domain": ".chaoxing.cn"},
        {"name": "drop1", "value": "4", "domain": ".bing.com"},
        {"name": "drop2", "value": "5", "domain": "notchaoxing.com"},
    ]
    names = [c.name for c in login._chaoxing_cookies(items)]
    assert names == ["keep1", "keep2", "keep3"]
