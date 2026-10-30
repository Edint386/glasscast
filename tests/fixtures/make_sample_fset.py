"""生成测试夹具 ``tests/fixtures/sample.fset``：本项目自己画的 48 帧弹球动画（32×30，8-bit RGBA PNG）。

为什么有这个文件：

- ``.fset`` 的格式（见 ``glasscast/fset.py`` 模块文档）是对照官方模拟器资源包里的 3 个样本
  （``genie_ball.fset`` 等）逐字节验证出来的，记录在 ``docs/research.md`` §4。
- 官方样本不归我们所有，不随仓库分发；这里换成一个**形状相同**的自制夹具：同样 48 帧、32×30、
  每帧都是完整的 8-bit RGBA PNG、帧首尾相接、reserved 全 0、无尾部填充（头部 4 + 12×48 = 580 字节）。

用法（在仓库根目录）::

    uv run python tests/fixtures/make_sample_fset.py            # 重新生成 sample.fset
    uv run python tests/fixtures/make_sample_fset.py --check    # 只核对：已提交的文件与画法逐像素一致

说明：

- 画面只用整数运算画出（不依赖 sin/cos 的浮点误差），像素内容完全确定。
- PNG 的压缩字节取决于 zlib 版本，换一台机器重新生成，文件字节数可能略有不同；
  测试里的精确字节数针对的是**已提交**的 ``sample.fset``，平时不需要重新生成。
  ``tests/test_fset.py`` 会逐像素核对已提交的文件与这里的画法一致。
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

from PIL import Image, ImageDraw

FRAMES = 48
SIZE = (32, 30)  # 宽 × 高，与官方样本 genie_ball.fset 相同（research.md §4）
RADIUS = 5
OUT = Path(__file__).with_name("sample.fset")


def ball_center(i: int) -> tuple[int, int]:
    """第 i 帧球心：水平方向 24 帧一个来回，竖直方向 16 帧弹一次（纯整数）。"""
    w, h = SIZE
    t = i % 24
    sweep = t if t < 12 else 24 - t  # 0..12..0
    cx = RADIUS + 1 + sweep * (w - 2 * RADIUS - 3) // 12  # 6..25
    k = i % 16
    lift = k * (16 - k)  # 0..64..0，抛物线
    floor = h - RADIUS - 1  # 24
    cy = floor - lift * (floor - RADIUS - 1) // 64  # 24..6
    return cx, cy


def draw_frame(i: int) -> Image.Image:
    """画第 i 帧：透明底、地面阴影（球越高越淡）、实心球（颜色随帧号渐变）。"""
    img = Image.new("RGBA", SIZE, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    w, h = SIZE
    cx, cy = ball_center(i)

    floor = h - RADIUS - 1
    height = floor - cy  # 0..18
    shadow_alpha = 160 - height * 6
    half = RADIUS + 1 - height // 6
    draw.ellipse((cx - half, h - 3, cx + half, h - 1), fill=(0, 0, 0, shadow_alpha))

    green = 255 - (i * 4) % 128
    draw.ellipse(
        (cx - RADIUS, cy - RADIUS, cx + RADIUS, cy + RADIUS),
        fill=(32, green, 96, 255),
        outline=(255, 255, 255, 255),
    )
    draw.point((cx - 2, cy - 2), fill=(255, 255, 255, 255))  # 高光
    return img


def draw_frames() -> list[Image.Image]:
    return [draw_frame(i) for i in range(FRAMES)]


def encode_png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False, compress_level=9)
    return buf.getvalue()


def build_sample() -> bytes:
    """按当前环境的 Pillow/zlib 重新打包（字节数可能与已提交文件略有不同，像素相同）。"""
    from glasscast.fset import build_fset

    return build_fset([encode_png(im) for im in draw_frames()])


def frames_match(data: bytes) -> bool:
    """``data`` 解析后的每一帧是否与 :func:`draw_frame` 逐像素一致（且都是 RGBA PNG）。"""
    from glasscast.fset import parse_fset

    blobs = parse_fset(data)
    if len(blobs) != FRAMES:
        return False
    for i, blob in enumerate(blobs):
        with Image.open(io.BytesIO(blob)) as im:
            if im.format != "PNG" or im.mode != "RGBA" or im.size != SIZE:
                return False
            if im.tobytes() != draw_frame(i).tobytes():
                return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成测试夹具 sample.fset")
    parser.add_argument("--out", type=Path, default=OUT, help=f"输出文件（默认 {OUT.name}）")
    parser.add_argument("--check", action="store_true", help="只核对已有文件与画法逐像素一致，不写文件")
    args = parser.parse_args(argv)

    if args.check:
        ok = args.out.exists() and frames_match(args.out.read_bytes())
        print(f"{args.out.name}: {'一致' if ok else '不一致或不存在'}")
        return 0 if ok else 1

    data = build_sample()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(data)
    print(f"已写入 {args.out}：{FRAMES} 帧 {SIZE[0]}×{SIZE[1]} RGBA PNG，{len(data):,} 字节")
    return 0


if __name__ == "__main__":
    sys.exit(main())
