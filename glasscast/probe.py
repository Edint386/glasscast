"""真机下载/播放探针用的测试文件（research.md §9.25）。

流畅投屏卡在"每张图约 0.65 s 的固定开销"：要突破只能一次下载里装很多帧（远程 ``.fset`` / ``.gif`` 片段、
拼图 JPEG）。这些格式能不能经过手机"千问AI穿戴" App 送到眼镜、眼镜下载是不是串行、每个文件的耗时怎么随
体积变化，只有真机能回答。本模块按名字生成**内容固定、体积可控**的测试文件，探针页一次把它们全请求一遍，
看日志里的请求间隔、再问用户哪些格子在动。

名字格式 ``<kind><KB>``（服务端路由 ``/s/<key>/screen.jpg/probe/<会话>_<name>.<ext>``）：

- ``f<KB>``：20 帧 200×91 8 位灰度 PNG 的 ``.fset``，每帧烧进名字和帧号，用噪声带把总体积调到约 KB 千字节；
- ``g<KB>``：20 帧、每帧 100 ms 的 GIF（同样内容，4 级灰度以外的噪声带也保留）；
- ``i<KB>``：一张 200×91 的 JPEG，用噪声带垫到约 KB 千字节。

``.fset`` 字节也可以用 ``.jpg`` 结尾的地址取（``fj<KB>``），看手机是只认后缀还是原样转发。
"""

from __future__ import annotations

import io
import random
import re
from functools import lru_cache

from PIL import Image, ImageDraw

from .frames import load_font
from .fset import build_fset

__all__ = ["PROBE_FRAMES", "PROBE_SIZE", "PROBE_NAME_RE", "build_probe_file", "probe_media_type"]

#: 探针片段的帧数和每帧尺寸（页面 ``total-frames`` 必须等于帧数）
PROBE_FRAMES = 20
PROBE_SIZE: tuple[int, int] = (200, 91)
#: 合法名字：f/fj（.fset 字节）、g（GIF）、i（JPEG），后接 1～512 的千字节数
PROBE_NAME_RE = re.compile(r"^(fj|f|g|i)([1-9][0-9]{0,2})$")
_MAX_KB = 512


def probe_media_type(kind: str) -> str:
    return {"f": "application/octet-stream", "fj": "application/octet-stream", "g": "image/gif", "i": "image/jpeg"}[kind]


def _frame(label: str, index: int, noise_rows: int, seed: int) -> Image.Image:
    """黑底，左上大字写名字、右侧大字写帧号，底部 ``noise_rows`` 行随机灰度噪声（只为把体积垫上去）。"""
    w, h = PROBE_SIZE
    im = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(im)
    draw.text((6, 4), label, font=load_font(30), fill=255)
    draw.text((132, 4), f"{index:02d}", font=load_font(40), fill=255)
    draw.rectangle((0, 0, w - 1, h - 1), outline=170)
    if noise_rows > 0:
        rng = random.Random(seed * 1000 + index)
        band = Image.frombytes("L", (w, noise_rows), bytes(rng.randrange(256) for _ in range(w * noise_rows)))
        im.paste(band, (0, h - noise_rows))
    return im


def _png(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, format="PNG", compress_level=9)
    return buf.getvalue()


def _encode(kind: str, label: str, noise_rows: int, seed: int) -> bytes:
    if kind in ("f", "fj"):
        return build_fset([_png(_frame(label, i, noise_rows, seed)) for i in range(PROBE_FRAMES)])
    if kind == "g":
        frames = [_frame(label, i, noise_rows, seed) for i in range(PROBE_FRAMES)]
        buf = io.BytesIO()
        frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=100, interlace=False, optimize=False)
        return buf.getvalue()
    im = _frame(label, 0, noise_rows, seed)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=95, progressive=False)
    return buf.getvalue()


@lru_cache(maxsize=32)
def build_probe_file(name: str) -> bytes:
    """按名字生成探针文件（结果缓存，内容固定）。体积是噪声带行数二分出来的，目标在约 ±10% 内；
    最小体积受帧本身限制（20 帧 PNG 的 .fset 至少约 3～5 KB），目标太小时就是最小那份。"""
    m = PROBE_NAME_RE.match(name)
    if not m:
        raise ValueError(f"探针名字应为 f/fj/g/i 加 1～{_MAX_KB} 的千字节数: {name!r}")
    kind, kb = m.group(1), int(m.group(2))
    if kb > _MAX_KB:
        raise ValueError(f"探针体积最大 {_MAX_KB} KB: {name!r}")
    target = kb * 1024
    seed = sum(map(ord, name))
    lo, hi = 0, PROBE_SIZE[1] - 50  # 上面 50 行留给文字
    best = _encode(kind, name, 0, seed)
    while lo <= hi:
        rows = (lo + hi) // 2
        data = _encode(kind, name, rows, seed)
        if abs(len(data) - target) < abs(len(best) - target):
            best = data
        if len(data) < target:
            lo = rows + 1
        else:
            hi = rows - 1
    return best
