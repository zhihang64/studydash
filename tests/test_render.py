"""``studydash.render``：把 payload 注入单文件模板。

关心两件事：产出必须是**自包含**的（断网也能看），注入必须是**安全**的
（课程名是从别人家网页抓来的，属于不可信输入）。
"""

from __future__ import annotations

import json
import re

import pytest

from studydash.render import PLACEHOLDER, inject, render

MINIMAL = """<!doctype html><html><head><title>学习进度看板 · studydash</title>
<style>body{}</style></head><body><main id="app"></main>
<script>window.__PROGRESS__ = __STUDYDASH_PAYLOAD__;</script></body></html>"""


# --------------------------------------------------------------------------- #
# 注入
# --------------------------------------------------------------------------- #

def test_inject_replaces_the_single_placeholder():
    html = inject(MINIMAL, {"a": 1})
    assert PLACEHOLDER not in html
    assert '"a":1' in html


def test_inject_requires_the_placeholder():
    with pytest.raises(ValueError) as exc:
        inject("<html>没有注入点</html>", {})
    assert "注入点" in str(exc.value)


def test_injected_blob_is_still_valid_json():
    """把 '<' 转义成 \\u003c 之后仍然得是合法 JSON，否则前端 JSON.parse 会炸。"""
    payload = {"name": "a<b>c & d", "n": 3}
    html = inject(MINIMAL, payload)
    # 用正则取，别用 rstrip(";</script>")——rstrip 是按字符集剥的，
    # JSON 里合法的 < / s / c / i / p / t 也会被一起啃掉。
    found = re.search(r"window\.__PROGRESS__ = (.*?);</script>", html, re.S)
    assert found, "没找到注入后的 payload"
    assert json.loads(found.group(1)) == payload


def test_closing_script_tag_in_data_cannot_break_out():
    """课程名里带 </script> 是最经典的 XSS 注入点。"""
    evil = "</script><img src=x onerror=alert(1)>"
    html = inject(MINIMAL, {"name": evil})
    # 只转义 "<" 就够了：payload 里的 </script> 变成 \u003c/script>，
    # 破不了 <script> 块。所以全文档只剩模板自己那一个闭合标签。
    assert html.count("</script>") == 1
    assert "\\u003c/script>" in html
    assert "<img src=x" not in html        # 尖括号全被转成了 \u003c


def test_line_and_paragraph_separators_are_escaped():
    """U+2028/2029 在 ES2019 之前是字符串字面量里的非法换行，会让整页 JS 语法错误。"""
    html = inject(MINIMAL, {"t": "a\u2028b\u2029c"})
    assert "\u2028" not in html
    assert "\u2029" not in html
    assert "\\u2028" in html and "\\u2029" in html


def test_ampersand_is_escaped():
    assert "\\u0026" in inject(MINIMAL, {"t": "a&b"})


def test_non_ascii_is_kept_readable():
    """ensure_ascii=False：中文课程名直接出现在 HTML 里，方便肉眼排查。"""
    assert "数据结构与算法" in inject(MINIMAL, {"name": "数据结构与算法"})


def test_inject_handles_unserialisable_exotics():
    """payload 里混进 set/datetime 也不能抛——退化用 str()。"""
    from datetime import datetime, timezone

    html = inject(MINIMAL, {"when": datetime(2026, 3, 14, tzinfo=timezone.utc), "s": {1, 2}})
    assert "2026-03-14" in html


# --------------------------------------------------------------------------- #
# 落盘
# --------------------------------------------------------------------------- #

def test_render_writes_atomically_and_creates_parents(tmp_path):
    tpl = tmp_path / "tpl.html"
    tpl.write_text(MINIMAL, encoding="utf-8")
    out = tmp_path / "deep" / "nested" / "dashboard.html"
    assert render({"a": 1}, tpl, out) == out
    assert out.is_file()
    assert list(tmp_path.rglob("*.tmp")) == []


def test_render_replaces_title_when_asked(tmp_path):
    tpl = tmp_path / "tpl.html"
    tpl.write_text(MINIMAL, encoding="utf-8")
    out = tmp_path / "d.html"
    render({"a": 1}, tpl, out, title="我的学习看板")
    assert "<title>我的学习看板</title>" in out.read_text(encoding="utf-8")


def test_render_escapes_the_title(tmp_path):
    tpl = tmp_path / "tpl.html"
    tpl.write_text(MINIMAL, encoding="utf-8")
    out = tmp_path / "d.html"
    render({}, tpl, out, title="<script>alert(1)</script>")
    text = out.read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in text
    assert "&lt;script&gt;" in text


def test_render_overwrites_previous_output(tmp_path):
    tpl = tmp_path / "tpl.html"
    tpl.write_text(MINIMAL, encoding="utf-8")
    out = tmp_path / "d.html"
    render({"n": "第一次很长很长" * 50}, tpl, out)
    render({"n": "短"}, tpl, out)
    assert "第一次" not in out.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# 真模板
# --------------------------------------------------------------------------- #

def test_real_template_has_the_placeholder(workspace):
    text = workspace.find_template().read_text(encoding="utf-8")
    assert PLACEHOLDER in text


def test_real_template_is_self_contained(workspace):
    """自包含是这个项目最值钱的特性之一：没有 CDN、没有外链字体、
    断网能用、拷到手机能用、发给自己能用。"""
    text = workspace.find_template().read_text(encoding="utf-8")
    assert not re.search(r'<script[^>]+\bsrc\s*=', text, re.I)
    assert not re.search(r'<link[^>]+\brel\s*=\s*["\']?stylesheet', text, re.I)
    assert not re.search(r'@import\s+url\(', text, re.I)
    assert "http://" not in text.replace("http://www.w3.org", "")


def test_real_template_renders_a_browsable_page(workspace, payload_json, tmp_path):
    out = tmp_path / "dashboard.html"
    render(payload_json, workspace.find_template(), out)
    html = out.read_text(encoding="utf-8")
    assert html.startswith("<!doctype html") or html.startswith("<!DOCTYPE html")
    assert PLACEHOLDER not in html
    assert "<main" in html
    assert len(html) > 20_000            # 不该退化成空壳
    assert "数据结构与算法" in html        # 数据确实进去了
