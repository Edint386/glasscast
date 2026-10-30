"""MCP 控制面工具：``start_cast`` / ``stop_cast`` / ``cast_status``（实现指南 §4.3、§9.1）。

MCP 只做控制面（research.md §1 第 3 条、§2.5）：平台每次工具调用都要经过大模型（秒级），不能参与逐帧刷新。
``start_cast`` 打开采集并返回眼镜页面要拉取的地址，之后的刷新全靠页面自己：

- ``image``（普通模式）：页面 ``cast-image`` 的 ``<tick>`` 每秒换一次图片地址（research.md §2.2、§9.2 E1）。
- ``fast``（流畅模式）：页面 ``cast-emit`` 用 emit 事件总线自环当节拍器，每 ``hops`` 跳换一张图
  （research.md §9.8）。``hops`` 由本工具按 ``fast_fps`` 和每跳毫秒数估算后下发，所以改帧率不用改页面。
  旧的 ``cast-fast``（``timer_url`` 指向的 2 帧 `.fset` 当定时器，research.md §9.2 E4、E9）过不了平台保存校验，
  ``timer_url``、``frame_duration_ms`` 仍保留在出参里，供参考页面使用。
- 页面由大模型按 SKILL.md 调用平台的 UI 页面渲染工具打开（平台把 MCP 出参只当 ``content[0].text``
  交给模型，``ui_schema.json`` 的 Hook 不命中，research.md §9.5）。
- 调帧率：``start_cast(mode="fast", fps=N)`` 先把运行时的 ``fast_fps`` 改成 N（1～10，
  :func:`~glasscast.pipeline.parse_fast_fps`），再开始投屏；模型随后带新的 ``hops`` 再打开一次 cast-emit
  （页面的 on-new-intent 写入新跳数）。``mode=image`` 时忽略 ``fps``（普通模式固定每秒 1 张），返回体里仍带
  流畅模式的当前帧率。

clip（`.fset` 分段流）已下线：``<frame-set>`` 无法换段（research.md §2.4、§9.2 E2），不再是对外选项；
大模型如果仍传 ``clip``（例如旧版 SKILL.md），按"用户想要流畅"处理成 ``fast``，出参 ``mode`` 回传实际模式。

平台约束：

- 工具必须 3000 ms 内返回（research.md §2.5；官方文档第 37 节 §2.1），这里所有工具都按 1 s 内设计：
  ``start_cast`` 最多等第一帧 :data:`DEFAULT_FIRST_FRAME_TIMEOUT_S` 秒，其余工具不阻塞。
- 返回体是 JSON 对象。工具函数的返回类型标注成 TypedDict（:class:`StartCastResult` 等），SDK 据此在
  ``tools/list`` 里给出逐字段的 ``outputSchema``，并在调用结果里同时放 ``structuredContent`` 和
  ``content[0].text``（JSON 文本，内容相同）。平台只把 ``content[0].text`` 这段 JSON 文本交给模型，
  ``ui_schema.json`` 的 Hook 在真机上不触发（research.md §9.5），所以 ``skill/ui_schema.json`` 是 ``[]``，
  页面由模型打开。``outputSchema`` 和 ``structuredContent`` 仍然保留，给 MCP 标准客户端和测试用。
  注意：SDK 按 TypedDict 校验并**只保留声明过的键**，新增返回字段必须同步加进 TypedDict，
  否则 structuredContent 里会悄悄缺这个字段（与文本 JSON 不一致，测试会发现）。
- 工具描述（description）会被平台交给大模型做工具选择，平台上改不了（官方文档第 37 节 §5.4），
  所以用中文写清触发场景。
- ``message`` 字段在失败时会被眼镜原样念出来（skill/SKILL.md），因此只写一句口语化的中文，
  技术细节放在 ``error`` 字段。

本模块把逻辑放在 :class:`CastController` 里（不依赖 SDK，便于直接单测），
:func:`build_mcp_server` 再把它注册成 SDK 2.x 的 :class:`~mcp.server.mcpserver.MCPServer` 工具。
"""

from __future__ import annotations

import logging
import time
from typing import Annotated, Any, Required, TypedDict

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import WithJsonSchema

from . import __version__
from .config import Settings
from .pipeline import (
    FAST_FPS_MAX,
    FAST_FPS_MIN,
    FAST_FPS_RANGE_TEXT,
    IMAGE_FRAME_DURATION_MS,
    PUBLIC_MODES,
    CastPipeline,
    FastFpsError,
    PublicMode,
    emit_hops_for,
    parse_fast_fps,
)

__all__ = [
    "DEFAULT_FIRST_FRAME_TIMEOUT_S",
    "FPS_PARAM_SCHEMA",
    "MODE_LABELS",
    "SERVER_INSTRUCTIONS",
    "TOOL_NAMES",
    "CastController",
    "CastStatusResult",
    "StartCastResult",
    "StopCastResult",
    "build_mcp_server",
    "normalize_mode",
]

log = logging.getLogger(__name__)

TOOL_NAMES: tuple[str, ...] = ("start_cast", "stop_cast", "cast_status")

#: start_cast 切换模式后最多等第一帧的秒数。远小于平台 3 s 超时，留出公网往返和大模型之外的余量
DEFAULT_FIRST_FRAME_TIMEOUT_S = 0.6
#: cast_status 里"画面刚刚更新"的阈值（秒），与 skill/SKILL.md 的话术一致
STALE_AFTER_S = 5.0

#: 模式的口语名（会出现在念给用户的 message 里）。clip 只可能由内部代码切入，给它一个不会和 fast 混淆的名字
MODE_LABELS: dict[str, str] = {"image": "普通模式", "fast": "流畅模式", "clip": "实验分段模式", "off": "未投屏"}

# 大模型偶尔会把中文或近义词填进 mode，这里宽松归一化；schema 里仍然只声明 image / fast 两个枚举值。
# clip / 视频 等旧说法都表示"想要更流畅"，归到 fast（clip 分段流已下线，research.md §2.4、§9.3）。
_MODE_ALIASES: dict[str, PublicMode] = {
    "image": "image",
    "img": "image",
    "picture": "image",
    "photo": "image",
    "jpeg": "image",
    "jpg": "image",
    "screen": "image",
    "normal": "image",
    "default": "image",
    "standard": "image",
    "普通": "image",
    "普通模式": "image",
    "标准": "image",
    "标准模式": "image",
    "图片": "image",
    "图片模式": "image",
    "截图": "image",
    "fast": "fast",
    "quick": "fast",
    "smooth": "fast",
    "fluent": "fast",
    "video": "fast",
    "high-fps": "fast",
    "clip": "fast",
    "clips": "fast",
    "流畅": "fast",
    "流畅模式": "fast",
    "快速": "fast",
    "快速模式": "fast",
    "高帧率": "fast",
    "视频": "fast",
    "视频模式": "fast",
    "动画": "fast",
}

# 下面几段文案里的 {fast_fps} 在 build_mcp_server 里换成实际的 --fast-fps（用 str.replace，不用 format，
# 免得文案里将来出现花括号时出错）
_FAST_FPS_SLOT = "{fast_fps}"

SERVER_INSTRUCTIONS = (
    "glasscast：把用户自己电脑的屏幕画面投到千问 AI 眼镜上。"
    "start_cast 开始投屏，mode 默认 image（普通模式，每秒一张，最稳）；用户要求流畅/快速/看视频时用 fast（流畅模式，"
    "默认每秒约 {fast_fps} 张，更费流量）。用户要调流畅模式的帧率（投屏调到 N 帧、每秒 N 张、快一点、慢一点）时调用 "
    "start_cast(mode=fast, fps=N)，N 为 1～10，换档后按返回的 hops 再打开一次流畅模式页面；需要当前帧率时先调 cast_status 看 fast_fps。"
    "start_cast 返回眼镜页面要拉取的地址；stop_cast 停止投屏；cast_status 查询投屏是否在进行、画面多久前更新、"
    "流畅模式当前帧率。画面由眼镜页面自己刷新，不要为了刷新画面反复调用工具，也不要轮询状态。"
)

START_CAST_DESCRIPTION = """开始把用户电脑的屏幕画面投到眼镜上，在两种投屏模式之间切换，或调节流畅模式的帧率。

何时调用：用户说"开始投屏""投屏""把电脑投到眼镜上""把电脑屏幕投过来""在眼镜上看电脑屏幕"时；
已经在投屏、用户说"换成流畅模式""切到快速模式""换回普通模式"时也调用它切换；
用户说"投屏调到 N 帧""每秒 N 张""快一点""慢一点""最流畅""最省流量"时用 mode=fast 加 fps 调帧率。
每次用户请求只调用一次。

不要用于：手机投屏、把眼镜画面投到电视或投影、拍照录像、远程操控电脑、朗读屏幕上的文字。

参数 mode：
- image（普通模式，默认）：每秒一张截图，延迟约 1～2 秒，最稳，适合看代码、文档、终端。
- fast（流畅模式）：默认每秒约 {fast_fps} 张截图，画面更跟手，但更吃带宽和流量，网络差时可能卡顿；
  用户说"流畅模式""快速模式""高帧率""看视频""看动画"时用它。
没听清用户要哪种时用 image。

参数 fps（可选，只对 fast 生效）：流畅模式每秒截图张数，范围 1～10，可以带小数（例如 2.5）；不传或为 null 时
沿用当前帧率。要调帧率必须同时传 mode=fast；mode=image 时忽略 fps（普通模式固定每秒 1 张）。
超出 1～10 时返回 success=false，message 说明范围。换档成功后要带新的 hops 再打开一次流畅模式页面。

返回 JSON 对象：success（是否成功）、mode（实际的投屏模式 image 或 fast）、url（页面拉取的图片地址）、
timer_url（只给本地模拟器的 cast-fast 参考页面用，打开页面时不要传）、width、height（画面尺寸）、
frame_duration_ms（同上，平台页面不用）、fps 和 fast_fps（调用之后生效的流畅模式帧率，两者相同）、
hops（流畅模式页面每换一张图的事件跳数，打开流畅模式页面时原样放进 dataSource）、page_fps（页面按 hops
估算的每秒拉图张数）、message（一句中文说明，失败时可直接念给用户）。
成功后按 mode 打开眼镜页面（image 用 cast-image，fast 用 cast-emit），页面自己刷新画面；
不要把 url、timer_url、hops 念给用户。"""

STOP_CAST_DESCRIPTION = """停止投屏：电脑停止截屏，眼镜上的画面不再更新。

何时调用：用户说"停止投屏""关闭投屏""结束投屏""别投了""不用投了"时。
返回 JSON 对象：success、previous_mode（停止前的模式）、message。重复调用也安全。"""

CAST_STATUS_DESCRIPTION = """查询电脑投屏的当前状态。

何时调用：用户问"投屏状态""投屏还在吗""投屏卡住了吗""画面多久没更新""现在是什么投屏模式""现在每秒几张"时；
用户说"快一点""慢一点"要在当前帧率上加减时，先调用它拿 fast_fps。不要轮询。
返回 JSON 对象：success、active（是否正在投屏）、mode（image 普通模式 / fast 流畅模式 / off 未投屏）、
last_frame_age_s（最近一帧是多少秒前截的，没有则为 null）、seq（画面序号）、consecutive_errors（连续截屏失败次数）、
fast_fps（流畅模式当前帧率，每秒张数，范围 1～10）、fast_frame_duration_ms（只给本地模拟器的 cast-fast 参考页面用）、
frame_duration_ms（同上，平台页面不用）、message（一句可以直接念给用户的中文状态）。"""

MODE_PARAM_DESCRIPTION = (
    "投屏模式：image（普通模式，默认，每秒一张截图，最稳）；"
    "fast（流畅模式，每秒约 {fast_fps} 张，更吃带宽，用户要求流畅/快速/看视频时用）"
)

# mode 的对外 JSON Schema（tools/list 里看到的样子）：只声明 image / fast 两个枚举值。
# 实际参数类型故意放宽成 Any：大模型常给可选参数填 null、偶尔填数字，若按 str 校验，SDK 会在进入函数前
# 拒绝并返回 isError 纯文本——没有 success / message，SKILL.md 的"原样播报 message"
# 就落空了（实现指南 §4.3）。放宽后一律交给 normalize_mode：null → image，无法识别 → success=false 的 JSON。
MODE_PARAM_SCHEMA: dict[str, Any] = {
    "type": "string",
    "enum": list(PUBLIC_MODES),
    "default": "image",
    "description": MODE_PARAM_DESCRIPTION,
}

FPS_PARAM_DESCRIPTION = (
    f"流畅模式帧率（每秒截图张数），范围 {FAST_FPS_RANGE_TEXT}，可以带小数（例如 2.5）。只在 mode=fast 时生效，"
    "mode=image 时忽略；不传或为 null 时沿用当前帧率。换档后要带新的 hops 再打开一次流畅模式页面"
)
# fps 的对外 JSON Schema：number 且可为 null。用 OpenAPI 风格的 "nullable": true 而不是 "type": ["number", "null"]：
# 平台会把 inputSchema 解析成自己的参数表（官方文档第 37 节 §5.4，类型按单个字符串理解），type 写成数组有解析
# 失败、整批工具刷新不到的风险【待验证】。同 mode 一样，实际参数类型放宽成 Any：范围不写进 minimum/maximum，
# 免得平台或 SDK 在进入函数前就拒绝；超范围交给 parse_fast_fps，回 success=false + 中文 message（实现指南 §4.3）。
FPS_PARAM_SCHEMA: dict[str, Any] = {
    "type": "number",
    "nullable": True,
    "description": FPS_PARAM_DESCRIPTION,
}


# --------------------------------------------------------------------------- 返回体（TypedDict → outputSchema）
#
# total=False + Required：成功和失败共用一个类型（失败时只有 success / message / error，外加可能的 mode），
# SDK 按它校验，缺少 Required 键或类型不符会让调用变成 isError，所以只把两种情况都必有的键标成 Required。


class StartCastResult(TypedDict, total=False):
    """start_cast 的返回体。成功时 url、timer_url、width、height、frame_duration_ms 齐全；失败时只有
    success=false、message（可直接念给用户）和 error（技术细节）。"""

    success: Required[bool]
    message: Required[str]
    #: 实际进入的模式 image / fast（失败时是归一化后的目标模式，或原样回显无法识别的输入）
    mode: str
    #: 页面拉取的图片地址（image / fast 共用；页面自己在后面拼 /<会话>_<序号>.jpg，地址必须以 .jpg 结尾）
    url: str
    #: cast-fast 参考页面（仅本地模拟器）的帧合集定时器：内容固定的 2 帧 8×8 灰度 PNG `.fset`
    timer_url: str
    width: int
    height: int
    #: fast：定时器每帧毫秒数 round(1000/fast_fps/2)，cast-fast 参考页面的 frame-duration 绑定它（平台页面不用）；image：1000
    frame_duration_ms: int
    #: 调用之后生效的流畅模式帧率（每秒张数）：fast 模式即本次投屏的帧率；image 模式忽略 fps 参数，仍报流畅模式的当前值
    fps: float
    #: 同 fps（与 cast_status、GET /admin/config 的同名字段一致）
    fast_fps: float
    #: cast-emit 页面每换一张图的事件跳数 round(1000/fast_fps/emit_hop_ms)，1～25（research.md §9.8）
    hops: int
    #: 页面按 hops 估算的拉图速率 1000/(hops×emit_hop_ms)，张/秒
    page_fps: float
    #: 调用前的模式（off / image / fast）
    previous_mode: str
    #: 返回时是否已经截到第一帧（false 只说明第一帧慢，页面先显示"等待画面"占位）
    first_frame: bool
    error: str | None


class StopCastResult(TypedDict, total=False):
    """stop_cast 的返回体。"""

    success: Required[bool]
    message: Required[str]
    previous_mode: str
    error: str | None


class CastStatusResult(TypedDict, total=False):
    """cast_status 的返回体。"""

    success: Required[bool]
    message: Required[str]
    active: bool
    mode: str
    last_frame_age_s: float | None
    seq: int
    frames_captured: int
    capture_fps: float | None
    consecutive_errors: int
    errors: int
    last_error: str | None
    clip_frames: int
    #: 与 start_cast 出参同义：fast → cast-fast 参考页面的定时器每帧毫秒数，image → 1000
    frame_duration_ms: int
    #: 流畅模式当前帧率（每秒张数，运行中可被 start_cast(fps=…) / glasscast set-fps 改）
    fast_fps: float
    #: cast-fast 参考页面的定时器每帧毫秒数 round(1000/fast_fps/2)，与当前模式无关
    fast_frame_duration_ms: int
    error: str | None


#: fps 不合法时念给用户的话（规格：用中文说明范围）
FPS_RANGE_MESSAGE = f"帧率只能调到每秒 {FAST_FPS_MIN:g} 到 {FAST_FPS_MAX:g} 张，可以带小数"


def _is_blank(value: object) -> bool:
    """可选参数"没给"：None 或空白字符串（大模型常给可选参数填 null 或空串）。"""
    return value is None or (isinstance(value, str) and not value.strip())


def _with_fast_fps(template: str, fast_fps: float) -> str:
    return template.replace(_FAST_FPS_SLOT, f"{fast_fps:g}")


def normalize_mode(value: object) -> PublicMode | None:
    """把工具入参里的 mode 归一化成 ``image`` / ``fast``；空值（None、空串）当作默认 ``image``，
    无法识别（包括非字符串）返回 None。旧的 ``clip`` 归到 ``fast``。"""
    if value is None:
        return "image"
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not text:
        return "image"
    return _MODE_ALIASES.get(text)


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(float(value), digits)


class CastController:
    """三个工具背后的逻辑。所有方法都不会阻塞在截屏/编码上，可以直接在事件循环里调用。

    - ``first_frame_timeout_s``：切换模式后最多等多久第一帧（或第一次截屏失败）。
      等到失败时会关掉采集并返回 ``success: false``——最常见的是 macOS 没有给屏幕录制权限。
      超时只说明第一帧慢，不算失败。
    """

    def __init__(
        self,
        settings: Settings,
        pipeline: CastPipeline,
        *,
        first_frame_timeout_s: float = DEFAULT_FIRST_FRAME_TIMEOUT_S,
        poll_interval_s: float = 0.01,
    ) -> None:
        if first_frame_timeout_s < 0 or poll_interval_s <= 0:
            raise ValueError("first_frame_timeout_s 必须 ≥ 0，poll_interval_s 必须 > 0")
        self.settings = settings
        self.pipeline = pipeline
        self.first_frame_timeout_s = float(first_frame_timeout_s)
        self.poll_interval_s = float(poll_interval_s)

    # ------------------------------------------------------------------ start_cast

    async def start_cast(self, mode: object = "image", fps: object = None) -> StartCastResult:
        target = normalize_mode(mode)
        if target is None:
            return {
                "success": False,
                "mode": str(mode),
                "message": "不支持这个投屏模式，请说普通模式或流畅模式",
                "error": f"mode 只能是 image 或 fast，收到 {mode!r}",
            }
        pipeline = self.pipeline
        fps_given = not _is_blank(fps)
        if target == "fast" and fps_given:
            try:
                requested = parse_fast_fps(fps)
            except FastFpsError as exc:
                # 不合法：什么都不改（不钳制、不开始投屏），message 说明范围和当前值
                current = pipeline.fast_fps
                return {
                    "success": False,
                    "mode": target,
                    "message": f"{FPS_RANGE_MESSAGE}，现在是每秒 {current:g} 张",
                    "error": str(exc),
                    "fps": current,
                    "fast_fps": current,
                }
            # 先写入运行时 fast_fps 再开始：已在 fast 模式时 start() 什么也不做，节拍由 setter 立即重排；
            # 之后的 _cast_payload 读到的就是新帧率对应的 frame_duration_ms
            pipeline.set_fast_fps(requested)
        previous = pipeline.mode
        before = pipeline.stats()
        # 同一模式但正处在连续失败的退避里（例如 --autostart 时还没有屏幕录制权限）：先停再开，
        # 打断退避立即重试一次，并像切换模式一样等第一帧，而不是直接回"进行中"
        restart = previous == target and before["consecutive_errors"] > 0
        try:
            if restart:
                pipeline.stop()
            pipeline.start(target)
        except RuntimeError as exc:  # 已关闭（服务正在退出）
            return {
                "success": False,
                "mode": target,
                "message": "电脑上的投屏程序正在退出，请重新启动它",
                "error": str(exc),
            }

        first_frame: bool | None = None  # None = 同模式且没有重启：不等第一帧，按当前实际状态报告
        if previous != target or restart:
            outcome = await self._wait_first_frame(target, before["frames_captured"], before["errors"])
            if outcome == "error":
                stats = pipeline.stats()
                # 第一帧就失败：多半是权限或显示器参数问题，继续空转只会刷错误日志，关掉采集
                pipeline.stop(clear=True)
                log.warning("start_cast(%s) 第一帧截屏失败，已停止采集：%s", target, stats["last_error"])
                return {
                    "success": False,
                    "mode": target,
                    "message": self._capture_failed_message(),
                    "error": stats["last_error"],
                }
            first_frame = outcome == "frame"

        payload = self._cast_payload(target)
        payload["previous_mode"] = previous
        payload["first_frame"] = self._has_frame() if first_frame is None else first_frame
        retuned = target == "fast" and fps_given  # 这次调用改了（或重申了）流畅模式帧率
        rate = f"每秒约 {payload['fps']:g} 帧"
        if restart:
            payload["message"] = f"投屏已重新开始（{MODE_LABELS[target]}）" + (f"，{rate}" if retuned else "")
        elif previous == target:
            payload["message"] = f"流畅模式已调到{rate}" if retuned else f"投屏已在进行中（{MODE_LABELS[target]}）"
        elif previous != "off":
            payload["message"] = f"已切换到{MODE_LABELS[target]}" + (f"，{rate}" if retuned else "")
        elif target == "image":
            payload["message"] = "投屏已开始，每秒刷新一次"
        else:
            payload["message"] = f"投屏已开始（流畅模式），{rate}"
        if target == "image" and fps_given:
            payload["message"] += "（帧率只对流畅模式有效，普通模式固定每秒 1 张）"
        log.info(
            "start_cast: %s → %s%s%s",
            previous,
            target,
            f"，fps={payload['fps']:g}" if retuned else "",
            "（重试）" if restart else "",
        )
        return payload

    async def _wait_first_frame(self, target: PublicMode, frames_before: int, errors_before: int) -> str:
        """轮询 stats 直到出现新帧（"frame"）、新错误（"error"）或超时（"timeout"）。"""
        deadline = time.monotonic() + self.first_frame_timeout_s
        while True:
            stats = self.pipeline.stats()
            if stats["frames_captured"] > frames_before:
                return "frame"
            if stats["errors"] > errors_before:
                return "error"
            if stats["mode"] != target:  # 期间被别的调用切走了
                return "timeout"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "timeout"
            await anyio.sleep(min(self.poll_interval_s, remaining))

    def _has_frame(self) -> bool:
        """当前模式下是否已经有可发布的画面（同模式重复调用时用，不等待）。image 与 fast 共用 snapshot。"""
        return self.pipeline.latest_snapshot() is not None

    def _capture_failed_message(self) -> str:
        if self.settings.source == "screen":
            return "电脑截屏失败，请在电脑的系统设置里给终端打开屏幕录制权限，然后重启投屏程序"
        return "电脑截屏失败，请检查电脑上的投屏程序"

    def _cast_payload(self, mode: PublicMode) -> StartCastResult:
        pipeline, settings = self.pipeline, self.settings
        width, height = pipeline.canvas
        # fast_fps 与帧时长一起读（运行中可能被 glasscast set-fps 并发修改），保证出参自洽
        fast_fps, fast_duration = pipeline.fast_settings()
        hops = emit_hops_for(fast_fps, settings.emit_hop_ms)
        return {
            "success": True,
            "mode": mode,
            "url": settings.image_url,
            "timer_url": settings.timer_url,
            "width": width,
            "height": height,
            # 用流水线的运行时参数（不是 settings 里的初始值），实现指南 §9.1
            "frame_duration_ms": fast_duration if mode == "fast" else pipeline.page_frame_duration_ms(mode),
            "fps": fast_fps,
            "fast_fps": fast_fps,
            # cast-emit 页面（流畅模式，emit 事件总线自环）每换一张图的跳数，research.md §9.8
            "hops": hops,
            # 页面按 hops 估算的拉图速率（张/秒）。fast_fps 低于下限时它会高于 fps：画面每秒仍只更新 fps 次，
            # 多拉的是重复帧（只影响流量）
            "page_fps": round(1000.0 / (hops * settings.emit_hop_ms), 1),
            "message": "",
        }

    # ------------------------------------------------------------------ stop_cast

    def stop_cast(self) -> StopCastResult:
        previous = self.pipeline.mode
        # clear=True：停止后地址不再能取到最后一帧画面（隐私），服务端改回"投屏已停止"占位图
        self.pipeline.stop(clear=True)
        log.info("stop_cast: %s → off", previous)
        return {
            "success": True,
            "previous_mode": previous,
            "message": "投屏已停止" if previous != "off" else "投屏本来就没有开启",
        }

    # ------------------------------------------------------------------ cast_status

    def cast_status(self) -> CastStatusResult:
        stats = self.pipeline.stats()
        mode = stats["mode"]
        active = mode != "off" and not stats["closed"]
        age = _round(stats["last_frame_age_s"])
        consecutive = stats["consecutive_errors"]
        label = MODE_LABELS.get(mode, mode)
        if not active:
            message = "现在没有在投屏"
        elif consecutive and (age is None or age >= STALE_AFTER_S):
            message = f"投屏已开启，但电脑截屏已经连续失败 {consecutive} 次，请看一下电脑上的投屏程序"
        elif age is None:
            message = "投屏已开启，但电脑端还没有截到画面"
        elif age < STALE_AFTER_S:
            message = f"正在投屏，{label}，画面刚刚更新"
        else:
            message = f"正在投屏，但画面已经 {round(age)} 秒没更新了，请看一下电脑上的投屏程序"
        # 字段与第一版相同（实现指南 §9.1：cast_status 不变）。frame_duration_ms 跟随当前模式，与 start_cast
        # 出参含义一致：fast → 定时器每帧时长，image → 1000；off / 内部 clip 仍报 clip 段内帧时长。
        if mode == "fast":
            frame_duration = stats.get("fast_frame_duration_ms", stats["frame_duration_ms"])
        elif mode == "image":
            frame_duration = IMAGE_FRAME_DURATION_MS
        else:
            frame_duration = stats["frame_duration_ms"]
        return {
            "success": True,
            "active": active,
            "mode": mode,
            "last_frame_age_s": age,
            "seq": stats["seq"],
            "frames_captured": stats["frames_captured"],
            "capture_fps": stats["capture_fps"],
            "consecutive_errors": consecutive,
            "errors": stats["errors"],
            "last_error": stats["last_error"],
            "clip_frames": stats["clip_frames"],
            "frame_duration_ms": frame_duration,
            # 运行时的当前值（与 frame_duration_ms 不同，和当前模式无关），"快一点/慢一点"据此加减
            "fast_fps": stats["fast_fps"],
            "fast_frame_duration_ms": stats["fast_frame_duration_ms"],
            "message": message,
        }


def build_mcp_server(
    settings: Settings,
    pipeline: CastPipeline,
    *,
    controller: CastController | None = None,
    first_frame_timeout_s: float = DEFAULT_FIRST_FRAME_TIMEOUT_S,
) -> MCPServer:
    """创建注册好 3 个工具的 MCPServer（mcp SDK 2.x）。

    每个 :func:`~glasscast.server.create_app` 都要新建一个：SDK 的 session manager 的 ``run()``
    每个实例只能调用一次（见 ``StreamableHTTPSessionManager.run``）。
    """
    ctl = controller or CastController(settings, pipeline, first_frame_timeout_s=first_frame_timeout_s)
    fast_fps = pipeline.fast_fps
    mode_schema = {**MODE_PARAM_SCHEMA, "description": _with_fast_fps(MODE_PARAM_DESCRIPTION, fast_fps)}
    server: MCPServer = MCPServer(
        "glasscast",
        title="电脑投屏",
        description="把电脑屏幕投到千问 AI 眼镜：开始/停止投屏（普通或流畅模式）、查询投屏状态",
        instructions=_with_fast_fps(SERVER_INSTRUCTIONS, fast_fps),
        version=__version__,
    )

    def failure(tool: str, exc: BaseException) -> dict[str, Any]:
        # 工具里的任何异常都转成 success=false 的 JSON 对象（三个 TypedDict 都兼容这个形状），
        # Skill 能按 message 处理
        log.exception("工具 %s 执行出错", tool)
        return {
            "success": False,
            "message": "电脑上的投屏程序出错了，请查看它的日志",
            "error": f"{type(exc).__name__}: {exc}",
        }

    async def start_cast(mode: Any = "image", fps: Any = None) -> StartCastResult:
        try:
            return await ctl.start_cast(mode, fps)
        except Exception as exc:  # noqa: BLE001
            return failure("start_cast", exc)  # type: ignore[return-value]

    # 本模块用了 from __future__ import annotations：注解是字符串，SDK 只按模块全局变量求值，看不到这里的
    # 局部 mode_schema。所以先定义函数，再把求值好的注解放回去，然后才注册（SDK 在注册时读签名）。
    start_cast.__annotations__ = {
        "mode": Annotated[Any, WithJsonSchema(mode_schema)],
        "fps": Annotated[Any, WithJsonSchema(FPS_PARAM_SCHEMA)],
        "return": StartCastResult,
    }
    server.tool(
        name="start_cast",
        title="开始投屏",
        description=_with_fast_fps(START_CAST_DESCRIPTION, fast_fps),
        annotations=ToolAnnotations(
            title="开始投屏", readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
        ),
    )(start_cast)

    @server.tool(
        name="stop_cast",
        title="停止投屏",
        description=STOP_CAST_DESCRIPTION,
        annotations=ToolAnnotations(
            title="停止投屏", readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
        ),
    )
    async def stop_cast() -> StopCastResult:
        try:
            return ctl.stop_cast()
        except Exception as exc:  # noqa: BLE001
            return failure("stop_cast", exc)  # type: ignore[return-value]

    @server.tool(
        name="cast_status",
        title="投屏状态",
        description=CAST_STATUS_DESCRIPTION,
        annotations=ToolAnnotations(title="投屏状态", readOnlyHint=True, idempotentHint=True, openWorldHint=False),
    )
    async def cast_status() -> CastStatusResult:
        try:
            return ctl.cast_status()
        except Exception as exc:  # noqa: BLE001
            return failure("cast_status", exc)  # type: ignore[return-value]

    return server
