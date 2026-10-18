# -*- coding: utf-8 -*-
"""生成应用图标，不依赖任何外部素材。

输出 assets/icon.png (1024, 给 README 用) 和 assets/icon.ico (多尺寸, 给 exe 用)。
画法：4 倍超采样后用 LANCZOS 缩到 1024，所以边框是干净的抗锯齿。
"""
from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 1024
SS = 4  # 超采样
W = SIZE * SS

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"

C1 = (79, 70, 229)    # indigo-600
C2 = (139, 92, 246)   # violet-500
WHITE = (255, 255, 255, 255)


def gradient(size: int) -> Image.Image:
    """对角线渐变。小图生成再放大，避免上千万次 Python 循环。"""
    small = Image.new("RGB", (256, 256))
    px = small.load()
    for y in range(256):
        for x in range(256):
            t = (x + y) / 510
            px[x, y] = tuple(round(C1[i] + (C2[i] - C1[i]) * t) for i in range(3))
    return small.resize((size, size), Image.BICUBIC)


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)

    base = gradient(W).convert("RGBA")

    # 圆角方形蒙版（iOS 风格圆角比例）
    mask = Image.new("L", (W, W), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, W - 1, W - 1], radius=int(W * 0.235), fill=255
    )
    icon = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    icon.paste(base, (0, 0), mask)
    d = ImageDraw.Draw(icon)

    # 进度环：留 3/4 缺口
    cx = cy = W / 2
    r = W * 0.290
    lw = int(W * 0.075)
    box = [cx - r, cy - r, cx + r, cy + r]
    d.ellipse(box, outline=(255, 255, 255, 58), width=lw)
    d.arc(box, start=-90, end=180, fill=WHITE, width=lw)

    # 中心三根递增柱
    heights = (0.42, 0.64, 0.88)
    bw = W * 0.072
    gap = W * 0.045
    span = len(heights) * bw + (len(heights) - 1) * gap
    x0 = cx - span / 2
    baseline = cy + W * 0.130
    max_h = W * 0.265
    for i, h in enumerate(heights):
        hh = max_h * h
        x = x0 + i * (bw + gap)
        d.rounded_rectangle(
            [x, baseline - hh, x + bw, baseline], radius=bw / 2.2, fill=WHITE
        )

    icon = icon.resize((SIZE, SIZE), Image.LANCZOS)
    icon.save(ASSETS / "icon.png")
    icon.save(
        ASSETS / "icon.ico",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print(f"已生成 {ASSETS / 'icon.png'}")
    print(f"已生成 {ASSETS / 'icon.ico'}")


if __name__ == "__main__":
    main()
