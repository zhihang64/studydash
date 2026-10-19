"""把服务端返回的二维码图片，原样搬进终端。

为什么需要这个：扫码登录要看到码。命令行里没有 <img>，但二维码的本质就是一张
黑白点阵，只要能把点阵还原出来，就能用半块字符（``▀``）画在终端里——一格
字符画两个模块，正好抵消字符 2:1 的长宽比。

这里刻意做了两件看起来多余、其实必要的事：

1. **不解析二维码的内容。** 我们知道服务器给的是二维码，但不用知道它编码了什么
   字符串（那是超星自定义的协议）。要显示一张二维码，只须还原它的**点阵**，
   不需要 Reed-Solomon 解码。这省掉了整个纠错解码器。
2. **自己解 PNG。** 只有 1~6 个 chunk、过滤器算法固定四种，比引一个图像库划算；
   本项目「零第三方运行时依赖」这条底线值这个代价。

颜色用 ANSI 明确指定黑底/白底，而不是靠终端的默认前景色——深色主题的终端会把
「白模块」画成黑的，整张码就反了。**反转的二维码很多手机扫不出来**，这不是审美问题。
"""

from __future__ import annotations

import struct
import sys
import zlib

__all__ = [
    "QrError",
    "decode_png",
    "matrix_from_png",
    "render_matrix",
    "render_png",
    "terminal_supports_color",
    "MODULE_SIZES",
]

# 合法二维码版本对应的边长（版本 1..40 → 21..177，步长 4）
MODULE_SIZES = tuple(range(21, 178, 4))

_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
# 半块字符：一格画上下两个模块，配合 2:1 的字形长宽比刚好是正方形模块
_UPPER_HALF = "\u2580"

_BLACK_FG = "30"
_BLACK_BG = "40"
_WHITE_FG = "37"
_WHITE_BG = "47"


class QrError(RuntimeError):
    """二维码图片看不懂（不是 PNG / 隔行扫描 / 点阵还原不出来）。"""


# --------------------------------------------------------------------------- #
# PNG 解码
# --------------------------------------------------------------------------- #
def decode_png(data: bytes) -> tuple[int, int, list[list[int]]]:
    """解出灰度点阵，返回 ``(宽, 高, 每行的灰度值列表)``。

    只支持二维码图片实际会用到的那部分 PNG：非隔行、位深 1/2/4/8。
    看不懂就抛 :class:`QrError`——静悄悄返回一张乱图比报错更糟。
    """
    if not data.startswith(_PNG_SIG):
        raise QrError("不是 PNG 图片")

    pos = len(_PNG_SIG)
    width = height = depth = color_type = interlace = None
    palette: list[int] = []
    idat = bytearray()

    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        ctype = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        if len(chunk) < length:
            raise QrError("PNG 数据被截断")
        pos += 12 + length

        if ctype == b"IHDR":
            if length < 13:
                raise QrError("IHDR 长度不对")
            (width, height, depth, color_type,
             _comp, _filt, interlace) = struct.unpack(">IIBBBBB", chunk[:13])
        elif ctype == b"PLTE":
            palette = [chunk[i] for i in range(0, len(chunk) - 2, 3)]
        elif ctype == b"IDAT":
            idat += chunk
        elif ctype == b"IEND":
            break

    if not width or not height:
        raise QrError("PNG 缺少 IHDR")
    if interlace:
        raise QrError("隔行扫描的 PNG 暂不支持")
    if depth not in (1, 2, 4, 8):
        raise QrError(f"不支持的位深：{depth}")
    if color_type not in _CHANNELS:
        raise QrError(f"不支持的颜色类型：{color_type}")
    if color_type in (2, 4, 6) and depth != 8:
        raise QrError(f"颜色类型 {color_type} 只支持 8 位深")

    channels = _CHANNELS[color_type]
    stride = (width * channels * depth + 7) // 8
    if not idat:
        raise QrError("PNG 里没有 IDAT 图像数据（文件被截断了？）")
    try:
        raw = zlib.decompress(bytes(idat))
    except (zlib.error, MemoryError) as exc:
        # 半张图（下载中断、被代理截断）在这里很常见。让 zlib.error 直接冒到调用方
        # 会绕过调用处 ``except qr.QrError`` 的兜底，用户看到的是 traceback 而不是
        # 一句人话。所以统一换成 QrError。
        raise QrError(f"PNG 图像数据解压失败（文件可能不完整）：{exc}") from exc
    expected = (stride + 1) * height
    if len(raw) < expected:
        raise QrError("PNG 像素数据不完整")

    rows: list[list[int]] = []
    prev = bytearray(stride)
    off = 0
    for _y in range(height):
        ftype = raw[off]
        off += 1
        line = bytearray(raw[off:off + stride])
        off += stride
        _unfilter(line, prev, ftype, channels)
        rows.append(_to_gray(line, width, depth, color_type, channels, palette))
        prev = line

    return width, height, rows


def _unfilter(line: bytearray, prev: bytearray, ftype: int, channels: int) -> None:
    """就地撤销 PNG 的行过滤（五种，见 PNG 规范 9.2）。"""
    n = len(line)
    if ftype == 0:
        return
    if ftype == 1:                                   # Sub
        for i in range(channels, n):
            line[i] = (line[i] + line[i - channels]) & 0xFF
    elif ftype == 2:                                 # Up
        for i in range(n):
            line[i] = (line[i] + prev[i]) & 0xFF
    elif ftype == 3:                                 # Average
        for i in range(n):
            left = line[i - channels] if i >= channels else 0
            line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
    elif ftype == 4:                                 # Paeth
        for i in range(n):
            left = line[i - channels] if i >= channels else 0
            up = prev[i]
            upleft = prev[i - channels] if i >= channels else 0
            p = left + up - upleft
            pa, pb, pc = abs(p - left), abs(p - up), abs(p - upleft)
            pred = left if (pa <= pb and pa <= pc) else (up if pb <= pc else upleft)
            line[i] = (line[i] + pred) & 0xFF
    else:
        raise QrError(f"未知的行过滤器：{ftype}")


def _to_gray(line: bytes, width: int, depth: int, color_type: int,
             channels: int, palette: list[int]) -> list[int]:
    """把一个扫描行转成 ``width`` 个 0~255 的灰度值。"""
    if depth < 8:
        per_byte = 8 // depth
        scale = (1 << depth) - 1
        out: list[int] = []
        for byte in line:
            for i in range(per_byte):
                shift = 8 - depth * (i + 1)
                value = (byte >> shift) & scale
                out.append(palette[value] if palette and value < len(palette) else value * 255 // scale)
            if len(out) >= width:
                break
        return out[:width]

    if color_type == 3:
        return [palette[b] if b < len(palette) else 255 for b in line[:width]]
    if color_type in (0, 4):
        return list(line[:width])
    # RGB / RGBA：用 R 通道即可，二维码只有黑白
    return [line[x * channels] for x in range(width)]


# --------------------------------------------------------------------------- #
# 点阵还原
# --------------------------------------------------------------------------- #
def matrix_from_png(data: bytes, *, max_modules: int = 177) -> list[list[bool]]:
    """从 PNG 还原二维码模块矩阵，``True`` = 黑（深色）模块。

    做法：先找深色像素的包围盒。二维码的三个定位图案分别贴着左上、右上、左下角，
    所以包围盒正好等于整块模块阵列——**不需要知道静默区的宽度**。再拿包围盒宽度
    除以合法边长，唯一整除的那个就是真实模块数。
    """
    width, height, rows = decode_png(data)
    ys = range(height)
    dark_x: list[int] = []
    dark_y: list[int] = []
    for y in ys:
        row = rows[y]
        for x in range(width):
            if row[x] < 128:
                dark_x.append(x)
                dark_y.append(y)
    if not dark_x:
        raise QrError("图里找不到任何深色像素，不像是二维码")

    x0, x1 = min(dark_x), max(dark_x)
    y0, y1 = min(dark_y), max(dark_y)
    span_w = x1 - x0 + 1
    span_h = y1 - y0 + 1

    candidates = [n for n in MODULE_SIZES if n <= max_modules and span_w % n == 0]
    # 越大的模块数意味着每个模块越小；取第一个让模块像素 >= 2 的，太小就没法可靠采样
    module_count = next((n for n in candidates if span_w // n >= 2), None)
    if module_count is None:
        raise QrError(f"包围盒 {span_w}x{span_h} 对不上任何二维码版本")
    if span_h % module_count:
        raise QrError(f"包围盒 {span_w}x{span_h} 不是正方形模块阵列")

    scale = span_w // module_count
    # 采样每个模块的中心点，避开边界上的抗锯齿
    half = scale // 2
    matrix: list[list[bool]] = []
    for r in range(module_count):
        cy = y0 + r * scale + half
        matrix.append([rows[cy][x0 + c * scale + half] < 128 for c in range(module_count)])
    return matrix


# --------------------------------------------------------------------------- #
# 终端渲染
# --------------------------------------------------------------------------- #
def terminal_supports_color() -> bool:
    """当前 stdout 是不是一个真终端。

    被重定向到文件、或接进管道时返回 ``False``：ANSI 转义序列在那两种场合没有意义，
    只会把文件变成一堆 ``\\x1b[30m``。``color=None``（默认）就是拿这个判断来决定的。
    """
    stream = sys.stdout
    if stream is None:
        return False
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def render_matrix(matrix: list[list[bool]], *, margin: int = 2,
                  color: bool | None = None, invert: bool = False) -> str:
    """把模块矩阵画成可扫描的终端文本。

    ``margin`` 是静默区（模块数）。规范要求 4，但终端里白边太浪费——一共两列就够
    手机对上焦了；你扫不出来就调大。

    ``color=True`` 用 ANSI 明确画黑白底，深色主题的终端也不会把码画反；
    ``color=False`` 退化成纯字符，适合同步到聊天窗口或日志。
    ``color=None``（默认）交给 :func:`terminal_supports_color` 判断——真终端上色，
    重定向/管道里退成纯字符。
    """
    if color is None:
        color = terminal_supports_color()
    if not matrix:
        raise QrError("空的模块矩阵")

    n = len(matrix)
    grid = [[False] * (n + margin * 2) for _ in range(n + margin * 2)]
    for r, row in enumerate(matrix):
        for c, dark in enumerate(row):
            grid[r + margin][c + margin] = dark
    if invert:
        grid = [[not cell for cell in row] for row in grid]

    size = len(grid)
    lines: list[str] = []
    # 上边距按半格对齐：一对一对地取行
    for top in range(0, size, 2):
        upper = grid[top]
        lower = grid[top + 1] if top + 1 < size else [False] * size
        if color:
            lines.append(_color_line(upper, lower))
        else:
            lines.append(_plain_line(upper, lower))
    return "\n".join(lines)


def _color_line(upper: list[bool], lower: list[bool]) -> str:
    """一个 ``▀`` 顶两个模块：前景画上半格，背景透出下半格。

    ``▀`` 是 **U+2580 UPPER HALF BLOCK**——它的字形只占字符格的上半部分，用**前景**色
    绘制；字符格的下半部分露出**背景**色。所以

    * ``fg`` 必须取 ``upper``（上半模块）的颜色，
    * ``bg`` 必须取 ``lower``（下半模块）的颜色。

    写反了不会报错，只会让屏幕上的二维码每两行颠倒一次：定位图案照样方方正正，
    看着完全正常，但手机扫不出来。这类错误只能靠「把打印出来的字符反解回矩阵、
    再和原图逐格比对」发现，别凭肉眼判断。
    """
    parts: list[str] = []
    current = None
    for i, top in enumerate(upper):
        bottom = lower[i] if i < len(lower) else False
        key = (top, bottom)
        if key != current:
            fg = _BLACK_FG if top else _WHITE_FG
            bg = _BLACK_BG if bottom else _WHITE_BG
            parts.append(f"\x1b[{fg};{bg}m")
            current = key
        parts.append(_UPPER_HALF)
    parts.append("\x1b[0m")
    return "".join(parts)


def _plain_line(upper: list[bool], lower: list[bool]) -> str:
    """无颜色时用半块字符表示：█ 全黑、▀ 上黑、▄ 下黑、空格全白。"""
    out: list[str] = []
    for i, top in enumerate(upper):
        bottom = lower[i] if i < len(lower) else False
        if top and bottom:
            out.append("\u2588")
        elif top:
            out.append("\u2580")
        elif bottom:
            out.append("\u2584")
        else:
            out.append(" ")
    return "".join(out).rstrip() or " "


def render_png(data: bytes, **kwargs) -> str:
    """一步到位：PNG 字节 → 终端文本。"""
    return render_matrix(matrix_from_png(data), **kwargs)
