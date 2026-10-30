"""采集线程：按当前模式定时截帧，发布"最新一张 JPEG"（或内部实验用的"最新一段 .fset"）。

投屏模式（research.md §1、§9.3）：

- ``image``（普通模式，方案三）：眼镜页面 ``<tick>`` 每秒换一次地址，重新下载一张 JPEG。上限 1 fps
  （所有 tick 共享一个 1000 ms 定时器，research.md §2.2），所以 ``image_fps`` 默认 1。
- ``fast``（流畅模式）：与 image 完全相同、同样产出 JPEG，只是截帧频率是 ``fast_fps``（默认 4）。
  现行眼镜页面是 ``cast-emit``（emit:// 事件倒数，每换一张图倒数 ``hops × 3`` 步，research.md §9.19），
  ``hops`` 由 MCP 层按 ``fast_fps`` 估算后下发（见 :data:`DEFAULT_EMIT_HOP_MS`）。真机上实际约每秒
  ``fast_fps / 2`` 张，设 4 是实测的稳定上限（research.md §1 最终结论、§9.20）。
  下面是旧页面 ``cast-fast`` 的机制（过不了平台保存校验 URI-001，只能在本地模拟器里用，research.md §9.4）：
  眼镜页面 ``cast-fast`` 用一个 2 帧的 `.fset` 当**定时器**（:func:`~glasscast.frames.build_timer_fset`），
  每播完一轮触发 ``data://update`` 重新下载一张图（research.md §9.2 E4、E9）：一轮 = 2 × ``frame-duration``，
  所以页面的 ``frame-duration`` 应为 :func:`fast_frame_duration_for` 算出的 ``round(1000 / fast_fps / 2)``。
  ``fast_fps`` 可以在运行中修改（:attr:`CastPipeline.fast_fps` 的 setter，MCP ``start_cast(fps=…)`` 与
  ``POST /admin/fps`` 都走它）：截帧节拍立即按新帧率重排；合法范围 :data:`FAST_FPS_MIN`～:data:`FAST_FPS_MAX`，
  超出一律拒绝（:func:`parse_fast_fps`），不静默钳制。眼镜页面的 ``frame-duration`` 只在页面打开时绑定一次
  （research.md §9.2 E9），所以页面要等下一次 ``start_cast`` 重新打开后才按新节拍拉图。
- ``clip``（方案一，**仅内部实验**）：攒满 ``clip_frames`` 帧打包成 `.fset` 发布，只保留最新一段。
  模拟器实测 ``<frame-set>`` 的 ``src`` 只在创建时求值一次、无法换段（research.md §2.4、§9.2 E2），
  所以 CLI / MCP 不再对外提供这个模式；代码和单测保留，``fset`` 子命令与 ``clip.fset`` 实验接口还在用。

输出色彩（research.md §9.2 E6）：眼镜屏幕是单色绿，默认 ``color_mode="gray"``，缩放后转 8 位灰度，
JPEG / PNG 都编码成单通道灰度文件（体积明显更小）；``rgb`` 只作对照选项。

MCP 只做控制面（research.md §2.5）：``start_cast`` 调 :meth:`CastPipeline.start`，之后的逐帧刷新
全靠眼镜页面自己拉取，本模块与 HTTP 层之间只通过 :meth:`latest_snapshot` / :meth:`latest_clip`
交换不可变的数据对象。

线程模型：一个守护线程常驻（第一次 :meth:`start` 时创建），``off`` 模式下阻塞等待、不占 CPU。
所有异常在线程内捕获、计数并指数退避（默认 1 s 起，最多 5 s），线程不会因异常退出。
每次模式切换递增"代号"（generation）：切换前已经开始的那一帧会被丢弃、攒帧缓冲清空，
因此 :meth:`stop` 返回之后不会再有任何新发布；那一帧如果失败，也不算进新模式的错误计数。
从别的模式进入 image/fast（两者共用 snapshot）或 clip 时，会丢弃上一轮残留的 snapshot/clip，
新数据产生前 ``latest_*()`` 返回 None（服务端应回占位，见 :meth:`placeholder_snapshot`）；
image 与 fast 之间互相切换时保留 snapshot（它最多是一个截帧周期之前的画面）。
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Literal

from PIL import Image

from .frames import (
    CANVAS,
    COLOR_MODES,
    FILE_BUDGET_BYTES,
    JPEG_ENCODERS,
    TIMER_FRAMES,
    ColorMode,
    FitMode,
    FrameSource,
    JpegEncoder,
    apply_color_mode,
    check_budget,
    encode_jpeg,
    encode_gif,
    encode_jpeg_target,
    encode_png,
    fit_to_canvas,
    frame_digest,
    placeholder_image,
)
from .fset import build_fset

__all__ = [
    "CLIP_COLOR_LADDER",
    "CLIP_RELAX_AFTER_SEGMENTS",
    "DEFAULT_FAST_FPS",
    "FAST_FPS_MAX",
    "FAST_FPS_MIN",
    "FAST_FPS_RANGE_TEXT",
    "FAST_FPS_WARN_ABOVE",
    "IMAGE_FRAME_DURATION_MS",
    "MAX_FRAME_DURATION_MS",
    "MIN_FRAME_DURATION_MS",
    "PUBLIC_MODES",
    "SNAPSHOT_MODES",
    "CastMode",
    "CastPipeline",
    "Clip",
    "ClipFormat",
    "FastFpsError",
    "PublicMode",
    "Snapshot",
    "fast_fps_warnings",
    "emit_hops_for",
    "DEFAULT_EMIT_HOP_MS",
    "EMIT_MAX_HOPS",
    "EMIT_HOP_MS_MIN",
    "EMIT_HOP_MS_MAX",
    "fast_frame_duration_for",
    "frame_duration_for",
    "parse_fast_fps",
]

log = logging.getLogger(__name__)

#: 采集线程支持的全部模式；``clip`` 只作内部实验保留（research.md §2.4、§9.3）
CastMode = Literal["off", "image", "fast", "clip"]
#: CLI ``--autostart`` 与 MCP ``start_cast`` 对外提供的投屏模式（实现指南 §9.1）
PublicMode = Literal["image", "fast"]
PUBLIC_MODES: tuple[str, ...] = ("image", "fast")
#: 产出 JPEG snapshot 的模式：两者共用 ``latest_snapshot``，只是截帧频率不同
SNAPSHOT_MODES: frozenset[str] = frozenset(PUBLIC_MODES)
ClipFormat = Literal["png", "jpeg"]

#: ``<frame-set>`` 的 ``frame-duration`` 最大 1000 ms，超过被截断（官方文档第 36 节 §3.3 帧合集；research.md §2.4）
MAX_FRAME_DURATION_MS = 1000
#: ``frame-duration`` 的下限 41 ms：官方文档第 36 节 §3.3 帧合集属性表写的是"默认 41"，没写更小的值是否被接受，
#: 这里按下限处理（保守），运行时改帧率时 round(1000/fps/2) 低于它一律拒绝（research.md §2.4）
MIN_FRAME_DURATION_MS = 41
#: fast 模式默认截帧频率：模拟器里帧合集定时器可到 4～5 fps（research.md §9.2 E4、§9.3），真机受带宽限制
DEFAULT_FAST_FPS = 4.0
#: 换算 hops 用的每跳毫秒数：hops = round(1000 / fast_fps / emit_hop_ms)。这个换算是按第一版 emit 自环页面
#: （每 hops 跳换一张图，research.md §9.8）定的；现行 cast-emit 页面每换一张图倒数 hops × 3 步（research.md §9.19），
#: 真机每步约 13 ms 外加每张图的下载和蓝牙开销，所以真机实际张数低于 fast_fps（设 4 约每秒 1.5 张，
#: 是实测的稳定上限）。可以用 GLASSCAST_EMIT_HOP_MS / --emit-hop-ms 调整，不用改页面。
DEFAULT_EMIT_HOP_MS = 20.0
EMIT_HOP_MS_MIN = 5.0
EMIT_HOP_MS_MAX = 500.0
#: start_cast 下发的 hops 上限；cast-emit 页面每换一张图倒数 hops × 3 步，缺 hops 时也按 25（75 步）。
#: 低于下限的 fast_fps 只降低截帧频率，页面拉图仍按下限（出参 page_fps 报告的是按 emit_hop_ms 估算的值）
EMIT_MAX_HOPS = 25
#: 流畅模式帧率的合法范围（每秒张数，含端点，可以带小数）。上限 10 → frame_duration_ms 50，仍高于
#: :data:`MIN_FRAME_DURATION_MS`；下限 1 → 500，与普通模式（每秒 1 张）持平，再低就没有"流畅"可言。
#: 启动参数 ``--fast-fps``、MCP ``start_cast(fps=…)``、``POST /admin/fps`` 共用这一个范围
FAST_FPS_MIN = 1.0
FAST_FPS_MAX = 10.0
#: 念给用户 / 打印给人看的范围写法
FAST_FPS_RANGE_TEXT = f"{FAST_FPS_MIN:g}～{FAST_FPS_MAX:g}"
#: fast_fps 超过它就提醒：设 4 是真机实测的稳定上限，4.5 以上可能卡住（research.md §9.20）
FAST_FPS_WARN_ABOVE = 4.0
#: image 模式页面的刷新间隔：``<tick refresh="second">`` 每秒一次（research.md §2.2），作为 start_cast 出参
IMAGE_FRAME_DURATION_MS = 1000

#: clip 段超过文件预算（1 MiB，research.md §2.3）时依次尝试的调色板颜色数，None = 不量化（全彩）。
#: 实测（模拟 Retina 满屏密集彩色文字，缩到 640×290）：10 帧 RGBA PNG 段约 2.2 MB，调 PNG 压缩级别基本没用；
#: 64 色约 1.1 MB，16 色约 0.6 MB，每帧量化约 6～8 ms。按 §5 的约 100 KB/s 估算，2 MB 的段要下载约 20 s，
#: 却只播 2 s，所以必须降级。量化后仍编码成 8-bit RGBA PNG，与官方 .fset 样本格式一致（research.md §4）。
CLIP_COLOR_LADDER: tuple[int | None, ...] = (None, 64, 16)
#: 在某个量化档位上连续这么多段都没超预算，就试着退回上一档（画面变简单后恢复画质）
CLIP_RELAX_AFTER_SEGMENTS = 5

# 统计窗口：编码/采集耗时取最近 N 次的平均，实际帧率取最近 N 次采集的时间跨度
_STATS_WINDOW = 50


@dataclass(frozen=True)
class Snapshot:
    """image / fast 模式的一帧。``seq`` 全局单调递增（与 :class:`Clip` 共用计数器），0 表示占位图。"""

    seq: int
    ts: float  # 发布时刻，time.time()（秒）
    data: bytes
    width: int
    height: int
    kind: Literal["jpeg", "png"]
    #: 同一帧的灰度 GIF（``gif_levels > 0`` 时才有，research.md §9.24）
    gif: bytes | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Clip:
    """clip 模式的一段 `.fset`。``seq`` 与 :class:`Snapshot` 共用计数器，0 表示占位段。"""

    seq: int
    ts: float  # 发布时刻，time.time()（秒）
    data: bytes  # 完整 .fset 文件
    frames: int  # 恒等于 clip_frames，页面 total-frames 必须与之相同
    frame_duration_ms: int  # 页面 frame-duration 必须与之相同
    width: int
    height: int
    kind: ClipFormat = "png"  # 段内每帧的编码格式


@dataclass(frozen=True)
class _ClipFrame:
    """clip 攒帧缓冲里的一帧：保留缩放后的原图，超预算时可以按更少的颜色重新编码。"""

    image: Image.Image
    blob: bytes
    colors: int | None  # 编码 blob 时用的量化颜色数，None = 全彩


def frame_duration_for(clip_fps: float) -> int:
    """clip 段内每帧时长 ``round(1000 / clip_fps)``，限制在 1～1000（research.md §2.4）。"""
    _check_fps(clip_fps, "clip_fps")
    return max(1, min(MAX_FRAME_DURATION_MS, round(1000.0 / clip_fps)))


def emit_hops_for(fast_fps: float, emit_hop_ms: float = DEFAULT_EMIT_HOP_MS) -> int:
    """cast-emit 页面每换一张图的事件跳数 ``round(1000 / fast_fps / emit_hop_ms)``，限制在 1～:data:`EMIT_MAX_HOPS`。"""
    _check_fps(fast_fps, "fast_fps")
    if not (isinstance(emit_hop_ms, (int, float)) and math.isfinite(emit_hop_ms) and emit_hop_ms > 0):
        raise ValueError(f"emit_hop_ms 必须是正的有限数: {emit_hop_ms!r}")
    return max(1, min(EMIT_MAX_HOPS, round(1000.0 / fast_fps / float(emit_hop_ms))))


def fast_frame_duration_for(fast_fps: float, timer_frames: int = TIMER_FRAMES) -> int:
    """fast 模式页面定时器的每帧时长 ``round(1000 / fast_fps / timer_frames)``，限制在 1～1000。

    页面 ``cast-fast`` 的帧合集定时器有 ``timer_frames``（默认 2）帧，播完一轮才拉一张图，
    所以每帧时长取"一张图的间隔 ÷ 帧数"（research.md §9.2 E4、E9；实现指南 §9.1）。
    ``frame-duration`` 超过 1000 会被截断（research.md §2.4），即 fast_fps < 0.5 时页面实际更快。
    """
    _check_fps(fast_fps, "fast_fps")
    if isinstance(timer_frames, bool) or int(timer_frames) < 1:
        raise ValueError(f"timer_frames 至少为 1: {timer_frames!r}")
    return max(1, min(MAX_FRAME_DURATION_MS, round(1000.0 / fast_fps / int(timer_frames))))


def _check_fps(fps: float, name: str) -> None:
    if not (isinstance(fps, (int, float)) and math.isfinite(fps) and fps > 0):
        raise ValueError(f"{name} 必须是正的有限数: {fps!r}")


class FastFpsError(ValueError):
    """流畅模式帧率不合法。消息是给人看的中文（含合法范围），可以直接打印或放进 HTTP 400 的 message。"""


def parse_fast_fps(value: object) -> float:
    """校验运行中要设置的流畅模式帧率，返回 float；不合法抛 :class:`FastFpsError`（不钳制）。

    - 接受 int / float，以及能解析成数字的字符串（大模型偶尔把数字填成 ``"6"``）；bool、None、NaN、无穷都拒绝。
    - 范围 :data:`FAST_FPS_MIN`～:data:`FAST_FPS_MAX`（含端点，可带小数，例如 2.5）。
    - 页面定时器帧时长 ``round(1000 / fps / 2)`` 必须在 :data:`MIN_FRAME_DURATION_MS`～:data:`MAX_FRAME_DURATION_MS`
      之间（官方文档第 36 节 §3.3；research.md §2.4）。在上面的范围内恒成立，这里仍单独检查，防止以后改了范围。
    """
    if isinstance(value, bool) or value is None:
        raise FastFpsError(f"帧率应为 {FAST_FPS_RANGE_TEXT} 之间的数字（每秒张数，可以带小数，例如 2.5），收到 {value!r}")
    if isinstance(value, str):
        try:
            fps = float(value.strip())
        except ValueError:
            raise FastFpsError(
                f"帧率应为 {FAST_FPS_RANGE_TEXT} 之间的数字（每秒张数，可以带小数，例如 2.5），收到 {value!r}"
            ) from None
    elif isinstance(value, (int, float)):
        try:
            fps = float(value)
        except OverflowError:
            raise FastFpsError(f"帧率只能是 {FAST_FPS_RANGE_TEXT}（每秒张数），收到 {value!r}") from None
    else:
        raise FastFpsError(f"帧率应为 {FAST_FPS_RANGE_TEXT} 之间的数字（每秒张数，可以带小数，例如 2.5），收到 {value!r}")
    if not math.isfinite(fps) or not FAST_FPS_MIN <= fps <= FAST_FPS_MAX:
        raise FastFpsError(f"帧率只能是 {FAST_FPS_RANGE_TEXT}（每秒张数，可以带小数，例如 2.5），收到 {value!r}")
    duration = round(1000.0 / fps / TIMER_FRAMES)
    if not MIN_FRAME_DURATION_MS <= duration <= MAX_FRAME_DURATION_MS:
        raise FastFpsError(
            f"帧率 {fps:g} 对应的页面定时器帧时长 {duration} ms 不在 {MIN_FRAME_DURATION_MS}～{MAX_FRAME_DURATION_MS} ms 之内"
            "（官方文档第 36 节 §3.3 帧合集）"
        )
    return fps


def fast_fps_warnings(fps: float) -> list[str]:
    """能用但大概率不是你想要的流畅模式帧率（启动信息、``POST /admin/fps`` 的返回和日志共用）。"""
    if fps > FAST_FPS_WARN_ABOVE:
        return [
            f"fast_fps={fps:g} 很高：真机下载带宽多半跟不上，单张图下载慢于换图间隔时请求会堆积"
            "（research.md §9.3），建议 4 或更低（真机实测稳定上限，4.5 以上可能卡住）"
        ]
    return []


def _avg_ms(values: deque[float]) -> float | None:
    return round(sum(values) / len(values) * 1000.0, 2) if values else None


class CastPipeline:
    """采集 + 编码流水线。线程安全：所有公开方法都可以从任意线程（含 asyncio 事件循环）调用，
    且都不会阻塞在截屏/编码上（:meth:`close` 会等线程退出，最多 ``timeout`` 秒）。

    构造参数（关键字）：

    - ``canvas`` / ``fit_mode``：输出尺寸与缩放方式，见 :func:`~glasscast.frames.fit_to_canvas`。
    - ``color_mode``：``gray``（默认）缩放后转 8 位灰度，JPEG/PNG 都输出单通道灰度；``rgb`` 保留彩色
      （research.md §9.2 E6：屏幕单色绿，颜色没有意义）。
    - ``image_fps`` / ``jpeg_quality`` / ``jpeg_subsampling`` / ``jpeg_encoder``：image 模式（JPEG）；编码器见 :func:`~glasscast.frames.encode_jpeg`。
    - ``jpeg_target_bytes``：> 0 时每张图取体积不超过它的最高质量（``jpeg_quality`` 变成上限，:func:`~glasscast.frames.encode_jpeg_target`）；0 = 固定质量。
    - ``fast_fps``：fast 模式截帧频率（默认 4）的**初始值**，其余与 image 模式相同；页面定时器帧时长见
      :attr:`fast_frame_duration_ms`。构造时只要求正的有限数（单测用很高的帧率加速）；运行中用
      :attr:`fast_fps` 的 setter 修改，按 :func:`parse_fast_fps` 的范围校验。
    - ``clip_fps`` / ``clip_frames`` / ``clip_format``：clip 模式（仅内部实验）；PNG 帧在 ``gray`` 下是
      8 位灰度（research.md §9.2 E6），在 ``rgb`` 下 ``clip_png_alpha=True`` 时编码为 RGBA，
      与官方 `.fset` 样本一致（research.md §4）。
    - ``skip_unchanged``：画面（精确像素摘要）没变时复用上次编码结果。image/fast 模式仍发布新的
      ``seq/ts``（眼镜每次都要拿到东西）；clip 模式照常把（复用的）帧加入段，段长固定。
    - clip 段超过 1 MiB 时自动按 :data:`CLIP_COLOR_LADDER` 量化重编码；量化到最少颜色仍超就丢弃该段、
      保留上一段（``stats()`` 的 ``clips_over_budget`` / ``clip_quantized_colors``）。
    - ``error_backoff_s`` / ``error_backoff_max_s``：连续出错时的退避，按 2 倍递增到上限。
    - ``close_source``：:meth:`close` 时是否调用 ``source.close()``（若有）。
    """

    def __init__(
        self,
        source: FrameSource,
        *,
        canvas: tuple[int, int] = CANVAS,
        fit_mode: FitMode = "fit",
        image_fps: float = 1.0,
        fast_fps: float = DEFAULT_FAST_FPS,
        jpeg_quality: int = 70,
        jpeg_encoder: JpegEncoder = "auto",
        jpeg_target_bytes: int = 0,
        gif_levels: int = 0,
        gif_delay_ms: int = 100,
        color_mode: ColorMode = "gray",
        clip_fps: float = 5.0,
        clip_frames: int = 10,
        clip_format: ClipFormat = "png",
        skip_unchanged: bool = True,
        jpeg_subsampling: int = 2,
        png_compress_level: int = 3,
        clip_png_alpha: bool = True,
        error_backoff_s: float = 1.0,
        error_backoff_max_s: float = 5.0,
        close_source: bool = False,
        thread_name: str = "glasscast-capture",
    ) -> None:
        if len(canvas) != 2 or int(canvas[0]) <= 0 or int(canvas[1]) <= 0:
            raise ValueError(f"canvas 必须是正的 (宽, 高): {canvas!r}")
        if fit_mode not in ("fit", "crop"):
            raise ValueError(f"fit_mode 必须是 'fit' 或 'crop': {fit_mode!r}")
        _check_fps(image_fps, "image_fps")
        _check_fps(fast_fps, "fast_fps")
        _check_fps(clip_fps, "clip_fps")
        if color_mode not in COLOR_MODES:
            raise ValueError(f"color_mode 必须是 'gray' 或 'rgb': {color_mode!r}")
        if not 1 <= int(jpeg_quality) <= 100:
            raise ValueError(f"jpeg_quality 应在 1～100: {jpeg_quality}")
        if gif_levels:
            encode_gif(Image.new("L", (1, 1)), gif_levels, gif_delay_ms)  # 只为校验参数，出错抛 ValueError
        if int(jpeg_target_bytes) < 0:
            raise ValueError(f"jpeg_target_bytes 不能为负: {jpeg_target_bytes}")
        if jpeg_encoder not in JPEG_ENCODERS:
            raise ValueError(f"jpeg_encoder 应为 auto / mozjpeg / pillow: {jpeg_encoder!r}")
        if jpeg_subsampling not in (0, 1, 2):
            raise ValueError(f"jpeg_subsampling 应为 0/1/2: {jpeg_subsampling}")
        if not 0 <= int(png_compress_level) <= 9:
            raise ValueError(f"png_compress_level 应在 0～9: {png_compress_level}")
        if int(clip_frames) < 1:
            raise ValueError(f"clip_frames 至少为 1: {clip_frames}")
        if clip_format not in ("png", "jpeg"):
            raise ValueError(f"clip_format 必须是 'png' 或 'jpeg': {clip_format!r}")
        if not (error_backoff_s >= 0 and error_backoff_max_s >= error_backoff_s):
            raise ValueError("需要 0 ≤ error_backoff_s ≤ error_backoff_max_s")

        self._source = source
        self._canvas = (int(canvas[0]), int(canvas[1]))
        self._fit_mode: FitMode = fit_mode
        self._image_fps = float(image_fps)
        self._fast_fps = float(fast_fps)
        self._fast_frame_duration_ms = fast_frame_duration_for(fast_fps)
        self._color_mode: ColorMode = color_mode
        self._jpeg_quality = int(jpeg_quality)
        self._jpeg_subsampling = jpeg_subsampling
        self._jpeg_encoder: JpegEncoder = jpeg_encoder
        self._jpeg_target_bytes = int(jpeg_target_bytes)
        self._last_jpeg_quality: int | None = None
        self._gif_levels = int(gif_levels)
        self._gif_delay_ms = int(gif_delay_ms)
        self._clip_fps = float(clip_fps)
        self._clip_frames = int(clip_frames)
        self._clip_format: ClipFormat = clip_format
        self._frame_duration_ms = frame_duration_for(clip_fps)
        self._skip_unchanged = bool(skip_unchanged)
        self._png_compress_level = int(png_compress_level)
        self._clip_png_alpha = bool(clip_png_alpha)
        self._backoff_base = float(error_backoff_s)
        self._backoff_max = float(error_backoff_max_s)
        self._close_source = close_source
        self._thread_name = thread_name

        # ---- 以下状态由 _cond 保护 ----
        self._cond = threading.Condition(threading.Lock())
        self._mode: CastMode = "off"
        self._gen = 0  # 每次模式切换 / 关闭 +1
        self._closed = False
        self._thread: threading.Thread | None = None
        # fast_fps 每次被改动 +1：采集线程据此提前醒来、按新周期重排下一帧（不必等完旧周期）
        self._rate_version = 0
        self._seq = 0
        self._snapshot: Snapshot | None = None
        self._clip: Clip | None = None
        self._clip_buffered = 0
        self._frames_captured = 0
        self._snapshots_published = 0
        self._clips_published = 0
        self._reused_frames = 0
        self._discarded_frames = 0
        self._clips_over_budget = 0
        self._clip_quantized_colors: int | None = None  # 最近一段实际用的量化颜色数（给 stats 看）
        self._errors = 0
        self._consecutive_errors = 0
        self._last_error: str | None = None
        self._last_error_mono: float | None = None
        self._last_capture_mono: float | None = None
        self._capture_times: deque[float] = deque(maxlen=_STATS_WINDOW)
        self._encode_times: deque[float] = deque(maxlen=_STATS_WINDOW)
        self._capture_stamps: deque[float] = deque(maxlen=_STATS_WINDOW)
        self._budget_warnings: dict[str, list[str]] = {}

        # 编码复用缓存 (精确像素摘要, [量化颜色数,] 编码结果)，只在采集线程内读写
        self._image_cache: tuple[str, bytes, bytes | None] | None = None
        self._clip_cache: tuple[str, int | None, bytes] | None = None
        # clip 量化档位（粘滞）：超预算时升档，连续 CLIP_RELAX_AFTER_SEGMENTS 段在预算内再降一档。只在采集线程内读写
        self._clip_colors: int | None = None
        self._clip_fit_streak = 0

        # 占位图缓存（服务端无帧时用），惰性生成
        self._placeholder_lock = threading.Lock()
        self._placeholder_snapshot: Snapshot | None = None
        self._placeholder_clip: Clip | None = None

        # 画布本身超预算时提前告警（research.md §2.3）。对外的 image/fast 模式都是 JPEG：文档折算约 640×386，
        # 按 3 字节/像素算（灰度 JPEG 解码后占多少内存文档没写，按彩色算偏保守）。clip（PNG 帧，4 字节/像素）
        # 只是内部实验，不在启动时告警；真攒出段时 _tick_clip 仍会检查并写进 stats 的 budget_warnings。
        for warning in check_budget(*self._canvas, 0, bytes_per_pixel=3):
            log.warning("画布 %s×%s，image/fast 模式（JPEG）：%s", self._canvas[0], self._canvas[1], warning)

    # ------------------------------------------------------------------ 只读属性

    @property
    def mode(self) -> CastMode:
        with self._cond:
            return self._mode

    @property
    def closed(self) -> bool:
        with self._cond:
            return self._closed

    @property
    def running(self) -> bool:
        """采集线程是否存活（第一次 start 之前为 False）。"""
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def canvas(self) -> tuple[int, int]:
        return self._canvas

    @property
    def color_mode(self) -> ColorMode:
        return self._color_mode

    @property
    def jpeg_quality(self) -> int:
        return self._jpeg_quality

    @property
    def image_fps(self) -> float:
        return self._image_fps

    @property
    def fast_fps(self) -> float:
        """流畅模式当前的截帧频率（运行中可改，见 setter）。"""
        with self._cond:
            return self._fast_fps

    @fast_fps.setter
    def fast_fps(self, value: float) -> None:
        self.set_fast_fps(value)

    @property
    def fast_frame_duration_ms(self) -> int:
        """fast 模式页面定时器每帧时长 ``round(1000 / fast_fps / 2)``（:func:`fast_frame_duration_for`），随 fast_fps 变。"""
        with self._cond:
            return self._fast_frame_duration_ms

    def fast_settings(self) -> tuple[float, int]:
        """同一时刻的 ``(fast_fps, fast_frame_duration_ms)``：两者一起读，不会被并发的修改拆开。"""
        with self._cond:
            return self._fast_fps, self._fast_frame_duration_ms

    def set_fast_fps(self, value: object) -> tuple[float, int]:
        """运行中修改流畅模式帧率，返回生效后的 ``(fast_fps, fast_frame_duration_ms)``。

        - 先按 :func:`parse_fast_fps` 校验（范围 :data:`FAST_FPS_MIN`～:data:`FAST_FPS_MAX`，帧时长 ≥ 41 ms），
          不合法抛 :class:`FastFpsError`，状态不变；不做钳制。
        - 立即影响 fast 模式的截帧节拍：采集线程被唤醒，从上一帧的计划时刻起按新周期重排下一帧
          （从 1 fps 调到 10 fps 不用再等将近 1 秒；调慢时也不会补发积压的帧）。
        - 不影响当前模式（image / off 时只是改了下次进入 fast 用的值），也不清空画面。
        - 眼镜上已经打开的 ``cast-fast`` 页面的 ``frame-duration`` 在打开时就绑定好了（research.md §9.2 E9），
          要等下一次 ``start_cast`` 重新打开页面才按新节拍拉图。
        """
        fps = parse_fast_fps(value)
        duration = fast_frame_duration_for(fps)
        with self._cond:
            previous = self._fast_fps
            self._fast_fps = fps
            self._fast_frame_duration_ms = duration
            if fps != previous:
                self._rate_version += 1
                self._cond.notify_all()
        if fps != previous:
            log.info("流畅模式帧率 %g → %g（页面定时器帧时长 %d ms）", previous, fps, duration)
        for warning in fast_fps_warnings(fps):
            log.warning("%s", warning)
        return fps, duration

    def page_frame_duration_ms(self, mode: str) -> int:
        """某个模式的页面"帧时长"（start_cast 出参 ``frame_duration_ms``，实现指南 §9.1）：

        fast → 定时器每帧时长；image → 1000（``<tick>`` 每秒一次）；clip → 段内每帧时长。
        """
        if mode == "fast":
            return self.fast_frame_duration_ms
        if mode == "image":
            return IMAGE_FRAME_DURATION_MS
        if mode == "clip":
            return self._frame_duration_ms
        raise ValueError(f"mode 必须是 'image'、'fast' 或 'clip': {mode!r}")

    @property
    def clip_frames(self) -> int:
        return self._clip_frames

    @property
    def frame_duration_ms(self) -> int:
        """clip 段内每帧时长（仅内部实验的 clip 模式；fast 模式见 :attr:`fast_frame_duration_ms`）。"""
        return self._frame_duration_ms

    @property
    def clip_format(self) -> ClipFormat:
        return self._clip_format

    # ------------------------------------------------------------------ 控制

    def start(self, mode: CastMode) -> None:
        """切到 ``image``、``fast`` 或（内部实验）``clip`` 模式；``off`` 等价于 :meth:`stop`。立即返回，不等第一帧。

        - 与当前模式相同时什么也不做（不清空攒帧缓冲、不丢弃已有输出）。
        - 模式变化时：清空攒帧缓冲，丢弃上一轮残留的输出（image↔fast 互切时保留 snapshot），并立即截第一帧。
          需要"有画面再返回"时接着调 :meth:`wait_for_snapshot` / :meth:`wait_for_clip`。
        - 关闭后调用抛 ``RuntimeError``。
        """
        if mode == "off":
            self.stop()
            return
        if mode not in ("image", "fast", "clip"):
            raise ValueError(f"mode 必须是 'image'、'fast'、'clip' 或 'off': {mode!r}")
        with self._cond:
            if self._closed:
                raise RuntimeError("CastPipeline 已关闭")
            if self._mode != mode:
                self._switch_locked(mode)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name=self._thread_name, daemon=True)
                self._thread.start()

    def stop(self, *, clear: bool = False) -> None:
        """切到 ``off``：线程空闲等待，不再截屏。可重复调用，关闭后调用也不报错。

        返回后不会再有新的 snapshot/clip 发布（正在进行的那一帧会被丢弃）。
        ``clear=True`` 同时丢弃最新的 snapshot/clip（隐私考虑：停止后不再能取到最后一帧）。
        """
        with self._cond:
            if self._mode != "off":
                self._switch_locked("off")
            if clear:
                self._clear_locked()

    def clear(self) -> None:
        """丢弃最新的 snapshot/clip（不改变模式）。"""
        with self._cond:
            self._clear_locked()

    def close(self, timeout: float = 5.0) -> None:
        """停止并结束线程（最多等 ``timeout`` 秒）。可重复调用。"""
        with self._cond:
            if self._closed:
                return
            self._closed = True
            self._mode = "off"
            self._gen += 1
            self._clip_buffered = 0
            self._cond.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
            if thread.is_alive():
                log.warning("采集线程 %s 秒内没有退出（可能卡在截屏调用里），已放弃等待", timeout)
        if self._close_source:
            closer = getattr(self._source, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:  # noqa: BLE001
                    log.warning("关闭帧源失败", exc_info=True)

    def __enter__(self) -> CastPipeline:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _switch_locked(self, mode: CastMode) -> None:
        previous = self._mode
        self._mode = mode
        self._gen += 1
        self._clip_buffered = 0  # 模式切换时清空攒帧缓冲（线程侧在下一轮真正清空）
        # 进入某个模式时丢弃上一轮残留的输出：clip 攒满第一段要 clip_frames/clip_fps 秒，
        # 这期间不能把几分钟前的旧画面当"最新"给眼镜；服务端在此期间返回占位。
        # image 与 fast 共用 snapshot、都在持续更新，互切时保留（最多是一个截帧周期之前的画面）。
        # 切到 off 不丢弃（stop(clear=True) 才丢）。
        if mode in SNAPSHOT_MODES and previous not in SNAPSHOT_MODES:
            self._snapshot = None
        elif mode == "clip":
            self._clip = None
        if previous == "off" and mode != "off":
            # 停止后重新开始是新的一轮：上一轮的"最近一次截帧时间"、连续失败次数和截帧时间戳都不属于这一轮。
            # 不清的话，新一轮第一帧还没出来时 cast_status 会按旧值报"画面已经 N 秒没更新""连续失败 N 次"，
            # 而不是"电脑端还没有截到画面"（mcp_tools.cast_status、skill/SKILL.md 模式三）；capture_fps 也会把
            # 停止期间的空档算进去。累计计数（frames_captured、errors 等）不清：start_cast 等第一帧时比较的
            # 就是它们。image ↔ fast 互切不经过 off，采集一直在继续，保留。
            self._last_capture_mono = None
            self._consecutive_errors = 0
            self._capture_stamps.clear()
        self._cond.notify_all()

    def _clear_locked(self) -> None:
        self._snapshot = None
        self._clip = None
        self._budget_warnings.clear()

    # ------------------------------------------------------------------ 读取

    def latest_snapshot(self) -> Snapshot | None:
        with self._cond:
            return self._snapshot

    def latest_clip(self) -> Clip | None:
        with self._cond:
            return self._clip

    def wait_for_snapshot(self, after_seq: int = 0, timeout: float = 5.0) -> Snapshot | None:
        """阻塞到出现 ``seq > after_seq`` 的 snapshot 并返回它；超时或已关闭返回 None。"""
        with self._cond:
            self._cond.wait_for(
                lambda: self._closed or (self._snapshot is not None and self._snapshot.seq > after_seq),
                timeout,
            )
            snap = self._snapshot
        return snap if snap is not None and snap.seq > after_seq else None

    def wait_for_clip(self, after_seq: int = 0, timeout: float = 5.0) -> Clip | None:
        """阻塞到出现 ``seq > after_seq`` 的 clip 并返回它；超时或已关闭返回 None。"""
        with self._cond:
            self._cond.wait_for(
                lambda: self._closed or (self._clip is not None and self._clip.seq > after_seq),
                timeout,
            )
            clip = self._clip
        return clip if clip is not None and clip.seq > after_seq else None

    def make_placeholder_snapshot(self, image: Image.Image) -> Snapshot:
        """把一张占位图按本流水线的画布、色彩和 JPEG 参数编码成 ``seq=0`` 的 :class:`Snapshot`（不缓存）。"""
        frame = apply_color_mode(fit_to_canvas(image, self._canvas, self._fit_mode), self._color_mode)
        return Snapshot(
            seq=0,
            ts=time.time(),
            data=self._encode_jpeg(frame),
            width=self._canvas[0],
            height=self._canvas[1],
            kind="jpeg",
            gif=self._encode_gif(frame),
        )

    def make_placeholder_clip(self, image: Image.Image) -> Clip:
        """把一张占位图重复 ``clip_frames`` 次打包成 ``seq=0`` 的 :class:`Clip`（帧编码与真实段相同，不缓存）。"""
        frame = fit_to_canvas(image, self._canvas, self._fit_mode)
        blob = self._encode_clip_frame(frame)
        return Clip(
            seq=0,
            ts=time.time(),
            data=build_fset([blob] * self._clip_frames),
            frames=self._clip_frames,
            frame_duration_ms=self._frame_duration_ms,
            width=self._canvas[0],
            height=self._canvas[1],
            kind=self._clip_format,
        )

    def placeholder_snapshot(self) -> Snapshot:
        """"等待画面"占位 JPEG（seq=0，画布尺寸），供服务端在还没有帧时返回。结果会缓存。"""
        with self._placeholder_lock:
            if self._placeholder_snapshot is None:
                self._placeholder_snapshot = self.make_placeholder_snapshot(placeholder_image(canvas=self._canvas))
            return self._placeholder_snapshot

    def placeholder_clip(self) -> Clip:
        """占位 `.fset`（seq=0）：同一张占位帧重复 ``clip_frames`` 次，保证页面 ``total-frames`` 恒成立。"""
        with self._placeholder_lock:
            if self._placeholder_clip is None:
                self._placeholder_clip = self.make_placeholder_clip(placeholder_image(canvas=self._canvas))
            return self._placeholder_clip

    def stats(self) -> dict[str, Any]:
        """运行状态（可直接 JSON 序列化）。"""
        now = time.monotonic()
        with self._cond:
            stamps = list(self._capture_stamps)
            capture_fps = (
                round((len(stamps) - 1) / (stamps[-1] - stamps[0]), 2)
                if len(stamps) >= 2 and stamps[-1] > stamps[0]
                else None
            )
            snap, clip = self._snapshot, self._clip
            return {
                "mode": self._mode,
                "running": self.running,
                "closed": self._closed,
                "seq": self._seq,
                "snapshot_seq": snap.seq if snap else None,
                "clip_seq": clip.seq if clip else None,
                "frames_captured": self._frames_captured,
                "snapshots_published": self._snapshots_published,
                "clips_published": self._clips_published,
                "reused_frames": self._reused_frames,
                "discarded_frames": self._discarded_frames,
                "clips_over_budget": self._clips_over_budget,
                "clip_quantized_colors": self._clip_quantized_colors,
                "capture_ms_avg": _avg_ms(self._capture_times),
                "encode_ms_avg": _avg_ms(self._encode_times),
                "capture_fps": capture_fps,
                "last_frame_age_s": (
                    round(now - self._last_capture_mono, 3) if self._last_capture_mono is not None else None
                ),
                "last_snapshot_bytes": len(snap.data) if snap else None,
                "last_gif_bytes": len(snap.gif) if snap and snap.gif is not None else None,
                "last_clip_bytes": len(clip.data) if clip else None,
                "errors": self._errors,
                "consecutive_errors": self._consecutive_errors,
                "last_error": self._last_error,
                "last_error_age_s": (
                    round(now - self._last_error_mono, 3) if self._last_error_mono is not None else None
                ),
                "canvas": list(self._canvas),
                "fit_mode": self._fit_mode,
                "color_mode": self._color_mode,
                "image_fps": self._image_fps,
                "fast_fps": self._fast_fps,
                "fast_frame_duration_ms": self._fast_frame_duration_ms,
                "jpeg_quality": self._jpeg_quality,
                "jpeg_encoder": self._jpeg_encoder,
                "jpeg_target_bytes": self._jpeg_target_bytes,
                "last_jpeg_quality": self._last_jpeg_quality,
                "gif_levels": self._gif_levels,
                "gif_delay_ms": self._gif_delay_ms,
                "skip_unchanged": self._skip_unchanged,
                "clip_fps": self._clip_fps,
                "clip_frames": self._clip_frames,
                "clip_format": self._clip_format,
                "frame_duration_ms": self._frame_duration_ms,
                "clip_buffered": self._clip_buffered,
                "budget_warnings": [w for ws in self._budget_warnings.values() for w in ws],
            }

    # ------------------------------------------------------------------ 线程

    def _period_locked(self, mode: CastMode) -> float:
        """当前模式的截帧周期（秒）。fast 与 image 只差截帧频率（实现指南 §9.1）。调用方持锁。"""
        if mode == "fast":
            return 1.0 / self._fast_fps
        if mode == "image":
            return 1.0 / self._image_fps
        return 1.0 / self._clip_fps

    def _run(self) -> None:
        current_gen = -1
        current_rate = -1
        buffer: list[_ClipFrame] = []  # clip 攒帧缓冲，只在本线程访问
        next_due = 0.0
        # 上一帧的计划时刻（成功截帧后才有；出错退避期间为 None）。运行中改帧率时据此重排下一帧
        last_due: float | None = None
        while True:
            try:
                with self._cond:
                    while not self._closed and self._mode == "off":
                        self._cond.wait()
                    if self._closed:
                        return
                    mode, gen, rate = self._mode, self._gen, self._rate_version
                    period = self._period_locked(mode)
                if gen != current_gen:
                    # 新一轮（模式切换）：清空缓冲，立即截第一帧
                    current_gen, current_rate = gen, rate
                    buffer.clear()
                    next_due = time.monotonic()
                    last_due = None
                elif rate != current_rate:
                    # 运行中改了 fast_fps：从上一帧的计划时刻起按新周期重排下一帧，不等完按旧帧率算出的等待；
                    # 已经过了就马上截，但不补发积压的帧。退避中（last_due 为 None）保持退避；
                    # image / clip 模式的节拍与 fast_fps 无关，不动
                    current_rate = rate
                    if mode == "fast" and last_due is not None:
                        next_due = max(last_due + period, time.monotonic())
                delay = next_due - time.monotonic()
                if delay > 0:
                    self._wait_unless_changed(gen, rate, delay)
                    continue

                try:
                    if mode in SNAPSHOT_MODES:
                        self._tick_image(gen)
                    else:
                        self._tick_clip(gen, buffer)
                except Exception as exc:  # noqa: BLE001 - 线程不能因任何帧失败而退出
                    buffer.clear()  # 段必须是连续的一段时间，出错后从头攒
                    backoff = self._record_error(exc, gen)
                    next_due = time.monotonic() + backoff
                    last_due = None
                    continue

                self._record_success()
                # 固定节拍；落后超过一个周期就不再追帧，从现在重新计时。截帧期间帧率被改的话，
                # 下一轮循环会看到新的 rate，按新周期重排
                last_due = next_due
                next_due += period
                now = time.monotonic()
                if next_due < now - period:
                    next_due = now
                    last_due = now - period  # 保持 next_due = last_due + period，改帧率时从这里重排
            except Exception:  # noqa: BLE001 - 兜底：记账/日志本身出错也不能让线程死掉
                log.exception("采集线程内部错误，%s 秒后继续", self._backoff_base)
                try:
                    self._wait_unless_switched(current_gen, max(self._backoff_base, 0.01))
                except Exception:  # noqa: BLE001
                    time.sleep(max(self._backoff_base, 0.01))

    def _wait_unless_switched(self, gen: int, timeout: float) -> None:
        """等待 ``timeout`` 秒；模式切换或关闭时提前返回。"""
        with self._cond:
            self._cond.wait_for(lambda: self._closed or self._gen != gen, timeout)

    def _wait_unless_changed(self, gen: int, rate: int, timeout: float) -> None:
        """等待 ``timeout`` 秒；模式切换、关闭或帧率被修改时提前返回。"""
        with self._cond:
            self._cond.wait_for(lambda: self._closed or self._gen != gen or self._rate_version != rate, timeout)

    def _capture(self) -> tuple[Image.Image, float, float]:
        t0 = time.monotonic()
        # 缩放在 RGB 上做，最后按 color_mode 转灰度（research.md §9.2 E6）；摘要和编码都基于转换后的图
        frame = apply_color_mode(fit_to_canvas(self._source.grab(), self._canvas, self._fit_mode), self._color_mode)
        return frame, t0, time.monotonic()

    def _tick_image(self, gen: int) -> None:
        frame, t0, t1 = self._capture()
        # 用精确像素摘要判断"画面没变"：64×32 均值摘要可能漏掉单个字符级的小变化
        digest = frame_digest(frame, size=None) if self._skip_unchanged else None
        cache = self._image_cache
        encode_s: float | None = None
        if digest is not None and cache is not None and cache[0] == digest:
            data, gif = cache[1], cache[2]  # 复用上一张 JPEG/GIF，但下面仍发布新的 seq/ts
        else:
            data = self._encode_jpeg(frame)
            gif = self._encode_gif(frame)
            encode_s = time.monotonic() - t1
            self._image_cache = (digest, data, gif) if digest is not None else None
        # JPEG 解码按 3 字节/像素算预算（官方文档折算 JPEG 最大约 640×386，research.md §2.3）
        warnings = check_budget(*self._canvas, len(data), bytes_per_pixel=3)
        with self._cond:
            if self._gen != gen or self._closed:
                self._discarded_frames += 1
                return
            self._account_capture_locked(t0, t1, encode_s)
            self._seq += 1
            self._snapshot = Snapshot(
                seq=self._seq,
                ts=time.time(),
                data=data,
                width=self._canvas[0],
                height=self._canvas[1],
                kind="jpeg",
                gif=gif,
            )
            self._snapshots_published += 1
            self._set_budget_warnings_locked("image", warnings)
            self._cond.notify_all()

    def _tick_clip(self, gen: int, buffer: list[_ClipFrame]) -> None:
        frame, t0, t1 = self._capture()
        colors = self._clip_colors
        digest = frame_digest(frame, size=None) if self._skip_unchanged else None
        cache = self._clip_cache
        encode_s: float | None = None
        if digest is not None and cache is not None and cache[0] == digest and cache[1] == colors:
            blob = cache[2]  # 画面没变：复用编码结果，但照常入段，段长固定
        else:
            blob = self._encode_clip_frame(frame, colors)
            encode_s = time.monotonic() - t1
            self._clip_cache = (digest, colors, blob) if digest is not None else None

        with self._cond:
            if self._gen != gen or self._closed:
                self._discarded_frames += 1
                return
            self._account_capture_locked(t0, t1, encode_s)
            buffer.append(_ClipFrame(frame, blob, colors))
            self._clip_buffered = len(buffer)
            if len(buffer) < self._clip_frames:
                return
            items = list(buffer)
        buffer.clear()

        # 打包（必要时量化重编码）放在锁外；发布前再确认一次代号
        data, used_colors, fits = self._pack_clip(items)
        self._update_clip_colors(items[0].colors, used_colors, fits)
        # 文档只写明 1 MiB 文件上限适用于远端 <image>；远端 .fset 是否同样受限未验证（research.md §2.3、§2.4）。
        # 按同一标准处理：带宽是硬约束（research.md §1 第 1 条、§5），超预算的段即使能显示也下载不完。
        warnings = [
            f"{w}（clip 段：也可减少 clip_frames；.fset 是否受此限制未验证）"
            for w in check_budget(*self._canvas, len(data))
        ]
        if not fits:
            warnings.append(
                f"clip 段量化到 {used_colors} 色后仍有 {len(data)} 字节，超过 {FILE_BUDGET_BYTES}，已丢弃该段、"
                "保留上一段；请缩小画布（--canvas）、裁小区域（--region）或减少 clip_frames"
                if used_colors is not None
                else f"clip 段 {len(data)} 字节超过 {FILE_BUDGET_BYTES}，已丢弃该段、保留上一段；"
                "请缩小画布或减少 clip_frames"
            )
        with self._cond:
            if self._gen != gen or self._closed:
                self._discarded_frames += len(items)
                return
            self._clip_buffered = 0
            self._clip_quantized_colors = used_colors
            if not fits:
                # 不发布：超预算的段在模拟器里会被拒绝显示，真机"显示异常或极慢"（research.md §2.3）
                self._clips_over_budget += 1
                self._set_budget_warnings_locked("clip", warnings)
                return
            self._seq += 1
            # 只保留最新一段：直接覆盖，不排队（research.md §5）
            self._clip = Clip(
                seq=self._seq,
                ts=time.time(),
                data=data,
                frames=len(items),
                frame_duration_ms=self._frame_duration_ms,
                width=self._canvas[0],
                height=self._canvas[1],
                kind=self._clip_format,
            )
            self._clips_published += 1
            self._set_budget_warnings_locked("clip", warnings)
            self._cond.notify_all()

    def _encode_clip_frame(self, frame: Image.Image, colors: int | None = None) -> bytes:
        """编码 clip 的一帧。``colors`` 不为 None 时先量化成该数量的调色板（只对 PNG 生效）。

        - ``gray``：8 位灰度 PNG（模拟器实测正常显示，约为 RGBA 的 1/3，research.md §9.2 E6）；
          量化后再转回灰度，只是灰阶少、压缩率高。
        - ``rgb``：按 RGBA（``clip_png_alpha``，或 RGB）编码，与官方样本那种 8-bit 真彩 PNG 一致（research.md §4）。
        """
        frame = apply_color_mode(frame, self._color_mode)
        if self._clip_format == "png":
            gray = self._color_mode == "gray"
            if colors is not None:
                frame = frame.quantize(colors=colors, method=Image.Quantize.FASTOCTREE)
                if gray:
                    frame = frame.convert("L")
            return encode_png(frame, self._png_compress_level, alpha=self._clip_png_alpha and not gray)
        # JPEG 帧：本地模拟器实测黑屏、也不触发 on-completed（tools/simulator/README.md §4 第 4 条；
        # research.md §4、§8.3），只作实验选项
        return self._encode_jpeg(frame)

    def _encode_jpeg(self, frame: Image.Image) -> bytes:
        """按本流水线的 JPEG 参数编码；设了 ``jpeg_target_bytes`` 时定体积、``jpeg_quality`` 作上限。"""
        if self._jpeg_target_bytes <= 0:
            data = encode_jpeg(frame, self._jpeg_quality, subsampling=self._jpeg_subsampling, encoder=self._jpeg_encoder)
            self._last_jpeg_quality = self._jpeg_quality
            return data
        data, quality = encode_jpeg_target(
            frame,
            self._jpeg_target_bytes,
            quality_max=self._jpeg_quality,
            quality_min=min(10, self._jpeg_quality),
            subsampling=self._jpeg_subsampling,
            encoder=self._jpeg_encoder,
        )
        self._last_jpeg_quality = quality
        return data

    def _encode_gif(self, frame: Image.Image) -> bytes | None:
        return encode_gif(frame, self._gif_levels, self._gif_delay_ms) if self._gif_levels else None

    def _pack_clip(self, items: list[_ClipFrame]) -> tuple[bytes, int | None, bool]:
        """把一段帧打包成 `.fset`；超过文件预算（1 MiB，research.md §2.3）时按 :data:`CLIP_COLOR_LADDER`
        逐档量化重编码。返回 ``(数据, 实际用的量化颜色数, 是否在预算内)``；JPEG 帧不量化。"""
        level = items[0].colors
        if any(item.colors != level for item in items):  # 档位只在段边界变化，正常走不到；保险起见统一重编码
            blobs = [self._encode_clip_frame(item.image, level) for item in items]
        else:
            blobs = [item.blob for item in items]
        data = build_fset(blobs)
        if len(data) <= FILE_BUDGET_BYTES:
            return data, level, True
        if self._clip_format != "png" or level not in CLIP_COLOR_LADDER:
            return data, level, False
        for colors in CLIP_COLOR_LADDER[CLIP_COLOR_LADDER.index(level) + 1 :]:
            data = build_fset([self._encode_clip_frame(item.image, colors) for item in items])
            level = colors
            if len(data) <= FILE_BUDGET_BYTES:
                return data, level, True
        return data, level, False

    def _update_clip_colors(self, start: int | None, used: int | None, fits: bool) -> None:
        """更新粘滞的量化档位（只在采集线程调用）：升档立即生效；在量化档位上连续
        :data:`CLIP_RELAX_AFTER_SEGMENTS` 段都在预算内，就退回上一档试试（画面变简单后恢复画质）。"""
        if used != start:
            log.info(
                "clip 段超过 %d 字节，改用 %s 编码%s",
                FILE_BUDGET_BYTES,
                f"{used} 色量化" if used is not None else "全彩",
                "" if fits else "（仍超预算，该段已丢弃）",
            )
        self._clip_colors = used
        if not fits or used != start or used is None or used not in CLIP_COLOR_LADDER:
            self._clip_fit_streak = 0
            return
        self._clip_fit_streak += 1
        if self._clip_fit_streak >= CLIP_RELAX_AFTER_SEGMENTS:
            self._clip_fit_streak = 0
            self._clip_colors = CLIP_COLOR_LADDER[CLIP_COLOR_LADDER.index(used) - 1]
            log.debug("clip 连续 %d 段在预算内，量化档位退回 %s", CLIP_RELAX_AFTER_SEGMENTS, self._clip_colors)

    # ------------------------------------------------------------------ 记账（调用方持锁）

    def _account_capture_locked(self, t0: float, t1: float, encode_s: float | None) -> None:
        self._frames_captured += 1
        self._last_capture_mono = t1
        self._capture_stamps.append(t1)
        self._capture_times.append(t1 - t0)
        if encode_s is None:
            self._reused_frames += 1
        else:
            self._encode_times.append(encode_s)

    def _set_budget_warnings_locked(self, key: str, warnings: list[str]) -> None:
        if warnings != self._budget_warnings.get(key, []):
            for w in warnings:
                log.warning("%s 输出超预算：%s", key, w)
        if warnings:
            self._budget_warnings[key] = warnings
        else:
            self._budget_warnings.pop(key, None)

    def _record_success(self) -> None:
        with self._cond:
            if self._consecutive_errors:
                log.info("采集恢复（此前连续失败 %d 次）", self._consecutive_errors)
            self._consecutive_errors = 0

    def _record_error(self, exc: BaseException, gen: int) -> float:
        """记录一次失败，返回退避秒数（base × 2^(连续次数-1)，封顶 max）。

        旧代号（模式切换或关闭之前就开始了）的那一帧失败，与新模式无关：只记为丢弃，不计入
        errors / consecutive_errors / last_error，也不退避（返回 0，下一轮立即按新代号截帧）。
        否则 start_cast 切模式时会把它当成新模式第一帧失败，关掉整个投屏，还播报错误的原因。
        """
        message = f"{type(exc).__name__}: {exc}"
        with self._cond:
            stale = self._gen != gen or self._closed
            if stale:
                self._discarded_frames += 1
            else:
                self._errors += 1
                self._consecutive_errors += 1
                self._last_error = message
                self._last_error_mono = time.monotonic()
                self._clip_buffered = 0
                n = self._consecutive_errors
        if stale:
            log.debug("模式切换前开始的那一帧失败，与当前模式无关，已忽略：%s", message)
            return 0.0
        backoff = min(self._backoff_max, self._backoff_base * (2 ** min(n - 1, 16)))
        # 日志限流：一轮连续失败只在第一次打印堆栈，之后每 30 次提示一次
        if n == 1:
            log.warning("采集失败，%.2f 秒后重试：%s", backoff, message, exc_info=exc)
        elif n % 30 == 0:
            log.warning("采集已连续失败 %d 次，最近一次：%s", n, message)
        else:
            log.debug("采集失败（连续第 %d 次）：%s", n, message)
        return backoff

