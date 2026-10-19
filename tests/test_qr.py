"""``studydash.qr`` 的测试。

这一组里最重要的是 **渲染往返**（``test_roundtrip_*``）：把 ``render_matrix`` 打出来的
字符按终端的真实语义反解回矩阵，再和输入逐格比对。它专门用来钉住一个已经踩过的坑——
把 ``▀``（U+2580 上半块）的前景/背景色写反。写反了屏幕上的码看着一样是方的、
定位图案也对，但每两行颠倒一次，手机扫不出来。只靠肉眼或只检查定位图案都抓不到。
"""

from __future__ import annotations

import struct
import zlib

import pytest

from studydash import qr

# ---------------------------------------------------------------- 测试用 PNG 生成器


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def png_from_gray(rows: list[list[int]], *, depth: int = 8, color_type: int = 0) -> bytes:
    """造一张最小的 PNG。``depth`` 支持 1 和 8，``color_type`` 支持 0/2/6。"""
    height = len(rows)
    width = len(rows[0])
    if depth == 1:
        packed = []
        for row in rows:
            bits = "".join("1" if v else "0" for v in row)
            bits += "0" * (-len(bits) % 8)
            packed.append(bytes(int(bits[i:i + 8], 2) for i in range(0, len(bits), 8)))
        raw = b"".join(b"\x00" + p for p in packed)
    elif depth == 8:
        raw = b"".join(b"\x00" + bytes(row) for row in rows)
    else:  # pragma: no cover - 只在写错测试时触发
        raise AssertionError(f"不支持的 depth={depth}")
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, depth, color_type, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(raw))
        + _chunk(b"IEND", b"")
    )


def png_of_matrix(matrix: list[list[bool]], *, scale: int = 4, quiet: int = 4,
                  depth: int = 1) -> bytes:
    """把模块矩阵画成 PNG（默认 1 位灰度，和超星自己发的那种一样）。"""
    n = len(matrix)
    side = (n + quiet * 2) * scale
    dark = 0 if depth == 1 else 0
    light = 1 if depth == 1 else 255
    rows = [[light] * side for _ in range(side)]
    for r in range(n):
        for c in range(n):
            if matrix[r][c]:
                for y in range((r + quiet) * scale, (r + quiet + 1) * scale):
                    for x in range((c + quiet) * scale, (c + quiet + 1) * scale):
                        rows[y][x] = dark
    return png_from_gray(rows, depth=depth)


def sample_matrix(n: int = 25, seed: int = 7) -> list[list[bool]]:
    """确定性伪随机矩阵（不是真二维码，但足够检验渲染保真度）。"""
    state = seed
    grid = []
    for _ in range(n):
        row = []
        for _ in range(n):
            state = (state * 1103515245 + 12345) & 0x7FFFFFFF
            row.append(bool((state >> 16) & 1))
        grid.append(row)
    return grid


# ---------------------------------------------------------------- 把渲染结果反解回矩阵
#
# 终端的真实语义（这就是本文件存在的理由）：
#   ▀ U+2580 上半块  -> 字形占字符格上半，用**前景**色画；下半露**背景**色
#   ▄ U+2584 下半块  -> 字形占字符格下半，用**前景**色画；上半露**背景**色
#   █ U+2588 全块    -> 整个字符格都是前景色
# 所以解析时必须按字形决定「前景色落在哪一格」，不能想当然。


def parse_plain(text: str) -> list[list[bool]]:
    """无色版：一行字符 = 两行模块。"""
    rows: list[list[bool]] = []
    for line in text.split("\n"):
        if not line:
            continue
        upper: list[bool] = []
        lower: list[bool] = []
        for ch in line:
            if ch == "\u2588":
                upper.append(True)
                lower.append(True)
            elif ch == "\u2580":
                upper.append(True)
                lower.append(False)
            elif ch == "\u2584":
                upper.append(False)
                lower.append(True)
            elif ch == " ":
                upper.append(False)
                lower.append(False)
            else:  # pragma: no cover
                raise AssertionError(f"渲染里出现了没见过的字符 {ch!r}")
        rows.append(upper)
        rows.append(lower)
    return rows


_ANSI = __import__("re").compile(r"\x1b\[([0-9;]*)m")


def parse_color(text: str) -> list[list[bool]]:
    """彩色版：按 ANSI 前景/背景色解出明暗，且**必须**认字形 ``▀`` 把前景放上半格。"""
    rows: list[list[bool]] = []
    for line in text.split("\n"):
        if not line:
            continue
        upper: list[bool] = []
        lower: list[bool] = []
        fg = bg = None
        i = 0
        while i < len(line):
            m = _ANSI.match(line, i)
            if m:
                for part in m.group(1).split(";"):
                    if not part:
                        continue
                    code = int(part)
                    if code == 0:
                        fg = bg = None
                    elif 30 <= code <= 37:
                        fg = code
                    elif 40 <= code <= 47:
                        bg = code
                i = m.end()
                continue
            ch = line[i]
            assert ch == "\u2580", f"彩色版只该用上半块，实际用了 {ch!r}"
            upper.append(fg == 30)   # 黑前景 -> 上半模块是暗的
            lower.append(bg == 40)   # 黑背景 -> 下半模块是暗的
            i += 1
        rows.append(upper)
        rows.append(lower)
    return rows


def _ink_box(rows: list[list[bool]]) -> tuple[int, int, int, int]:
    width = max(len(r) for r in rows)
    for r in rows:
        r.extend([False] * (width - len(r)))
    ys = [i for i, r in enumerate(rows) if any(r)]
    xs = [j for j in range(width) if any(r[j] for r in rows)]
    return ys[0], ys[-1] + 1, xs[0], xs[-1] + 1


# ---------------------------------------------------------------- 渲染往返（回归钉子）


@pytest.mark.parametrize("margin", [0, 1, 2, 4])
def test_roundtrip_plain(margin: int) -> None:
    """无色版：屏幕上的方块要能逐格还原成输入矩阵。"""
    matrix = sample_matrix(25)
    rows = parse_plain(qr.render_matrix(matrix, margin=margin, color=False))
    top, bottom, left, right = _ink_box(rows)
    assert (bottom - top, right - left) == (25, 25), "屏幕墨迹的形状不对"
    for r in range(25):
        for c in range(25):
            assert rows[top + r][left + c] == matrix[r][c], f"第 {r},{c} 格显示走样"


@pytest.mark.parametrize("margin", [0, 1, 2, 4])
def test_roundtrip_color(margin: int) -> None:
    """彩色版：前景必须画上半格、背景画下半格。

    这条就是给 ``_color_line`` 那处前景/背景写反准备的——写反时本用例会大面积红。
    """
    matrix = sample_matrix(25, seed=11)
    rows = parse_color(qr.render_matrix(matrix, margin=margin, color=True))
    # 彩色版连静区一起画成白底，所以整块都是「墨迹」，尺寸应是 见方+2*margin
    assert len(rows) in (25 + margin * 2, 25 + margin * 2 + 1), "彩色版行数不对"
    assert {len(r) for r in rows} == {25 + margin * 2}, "彩色版各行宽度不一致"
    inside = [r[margin:margin + 25] for r in rows[margin:margin + 25]]
    for r in range(25):
        for c in range(25):
            assert inside[r][c] == matrix[r][c], f"第 {r},{c} 格显示走样"


def test_color_uses_explicit_black_and_white_background() -> None:
    """深色终端下不能靠默认背景当白格，否则整张码会被显示成反色码。"""
    text = qr.render_matrix(sample_matrix(21), color=True)
    codes = {int(c) for m in _ANSI.finditer(text) for c in m.group(1).split(";") if c}
    assert 30 in codes, "没有黑前景"
    assert 40 in codes, "没有黑背景"
    assert {37, 47} <= codes, "没有显式白前景/白背景"


def test_color_resets_attributes_at_line_end() -> None:
    """每行末尾必须复位，否则颜色会漏到 prompt 和后续输出里。"""
    for line in qr.render_matrix(sample_matrix(21), color=True).split("\n"):
        assert line.endswith("\x1b[0m"), "行尾没有复位"


def test_invert_flips_every_module() -> None:
    matrix = sample_matrix(21, seed=3)
    rows = parse_plain(qr.render_matrix(matrix, margin=0, color=False, invert=True))
    top, bottom, left, right = _ink_box(rows)
    for r in range(21):
        for c in range(21):
            assert rows[top + r][left + c] is not matrix[r][c]


def test_plain_render_trims_trailing_blank_columns_to_avoid_wrapping() -> None:
    """行尾空白被去掉是**故意的**：行宽超宽会让终端折行，一折行整张码就废了。"""
    matrix = [[False] * 21 for _ in range(21)]
    matrix[0][0] = True
    text = qr.render_matrix(matrix, margin=2, color=False)
    assert max(len(line) for line in text.split("\n")) <= 4


def test_render_rejects_empty_matrix() -> None:
    with pytest.raises(qr.QrError, match="空"):
        qr.render_matrix([])


# ---------------------------------------------------------------- PNG 解码


def test_decode_png_returns_size_and_gray_rows() -> None:
    rows = [[0, 255], [255, 0]]
    width, height, gray = qr.decode_png(png_from_gray(rows))
    assert (width, height) == (2, 2)
    assert gray == rows


def test_decode_png_handles_one_bit_depth() -> None:
    """超星发来的就是 1 位灰度图，这条路径必须能走。

    ``decode_png`` 会把所有位深统一归一到 0/255 两档灰度（不是 0/1），
    这样后面判「暗」只要比一个阈值就够了。
    """
    matrix = sample_matrix(21)
    data = png_of_matrix(matrix, scale=3, quiet=4, depth=1)
    width, height, gray = qr.decode_png(data)
    assert width == height == (21 + 8) * 3
    assert {v for row in gray for v in row} == {0, 255}


def test_matrix_from_png_recovers_a_one_bit_png() -> None:
    matrix = sample_matrix(25)
    assert qr.matrix_from_png(png_of_matrix(matrix, scale=4, quiet=4, depth=1)) == matrix


def test_matrix_from_png_recovers_an_eight_bit_png() -> None:
    matrix = sample_matrix(21)
    assert qr.matrix_from_png(png_of_matrix(matrix, scale=5, quiet=2, depth=8)) == matrix


@pytest.mark.parametrize("n", [21, 25, 29, 33, 53])
def test_matrix_from_png_handles_every_standard_size(n: int) -> None:
    matrix = sample_matrix(n)
    assert qr.matrix_from_png(png_of_matrix(matrix, scale=3, quiet=4, depth=1)) == matrix


def test_render_png_is_the_composition() -> None:
    matrix = sample_matrix(21, seed=5)
    data = png_of_matrix(matrix, scale=4, quiet=4, depth=1)
    assert qr.render_png(data, color=False) == qr.render_matrix(matrix, color=False)


def test_decode_png_rejects_non_png() -> None:
    with pytest.raises(qr.QrError, match="PNG"):
        qr.decode_png(b"not a png at all")


def test_decode_png_rejects_truncated_png() -> None:
    data = png_of_matrix(sample_matrix(21), scale=2, quiet=4)
    with pytest.raises(qr.QrError):
        qr.decode_png(data[:40])


def test_matrix_from_png_rejects_too_many_modules() -> None:
    data = png_of_matrix(sample_matrix(21), scale=3, quiet=4)
    with pytest.raises(qr.QrError):
        qr.matrix_from_png(data, max_modules=8)


def test_module_sizes_are_all_legal_qr_versions() -> None:
    assert qr.MODULE_SIZES[0] == 21
    assert qr.MODULE_SIZES[-1] == 177
    assert all((n - 17) % 4 == 0 for n in qr.MODULE_SIZES)
    assert len(qr.MODULE_SIZES) == 40
