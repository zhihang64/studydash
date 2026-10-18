"""渲染：把聚合结果塞进单文件 HTML 模板。

模板里有一个占位符 ``__STUDYDASH_PAYLOAD__``，本模块把它替换成 JSON 字面量，
产出一个**自包含**的 HTML —— 没有外链 CSS/JS/字体，双击就能看，断网也能看，
发给自己手机也能看。

安全上只有一件事要小心：payload 里含课程名、老师名、作业标题，这些都是从
别人家网页上抓来的，属于不可信输入。
* 注入 JSON 时把 ``</`` 转义成 ``<\\/``，防止出现 ``</script>`` 提前闭合脚本块；
* 模板侧一律用 ``textContent`` / ``createElement`` 渲染，不拼 innerHTML。
两边都做了才算稳。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

__all__ = ["inject", "render", "PLACEHOLDER"]

#: 模板里的注入点。用大写长名字，避免和页面里其它东西撞。
PLACEHOLDER = "__STUDYDASH_PAYLOAD__"

#: 尽量让 JSON 直接当 JS 字面量用
_JS_ESCAPES = (
    ("<", "\\u003c"),          # 挡住 </script> 和 <!--
    ("\u2028", "\\u2028"),      # 老引擎里这两个是换行，会撕裂字符串字面量
    ("\u2029", "\\u2029"),
    ("&", "\\u0026"),          # 有些老解析器会在这里栽跟头
)


def inject(template: str, payload: Mapping[str, Any]) -> str:
    """把 payload 注入模板，返回完整 HTML。"""
    if PLACEHOLDER not in template:
        raise ValueError(
            f"模板里没有找到注入点 {PLACEHOLDER}，请确认用的是 studydash 的模板。"
        )
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    for raw, escaped in _JS_ESCAPES:
        blob = blob.replace(raw, escaped)
    return template.replace(PLACEHOLDER, blob)


def render(
    payload: Mapping[str, Any],
    template_path: Path,
    out_path: Path,
    *,
    title: str = "",
) -> Path:
    """渲染并写盘。返回写好的路径。"""
    template = template_path.read_text(encoding="utf-8")
    html = inject(template, payload)
    if title:
        html = html.replace(
            "<title>学习进度看板 · studydash</title>",
            f"<title>{_esc_html(title)}</title>",
            1,
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".tmp")
    tmp.write_text(html, encoding="utf-8")
    tmp.replace(out_path)
    return out_path


def _esc_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
