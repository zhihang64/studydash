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

#: 一份不依赖 fixture 的最小列表页：两门课，一个带教师一个不带。
MINIMAL_LIST = """
<html><body>
<ul id="courseList">
  <li>
    <a title="数据结构与算法" href="/mycourse/studentcourse?courseid=111&clazzid=222&cpi=333">x</a>
    <p class="course-name">数据结构与算法</p>
    <span>授课教师：张明</span>
    <span class="progress">66.7%</span>
  </li>
  <li>
    <a href="/mycourse/studentcourse?courseid=444&clazzid=555&cpi=666">计算机网络</a>
    <span class="progress">46%</span>
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

def test_meta_is_honest_about_being_beta(provider):
    """功能没被真实账号验证过就要写 beta，不然用户会以为它一定能用。"""
    assert provider.meta.id == "chaoxing"
    assert provider.meta.status == "beta"
    assert provider.meta.auth == "cookie"
    assert "章节" in provider.meta.note


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
    assert "2 个 <li>" in text


def test_diagnose_notes_empty_container(provider):
    text = provider._diagnose_list_page('<html><ul id="courseList"></ul></html>')
    assert "0 个 <li>" in text


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


def test_probe_plan_covers_login_and_list(provider):
    urls = [url for _, url in provider.probe_plan()]
    assert any("i.chaoxing.com" in u for u in urls)
    assert any("courses/list" in u for u in urls)
