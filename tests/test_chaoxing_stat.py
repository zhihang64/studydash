"""``providers/chaoxing_stat.py`` 的解析与诊断测试（不联网）。

这个适配器移植自第三方工具 ``chaoxing_tool`` 的**只读查询**部分。测试要守住的
三件事：

1. **只读**：它确实只声明了一个查询端点，而且那个端点里没有写动作词——否则
   ``HttpClient`` 会在运行时抛 :class:`ReadOnlyError`，用户看到的是一句
   莫名其妙的报错，而不是"统计读不到"。
2. **解析**：``jobEnc``/时长/任务点这几个正则能吃下真实响应的结构，包括
   上游那个贪婪的 ``(.*)`` 会踩到的边界。
3. **降级**：某一个统计端点挂了，不能让整门课消失，也不能让整轮抓取崩掉。

所有网络调用都被 monkeypatch 掉，跑测试不会碰任何真实服务器。
"""

from __future__ import annotations

import json
from io import StringIO

import pytest

from studydash.htmlx import regex_first
from studydash.http import validate_query_endpoints
from studydash.log import Log
from studydash.model import Course
from studydash.providers import AccountConfig, HttpSettings
from studydash.providers.chaoxing_stat import (
    EP_COURSE_LIST,
    EP_PV_CHART,
    EP_STUDY_DATA,
    EP_TASK_DETAIL,
    EP_TASK_INDEX,
    QUERY_ENDPOINTS,
    RE_JOB_ENC,
    ChaoxingStatProvider,
)

#: 仿真的课程列表。结构照抄 ``test_chaoxing.MINIMAL_LIST`` 的第一条——本适配器
#: 复用 ``ChaoxingProvider._parse_course_list``，所以只要能让它解析出来就行。
COURSE_LIST = """
<html><body>
<ul id="courseList" class="course-list">
  <li class="course clearfix catalog_0 learnCourse" info="111_222" id="c_111">
    <input class="clazzId" value="222">
    <div class="course-info">
      <h3 class="inlineBlock"><a class="color1"
          href="/visit/stucoursemiddle?courseid=111&clazzid=222&cpi=333">x</a></h3>
      <span class="course-name overHidden2" title="数据结构与算法">数据结构与算法</span>
      <p class="line2 color3" title="张明">张明</p>
    </div>
  </li>
</ul>
</body></html>
"""

#: 学习数据页：``jobEnc`` 后面紧跟别的东西，正是上游贪婪正则踩坑的形状。
STUDY_DATA_PAGE = """
<html><head><script type="text/javascript">
var courseId = '111';
var jobEnc = 'A1B2C3D4E5F6';
var other = 'x;y;z';
</script></head><body>学习数据</body></html>
"""

#: 学习时长页：``总时长`` 与 ``div.fl.min`` 两处都出现。
TASK_INDEX_PAGE = """
<html><body>
<div class="total">总时长 480 分钟</div>
<div class="fl min"><span>235.5</span> 分钟</div>
</body></html>
"""

TASK_DETAIL_JSON = {
    "data": {
        "results": [
            {"title": "第一章 绪论", "status": 1, "progress": "100%", "nested": {"a": 1}},
            {"title": "第二章 线性表", "status": 0, "progress": "45%"},
        ]
    }
}


@pytest.fixture
def provider() -> ChaoxingStatProvider:
    p = ChaoxingStatProvider(
        AccountConfig(platform="chaoxing_stat", label="测试", cookie="_uid=1; _d=2"),
        HttpSettings(delay=0.0, timeout=1.0, retries=0),
    )
    p.log = Log(stream=StringIO())
    return p


def _fake_http(provider, *, texts=None, jsons=None, posts=None):
    """把四个网络入口整体替换掉，按 URL 前缀分派。

    传进来的 ``texts``/``jsons``/``posts`` 是 ``{端点: 返回值}``。没配到的端点
    返回空/None——正好模拟"这个接口挂了"，用来测降级。
    """
    texts = texts or {}
    jsons = jsons or {}
    posts = posts or {}

    def fake_text(url, params=None, *, headers=None, note="", default=""):
        return texts.get(url, default)

    def fake_json(url, params=None, *, headers=None, note="", default=None):
        return jsons.get(url, default)

    def fake_post(url, data=None, *, headers=None, note="", default=None):
        return posts.get(url, default)

    provider.http.get_text = fake_text
    provider.http.get_json = fake_json
    provider.http.post_query_json = fake_post


# --------------------------------------------------------------------------- #
# 元信息与只读边界
# --------------------------------------------------------------------------- #

def test_meta_is_consistent(provider):
    meta = provider.meta
    assert meta.id == "chaoxing_stat"
    assert meta.name and meta.short
    # stat2 那一套没在真实账号上跑通过，就不许标 stable。
    assert meta.status == "unverified"
    assert not meta.verified, "没验证过就不许填验证日期"
    assert meta.auth == "cookie"
    assert meta.color.startswith("#") and len(meta.color) == 7


def test_declares_exactly_one_query_endpoint(provider):
    """统计里只有学习次数要 POST，其余三个都是 GET。多开一个都是多余的门。"""
    kwargs = provider.client_kwargs()
    assert kwargs["query_endpoints"] == QUERY_ENDPOINTS
    assert len(QUERY_ENDPOINTS) == 1
    assert EP_PV_CHART in QUERY_ENDPOINTS


def test_query_endpoints_pass_the_readonly_gate():
    """启动期体检：端点必须是绝对 URL，且不含写动作词。

    ``validate_query_endpoints`` 是 ``HttpClient.__init__`` 里真的会调的那个函数，
    这里直接用它，而不是另写一套判断——否则测的是测试自己的逻辑。
    """
    assert validate_query_endpoints(QUERY_ENDPOINTS) == QUERY_ENDPOINTS


def test_post_endpoint_is_not_accidentally_writable():
    """把"为什么这个 POST 是安全的"钉死：地址是查询式路径，不含写动词。"""
    lowered = EP_PV_CHART.lower()
    for word in ("submit", "save", "answer", "commit", "finish", "report", "sign"):
        assert word not in lowered, f"{EP_PV_CHART} 里出现了写动作词 {word}"


# --------------------------------------------------------------------------- #
# 正则
# --------------------------------------------------------------------------- #

def test_job_enc_regex_stops_at_first_quote():
    """上游写的是 ``jobEnc = '(.*)';``，在 ``re.S`` 下会吞掉后面所有分号。"""
    token = regex_first(RE_JOB_ENC, STUDY_DATA_PAGE)
    assert token == "A1B2C3D4E5F6"


def test_job_enc_regex_tolerates_spacing():
    assert regex_first(RE_JOB_ENC, "jobEnc='ZZ99';") == "ZZ99"
    assert regex_first(RE_JOB_ENC, "jobEnc  =  'Q1';") == "Q1"


def test_job_enc_missing_returns_empty():
    assert regex_first(RE_JOB_ENC, "<html>没有令牌</html>") == ""


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #

def test_parse_course_list_is_borrowed_not_copied(provider):
    """课程列表解析复用 ``ChaoxingProvider``，这里只确认借用这条路是通的。"""
    courses = provider._course_list(COURSE_LIST)
    assert len(courses) == 1
    assert courses[0].name == "数据结构与算法"
    assert courses[0].extra["class_id"] == "222"
    assert courses[0].extra["cpi"] == "333"


def test_minutes_prefers_structured_node(provider):
    _fake_http(provider, texts={EP_TASK_INDEX: TASK_INDEX_PAGE})
    minutes = provider._minutes("111", "222", "333", "ENC")
    assert minutes is not None
    done, total = minutes
    assert done == 235.5
    assert total == 480.0


def test_minutes_falls_back_to_regex(provider):
    """htmlx 找不到 ``div.fl.min`` 时，正则兜底要能接住。"""
    page = '<div class="min fl"><span>88</span></div><p>总时长 100 分钟</p>'
    _fake_http(provider, texts={EP_TASK_INDEX: page})
    minutes = provider._minutes("111", "222", "333", "ENC")
    assert minutes == (88.0, 100.0)


def test_minutes_returns_none_on_empty_page(provider):
    _fake_http(provider, texts={EP_TASK_INDEX: ""})
    assert provider._minutes("111", "222", "333", "ENC") is None


def test_minutes_tolerates_missing_total(provider):
    """``总时长`` 没写出来时，已学时长仍然是有效信息，不该整条丢掉。"""
    _fake_http(provider, texts={EP_TASK_INDEX: '<div class="fl min"><span>12</span></div>'})
    assert provider._minutes("111", "222", "333", "ENC") == (12.0, 0.0)


def test_study_count_reads_total(provider):
    _fake_http(provider, posts={EP_PV_CHART: {"total": 17}})
    assert provider._study_count("111", "222", "333") == 17


def test_study_count_uses_current_month(provider):
    """上游把 ``year=2023&month=03`` 写死了，这里必须是当前月。"""
    seen = {}

    def fake_post(url, data=None, *, headers=None, note="", default=None):
        seen.update(data or {})
        return {"total": 3}

    provider.http.post_query_json = fake_post
    provider._study_count("111", "222", "333")
    assert seen["year"] not in ("2023", ""), "年份不该写死"
    assert len(seen["month"]) == 2, "月要两位补零"
    assert seen["clazzid"] == "222" and seen["courseid"] == "111"


def test_study_count_none_on_bad_payload(provider):
    _fake_http(provider, posts={EP_PV_CHART: None})
    assert provider._study_count("111", "222", "333") is None


def test_task_detail_flattens_rows(provider):
    _fake_http(provider, jsons={EP_TASK_DETAIL: TASK_DETAIL_JSON})
    rows = provider._task_detail("111", "222", "333", "ENC")
    assert rows is not None and len(rows) == 2
    assert rows[0]["title"] == "第一章 绪论"
    # 嵌套字典不往 extra 里塞，否则输出会变成一坨看不懂的树。
    assert "nested" not in rows[0]


def test_task_detail_none_on_unexpected_shape(provider):
    _fake_http(provider, jsons={EP_TASK_DETAIL: {"data": {"results": "不是列表"}}})
    assert provider._task_detail("111", "222", "333", "ENC") is None


# --------------------------------------------------------------------------- #
# 端到端（假网络）
# --------------------------------------------------------------------------- #

def test_fetch_fills_stats(provider):
    _fake_http(
        provider,
        texts={
            EP_COURSE_LIST: COURSE_LIST,
            EP_STUDY_DATA: STUDY_DATA_PAGE,
            EP_TASK_INDEX: TASK_INDEX_PAGE,
        },
        jsons={EP_TASK_DETAIL: TASK_DETAIL_JSON},
        posts={EP_PV_CHART: {"total": 17}},
    )
    courses = provider.fetch()
    assert len(courses) == 1
    course = courses[0]
    assert course.extra["stat2"] is True
    assert course.extra["minutes_done"] == 235.5
    assert course.extra["minutes_total"] == 480.0
    assert course.extra["study_count"] == 17
    assert course.extra["task_count"] == 2
    assert course.progress and course.progress[0].label == "视频时长"
    assert course.progress[0].done == 236
    assert course.progress[0].total == 480


def test_fetch_survives_one_dead_endpoint(provider):
    """学习时长挂了，学习次数和任务点还得照常留下来。"""
    _fake_http(
        provider,
        texts={EP_COURSE_LIST: COURSE_LIST, EP_STUDY_DATA: STUDY_DATA_PAGE},
        jsons={EP_TASK_DETAIL: TASK_DETAIL_JSON},
        posts={EP_PV_CHART: {"total": 9}},
    )
    course = provider.fetch()[0]
    assert "minutes_done" not in course.extra
    assert course.extra["study_count"] == 9
    assert course.extra["task_count"] == 2


def test_course_without_clazzid_is_skipped(provider):
    """没有 clazzid 的课程拼不出 stat2 参数，跳过比打一串 404 好。"""
    def fake_fill(course: Course) -> bool:
        return provider._fill_course(course)

    course = Course(id="999", name="跨校共享课", platform="chaoxing_stat", extra={})
    assert fake_fill(course) is False
    assert "stat2" not in course.extra


def test_empty_course_list_raises(provider):
    from studydash.providers import ProviderError

    _fake_http(provider, texts={EP_COURSE_LIST: "<html><body>空</body></html>"})
    with pytest.raises(ProviderError):
        provider.fetch()


def test_logged_out_page_raises_auth_error(provider):
    from studydash.providers import AuthError

    # 必须命中 base.LOGIN_PAGE_SIGNATURES 里真实存在的那几组特征，否则
    # looks_logged_out 会判否，测试就变成在验证一段自己编出来的 html。
    login_page = (
        '<html><head><title>用户登录</title></head><body>'
        '<form id="login" action="/fanyalogin" method="post">'
        '<input type="password" name="pwd"></form></body></html>'
    )
    _fake_http(provider, texts={EP_COURSE_LIST: login_page})
    with pytest.raises(AuthError):
        provider.fetch()


def test_missing_cookie_raises_auth_error():
    from studydash.providers import AuthError

    bare = ChaoxingStatProvider(
        AccountConfig(platform="chaoxing_stat", label="无 Cookie"),
        HttpSettings(delay=0.0, timeout=1.0, retries=0),
    )
    with pytest.raises(AuthError):
        bare.fetch()


# --------------------------------------------------------------------------- #
# probe
# --------------------------------------------------------------------------- #

def test_probe_plan_defaults_to_course_list_only(provider):
    """没有课程三元组就打不通统计端点，默认别把它们列进计划。"""
    plan = provider.probe_plan()
    assert plan == [("课程列表", EP_COURSE_LIST)]


def test_probe_plan_expands_with_course_triple():
    p = ChaoxingStatProvider(
        AccountConfig(
            platform="chaoxing_stat",
            label="测试",
            cookie="_uid=1",
            options={"probe_course": "111:222:333"},
        ),
        HttpSettings(delay=0.0, timeout=1.0, retries=0),
    )
    p.log = Log(stream=StringIO())
    _fake_http(p, texts={EP_STUDY_DATA: STUDY_DATA_PAGE})

    plan = p.probe_plan()
    urls = [entry[1] for entry in plan]
    assert EP_STUDY_DATA in urls[1]
    assert any(EP_TASK_INDEX in u for u in urls)
    assert any(EP_TASK_DETAIL in u for u in urls)

    # POST 那条必须是四元组，且端点在白名单里——probe 靠这个才发得出去。
    posts = [e for e in plan if len(e) == 4]
    assert len(posts) == 1
    label, url, method, form = posts[0]
    assert label and method == "POST"
    assert url in p.client_kwargs()["query_endpoints"]
    assert form["clazzid"] == "222"


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #

def test_this_month_is_two_digit():
    from studydash.providers.chaoxing_stat import _this_month

    year, month = _this_month()
    assert year.isdigit() and len(year) == 4
    assert month.isdigit() and len(month) == 2


def test_flatten_keeps_scalars(provider):
    from studydash.providers.chaoxing_stat import _flatten

    out = _flatten({"a": "x" * 500, "b": 1, "c": None, "d": {"deep": 1}, "e": True})
    assert len(out["a"]) <= 120
    assert out["b"] == 1 and out["c"] is None and out["e"] is True
    assert "d" not in out


def test_flatten_handles_non_dict():
    from studydash.providers.chaoxing_stat import _flatten

    assert _flatten("字符串") == {"_value": "字符串"}


def test_json_roundtrip_of_extra(provider):
    """extra 最后要进 JSON 输出，不能出现不可序列化的对象。"""
    _fake_http(
        provider,
        texts={EP_COURSE_LIST: COURSE_LIST, EP_STUDY_DATA: STUDY_DATA_PAGE},
        jsons={EP_TASK_DETAIL: TASK_DETAIL_JSON},
        posts={EP_PV_CHART: {"total": 5}},
    )
    course = provider.fetch()[0]
    json.dumps(course.extra, ensure_ascii=False)
