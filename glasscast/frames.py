"""截屏源、缩放/裁剪到眼镜画布、JPEG/PNG 编码、画面摘要与预算检查。

关键平台约束：

- 画布默认 640×290，等于 S1 一屏（research.md §2.3）。
- 远程图预算（WASM 模拟器的编写期检查，超限即"上不了真机"的信号，research.md §2.3；
  官方文档第 31～36 节"远端图"限制表）：解码后像素字节 ≤ 742 400 B（640×290×4，ARGB8888），
  文件 ≤ 1 MiB。文档的折算是 PNG/GIF 最大 640×290、JPEG 最大约 640×386，
  即 JPEG 按 3 字节/像素计、PNG 按 4 字节/像素计。
- 必须在服务端先缩到 ≤ 画布尺寸再给眼镜，``image-fit="stretch"`` 救不了（research.md §2.3）。
- 眼镜屏幕是**单色绿**（画布像素 R=B=0，G=亮度），颜色没有意义；8 位灰度图显示正常且体积小得多
  （灰度 JPEG ≈ 彩色的 70%，灰度 PNG ≈ RGBA 的 1/3，research.md §9.2 E6）。所以默认 ``color_mode="gray"``：
  :func:`fit_to_canvas` 之后用 :func:`apply_color_mode` 转成 ``L``，:func:`encode_jpeg` / :func:`encode_png`
  对 ``L`` 输入直接输出单通道灰度文件；``rgb`` 仅作对照/调试选项保留。
- 帧合集 ``<frame-set>`` 无法换段，只能当**定时器**驱动 ``<image>`` 亚秒级轮询（research.md §9.2 E4、§9.3），
  :func:`build_timer_fset` 生成那个固定不变的最小 `.fset`（2 帧 8×8 灰度 PNG）。

测试只用 :class:`StaticSource` / :class:`SyntheticSource`；:class:`ScreenSource` 依赖 macOS
屏幕录制权限，测试里通过注入假的 mss 工厂来验证，不真截屏。
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from functools import lru_cache
from typing import Any, Literal, Protocol, get_args, runtime_checkable

from PIL import Image, ImageDraw, ImageFont

from .fset import build_fset

__all__ = [
    "CANVAS",
    "COLOR_MODES",
    "FILE_BUDGET_BYTES",
    "PIXEL_BUDGET_BYTES",
    "TIMER_FRAMES",
    "TIMER_SIZE",
    "ColorMode",
    "FitMode",
    "FrameSource",
    "GIF_LEVELS",
    "JPEG_ENCODERS",
    "JpegEncoder",
    "ScreenSource",
    "StaticSource",
    "SyntheticSource",
    "apply_color_mode",
    "build_timer_fset",
    "check_budget",
    "encode_jpeg",
    "encode_gif",
    "encode_jpeg_target",
    "encode_png",
    "find_mozjpeg",
    "fit_to_canvas",
    "frame_digest",
    "list_monitors",
    "load_font",
    "placeholder_image",
]

log = logging.getLogger(__name__)

#: 默认画布 (宽, 高)，等于 S1 一屏预算（research.md §2.3）
CANVAS: tuple[int, int] = (640, 290)
#: 远端图解码后像素字节上限：一屏 ARGB8888 = 640×290×4（research.md §2.3）
PIXEL_BUDGET_BYTES = 742_400
#: 远端图文件字节上限 1 MiB（research.md §2.3）
FILE_BUDGET_BYTES = 1_048_576

#: 定时器 `.fset` 的帧数与帧尺寸：页面 ``<frame-set total-frames="2" width="8" height="8">`` 必须与之一致
#: （`.fset` 文件头里没有帧尺寸，全靠 XML 声明，research.md §4；定时器用法见 research.md §9.2 E4）
TIMER_FRAMES = 2
TIMER_SIZE: tuple[int, int] = (8, 8)

FitMode = Literal["fit", "crop"]
#: 输出色彩：gray = 8 位灰度（默认，眼镜屏幕是单色绿，research.md §9.2 E6）；rgb = 彩色（对照/调试用）
ColorMode = Literal["gray", "rgb"]
COLOR_MODES: tuple[str, ...] = get_args(ColorMode)
#: JPEG 编码器：auto = 装了 mozjpeg 就用它，否则 Pillow（libjpeg-turbo）；research.md §9.21
JpegEncoder = Literal["auto", "mozjpeg", "pillow"]
JPEG_ENCODERS: tuple[str, ...] = get_args(JpegEncoder)
#: 找 mozjpeg 的 cjpeg 的位置（Homebrew 的 mozjpeg 是 keg-only，不在 PATH 上）；环境变量 GLASSCAST_MOZJPEG 优先
MOZJPEG_CANDIDATES: tuple[str, ...] = ("/opt/homebrew/opt/mozjpeg/bin/cjpeg", "/usr/local/opt/mozjpeg/bin/cjpeg")
#: mozjpeg 量化表：2 = Mannos-Sakrison 心理视觉表（research.md §9.21：同等 ssimulacra2 体积比 Pillow 小 16～25%）
MOZJPEG_QUANT_TABLE = 2
#: GIF 灰度级数（均匀分档、不抖动；research.md §9.24）
GIF_LEVELS: tuple[int, ...] = (2, 4, 8, 16)
RGB = tuple[int, int, int]
Font = ImageFont.FreeTypeFont | ImageFont.ImageFont

# 缩放用 LANCZOS（指南 §3.2）。reducing_gap 先用整数倍 reduce 粗缩再做 LANCZOS：
# 实测把 Retina 全屏缩到画布高度，2.0 比不设快约 2.6 倍（28 ms → 11 ms），
# 与精确 LANCZOS 的平均差 < 0.5 灰阶；3.0 在这个缩放比下不会触发 reduce，没有加速。
_RESAMPLE = Image.Resampling.LANCZOS
_REDUCING_GAP = 2.0


# --------------------------------------------------------------------------- 帧源


@runtime_checkable
class FrameSource(Protocol):
    """帧源：每次 :meth:`grab` 返回一张 RGB 图（尺寸任意，由调用方 fit 到画布）。

    实现可以额外提供 ``close()``；:class:`~glasscast.pipeline.CastPipeline` 在
    ``close_source=True`` 时会调用它。
    """

    def grab(self) -> Image.Image: ...


MssFactory = Callable[[], Any]


def _default_mss_factory() -> Any:
    # 延迟导入：没有 mss 或没有显示环境时，frames 模块仍可导入（测试/合成源不需要它）
    import mss

    # mss 10.2 起推荐直接用 mss.MSS，旧工厂 mss.mss() 会发 DeprecationWarning、将来会移除；
    # 9.x 没有 mss.MSS（只有各平台的实现类），只能用 mss.mss()。两者返回的对象接口相同（monitors / grab / close）。
    factory = getattr(mss, "MSS", None) or mss.mss
    return factory()


def list_monitors(mss_factory: MssFactory | None = None) -> list[dict[str, int]]:
    """列出 mss 看到的显示器。下标 0 是所有显示器的并集，1 起是各个显示器。

    坐标系与 mss 一致：macOS 上是逻辑点（Retina 下截出来的图是物理像素，约 2 倍）。
    """
    factory = mss_factory or _default_mss_factory
    sct = factory()
    try:
        return [dict(m) for m in sct.monitors]
    finally:
        sct.close()


class ScreenSource:
    """用 mss 截屏。

    - ``monitor``：mss 的显示器下标，1 = 主屏，0 = 所有显示器拼成的大图。
    - ``region``：``(left, top, width, height)``，**相对所选显示器左上角**，单位与 mss 坐标系相同
      （macOS 为逻辑点；mss 在 Retina 上按物理像素返回图像，所以无需自己乘缩放比）。
      区域会被裁到显示器范围内，完全落在外面则报错。主屏（monitor=1）左上角就是 (0, 0)，
      此时 region 等同于全局屏幕坐标。
    - mss 实例不能跨线程共享：每个调用线程在第一次 :meth:`grab` 时惰性创建自己的实例
      （``threading.local``），:meth:`close` 统一释放。

    macOS 需要"屏幕录制"权限：首次截屏会弹系统授权；被拒绝时 mss 可能抛
    ``ScreenShotError``，也可能只截到桌面壁纸/黑屏（无法可靠区分，交给上层提示）。
    """

    def __init__(
        self,
        monitor: int = 1,
        region: tuple[int, int, int, int] | None = None,
        *,
        mss_factory: MssFactory | None = None,
    ) -> None:
        if monitor < 0:
            raise ValueError(f"monitor 必须 ≥ 0: {monitor}")
        if region is not None:
            if len(region) != 4:
                raise ValueError(f"region 应为 (left, top, width, height): {region!r}")
            if region[2] <= 0 or region[3] <= 0:
                raise ValueError(f"region 宽高必须为正: {region!r}")
        self.monitor = monitor
        self.region = tuple(int(v) for v in region) if region is not None else None
        self._factory: MssFactory = mss_factory or _default_mss_factory
        self._local = threading.local()
        self._lock = threading.Lock()
        self._instances: list[Any] = []

    def _sct(self) -> Any:
        sct = getattr(self._local, "sct", None)
        if sct is None:
            sct = self._factory()
            self._local.sct = sct
            with self._lock:
                self._instances.append(sct)
        return sct

    def capture_rect(self) -> dict[str, int]:
        """解析出本次要截的矩形（mss 的 monitor 字典）。"""
        monitors = self._sct().monitors
        if self.monitor >= len(monitors):
            raise ValueError(f"monitor={self.monitor} 不存在；可用下标 0～{len(monitors) - 1}（0 为全部显示器）")
        mon = monitors[self.monitor]
        if self.region is None:
            return {k: int(mon[k]) for k in ("left", "top", "width", "height")}
        rl, rt, rw, rh = self.region
        left = max(mon["left"] + rl, mon["left"])
        top = max(mon["top"] + rt, mon["top"])
        right = min(mon["left"] + rl + rw, mon["left"] + mon["width"])
        bottom = min(mon["top"] + rt + rh, mon["top"] + mon["height"])
        if right <= left or bottom <= top:
            raise ValueError(f"region={self.region!r} 完全落在显示器 {self.monitor}（{mon!r}）之外")
        return {"left": left, "top": top, "width": right - left, "height": bottom - top}

    def grab(self) -> Image.Image:
        shot = self._sct().grab(self.capture_rect())
        # mss 给的是 BGRA 原始字节（第 4 字节无意义），直接按 BGRX 解成 RGB，避免逐像素转换
        return Image.frombytes("RGB", tuple(shot.size), bytes(shot.bgra), "raw", "BGRX")

    def close(self) -> None:
        """释放所有线程创建的 mss 实例；可重复调用。之后再 grab 会重新创建。"""
        with self._lock:
            instances, self._instances = self._instances, []
        for sct in instances:
            try:
                sct.close()
            except Exception:  # noqa: BLE001 - 释放资源时不让异常外泄
                log.debug("关闭 mss 实例失败", exc_info=True)
        self._local = threading.local()


class StaticSource:
    """测试用：固定返回一张图，或按顺序循环返回一组图。

    每次返回副本（RGB），调用方修改返回值不会影响后续帧。``loop=False`` 时播完停在最后一张。
    """

    def __init__(self, images: Image.Image | Sequence[Image.Image], *, loop: bool = True) -> None:
        seq = [images] if isinstance(images, Image.Image) else list(images)
        if not seq:
            raise ValueError("StaticSource 至少需要一张图")
        self._images = [im.convert("RGB") for im in seq]
        self._loop = loop
        self._index = 0
        self._lock = threading.Lock()
        self.grabs = 0

    def grab(self) -> Image.Image:
        with self._lock:
            im = self._images[self._index]
            if self._index + 1 < len(self._images):
                self._index += 1
            elif self._loop:
                self._index = 0
            self.grabs += 1
        return im.copy()


class SyntheticSource:
    """测试/演示用：画一张带自增计数器、时间戳和移动色块的图，便于肉眼确认刷新。

    每次 :meth:`grab` 计数器 +1，所以相邻两帧一定不同。``clock`` 可注入，便于得到确定的画面。
    不需要屏幕录制权限，``glasscast serve --source synthetic`` 联调用。
    """

    def __init__(
        self,
        size: tuple[int, int] = CANVAS,
        *,
        title: str = "glasscast synthetic",
        clock: Callable[[], float] = time.time,
        start: int = 0,
    ) -> None:
        _check_size(size, "size")
        self.size = (int(size[0]), int(size[1]))
        self.title = title
        self._clock = clock
        self._lock = threading.Lock()
        self.count = start

    def grab(self) -> Image.Image:
        with self._lock:
            self.count += 1
            n = self.count
        now = self._clock()
        w, h = self.size
        im = Image.new("RGB", self.size, (12, 16, 24))
        draw = ImageDraw.Draw(im)

        # 顶部移动色块：位置和颜色随计数器变化，低帧率下也能看出在动
        bar_w = max(4, w // 10)
        x = (n * max(1, w // 32)) % max(1, w - bar_w + 1)
        hue = (n * 37) % 256
        draw.rectangle((x, 0, x + bar_w - 1, max(1, h // 20)), fill=(hue, 255 - hue, 160))
        # 外框：便于在眼镜上确认画布边缘没有被裁掉
        draw.rectangle((0, 0, w - 1, h - 1), outline=(80, 160, 255), width=max(1, h // 145))

        stamp = datetime.fromtimestamp(now).strftime("%H:%M:%S.") + f"{int(now * 1000) % 1000:03d}"
        big = load_font(max(8, h // 4), cjk=not self.title.isascii())
        mid = load_font(max(8, h // 7))
        small = load_font(max(8, h // 12), cjk=not self.title.isascii())
        _draw_centered(draw, (w // 2, h * 40 // 100), f"#{n:06d}", big, (255, 255, 255))
        _draw_centered(draw, (w // 2, h * 68 // 100), stamp, mid, (120, 220, 255))
        _draw_centered(draw, (w // 2, h * 88 // 100), self.title, small, (150, 150, 150))
        return im


def placeholder_image(
    message: str = "等待画面…",
    canvas: tuple[int, int] = CANVAS,
    *,
    detail: str | None = "waiting for screen",
    bg: RGB = (0, 0, 0),
) -> Image.Image:
    """画一张占位图（服务端无帧时用）。中文用系统 CJK 字体，找不到时下方的英文 ``detail`` 仍可读。"""
    _check_size(canvas, "canvas")
    w, h = canvas
    im = Image.new("RGB", canvas, bg)
    draw = ImageDraw.Draw(im)
    draw.rectangle((0, 0, w - 1, h - 1), outline=(60, 60, 60), width=max(1, h // 145))
    _draw_centered(draw, (w // 2, h * 42 // 100), message, load_font(max(8, h // 6), cjk=True), (230, 230, 230))
    if detail:
        _draw_centered(draw, (w // 2, h * 70 // 100), detail, load_font(max(8, h // 12)), (140, 140, 140))
    return im


# --------------------------------------------------------------------------- 字体

# 拉丁/数字字体候选：Pillow 自带字体优先（跨机器一致），其次各平台系统等宽字体
_LATIN_FONT_CANDIDATES: tuple[str, ...] = (
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/SFNSMono.ttf",
    "/System/Library/Fonts/Monaco.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
    "C:/Windows/Fonts/consola.ttf",
)
# 中文字体候选：Pillow 自带字体不含 CJK 字形
_CJK_FONT_CANDIDATES: tuple[str, ...] = (
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "C:/Windows/Fonts/msyh.ttc",
)


@lru_cache(maxsize=32)
def load_font(size: int, *, cjk: bool = False) -> Font:
    """加载指定字号的字体，逐级回退，永不抛异常。

    - ``cjk=False``：Pillow 自带字体（需 FreeType）→ 系统等宽字体 → Pillow 位图字体。
    - ``cjk=True``：系统中文字体 → 同上（此时中文会显示成方框，但不会崩）。
    """
    candidates = _CJK_FONT_CANDIDATES if cjk else ()
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        font = ImageFont.load_default(size=size)
        if isinstance(font, ImageFont.FreeTypeFont):
            return font
    except Exception:  # noqa: BLE001 - 无 FreeType 等情况，继续回退
        pass
    for path in _LATIN_FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    log.debug("没有可用的 TrueType 字体，退回 Pillow 位图字体")
    return ImageFont.load_default()


def _draw_centered(draw: ImageDraw.ImageDraw, center: tuple[int, int], text: str, font: Font, fill: RGB) -> None:
    if not isinstance(font, ImageFont.FreeTypeFont):
        # 最后一级回退的位图字体只支持 Latin-1，中文会抛 UnicodeEncodeError；换成 "?" 保证不崩
        text = text.encode("latin-1", "replace").decode("latin-1")
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    x = center[0] - (right - left) // 2 - left
    y = center[1] - (bottom - top) // 2 - top
    draw.text((x, y), text, font=font, fill=fill)


# --------------------------------------------------------------------------- 缩放


def _check_size(size: tuple[int, int], name: str) -> None:
    if len(size) != 2 or int(size[0]) <= 0 or int(size[1]) <= 0:
        raise ValueError(f"{name} 必须是正的 (宽, 高): {size!r}")


def fit_to_canvas(
    im: Image.Image,
    canvas: tuple[int, int] = CANVAS,
    mode: FitMode = "fit",
    bg: RGB = (0, 0, 0),
) -> Image.Image:
    """把任意尺寸的图变成恰好 ``canvas`` 大小的 RGB 图。

    - ``fit``：等比缩放到能完整放进画布，居中贴到 ``bg`` 底色上（letterbox / pillarbox）。
    - ``crop``：先按画布宽高比居中裁剪，再缩放到画布，画面铺满、无黑边。

    输出尺寸恒等于 ``canvas``；极端宽高比（如 10000×1）也只会得到一条 ≥1 像素的内容。
    输出恒为 RGB；要灰度再接 :func:`apply_color_mode`（缩放在 RGB 上做，转灰度放最后，结果与先转灰度再缩放
    只差舍入，但保持本函数"输出 RGB"的约定不变）。
    """
    _check_size(canvas, "canvas")
    if mode not in ("fit", "crop"):
        raise ValueError(f"mode 必须是 'fit' 或 'crop': {mode!r}")
    cw, ch = int(canvas[0]), int(canvas[1])
    src = im if im.mode == "RGB" else im.convert("RGB")
    w, h = src.size
    if w <= 0 or h <= 0:
        raise ValueError(f"输入图尺寸非法: {src.size!r}")
    if (w, h) == (cw, ch):
        return src.copy() if src is im else src

    if mode == "crop":
        target = cw / ch
        if w / h > target:
            crop_w = min(w, max(1, round(h * target)))
            box = ((w - crop_w) / 2, 0.0, (w + crop_w) / 2, float(h))
        else:
            crop_h = min(h, max(1, round(w / target)))
            box = (0.0, (h - crop_h) / 2, float(w), (h + crop_h) / 2)
        return src.resize((cw, ch), _RESAMPLE, box=box, reducing_gap=_REDUCING_GAP)

    scale = min(cw / w, ch / h)
    nw = min(cw, max(1, round(w * scale)))
    nh = min(ch, max(1, round(h * scale)))
    scaled = src if (nw, nh) == (w, h) else src.resize((nw, nh), _RESAMPLE, reducing_gap=_REDUCING_GAP)
    out = Image.new("RGB", (cw, ch), tuple(bg))
    out.paste(scaled, ((cw - nw) // 2, (ch - nh) // 2))
    return out


def apply_color_mode(im: Image.Image, color_mode: ColorMode = "gray") -> Image.Image:
    """按输出色彩转换：``gray`` → 8 位灰度 ``L``（ITU-R 601 亮度），``rgb`` → ``RGB``。

    眼镜屏幕是单色绿、只显示亮度（research.md §9.2 E6），灰度不损失任何可见信息，体积却小得多。
    已经是目标模式时原样返回（不复制）。
    """
    if color_mode not in COLOR_MODES:
        raise ValueError(f"color_mode 必须是 'gray' 或 'rgb': {color_mode!r}")
    target = "L" if color_mode == "gray" else "RGB"
    return im if im.mode == target else im.convert(target)


# --------------------------------------------------------------------------- 编码


@lru_cache(maxsize=1)
def find_mozjpeg() -> str | None:
    """mozjpeg 的 ``cjpeg`` 路径；没装（或找到的是 libjpeg-turbo 的 cjpeg）时返回 None。结果缓存。"""
    env = os.environ.get("GLASSCAST_MOZJPEG", "").strip()
    for path in ((env,) if env else ()) + MOZJPEG_CANDIDATES:
        if not os.access(path, os.X_OK):
            continue
        try:
            out = subprocess.run([path, "-version"], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        if "mozjpeg" in (out.stdout + out.stderr).lower():
            return path
    return None


_mozjpeg_failed = False


def _encode_mozjpeg(cjpeg: str, im: Image.Image, quality: int, subsampling: int) -> bytes:
    """经 stdin/stdout 调 mozjpeg：基线、优化霍夫曼、trellis 量化（默认开）、量化表 2。"""
    if im.mode == "L":
        header, extra = b"P5", []
    else:
        im = im if im.mode == "RGB" else im.convert("RGB")
        header, extra = b"P6", ["-sample", {2: "2x2", 1: "2x1", 0: "1x1"}[subsampling]]
    pnm = header + f"\n{im.width} {im.height}\n255\n".encode() + im.tobytes()
    cmd = [cjpeg, "-baseline", "-quality", str(int(quality)), "-quant-table", str(MOZJPEG_QUANT_TABLE), *extra]
    return subprocess.run(cmd, input=pnm, capture_output=True, check=True, timeout=10).stdout


def encode_jpeg(im: Image.Image, quality: int = 70, *, subsampling: int = 2, encoder: JpegEncoder = "auto") -> bytes:
    """编码为基线 JPEG（关闭 progressive，嵌入端解码器对渐进式支持不确定）。

    - 输入是 ``L``（灰度）时输出单通道灰度 JPEG（模拟器实测正常显示，research.md §9.2 E6），
      ``subsampling`` 对它没有意义、被忽略；其余模式先转 RGB。
    - ``subsampling``：2 = 4:2:0（默认，体积小），1 = 4:2:2，0 = 4:4:4（彩色小字更清晰，体积约大 30%）。
    - ``encoder``：``auto``（默认）装了 mozjpeg 就用它（trellis 量化 + 心理视觉量化表，同等观感体积小 16～25%，
      research.md §9.21），否则 Pillow；``mozjpeg`` 没装时报错；mozjpeg 运行出错时记一次警告并改用 Pillow。
      同一个 ``quality`` 数值两个编码器的画质和体积不同，换编码器后要按体积重新选质量。
    - Pillow 开 ``optimize``（按本帧统计重算霍夫曼表）：画质不变、仍是基线 JPEG，体积小 10～15%（research.md §9.20）。
    """
    global _mozjpeg_failed
    if not 1 <= int(quality) <= 100:
        raise ValueError(f"JPEG quality 应在 1～100: {quality}")
    if subsampling not in (0, 1, 2):
        raise ValueError(f"subsampling 应为 0/1/2: {subsampling}")
    if encoder not in JPEG_ENCODERS:
        raise ValueError(f"encoder 应为 {' / '.join(JPEG_ENCODERS)}: {encoder!r}")
    if encoder != "pillow" and not (encoder == "auto" and _mozjpeg_failed):
        cjpeg = find_mozjpeg()
        if cjpeg is None and encoder == "mozjpeg":
            raise RuntimeError("没找到 mozjpeg 的 cjpeg：brew install mozjpeg，或用环境变量 GLASSCAST_MOZJPEG 指定路径")
        if cjpeg is not None:
            try:
                return _encode_mozjpeg(cjpeg, im, quality, subsampling)
            except (OSError, subprocess.SubprocessError) as exc:
                if encoder == "mozjpeg":
                    raise
                _mozjpeg_failed = True
                log.warning("mozjpeg 编码失败（%s），之后改用 Pillow", exc)
    buf = io.BytesIO()
    if im.mode == "L":
        im.save(buf, format="JPEG", quality=int(quality), progressive=False, optimize=True)
        return buf.getvalue()
    src = im if im.mode == "RGB" else im.convert("RGB")
    src.save(buf, format="JPEG", quality=int(quality), progressive=False, optimize=True, subsampling=subsampling)
    return buf.getvalue()


def encode_jpeg_target(
    im: Image.Image,
    max_bytes: int,
    *,
    quality_max: int = 90,
    quality_min: int = 10,
    subsampling: int = 2,
    encoder: JpegEncoder = "auto",
) -> tuple[bytes, int]:
    """在 ``quality_min``～``quality_max`` 里二分，找体积 ≤ ``max_bytes`` 的最高质量，返回 ``(JPEG, 质量)``。

    每张图编码约 7 次（mozjpeg 480×218 一次约 5 ms）。体积随质量基本单调；最低质量仍超标时返回最低质量那张。
    流畅模式每张图越小越不容易卡（research.md §9.20），定体积能让复杂画面自动降质量、简单画面用更高质量（§9.22）。
    """
    if max_bytes <= 0:
        raise ValueError(f"max_bytes 应为正数: {max_bytes}")
    if not 1 <= quality_min <= quality_max <= 100:
        raise ValueError(f"需要 1 ≤ quality_min ≤ quality_max ≤ 100: {quality_min}, {quality_max}")
    lo, hi = int(quality_min), int(quality_max)
    best: tuple[bytes, int] | None = None
    while lo <= hi:
        q = (lo + hi) // 2
        data = encode_jpeg(im, q, subsampling=subsampling, encoder=encoder)
        if len(data) <= max_bytes:
            best, lo = (data, q), q + 1
        else:
            hi = q - 1
    if best is None:
        return encode_jpeg(im, int(quality_min), subsampling=subsampling, encoder=encoder), int(quality_min)
    return best


def encode_png(im: Image.Image, compress_level: int = 3, *, alpha: bool = False) -> bytes:
    """编码为 PNG，默认压缩级别 3（速度优先）。

    - ``alpha=True`` 输出 8-bit RGBA（alpha 恒为 255）——官方 `.fset` 样本的帧就是这种格式
      （research.md §4）；RGB PNG 能否被眼镜端解码未实测。
    - ``alpha=False`` 且输入是 ``L`` 时输出 8 位灰度 PNG（色彩类型 0）：模拟器实测正常显示，体积约为
      RGBA 的 1/3（research.md §9.2 E6）；其余模式输出 RGB。
    """
    if not 0 <= int(compress_level) <= 9:
        raise ValueError(f"PNG compress_level 应在 0～9: {compress_level}")
    if alpha:
        target_mode = "RGBA"
    else:
        target_mode = "L" if im.mode == "L" else "RGB"
    src = im if im.mode == target_mode else im.convert(target_mode)
    buf = io.BytesIO()
    src.save(buf, format="PNG", compress_level=int(compress_level), optimize=False)
    return buf.getvalue()


@lru_cache(maxsize=len(GIF_LEVELS))
def _gray_levels_lut(levels: int) -> tuple[int, ...]:
    step = 255 / (levels - 1)
    return tuple(round(round(v / step) * step) for v in range(256))


def encode_gif(im: Image.Image, levels: int = 4, delay_ms: int = 100) -> bytes:
    """编码为单帧灰度 GIF：先转灰度，再均匀分成 ``levels`` 级（不抖动，文字边缘干净、LZW 压得好）。

    - 非隔行（Pillow 默认隔行，嵌入端解码器支持不确定，同 JPEG 关 progressive）；
    - ``delay_ms`` 写进图形控制扩展（GIF 以 10 ms 为单位，必须是 10 的倍数，0 = 不写帧时长）；
      ``<gif repeat-count="1" on-completed>`` 页面靠它控制每张至少显示多久（research.md §9.24）；
    - 不写 NETSCAPE 循环块：只播一遍由页面的 ``repeat-count`` 决定。
    """
    if levels not in GIF_LEVELS:
        raise ValueError(f"GIF 灰度级数应为 {' / '.join(map(str, GIF_LEVELS))}: {levels!r}")
    if not (isinstance(delay_ms, int) and 0 <= delay_ms <= 655_350 and delay_ms % 10 == 0):
        raise ValueError(f"GIF 帧时长应为 0～655350 之间 10 的倍数（毫秒）: {delay_ms!r}")
    gray = im if im.mode == "L" else im.convert("L")
    buf = io.BytesIO()
    options: dict[str, Any] = {"interlace": False}
    if delay_ms:
        options["duration"] = delay_ms
    gray.point(_gray_levels_lut(levels)).save(buf, format="GIF", **options)
    return buf.getvalue()


@lru_cache(maxsize=8)
def build_timer_fset(frames: int = TIMER_FRAMES, size: tuple[int, int] = TIMER_SIZE) -> bytes:
    """页面定时器用的最小 `.fset`：``frames`` 帧 ``size`` 大小的全黑 8 位灰度 PNG，内容固定（结果缓存）。

    用法（research.md §9.2 E4、E9；``pages/cast-fast``）：``<frame-set total-frames="2" repeat-count="1"
    action="{{d:act}}" on-completed="data://update?i={{d:i + 1}}&amp;act=start">`` 每播完一轮触发一次
    ``data://update``，重新应用全部绑定 → 帧合集从头再播、``<image src="…/{{f:timestamp()}}_{{d:i}}.jpg">``
    换地址重新下载。
    帧合集在这里只是节拍器，不是播放器：画面内容无所谓，全黑在黑底页面上看不见。

    - 帧必须是 PNG：JPEG 帧黑屏且不触发 ``on-completed``（research.md §4、§9.2 E5）；
      灰度 PNG 显示正常（research.md §9.2 E6），每帧约 70 字节。
    - 一轮时长 = ``frames × frame-duration``；glasscast 的 ``frame_duration_ms`` 按 2 帧一轮折算
      （:func:`glasscast.pipeline.fast_frame_duration_for`），所以页面的 ``total-frames`` 必须等于 ``frames``。
    - 压缩级别固定 9、不做 optimize，Pillow 不写时间戳，同一环境下逐字节稳定。
    """
    if isinstance(frames, bool) or int(frames) < 1:
        raise ValueError(f"定时器帧数至少为 1: {frames!r}")
    _check_size(size, "size")
    blob = encode_png(Image.new("L", (int(size[0]), int(size[1])), 0), 9)
    return build_fset([blob] * int(frames))


# --------------------------------------------------------------------------- 摘要与预算


def frame_digest(im: Image.Image, *, size: tuple[int, int] | None = (64, 32)) -> str:
    """画面摘要，用于"画面没变就不重编码"。

    - 默认（指南 §3.2）：缩到 64×32 灰度（BOX 均值）再哈希，对同图稳定、对不同图不同，
      但极小的局部变化（例如 1 像素光标）可能被均值抹掉。
    - ``size=None``：对全分辨率像素精确哈希（``L`` 灰度图直接哈希灰度字节，其余转 RGB）。640×290 只有
      0.5 MB，blake2b 不到 1 ms，任何像素变化都能发现；:class:`~glasscast.pipeline.CastPipeline` 用这个模式，
      避免终端里敲一个字符却一直复用旧图。
    """
    h = hashlib.blake2b(digest_size=16)
    if size is None:
        src = im if im.mode in ("L", "RGB") else im.convert("RGB")
        h.update(f"{src.mode.lower()}:{src.size[0]}x{src.size[1]}:".encode())
        h.update(src.tobytes())
    else:
        _check_size(size, "size")
        thumb = im.convert("L").resize((int(size[0]), int(size[1])), Image.Resampling.BOX)
        h.update(f"l:{thumb.size[0]}x{thumb.size[1]}:".encode())
        h.update(thumb.tobytes())
    return h.hexdigest()


def check_budget(width: int, height: int, nbytes: int, *, bytes_per_pixel: int = 4) -> list[str]:
    """检查远程图预算（research.md §2.3），返回告警列表；空列表表示没超。

    - 像素：``width*height*bytes_per_pixel > 742_400``。默认按 PNG/ARGB8888 的 4 字节；
      JPEG 传 3（文档折算 JPEG 最大约 640×386 即按 3 字节/像素）。
    - 文件：``nbytes > 1_048_576``。
    等于上限不告警。
    """
    warnings: list[str] = []
    pixel_bytes = int(width) * int(height) * int(bytes_per_pixel)
    if pixel_bytes > PIXEL_BUDGET_BYTES:
        warnings.append(
            f"解码后像素字节 {width}×{height}×{bytes_per_pixel}={pixel_bytes} 超过 {PIXEL_BUDGET_BYTES}"
            "（一屏 ARGB8888），模拟器会拒绝显示，真机显示异常或极慢；请缩小画布"
        )
    if int(nbytes) > FILE_BUDGET_BYTES:
        warnings.append(f"文件 {nbytes} 字节超过 {FILE_BUDGET_BYTES}（1 MiB），请降低质量或缩小画布")
    return warnings
