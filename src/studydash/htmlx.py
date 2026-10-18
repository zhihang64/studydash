"""极简 HTML 解析器（纯 stdlib）。

为什么不直接依赖 lxml / BeautifulSoup：
本项目的核心分发形态是「下载一个 exe 就能用」，多一个原生依赖就多一份
杀软误报和体积负担。而各平台的页面结构都相当规整，一个 ~200 行的容错
解析器足够覆盖。

能力范围（刻意保持很小）：
* 解析成 ``Node`` 树，容忍标签未闭合 / 多余闭合
* ``find`` / ``findall`` 按标签、class、id、任意属性查找
* ``text``（直接文本）/ ``all_text``（递归文本）
* 当结构恶心到解析器啃不动时，退回调用方自己的正则——见 ``regex_first``

不提供：CSS 选择器全语法、XPath、命名空间。
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any, Callable, Iterable, Iterator

__all__ = ["Node", "parse", "parse_fragment", "regex_first", "regex_all", "strip_tags"]

#: HTML 自闭合标签，不压栈
VOID_TAGS = frozenset(
    {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }
)

_WS = re.compile(r"\s+")


class Node:
    """一个 HTML 元素。"""

    __slots__ = ("tag", "attrs", "children", "parent", "_text", "index")

    def __init__(
        self,
        tag: str,
        attrs: dict[str, str] | None = None,
        parent: "Node | None" = None,
    ) -> None:
        self.tag = tag.lower()
        self.attrs: dict[str, str] = attrs or {}
        self.children: list[Node] = []
        self.parent = parent
        self._text: list[str] = []
        self.index = 0

    # -- 属性 -------------------------------------------------------------- #

    def get(self, name: str, default: Any = None) -> Any:
        return self.attrs.get(name.lower(), default)

    @property
    def classes(self) -> frozenset[str]:
        raw = self.attrs.get("class", "")
        return frozenset(part for part in _WS.split(raw) if part)

    @property
    def id(self) -> str:
        return self.attrs.get("id", "")

    @property
    def href(self) -> str:
        return self.attrs.get("href", "") or self.attrs.get("data-href", "")

    def has_class(self, name: str) -> bool:
        return name in self.classes

    # -- 文本 -------------------------------------------------------------- #

    @property
    def text(self) -> str:
        """本元素直接包含的文本（不含子元素），已压缩空白。"""
        return _WS.sub(" ", "".join(self._text)).strip()

    @property
    def all_text(self) -> str:
        """递归收集所有后代文本，已压缩空白。"""
        parts: list[str] = ["".join(self._text)]
        for child in self.children:
            parts.append(child.all_text)
        return _WS.sub(" ", " ".join(p for p in parts if p)).strip()

    def texts(self) -> list[str]:
        """递归收集所有文本片段，逐个 strip 后保留非空的。"""
        out: list[str] = []
        if self._text:
            joined = _WS.sub(" ", "".join(self._text)).strip()
            if joined:
                out.append(joined)
        for child in self.children:
            out.extend(child.texts())
        return out

    # -- 遍历 -------------------------------------------------------------- #

    def iter_nodes(self) -> Iterator["Node"]:
        """先序遍历所有后代（不含自己）。"""
        for child in self.children:
            yield child
            yield from child.iter_nodes()

    def ancestors(self) -> Iterator["Node"]:
        node = self.parent
        while node is not None:
            yield node
            node = node.parent

    def _matches(
        self,
        tag: str | Iterable[str] | None,
        cls: str | None,
        node_id: str | None,
        attrs: dict[str, Any] | None,
    ) -> bool:
        if tag is not None:
            if isinstance(tag, str):
                if self.tag != tag.lower():
                    return False
            elif self.tag not in {t.lower() for t in tag}:
                return False
        if cls is not None and not self.has_class(cls):
            return False
        if node_id is not None and self.id != node_id:
            return False
        for key, want in (attrs or {}).items():
            have = self.attrs.get(key.lower())
            if callable(want):
                if not want(have):
                    return False
            elif want is True:
                if have is None:
                    return False
            elif have != want:
                return False
        return True

    def findall(
        self,
        tag: str | Iterable[str] | None = None,
        cls: str | None = None,
        id: str | None = None,          # noqa: A002 - 与 HTML 属性同名更直观
        **attrs: Any,
    ) -> list["Node"]:
        """找所有匹配的后代。``id`` 与 ``attrs`` 里的键都是 HTML 属性名。"""
        return [
            node
            for node in self.iter_nodes()
            if node._matches(tag, cls, id, attrs)
        ]

    def find(
        self,
        tag: str | Iterable[str] | None = None,
        cls: str | None = None,
        id: str | None = None,          # noqa: A002
        **attrs: Any,
    ) -> "Node | None":
        """找第一个匹配的后代。"""
        for node in self.iter_nodes():
            if node._matches(tag, cls, id, attrs):
                return node
        return None

    def find_text(
        self,
        tag: str | Iterable[str] | None = None,
        cls: str | None = None,
        id: str | None = None,          # noqa: A002
        default: str = "",
        **attrs: Any,
    ) -> str:
        node = self.find(tag, cls, id, **attrs)
        return node.all_text if node is not None else default

    def attr_text(
        self,
        attr: str,
        tag: str | Iterable[str] | None = None,
        cls: str | None = None,
        id: str | None = None,          # noqa: A002
        default: str = "",
        **attrs: Any,
    ) -> str:
        node = self.find(tag, cls, id, **attrs)
        if node is None:
            return default
        return node.attrs.get(attr.lower(), default) or default

    def children_by_tag(self, tag: str) -> list["Node"]:
        tag = tag.lower()
        return [c for c in self.children if c.tag == tag]

    def json_attr(self, name: str = "data-json", default: Any = None) -> Any:
        """读一个内嵌 JSON 的属性（很多平台把参数塞在 data-* 里）。"""
        import json

        raw = self.attrs.get(name.lower())
        if not raw:
            return default
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return default

    # -- 调试 -------------------------------------------------------------- #

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        cls = ".".join(sorted(self.classes))
        head = self.tag + (f"#{self.id}" if self.id else "") + (f".{cls}" if cls else "")
        return f"<Node {head} children={len(self.children)}>"

    def dump(self, depth: int = 0, max_depth: int = 3) -> str:  # pragma: no cover
        lines = ["  " * depth + repr(self)]
        if depth < max_depth:
            for child in self.children:
                lines.append(child.dump(depth + 1, max_depth))
        return "\n".join(lines)


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self.stack: list[Node] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        parent = self.stack[-1]
        node = Node(tag, {k.lower(): (v or "") for k, v in attrs}, parent)
        node.index = len(parent.children)
        parent.children.append(node)
        if tag.lower() not in VOID_TAGS:
            self.stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in VOID_TAGS:
            return
        # 从栈顶往下找最近的同名开标签；找不到就当成多余闭合标签忽略
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data: str) -> None:
        if data:
            self.stack[-1]._text.append(data)


def parse(html: str) -> Node:
    """把整篇 HTML 解析成以 ``#document`` 为根的元素树。"""
    builder = _Builder()
    builder.feed(html or "")
    builder.close()
    return builder.root


def parse_fragment(html: str) -> Node:
    """解析一段 HTML 片段，返回包住它的虚拟根。"""
    return parse(html)


# --------------------------------------------------------------------------- #
# 正则兜底
# --------------------------------------------------------------------------- #

def regex_first(pattern: str, text: str, group: int = 1, default: str = "") -> str:
    """结构太脏、解析器啃不动时的兜底：取第一个匹配。"""
    if not text:
        return default
    m = re.search(pattern, text, re.S | re.I)
    if not m:
        return default
    try:
        return (m.group(group) or "").strip()
    except IndexError:
        return default


def regex_all(pattern: str, text: str, group: int = 1) -> list[str]:
    if not text:
        return []
    out: list[str] = []
    for m in re.finditer(pattern, text, re.S | re.I):
        try:
            value = m.group(group)
        except IndexError:
            continue
        if value is not None:
            out.append(value.strip())
    return out


_TAG = re.compile(r"<[^>]+>")


def strip_tags(html: str) -> str:
    """粗暴去标签 + 还原常见实体，用于取纯文本标题。"""
    import html as _html

    text = _TAG.sub(" ", html or "")
    text = _html.unescape(text)
    return _WS.sub(" ", text).strip()
