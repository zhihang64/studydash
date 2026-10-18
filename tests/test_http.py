"""``studydash.http``：只读保证的实际执行者。

这个文件是整个项目里最该被钉死的一处。README、文档、界面上的「只读」三个字
最终都要落在这里的几条断言上；它们一旦放松，前面所有承诺都是空话。
"""

from __future__ import annotations

import json

import pytest

from studydash.http import (
    READ_METHODS,
    HttpClient,
    HttpError,
    ReadOnlyError,
    Response,
    assert_readonly,
    validate_query_endpoints,
)

RPC = "https://www.example.edu/web/j/courseBean.getMyCourseRpc.rpc"


def _client(**kw) -> HttpClient:
    kw.setdefault("timeout", 0.2)
    kw.setdefault("delay", 0.0)
    kw.setdefault("retries", 0)
    return HttpClient(**kw)


# --------------------------------------------------------------------------- #
# POST 白名单
# --------------------------------------------------------------------------- #

def test_post_to_undeclared_host_is_refused():
    """默认客户端一个查询端点都没声明，POST 必须一步都走不出去。"""
    c = _client()
    with pytest.raises(ReadOnlyError) as exc:
        c.post_query("https://evil.example.com/api", {"a": 1})
    assert "只读" in str(exc.value)


def test_post_to_declared_endpoint_passes_the_guard(monkeypatch):
    """声明过的端点要能过闸——否则慕课/智慧树这类 RPC 平台根本没法接。

    这里把真正的网络层换掉，只验证「闸门放行」这一步。
    """
    c = _client(query_endpoints=[RPC])
    seen: dict[str, object] = {}

    def fake_request(url, *, method="GET", data=None, headers=None, note=""):
        seen.update(url=url, method=method, data=data)
        return Response(url=url, status=200, headers={}, content=b"{}")

    monkeypatch.setattr(c, "_request", fake_request)
    c.post_query(RPC, {"courseId": 1})
    assert seen["method"] == "POST"
    assert seen["data"] == {"courseId": 1}


def test_declared_endpoint_does_not_leak_to_neighbours(monkeypatch):
    """回归：``…Rpc.rpc`` 不能顺带放行 ``…Rpc.rpcEVIL``。"""
    c = _client(query_endpoints=[RPC])
    monkeypatch.setattr(
        c, "_request", lambda *a, **k: pytest.fail("不该发出去")
    )
    for bad in (RPC + "EVIL", RPC + "-other", RPC + "X"):
        with pytest.raises(ReadOnlyError):
            c.post_query(bad)


def test_directory_style_endpoint_matches_children(monkeypatch):
    c = _client(query_endpoints=["https://a.example.com/api/"])
    monkeypatch.setattr(
        c, "_request", lambda url, **k: Response(url, 200, {}, b"{}")
    )
    c.post_query("https://a.example.com/api/items", {})
    c.post_query("https://a.example.com/api", {})       # 归一化后也算命中
    with pytest.raises(ReadOnlyError):
        c.post_query("https://a.example.com/apitest", {})


def test_post_query_json_hides_transport_errors():
    """取数据失败返回 default，不把异常抛到上层——一个端点烂掉不该终止整次抓取。"""
    c = _client(query_endpoints=[RPC])
    assert c.post_query_json(RPC, {}, default={"fallback": True}) == {"fallback": True}


# --------------------------------------------------------------------------- #
# 端点白名单的静态检查
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "url",
    [
        "https://mooc1.chaoxing.com/work/doHomeWorkNew?courseId=1",
        "https://zhibo.chaoxing.com/saveTimePc?streamName=x",
        "https://mooc1.chaoxing.com/api/answer/submit",
        "https://x.example.com/api/upload",
        "https://x.example.com/api/checkin",
        "https://x.example.com/api/report",
    ],
)
def test_validate_rejects_write_looking_endpoints(url):
    with pytest.raises(ReadOnlyError):
        validate_query_endpoints([url])


@pytest.mark.parametrize(
    "url",
    [
        RPC,
        "https://www.icourse163.org/web/j/resourceBean.getLessonUnitLearnVo.rpc",
        "https://studyh5.zhihuishu.com/v2/studentStudy/getRecruitTask",
    ],
)
def test_validate_accepts_real_query_endpoints(url):
    assert validate_query_endpoints([url]) == (url,)


def test_validate_rejects_relative_and_blank():
    with pytest.raises(ReadOnlyError):
        validate_query_endpoints(["/api/x"])
    assert validate_query_endpoints(["", "   "]) == ()
    assert validate_query_endpoints([]) == ()


def test_read_methods_are_only_get_and_post():
    """有人把 DELETE 加进 READ_METHODS 的话，这里会先炸。"""
    assert set(READ_METHODS) == {"GET", "POST"}


# --------------------------------------------------------------------------- #
# 错误信息 / 身体解码
# --------------------------------------------------------------------------- #

def test_http_error_message_carries_url_reason_status():
    exc = HttpError("https://x/y", "超时", 504)
    assert "https://x/y" in str(exc)
    assert "超时" in str(exc)
    assert "504" in str(exc)


def test_response_decodes_utf8_and_gb18030():
    r = Response("u", 200, {}, "中文内容".encode())
    assert r.text == "中文内容"

    gbk = Response("u", 200, {"content-type": "text/html; charset=gbk"}, "中文".encode("gbk"))
    assert gbk.encoding == "gbk"
    assert gbk.text == "中文"


def test_response_json_degrades():
    assert Response("u", 200, {}, json.dumps({"a": 1}).encode()).json() == {"a": 1}
    assert Response("u", 200, {}, b"<html>").json() is None
    assert Response("u", 200, {}, b"<html>").json(default={}) == {}
    assert len(Response("u", 200, {}, b"abcd")) == 4


def test_encoding_falls_back_to_utf8_without_charset():
    assert Response("u", 200, {"content-type": "text/html"}, b"").encoding == "utf-8"


# --------------------------------------------------------------------------- #
# 抗网络异常（本机回环，不碰外网）
# --------------------------------------------------------------------------- #

def test_get_survives_a_dead_socket():
    """连接被拒要变成 HttpError，而不是把 URLError 漏给上层。"""
    c = _client()
    with pytest.raises(HttpError):
        c.get("http://127.0.0.1:1/nothing")


def test_get_text_swallows_error_but_get_raises():
    c = _client()
    with pytest.raises(HttpError):
        c.get("http://127.0.0.1:1/nothing")
    assert c.get_text("http://127.0.0.1:1/nothing", default="兜底") == "兜底"
    assert c.get_json("http://127.0.0.1:1/nothing", default={}) == {}


def test_readonly_report_shape():
    c = _client(query_endpoints=[RPC])
    report = c.readonly_report()
    assert report["readonly"] is True
    assert RPC in report["post_endpoints"]
    assert set(report["methods"]) == {"GET", "POST"}
    assert report["requests"] == 0


# --------------------------------------------------------------------------- #
# 源码级自检：不允许绕开本模块直接发请求
# --------------------------------------------------------------------------- #

def test_no_module_bypasses_this_client():
    """CI 的守门员：谁在别处 import urllib.request / requests，这里立刻红。

    否则「只读」就退化成一句口号——随便哪个适配器都能自己开一条写通道。
    """
    assert assert_readonly() == []


def test_assert_readonly_detects_a_planted_violation(tmp_path):
    """反过来验证守门员本身有效，避免它变成一个永远返回空的摆设。"""
    bad = tmp_path / "bad.py"
    bad.write_text("import requests\nrequests.post('https://x/y')\n", encoding="utf-8")
    assert assert_readonly([str(bad)]) == [str(bad)]
