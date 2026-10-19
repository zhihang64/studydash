"""``providers/chaoxing.py`` 的解析与诊断测试（不联网）。

重点在两件事：

1. **解析**：拿一份仿真的课程列表 HTML，确认能解析出课程、教师、百分比。
2. **诊断**：解析为空时，说出来的话必须能让用户自己判断「是 Cookie 的问题
   还是平台改版」——这是这个适配器标 ``beta`` 时最该守住的能力。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from studydash.providers import AccountConfig, HttpSettings
from studydash.providers.chaoxing import ChaoxingProvider

FIXTURE = Path(__file__).parent / "fixture_course_list.html"

#: 一份不依赖 fixture 的最小列表页。结构照抄真实页面的两个坑：
#: ① 每个课程 ``<li>`` 里还嵌着一层菜单 ``<ul><li>``，用非贪婪 ``.*?`` 配到
#:    ``</ul>`` 会在这里被截断；② 课名在 ``<span class="course-name">`` 里，
#:    教师在一行 ``color3`` 的 ``<p>`` 里，页面正文根本没写"教师"两个字。
#: 第二门课故意用老版结构（课名在链接 ``title``、教师写成 ``讲师：``），
#: 保证兜底链也被测到。
MINIMAL_LIST = """
<html><body>
<ul id="courseList" class="course-list">
  <li class="course clearfix catalog_0 learnCourse" info="111_222" id="c_111">
    <input class="clazzId" value="222">
    <ul class="hanlde-list">
      <li class="move-to">移动到</li>
      <li class="quit-course">退课</li>
    </ul>
    <div class="course-info">
      <h3 class="inlineBlock"><a class="color1" href="/visit/stucoursemiddle?courseid=111&clazzid=222&cpi=333">x</a></h3>
      <span class="course-name overHidden2" title="数据结构与算法">数据结构与算法</span>
      <p class="line2 color3" title="张明">张明</p>
      <p class="overHidden1">班级：专业赛道</p>
    </div>
    <span class="progress">66.7%</span>
  </li>
  <li class="course-item">
    <a href="/mycourse/studentstudy?courseid=444&clazzid=555&cpi=666" title="计算机网络">计算机网络</a>
    <span class="course-info">讲师：李四</span>
    <span class="course-percent">46%</span>
  </li>
</ul>
</body></html>
"""

LOGIN_PAGE = """
<html><head><title>用户登录</title></head><body>
<div class="login-box"><h2>用户登录</h2>
<form id="login" action="/fanyalogin" method="post">
<input type="hidden" name="fid" value="-1">
<input type="hidden" name="refer" value="https://i.chaoxing.com">
<input type="text" name="uname" id="uname">
<input type="password" name="password" id="pwd">
</form></div></body></html>
"""


@pytest.fixture
def provider() -> ChaoxingProvider:
    return ChaoxingProvider(
        AccountConfig(platform="chaoxing", label="测试", cookie="_uid=1; _d=2"),
        HttpSettings(delay=0.0, timeout=1.0, retries=0),
    )


# --------------------------------------------------------------------------- #
# 元信息
# --------------------------------------------------------------------------- #

def test_meta_status_matches_what_is_actually_supported(provider):
    """status 跟着「已验证的能力」走，不能跟着心情走。

    这个断言不写死某个取值：超星的进度已经用真实账号跑通过，作业/待办还没接入，
    所以稳定档必须带验证日期，而 note 里必须把那块没做的讲出来——
    「待办 0」最容易被用户读成「没有作业」。
    """
    meta = provider.meta
    assert meta.id == "chaoxing"
    assert meta.auth == "cookie"
    if meta.status == "stable":
        assert meta.verified, "标成 stable 就必须写上验证日期"
    assert "作业" in meta.note, "作业/待办尚未接入，note 必须讲清楚"


def test_client_declares_no_post_endpoints(provider):
    """课程列表走 GET，所以不该给自己开任何 POST 白名单。"""
    assert provider.client_kwargs() == {} or "query_endpoints" not in provider.client_kwargs()


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #

def test_parse_minimal_list(provider):
    courses = provider._parse_course_list(MINIMAL_LIST)
    names = [c.name for c in courses]
    assert names == ["数据结构与算法", "计算机网络"]
    assert courses[0].id == "111"
    assert courses[0].teacher == "张明"
    assert courses[1].teacher == "李四"


def test_parse_survives_nested_li_menu(provider):
    """回归：每个课程条目里嵌着一层菜单 ``<ul><li>``。

    旧实现用 ``<ul id=courseList>(.*?)</ul>`` 取容器、用
    ``<li>(.*?)(?=<li|</ul>|\\Z)`` 切条目，两者都会在菜单那层被截断——
    结果是 22 门课只出 1 门，且课名退化成「课程 <id>」。
    """
    courses = provider._parse_course_list(MINIMAL_LIST)
    assert len(courses) == 2, "课程条目被内层菜单截断了"
    assert all(not c.name.startswith("课程 ") for c in courses)


def test_parse_fixture_list(provider):
    """fixture 里三门课，课名都必须是真的课名，不能退化成「课程 <id>」占位。"""
    courses = provider._parse_course_list(FIXTURE.read_text(encoding="utf-8"))
    assert len(courses) >= 2
    for course in courses:
        assert course.id.isdigit()
        assert course.name
        assert not course.name.startswith("课程 "), f"{course.id} 的课名没解析出来"
    assert len({c.id for c in courses}) == len(courses), "课程 id 不该重复"


def test_parse_empty_returns_nothing(provider):
    assert provider._parse_course_list("<html><body>什么都没有</body></html>") == []


# --------------------------------------------------------------------------- #
# 诊断：解析为空时必须说清楚原因
# --------------------------------------------------------------------------- #

def test_diagnose_detects_login_page(provider):
    text = provider._diagnose_list_page(LOGIN_PAGE)
    assert "登录页" in text


def test_diagnose_reports_missing_container(provider):
    text = provider._diagnose_list_page("<html><body><div>改版了</div></body></html>")
    assert "找不到" in text
    assert "courseList" in text


def test_diagnose_counts_items_in_container(provider):
    text = provider._diagnose_list_page(MINIMAL_LIST)
    assert "识别到 2 门课" in text


def test_diagnose_notes_empty_container(provider):
    text = provider._diagnose_list_page('<html><ul id="courseList"></ul></html>')
    assert "识别到 0 门课" in text


def test_diagnose_flags_data_present_but_unmatched(provider):
    """数据就在正文里、只是没匹配上——这是最需要说清楚的一种情况。"""
    text = provider._diagnose_list_page(
        '<html><script>var d = {"courseId": 1};</script></body></html>'
    )
    assert "courseId" in text
    assert "提取逻辑" in text


def test_diagnose_always_reports_size(provider):
    text = provider._diagnose_list_page("x" * 1234)
    assert "1234 字节" in text


# --------------------------------------------------------------------------- #
# fetch 的前置检查（不联网，靠假 HttpClient）
# --------------------------------------------------------------------------- #

def test_fetch_without_cookie_raises():
    from studydash.providers.base import AuthError

    provider = ChaoxingProvider(
        AccountConfig(platform="chaoxing", label="空的", cookie=""),
        HttpSettings(delay=0.0, timeout=1.0, retries=0),
    )
    with pytest.raises(AuthError):
        provider.fetch()


def test_probe_plan_starts_from_course_list(provider):
    """自检与抓取打同一个地址。

    回归：这里原先多探一个 i.chaoxing.com/base/settings 当「登录态自检」，而它对
    有效会话也返回登录页，于是每个真账号都被判成 Cookie 过期。
    """
    urls = [url for _, url in provider.probe_plan()]
    assert any("courses/list" in u for u in urls)
    assert not any("base/settings" in u for u in urls)


# --------------------------------------------------------------------------- #
# 课程页：任务点进度
# --------------------------------------------------------------------------- #
#
# 真实页面有两种形状，必须都能吃下：
#
# * 新版——表头写「已完成任务点: 57/1442」，能算百分比；
# * 旧版——表头压根没有这几个字（或者写死 0/0），只有每个章节节点上的余量。
#
# 旧版算不出百分比，但也没法「按余量估」：节点上的 ``knowledgeJobCount``
# 实测**等于** ``catalog_points_yi``（1325 个节点上两列逐个相等），
# 它是「这个节点还剩几个任务点」，不是「这个节点有几个任务点」。
# 拿它当分母会得出一堆假的 100%。

PAGE_NEW = """
<html><body>
<h2 class="xs_head_name">
  已完成任务点: <span style="color:#00B368">57</span>/1442
  <div class="catalog_jindu"><div class="catalog_ressbar_width" style="width:3%"></div></div>
</h2>
<div class="chapter_item" href="javascript:;" title="第1章 绪论">
  <div class="catalog_name"><span class="catalog_sbar">1</span>第1章 绪论</div>
</div>
<div class="chapter_item" id="cur1177417087"
     onclick="toOld('264213701','1177417087','147924912',0)"
     title="从产业升级看高质量发展成效">
  <div class="catalog_title">
    <div class="catalog_name newCatalog_name">
      <span class="catalog_sbar">1.1</span>
      <input type="hidden" value="2" class="knowledgeJobCount"/>
      <span class="catalog_points_yi">2</span><span class="bntHoverTips">2个待完成任务点<i></i></span>
    </div>
  </div>
</div>
<div class="chapter_item" id="cur1177417088"
     onclick="toOld('264213701','1177417088','147924913',0)"
     title="高质量发展">
  <div class="catalog_title">
    <div class="catalog_name newCatalog_name">
      <span class="catalog_sbar">1.2</span>
      <input type="hidden" value="0" class="knowledgeJobCount"/>
    </div>
  </div>
</div>
</body></html>
"""

#: 旧版页面：没有「已完成任务点」表头，只有节点余量。
PAGE_OLD = """
<html><body>
<h2 class="xs_head_name">形势与政策 <div class="catalog_jindu"></div></h2>
<div class="chapter_item" id="cur913354511"
     onclick="toOld('244022903','913354511','121429923',0)" title="课前学习">
  <div class="catalog_name newCatalog_name">
    <span class="catalog_sbar">1.1</span>
    <input type="hidden" value="9" class="knowledgeJobCount"/>
    <span class="catalog_points_yi">9</span><span class="bntHoverTips">9个待完成任务点<i></i></span>
  </div>
</div>
<div class="chapter_item" id="cur913354512"
     onclick="toOld('244022903','913354512','121429924',0)" title="课中讨论">
  <div class="catalog_name newCatalog_name">
    <span class="catalog_sbar">1.2</span>
    <input type="hidden" value="5" class="knowledgeJobCount"/>
    <span class="catalog_points_yi">5</span><span class="bntHoverTips">5个待完成任务点<i></i></span>
  </div>
</div>
</body></html>
"""


def test_course_page_reads_header_progress(provider):
    """新版页面：表头 57/1442 就是要显示的那个百分比。"""
    info = provider._parse_course_page(PAGE_NEW)
    assert (info["done"], info["total"]) == (57, 1442)
    assert info["pending"] == 2
    # 三个 chapter_item 里，第一个是一级分组标题（没有 id，也不带 toOld），不算节点
    assert len(info["nodes"]) == 2
    assert [n["sbar"] for n in info["nodes"]] == ["1.1", "1.2"]
    assert info["nodes"][0]["name"] == "从产业升级看高质量发展成效"


def test_course_page_without_header_reports_only_remainder(provider):
    """旧版页面：没有表头就老实说没有，只把余量合计出来。"""
    info = provider._parse_course_page(PAGE_OLD)
    assert (info["done"], info["total"]) == (0, 0)
    assert info["pending"] == 14
    assert len(info["nodes"]) == 2


def test_node_job_count_is_remainder_not_total(provider):
    """``knowledgeJobCount`` 是余量，等于 ``catalog_points_yi``。

    这个断言看着废话，但它挡的是一个很容易犯的错：把它当「任务点总数」，
    然后算 done = jobs - pending，得到满屏假的 100%。
    """
    info = provider._parse_course_page(PAGE_OLD)
    for node in info["nodes"]:
        assert node["jobs"] == node["pending"]


def test_fill_progress_keeps_remainder_when_no_percentage(provider):
    """算不出百分比时不要造一个 Progress，把余量放进 extra 让卡片去解释。"""
    from studydash.model import Course

    course = Course(id="1", name="形势与政策", platform="chaoxing")
    provider._course_page_info = lambda _c: {
        "done": 0, "total": 0, "pending": 14, "nodes": [{}, {}],
    }
    provider._fill_progress([course])

    assert course.progress == []              # 没造假的 0/0 进度条
    assert course.extra["task_points_pending"] == 14
    assert course.extra["chapter_count"] == 2


def test_fill_progress_uses_header_when_available(provider):
    from studydash.model import Course

    course = Course(id="1", name="计算机网络", platform="chaoxing")
    provider._course_page_info = lambda _c: {
        "done": 57, "total": 1442, "pending": 1385, "nodes": [],
    }
    provider._fill_progress([course])

    assert len(course.progress) == 1
    assert (course.progress[0].done, course.progress[0].total) == (57, 1442)


def test_fill_progress_respects_the_cap(provider):
    """22 门课每门一次 GET，上限不生效的话一次抓取要几十秒。"""
    from studydash.model import Course

    provider.account.options["max_progress"] = 2
    calls: list[str] = []

    def fake(course):
        calls.append(course.id)
        return {"done": 1, "total": 2, "pending": 1, "nodes": []}

    provider._course_page_info = fake
    courses = [Course(id=str(i), name=f"课{i}", platform="chaoxing") for i in range(5)]
    provider._fill_progress(courses)

    assert calls == ["0", "1"]
    assert courses[2].progress == []
