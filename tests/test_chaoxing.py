"""``providers/chaoxing.py`` 的解析与诊断测试（不联网）。

重点在两件事：

1. **解析**：拿一份仿真的课程列表 HTML，确认能解析出课程、教师、百分比。
2. **诊断**：解析为空时，说出来的话必须能让用户自己判断「是 Cookie 的问题
   还是平台改版」——这是这个适配器标 ``beta`` 时最该守住的能力。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from studydash.providers import AccountConfig, HttpSettings
from studydash.providers import chaoxing as chaoxing_mod
from studydash.providers.chaoxing import (
    ChaoxingProvider,
    _align_deadline_year,
    _remaining_hours,
)

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

    这个断言不写死某个取值：超星的课程、进度、作业/待办都已用真实账号跑通过，
    所以 stable 必须带验证日期，note 里也要把作业能力写出来——
    否则用户仍会把「待办 0」读成「没有作业」。
    """
    meta = provider.meta
    assert meta.id == "chaoxing"
    assert meta.auth == "cookie"
    if meta.status == "stable":
        assert meta.verified, "标成 stable 就必须写上验证日期"
    assert "作业" in meta.note, "作业/待办已接入，note 必须讲清楚"


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
    assert any("work/stu-work" in u for u in urls)
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


#: 新版课程页（2026-10 实测）：``div.fanyaChapterWhite`` 里是「第N章 → 节 → 小节」
#: 的树，``li`` 套 ``li``。这里刻意做了三层和一个锁住的节点。
PAGE_TREE = """
<html><body>
<div class="fanyaChapterWhite">
  <div class="chapter_head">
    <h2 class="xs_head_name">已完成任务点: <span>5</span>/9</h2>
  </div>
  <div class="chapter_body">
    <div class="chapter_td">
      <div class="chapter_unit">
        <div class="chapter_item">
          <div class="catalog_name"><a class="clicktitle"><span>第1章 统计学的产生与发展</span></a></div>
        </div>
        <div class="catalog_level"><ul>
          <li>
            <div class="chapter_item" id="cur1236661559"
                 onclick="toOld('266688496','1236661559','153613588',0)"
                 title="统计学的性质与分类">
              <div class="catalog_title">
                <div class="catalog_name">
                  <a class="clicktitle">统计学的性质与分类</a>
                  <span class="catalog_sbar">1.1</span>
                </div>
                <div class="catalog_task">
                  <input type="hidden" value="2" class="knowledgeJobCount"/>
                  <span class="catalog_points_yi">2</span>
                </div>
              </div>
            </div>
            <ul>
              <li>
                <div class="chapter_item" id="cur1236661639"
                     onclick="toOld('266688496','1236661639','153613588',0)"
                     title="折线图">
                  <div class="catalog_title">
                    <div class="catalog_name">
                      <a class="clicktitle">折线图<span class="catalog_sbar">1.1.1</span></a>
                    </div>
                    <div class="catalog_task">
                      <input type="hidden" value="3" class="knowledgeJobCount"/>
                      <span class="catalog_points_yi">3</span>
                    </div>
                  </div>
                </div>
              </li>
            </ul>
          </li>
          <li>
            <div class="chapter_item" title="被锁住的章节">
              <div class="catalog_title">
                <div class="catalog_name"><a class="clicktitle">被锁住的章节</a></div>
              </div>
            </div>
          </li>
        </ul></div>
      </div>
      <div class="chapter_unit">
        <div class="chapter_item">
          <div class="catalog_name"><a class="clicktitle"><span>第2章 数据的收集</span></a></div>
        </div>
        <div class="catalog_level"><ul>
          <li>
            <div class="chapter_item" id="cur1236661561"
                 onclick="toOld('266688496','1236661561','153613588',0)"
                 title="数据的计量与分类">
              <div class="catalog_title">
                <div class="catalog_name">
                  <a class="clicktitle">数据的计量与分类</a>
                  <span class="catalog_sbar">2.1</span>
                </div>
                <div class="catalog_task">
                  <input type="hidden" value="0" class="knowledgeJobCount"/>
                  <span class="catalog_points_yi">0</span>
                </div>
              </div>
            </div>
          </li>
        </ul></div>
      </div>
    </div>
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


def test_chapter_tree_keeps_units_instead_of_flattening(provider):
    """``chapter_unit`` 是「第N章」分组，节点要能说清自己属于哪一章。

    回归：旧实现只扫扁平的 ``chapter_item``，分组标题因为「没有 knowledgeid」
    被整段跳过，用户只能看到「1.1 统计学的性质与分类」，看不到「第1章」。
    """
    info = provider._parse_course_page(PAGE_TREE)
    assert [unit["name"] for unit in info["units"]] == ["第1章 统计学的产生与发展", "第2章 数据的收集"]
    assert [n["unit"] for n in info["nodes"]] == [
        "第1章 统计学的产生与发展", "第1章 统计学的产生与发展",
        "第1章 统计学的产生与发展", "第2章 数据的收集",
    ]
    assert [n["sbar"] for n in info["nodes"]] == ["1.1", "1.1.1", "", "2.1"]
    assert info["pending"] == 5


def test_chapter_tree_nested_li_does_not_truncate(provider):
    """``li`` 套 ``li`` 时深度要算对，且子节点不能把父节点截掉。"""
    info = provider._parse_course_page(PAGE_TREE)
    depths = {n["name"]: n["depth"] for n in info["nodes"]}
    assert depths["统计学的性质与分类"] == 0
    assert depths["折线图"] == 1
    # 子节点 3 个任务点 + 父节点 2 个 = 5，说明两层都被读到了
    assert [n["pending"] for n in info["nodes"]] == [2, 3, 0, 0]


def test_chapter_name_drops_sbar_even_inside_the_link(provider):
    """有的节点把 ``span.catalog_sbar`` 塞进 ``a.clicktitle``，名字不能带编号。"""
    info = provider._parse_course_page(PAGE_TREE)
    assert info["nodes"][1]["name"] == "折线图"
    assert info["nodes"][1]["sbar"] == "1.1.1"


def test_locked_chapter_is_marked_unavailable(provider):
    """没有 knowledgeid 的节点点了也打不开——照样列出来，但标记成不可用。"""
    info = provider._parse_course_page(PAGE_TREE)
    locked = [n for n in info["nodes"] if n["name"] == "被锁住的章节"][0]
    assert locked["available"] is False
    assert locked["id"] == ""


def test_fill_progress_collects_only_chapters_with_remaining_work(provider):
    """课程卡只带「还差任务点」的章节；0 余量和锁住的节点不上卡片。"""
    from studydash.model import Course

    course = Course(id="1", name="统计学", platform="chaoxing")
    provider._course_page_info = lambda _c: provider._parse_course_page(PAGE_TREE)
    provider._fill_progress([course])

    assert course.extra["chapter_count"] == 4
    chapters = course.extra["chapters"]
    assert [c["name"] for c in chapters] == ["统计学的性质与分类", "折线图"]
    assert chapters[0] == {
        "name": "统计学的性质与分类",
        "unit": "第1章 统计学的产生与发展",
        "sbar": "1.1",
        "depth": 0,
        "pending": 2,
    }


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


# --------------------------------------------------------------------------- #
# 作业 / 待办
# --------------------------------------------------------------------------- #
#
# 夹具必须**同时**覆盖两种真实结构（和课程页的教训一样）：
#
# * 未提交项：``<span class="status">未提交</span>`` + 「剩余 X 小时」；
# * 已完成 / 待批阅：没有 ``class="status"``，状态直接写在
#   ``<span aria-hidden="true">`` 里，而且**没有**剩余时间。
#
# 课程名在列表页被截断成「大数据统计分...」，解析必须靠 courseId 回查，
# 不能把这段显示文本当成课名。

WORK_LIST = """
<html><body>
<ul class="nav">
  <li onclick="goTask(this);" data="https://mooc1.chaoxing.com/mooc-ans/android/mtaskmsgspecial?taskrefId=100&amp;msgId=0&amp;courseId=111&amp;userId=9&amp;clazzId=222&amp;type=work&amp;enc_task=aaa">
    <span class="spanImg"><i class="redPoint"></i></span>
    <div role="option">
      <p aria-hidden="true">第5章 参数估计</p>
      <span class="status" aria-hidden="true">未提交</span>
      <span aria-hidden="true">《大数据统计分...》</span>
      <span class="fr" aria-hidden="true">剩余995小时51分钟</span>
      <span aria-label="作业名称第5章 参数估计作业状态未提交"></span>
    </div>
  </li>
  <li onclick="goTask(this);" data="https://mooc1.chaoxing.com/mooc-ans/android/mtaskmsgspecial?taskrefId=101&amp;msgId=0&amp;courseId=111&amp;userId=9&amp;clazzId=222&amp;type=work&amp;enc_task=bbb">
    <span class="spanImg"><img src="task-work-gray.png"></span>
    <div role="option">
      <p aria-hidden="true">第1章 导论</p>
      <span  aria-hidden="true">已完成</span>
      <span aria-hidden="true">《大数据统计分...》</span>
    </div>
  </li>
  <li onclick="goTask(this);" data="https://mooc1.chaoxing.com/mooc-ans/android/mtaskmsgspecial?taskrefId=102&amp;msgId=0&amp;courseId=333&amp;userId=9&amp;clazzId=444&amp;type=work&amp;enc_task=ccc">
    <span class="spanImg"><img src="task-work.png"></span>
    <div role="option">
      <p aria-hidden="true">实验1_初识算法与AI协作</p>
      <span  aria-hidden="true">待批阅</span>
      <span aria-hidden="true">《算法设计与分...》</span>
    </div>
  </li>
</ul>
</body></html>
"""

PENDING_DETAIL = """
<html><body>
<div class="zwork">
  <div class="tmTimeNew">
    <h3 role="none">第5章 参数估计</h3>
    <span role="option">
      <span aria-label="开始时间09月29日10时04分29秒"></span>
      <h4 aria-hidden="true">开始时间：09-29 10:04</h4>
    </span>
    <span role="option">
      <span aria-label="截止时间11月30日10时04分46秒"></span>
      <h4 aria-hidden="true">截止时间：11-30 10:04</h4>
    </span>
  </div>
  <div class="zwork_info"><p>共包含10道题目。</p></div>
</div>
</body></html>
"""

COMPLETED_DETAIL = """
<html><body>
<div class="wid750 padTop250">
  <p class="Number"> 100 <span>分</span> </p>
  <div class="BtmCon"><p>共10题，客观题10道，主观题0道</p></div>
</div>
</body></html>
"""


def test_parse_work_list_mixed_structures(provider):
    items = provider._parse_work_list(WORK_LIST)
    assert [i["status"] for i in items] == ["未提交", "已完成", "待批阅"]
    assert [i["title"] for i in items] == [
        "第5章 参数估计",
        "第1章 导论",
        "实验1_初识算法与AI协作",
    ]
    assert [i["course_id"] for i in items] == ["111", "111", "333"]
    assert [i["id"] for i in items] == ["100", "101", "102"]
    # 只有未提交项有剩余时间
    assert items[0]["remaining_hours"] == pytest.approx(995 + 51 / 60)
    assert items[1]["remaining_hours"] is None
    assert items[2]["remaining_hours"] is None
    assert all(i["url"].startswith("https://mooc1.chaoxing.com/") for i in items)


def test_parse_work_list_empty(provider):
    assert provider._parse_work_list("<html><body>没有作业</body></html>") == []


def test_parse_work_detail_reads_absolute_deadline(provider):
    info = provider._parse_work_detail(PENDING_DETAIL)
    due = info["due"]
    assert due is not None
    assert (due.month, due.day, due.hour, due.minute) == (11, 30, 10, 4)
    assert info["score"] is None


def test_parse_work_detail_falls_back_to_aria_label(provider):
    """有的版本只留 aria-label，没有可见的 ``<h4>``。"""
    html = '<span aria-label="截止时间01月05日23时59分07秒"></span>'
    due = provider._parse_work_detail(html)["due"]
    assert due is not None
    assert (due.month, due.day, due.hour, due.minute) == (1, 5, 23, 59)


def test_parse_work_detail_reads_score(provider):
    info = provider._parse_work_detail(COMPLETED_DETAIL)
    assert info["score"] == 100.0
    assert info["due"] is None


def test_remaining_hours_variants():
    assert _remaining_hours("剩余995小时51分钟") == pytest.approx(995 + 51 / 60)
    assert _remaining_hours("剩余45分钟") == pytest.approx(0.75)
    assert _remaining_hours("剩余1天2小时") == pytest.approx(26)
    assert _remaining_hours("已完成") is None


def test_align_deadline_year_handles_new_year(monkeypatch):
    """12 月看到「01-05」时不能补成今年（已经过去快一年）。"""
    fixed = datetime(2026, 12, 20, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(chaoxing_mod, "now", lambda: fixed)
    target = datetime(2027, 1, 5, 23, 59, tzinfo=timezone.utc)
    remaining = (target - fixed).total_seconds() / 3600
    out = _align_deadline_year(datetime(2026, 1, 5, 23, 59, tzinfo=timezone.utc), remaining)
    assert out == target


def test_align_deadline_year_keeps_same_year_without_hint(monkeypatch):
    due = datetime(2026, 1, 5, 23, 59, tzinfo=timezone.utc)
    assert _align_deadline_year(due, None) is due


def test_fill_works_attaches_only_to_known_courses(provider, monkeypatch):
    """课程名按 courseId 回查；不在课程列表里的课程不造假卡片。"""
    from studydash.model import Course, now as model_now

    detail_calls: list[str] = []

    def fake_get_text(url, *args, **kwargs):
        if "work/stu-work" in url:
            return WORK_LIST
        if "taskrefId=100" in url:
            detail_calls.append(url)
            return PENDING_DETAIL
        if "taskrefId=101" in url:
            detail_calls.append(url)
            return COMPLETED_DETAIL
        return ""

    monkeypatch.setattr(provider.http, "get_text", fake_get_text)
    c1 = Course(id="111", name="大数据统计分析基础", platform="chaoxing")
    c3 = Course(id="999", name="别的课", platform="chaoxing")   # 333 不在课程列表里

    provider._fill_works([c1, c3])

    assert len(c1.assignments) == 2
    assert c3.assignments == []
    pending = next(a for a in c1.assignments if a.id == "100")
    assert pending.title == "第5章 参数估计"
    assert pending.course == "大数据统计分析基础"
    assert pending.status == "pending"
    assert pending.due is not None
    # 详情页的绝对截止时间原样落下来（夹具里是 11-30 10:04）。
    # 不要拿「剩余 995h51m」反推精确到分钟的时刻：夹具里的剩余时间是写死的历史值，
    # 和跑测试的当下必然有偏差，只会让用例在一天里的不同时刻随机红。
    assert (pending.due.month, pending.due.day, pending.due.hour, pending.due.minute) == (
        11, 30, 10, 4,
    )
    assert pending.due > model_now(), "年份没对齐，截止时间落到了过去"

    done = next(a for a in c1.assignments if a.id == "101")
    assert done.status == "submitted"
    assert done.finished
    assert done.due is None

    # 已完成项不该再发详情请求；只有未提交的 100 发了一次
    assert len(detail_calls) == 1 and "taskrefId=100" in detail_calls[0]


def test_fill_works_keeps_pending_review_label(provider, monkeypatch):
    """「待批阅」也归 done，但卡片上要留一句，别让用户以为已经批完。"""
    from studydash.model import Course

    def fake_get_text(url, *args, **kwargs):
        return WORK_LIST if "work/stu-work" in url else ""

    monkeypatch.setattr(provider.http, "get_text", fake_get_text)
    course = Course(id="333", name="算法设计与分析", platform="chaoxing")
    provider._fill_works([course])

    assert len(course.assignments) == 1
    item = course.assignments[0]
    assert item.status == "submitted"
    assert item.finished
    assert item.detail == "待批阅"


def test_fill_works_respects_detail_cap(provider, monkeypatch):
    """详情请求有上限，超出的未提交项退回「剩余时间」估算，不编截止时间。"""
    from studydash.model import Course, now as model_now

    provider.account.options["max_work_details"] = 0
    detail_calls: list[str] = []

    def fake_get_text(url, *args, **kwargs):
        if "work/stu-work" in url:
            return WORK_LIST
        detail_calls.append(url)
        return PENDING_DETAIL

    monkeypatch.setattr(provider.http, "get_text", fake_get_text)
    course = Course(id="111", name="大数据统计分析基础", platform="chaoxing")
    provider._fill_works([course])

    assert detail_calls == []
    pending = next(a for a in course.assignments if a.id == "100")
    assert pending.due is not None
    expected = model_now() + timedelta(hours=995 + 51 / 60)
    assert abs((pending.due - expected).total_seconds()) < 120


def test_fill_works_warns_when_page_structure_changes(provider, monkeypatch):
    """拿不到条目时必须说「可能改版了」，而不是安静地当作没有作业。"""
    from io import StringIO

    from studydash.log import Log

    stream = StringIO()
    provider.log = Log(stream=stream)
    monkeypatch.setattr(provider.http, "get_text", lambda *a, **k: "<html><body>改版</body></html>")

    provider._fill_works([])

    assert "无法识别" in stream.getvalue()


def test_fetch_skips_works_when_disabled(provider, monkeypatch):
    monkeypatch.setattr(provider.http, "get_text", lambda *a, **k: MINIMAL_LIST)
    provider.account.options["works"] = False
    provider._fill_progress = lambda courses: None
    calls: list[list] = []
    provider._fill_works = lambda courses: calls.append(courses)

    provider.fetch()

    assert calls == []


def test_fetch_fills_works_by_default(provider, monkeypatch):
    monkeypatch.setattr(provider.http, "get_text", lambda *a, **k: MINIMAL_LIST)
    provider._fill_progress = lambda courses: None
    calls: list[list] = []
    provider._fill_works = lambda courses: calls.append(courses)

    provider.fetch()

    assert len(calls) == 1
