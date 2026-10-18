"""``studydash.htmlx``：零依赖的 HTML 解析与文本提取。

这些用例大多来自真实踩过的坑，尤其是「最后一个元素匹配不到」那条——
它让一个 4 门课的列表页只解析出 2 门课，而且不报错。
"""

from __future__ import annotations

import pytest

from studydash.htmlx import (
    VOID_TAGS,
    Node,
    parse,
    parse_fragment,
    regex_all,
    regex_first,
    strip_tags,
)


# --------------------------------------------------------------------------- #
# 基本解析
# --------------------------------------------------------------------------- #

def test_parse_finds_by_tag_class_id():
    doc = parse(
        """
        <ul id="courseList">
          <li class="course-card"><h3>数据结构</h3></li>
          <li class="course-card extra"><h3>操作系统</h3></li>
        </ul>
        """
    )
    lis = doc.findall("li")
    assert len(lis) == 2
    assert [li.get("id") for li in lis] == [None, None]
    assert doc.find(cls="extra").find_text("h3") == "操作系统"
    assert doc.find(id="courseList") is not None


def test_findall_by_class_matches_one_token_not_substring():
    """class="course-card-name" 不该被 cls="course-card" 命中。"""
    doc = parse('<div class="course-card-name">x</div><div class="course-card">y</div>')
    hits = doc.findall("div", cls="course-card")
    assert len(hits) == 1
    assert hits[0].texts() == ["y"]


def test_iter_nodes_and_ancestors():
    doc = parse("<div class='a'><span class='b'><em>deep</em></span></div>")
    em = doc.find("em")
    assert em is not None
    # ancestors() 由近及远；最后一项是文档根，它没有 class，所以只比前两跳。
    assert [n.get("class") for n in list(em.ancestors())[:2]] == ["b", "a"]


def test_get_returns_default_for_missing():
    node = parse("<i data-x='1'>t</i>").find("i")
    assert node.get("data-x") == "1"
    assert node.get("nope") is None
    assert node.get("nope", "fallback") == "fallback"


def test_has_class():
    node = parse("<i class='a b c'>t</i>").find("i")
    assert node.has_class("b")
    assert not node.has_class("ab")


def test_attr_text_and_find_text_defaults():
    doc = parse("<a href='/x' title='去这儿'>点我</a>")
    assert doc.attr_text("href", tag="a") == "/x"
    assert doc.find_text("a") == "点我"
    assert doc.find_text("span", default="没这个标签") == "没这个标签"
    assert doc.attr_text("title", tag="span", default="-") == "-"


def test_children_by_tag_is_direct_children_only():
    doc = parse("<ul><li>1</li><li><ul><li>nested</li></ul></li></ul>")
    top = doc.find("ul")
    assert len(top.children_by_tag("li")) == 2
    assert top.find_text("li") == "1"


def test_json_attr_parses_and_degrades():
    doc = parse("""<div data-json='{"a": 1, "b": [2]}'>x</div><div data-json='{bad'>y</div>""")
    good, bad = doc.findall("div")
    assert good.json_attr() == {"a": 1, "b": [2]}
    assert bad.json_attr() is None
    assert bad.json_attr(default={}) == {}


# --------------------------------------------------------------------------- #
# 空白 / 实体 / 容错
# --------------------------------------------------------------------------- #

def test_void_tags_do_not_swallow_following_content():
    """<br> 不闭合；解析器若不认识它，后面的 <p> 会被吃进 <br> 里。"""
    doc = parse("<div><br><p>第二段</p></div>")
    assert doc.find_text("p") == "第二段"
    assert "br" in VOID_TAGS


def test_unclosed_tags_are_recovered():
    doc = parse("<ul><li>甲<li>乙<li>丙</ul>")
    assert [li.texts()[0] for li in doc.findall("li")] == ["甲", "乙", "丙"]


def test_comments_scripts_and_styles_are_ignored():
    doc = parse(
        "<div><!-- 注释里的 <li> 不该被当成节点 -->"
        "<script>var x = '<li>JS 里的</li>';</script>"
        "<style>li { color: red }</style><li>真的</li></div>"
    )
    assert [li.texts()[0] for li in doc.findall("li")] == ["真的"]


def test_entity_decoding():
    doc = parse("<p>A&amp;B &lt;tag&gt; &quot;q&quot; &#39;s&#39; &#x4e2d; &nbsp;X</p>")
    # all_text 会把连续空白（含 &nbsp; 解出的 U+00A0）规整成单个半角空格。
    assert doc.find_text("p") == "A&B <tag> \"q\" 's' 中 X"


def test_texts_flattens_nested_and_strips():
    doc = parse("<div>\n  <span> 甲 </span>\n  <span><b>乙</b></span>\n</div>")
    assert doc.find("div").texts() == ["甲", "乙"]


def test_strip_tags():
    # 标签被替换成空格（而不是直接删掉）："<p>a<br>b</p>" 要读成 "a b" 而不是
    # 粘在一起的 "ab"，课程名里的 <br> 才不会把两个词拼成一个。
    assert strip_tags("<p>a<br>b</p>") == "a b"
    assert strip_tags("a   b") == "a b"          # 连续空白折叠
    assert strip_tags("  <b> 空格 </b>  ") == "空格"
    assert strip_tags("") == ""
    assert strip_tags("纯文本") == "纯文本"


def test_parse_fragment_treats_input_as_body_fragment():
    frag = parse_fragment("<li>甲</li><li>乙</li>")
    assert len(frag.findall("li")) == 2


def test_node_dump_is_bounded_and_does_not_explode():
    doc = parse("<div>" + "<span>x</span>" * 50 + "</div>")
    dumped = doc.dump()
    assert isinstance(dumped, str)
    assert len(dumped) < 4000


def test_empty_input_gives_usable_node():
    for blank in ("", "   ", "不是 HTML"):
        node = parse(blank)
        assert isinstance(node, Node)
        assert node.findall("div") == []


# --------------------------------------------------------------------------- #
# 正则工具（含历史 bug 的回归）
# --------------------------------------------------------------------------- #

def test_regex_first_and_default():
    assert regex_first(r"jobEnc = '(.*)';", "x jobEnc = 'abc'; y") == "abc"
    assert regex_first(r"缺失(.*)", "没有", default="兜底") == "兜底"
    assert regex_first(r"a(.)b", "a-b") == "-"


def test_regex_all():
    assert regex_all(r"id=(\d+)", "id=1 id=22 id=333") == ["1", "22", "333"]
    assert regex_all(r"id=(\d+)", "无") == []


def test_regex_all_captures_last_element():
    """回归：``(?=<li\\b|</ul>)$`` 型的先行断言必须让最后一项也被切出来。

    当初把 ``</ul>`` 写进 lookahead，却没意识到捕获组里根本不含 ``</ul>``，
    于是最后一条永远匹配不上——列表页 4 门课只解析出 2 门，而且静默无错。
    """
    body = "<li>甲</li><li>乙</li><li>丙</li>"
    pattern = r"<li>(.*?)</li>(?=<li\b|</ul>|\Z)"
    assert regex_all(pattern, body) == ["甲", "乙", "丙"]
