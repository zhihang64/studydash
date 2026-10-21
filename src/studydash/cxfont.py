"""学习通题面的「换形字体」解码（纯标准库，不引第三方字体库）。

学习通把选择题、判断题的题干和选项用一套重命名字形的 TTF 渲染：文字本身还是
正常 Unicode，只是码位被换成了形近字，人眼看到的是「海镚崧青州」这类乱码。
页面里 ``<style id="cxSecretStyle">`` 内联了那份字体（base64）。

还原办法：字形轮廓是唯一的。把页面字体的每个字形算出「轮廓坐标 + 是否在曲线上」
的 md5，再拿这些哈希去查一张参考表（:data:`_TABLE_PATH`，1.6MB，来自社区整理的
字形指纹库），就能换回真实字符。

只依赖 :mod:`struct` 与 :mod:`hashlib`：真字体解析只做必需的那一小块
（``head`` / ``maxp`` / ``loca`` / ``glyf`` / ``cmap``），复合字形直接跳过。
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
import unicodedata
from collections.abc import Iterable
from pathlib import Path

__all__ = [
    "FontDecodeError",
    "decode_text",
    "extract_font_base64",
    "font_table_loaded",
]

_TABLE_PATH = Path(__file__).resolve().parent / "data" / "font_map_table.json"

#: 页面样式里内联字体的写法
_RE_FONT = r"base64,([\w\W]+?)'"


class FontDecodeError(Exception):
    """字体加密解不开时抛这个（缺字体、缺参考表、字形对不上）。"""


# --------------------------------------------------------------------------- #
# 参考表：字形指纹 -> 真实字符
# --------------------------------------------------------------------------- #

_HASH_TO_CHAR: dict[str, str] | None = None


def _name_to_char(name: str) -> str:
    """把字形名换成字符：``uni4E00`` -> ``一``，单字符名原样返回。"""
    if len(name) == 1:
        return name
    if name.startswith("uni") and len(name) == 7:
        try:
            return chr(int(name[3:], 16))
        except ValueError:
            return ""
    if name.startswith("u") and 5 <= len(name) <= 7:
        try:
            return chr(int(name[1:], 16))
        except ValueError:
            return ""
    return ""


def _load_table() -> dict[str, str]:
    """载入并反转参考表（``字形名 -> 哈希`` 换成 ``哈希 -> 字符``）。"""
    global _HASH_TO_CHAR
    if _HASH_TO_CHAR is not None:
        return _HASH_TO_CHAR
    if not _TABLE_PATH.exists():
        raise FontDecodeError(f"缺字体参考表：{_TABLE_PATH}")
    try:
        raw = json.loads(_TABLE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FontDecodeError(f"字体参考表读不了：{exc}") from exc
    table: dict[str, str] = {}
    for name, digest in raw.items():
        char = _name_to_char(str(name))
        if char and digest:
            table.setdefault(str(digest), char)
    _HASH_TO_CHAR = table
    return table


def font_table_loaded() -> bool:
    """参考表是否已载入内存（给 doctor 用，不触发读盘）。"""
    return _HASH_TO_CHAR is not None


# --------------------------------------------------------------------------- #
# 极简 TTF 解析
# --------------------------------------------------------------------------- #

def _tables(data: bytes) -> dict[str, tuple[int, int]]:
    """读表目录：``tag -> (offset, length)``。"""
    if len(data) < 12:
        raise FontDecodeError("字体数据太短")
    num_tables = struct.unpack_from(">H", data, 4)[0]
    out: dict[str, tuple[int, int]] = {}
    for i in range(num_tables):
        base = 12 + i * 16
        if base + 16 > len(data):
            break
        tag = data[base : base + 4].decode("latin-1")
        offset, length = struct.unpack_from(">II", data, base + 8)
        out[tag] = (offset, length)
    return out


def _cmap_lookup(data: bytes, tables: dict[str, tuple[int, int]]) -> dict[int, int]:
    """建 ``码位 -> 字形号`` 映射。支持 format 4 与 format 12。"""
    entry = tables.get("cmap")
    if entry is None:
        raise FontDecodeError("字体里没有 cmap 表")
    offset, _length = entry
    count = struct.unpack_from(">H", data, offset + 2)[0]
    best: tuple[int, int] | None = None
    for i in range(count):
        rec = offset + 4 + i * 8
        plat, enc, sub = struct.unpack_from(">HHI", data, rec)
        if (plat, enc) in ((3, 1), (3, 10), (0, 3), (0, 4), (0, 6)):
            best = (enc, offset + sub) if best is None or enc > best[0] else best
    if best is None:
        raise FontDecodeError("字体里没有可用的 Unicode cmap 子表")
    sub_offset = best[1]
    fmt = struct.unpack_from(">H", data, sub_offset)[0]
    mapping: dict[int, int] = {}
    if fmt == 4:
        seg_x2 = struct.unpack_from(">H", data, sub_offset + 6)[0]
        seg = seg_x2 // 2
        ends = struct.unpack_from(f">{seg}H", data, sub_offset + 14)
        starts_at = sub_offset + 16 + seg_x2
        starts = struct.unpack_from(f">{seg}H", data, starts_at)
        deltas = struct.unpack_from(f">{seg}h", data, starts_at + seg_x2)
        range_off_pos = starts_at + seg_x2 * 2
        range_offs = struct.unpack_from(f">{seg}H", data, range_off_pos)
        for i in range(seg):
            start, end = starts[i], ends[i]
            if start == 0xFFFF:
                continue
            for code in range(start, min(end, 0xFFFE) + 1):
                if range_offs[i] == 0:
                    gid = (code + deltas[i]) & 0xFFFF
                else:
                    pos = range_off_pos + i * 2 + range_offs[i] + (code - start) * 2
                    if pos + 2 > len(data):
                        continue
                    gid = struct.unpack_from(">H", data, pos)[0]
                    if gid:
                        gid = (gid + deltas[i]) & 0xFFFF
                if gid:
                    mapping[code] = gid
    elif fmt == 12:
        groups = struct.unpack_from(">I", data, sub_offset + 12)[0]
        pos = sub_offset + 16
        for _ in range(groups):
            start, end, gid = struct.unpack_from(">III", data, pos)
            pos += 12
            for code in range(start, min(end, start + 0xFFFF) + 1):
                mapping[code] = gid + (code - start)
    else:
        raise FontDecodeError(f"暂不支持 cmap format {fmt}")
    return mapping


def _glyph_outline(
    data: bytes,
    table: dict[str, tuple[int, int]],
    gid: int,
) -> tuple[list[tuple[int, int, int]], list[int]]:
    """解析一个简单字形的轮廓，返回 ``[(x, y, flag), ...]`` 与轮廓端点。"""
    glyf_off, glyf_len = table["glyf"]
    loca_off, _ = table["loca"]
    short = table["_loca_short"]
    if short:
        if loca_off + (gid + 1) * 2 > len(data):
            raise FontDecodeError("loca 越界")
        start = struct.unpack_from(">H", data, loca_off + gid * 2)[0] * 2
        end = struct.unpack_from(">H", data, loca_off + (gid + 1) * 2)[0] * 2
    else:
        if loca_off + (gid + 1) * 4 > len(data):
            raise FontDecodeError("loca 越界")
        start = struct.unpack_from(">I", data, loca_off + gid * 4)[0]
        end = struct.unpack_from(">I", data, loca_off + (gid + 1) * 4)[0]
    if end <= start or start >= glyf_len:
        return [], []
    base = glyf_off + start
    contours = struct.unpack_from(">h", data, base)[0]
    if contours < 0:
        return [], []          # 复合字形：参考表里不会有，直接放弃
    if contours == 0:
        return [], []
    pos = base + 10
    ends = list(struct.unpack_from(f">{contours}H", data, pos))
    pos += contours * 2
    ins_len = struct.unpack_from(">H", data, pos)[0]
    pos += 2 + ins_len

    total = ends[-1] + 1
    flags: list[int] = []
    while len(flags) < total:
        flag = data[pos]
        pos += 1
        flags.append(flag)
        if flag & 0x08:
            repeat = data[pos]
            pos += 1
            flags.extend([flag] * repeat)

    def read_coords(short_bit: int, same_bit: int) -> list[int]:
        nonlocal pos
        out: list[int] = []
        value = 0
        for flag in flags[:total]:
            if flag & short_bit:
                delta = data[pos]
                pos += 1
                value += delta if flag & same_bit else -delta
            elif not flag & same_bit:
                value += struct.unpack_from(">h", data, pos)[0]
                pos += 2
            out.append(value)
        return out

    xs = read_coords(0x02, 0x10)
    ys = read_coords(0x04, 0x20)
    points = [(xs[i], ys[i], flags[i] & 0x01) for i in range(total)]
    return points, ends


def _hash_glyph(points: list[tuple[int, int, int]], ends: list[int]) -> str:
    """按上游同样的规则算字形指纹：坐标 + 是否在曲线上，串起来取 md5。"""
    if not ends or not points:
        return ""
    chunks: list[str] = []
    last = 0
    for end in ends:
        for i in range(last, min(end + 1, len(points))):
            x, y, flag = points[i]
            chunks.append(f"{x}{y}{flag}")
        last = end + 1
    if not chunks:
        return ""
    return hashlib.md5("".join(chunks).encode()).hexdigest()


def _decoder_table(font_bytes: bytes, codes: Iterable[int]) -> dict[int, str]:
    """给需要的码位算出「码位 -> 字形指纹」。"""
    tables = _tables(font_bytes)
    if "glyf" not in tables or "loca" not in tables or "head" not in tables:
        raise FontDecodeError("字体缺 glyf/loca/head 表")
    head_off, _ = tables["head"]
    loca_format = struct.unpack_from(">h", font_bytes, head_off + 50)[0]
    tables["_loca_short"] = (1 if loca_format == 0 else 0)  # type: ignore[assignment]
    cmap = _cmap_lookup(font_bytes, tables)

    out: dict[int, str] = {}
    for code in codes:
        gid = cmap.get(code)
        if not gid:
            continue
        try:
            points, ends = _glyph_outline(font_bytes, tables, gid)
        except (struct.error, IndexError):
            continue
        digest = _hash_glyph(points, ends)
        if digest:
            out[code] = digest
    return out


# --------------------------------------------------------------------------- #
# 对外
# --------------------------------------------------------------------------- #

def extract_font_base64(html: str) -> bytes:
    """从页面 ``<style id="cxSecretStyle">`` 里抠出内联字体（bytes）。"""
    import re

    style = re.search(r'<style[^>]*id="cxSecretStyle"[^>]*>([\w\W]*?)</style>', html)
    if not style:
        raise FontDecodeError("页面里没有 cxSecretStyle，可能没开字体加密")
    match = re.search(_RE_FONT, style.group(1))
    if not match:
        raise FontDecodeError("cxSecretStyle 里没有 base64 字体")
    try:
        return base64.b64decode(match.group(1))
    except (TypeError, ValueError) as exc:
        raise FontDecodeError(f"字体 base64 解不开：{exc}") from exc


def _unradical(text: str) -> str:
    """把康熙部首 / CJK 部首补集的字符归一成常用汉字。"""
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if 0x2E80 <= code <= 0x2FDF:
            ch = unicodedata.normalize("NFKC", ch)
        out.append(ch)
    return "".join(out)


def decode_text(html: str, text: str) -> str:
    """用页面内联字体把 ``text`` 里的加密字还原（解不开的字符原样保留）。"""
    if not html or not text:
        return text
    font_bytes = extract_font_base64(html)
    mapping = _decoder_table(font_bytes, {ord(ch) for ch in text})
    known = _load_table()
    out: list[str] = []
    for ch in text:
        digest = mapping.get(ord(ch))
        restored = known.get(digest) if digest else None
        out.append(restored or ch)
    return _unradical("".join(out))
