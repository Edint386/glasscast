"""``glasscast`` 命令行入口（实现指南 §4.4）。

子命令：

- ``glasscast serve``：启动 HTTP + MCP 服务（一个进程一个端口）。
- ``glasscast snapshot OUT.jpg``：截一张并按画布处理，用来调分辨率 / 裁剪区域。
- ``glasscast monitors``：列出显示器（给 ``--monitor`` / ``--region`` 用）。
- ``glasscast fset build|inspect|extract``：`.fset` 帧合集工具（格式见 research.md §4）。
- ``glasscast set-fps FPS``：不重启地修改正在运行的 serve 的流畅模式档位（``POST /admin/fps``）。
- ``glasscast config``：查看正在运行的 serve 的当前配置（``GET /admin/config``）。

配置优先级：命令行 > 环境变量 ``GLASSCAST_*`` > 默认值（见 :mod:`glasscast.config`）。
``set-fps`` / ``config`` 只连本机：``--host`` / ``--port`` / ``--token`` 不给时读 ``GLASSCAST_HOST`` /
``GLASSCAST_PORT`` / ``GLASSCAST_TOKEN``，再不行用 127.0.0.1:8766。管理接口在公网 vhost 上是 404（docs/deploy.md）。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, Any

from PIL import Image

from . import __version__
from .config import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    ENV_VARS,
    ConfigError,
    Settings,
    load_settings,
    parse_canvas,
    parse_region,
)
from .frames import (
    CANVAS,
    FrameSource,
    ScreenSource,
    SyntheticSource,
    apply_color_mode,
    check_budget,
    encode_jpeg,
    encode_jpeg_target,
    encode_png,
    find_mozjpeg,
    fit_to_canvas,
    list_monitors,
)
from .fset import FsetError, build_fset, detect_frame_format, inspect_fset, parse_fset
from .pipeline import FAST_FPS_RANGE_TEXT, device_fps_for, emit_hops_for, parse_fast_fps

if TYPE_CHECKING:  # httpx 只在 set-fps / config 里用到，运行时延迟导入
    import httpx

__all__ = ["admin_client", "build_parser", "main", "make_source", "print_banner", "probe_source"]

log = logging.getLogger("glasscast")

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2

_MACOS_PERMISSION_HINT = (
    "macOS 需要「屏幕录制」权限：首次截屏会弹系统授权框。请在 系统设置 → 隐私与安全性 → 屏幕录制 里"
    "打开运行本命令的终端（Terminal / iTerm / VS Code），然后重启 glasscast。"
    "被拒绝时只能截到黑屏/桌面壁纸或直接报错；没有权限也想联调时用 --source synthetic。"
)


# --------------------------------------------------------------------------- 参数解析


def _argtype(parse: Callable[[str], Any], name: str) -> Callable[[str], Any]:
    """把 config 里的解析函数包装成 argparse 的 type（错误变成 argparse 的友好报错）。"""

    def convert(text: str) -> Any:
        try:
            return parse(text)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from None

    convert.__name__ = name
    return convert


def _region_text(text: str) -> str:
    """``--region`` 只在 argparse 里校验格式，原字符串交给 config 解析。

    不能让 argparse 直接返回 :func:`parse_region` 的结果：``none`` / ``full`` 会变成 None，
    而 :func:`~glasscast.config.load_settings` 把 None 当作"命令行没给"，环境变量 ``GLASSCAST_REGION``
    就会盖过 ``--region none``，违反"命令行 > 环境变量"。原样传字符串时 ``"none"`` 不是 None，
    config 解析成全屏后正常覆盖环境变量。
    """
    parse_region(text)
    return text


def _fps_arg(text: str) -> float:
    """``set-fps`` 的 FPS：本地先按服务端同一个规则校验（1～10，可带小数），不合法直接报参数错误。"""
    return parse_fast_fps(text)


def _add_admin_options(parser: argparse.ArgumentParser) -> None:
    """``set-fps`` / ``config`` 共用：连哪个本机 glasscast serve、用什么 token。"""
    parser.add_argument(
        "--host", help=f"glasscast serve 的地址，默认读 {ENV_VARS['host']}，再默认 {DEFAULT_HOST}（只连本机）"
    )
    parser.add_argument("--port", type=int, help=f"glasscast serve 的端口，默认读 {ENV_VARS['port']}，再默认 {DEFAULT_PORT}")
    parser.add_argument(
        "--token",
        help=f"与 glasscast serve 相同的 Bearer Token，默认读环境变量 {ENV_VARS['token']}（建议用环境变量，命令行参数会出现在进程列表里）",
    )
    parser.add_argument("--json", action="store_true", help="原样输出接口返回的 JSON")


def _add_capture_options(parser: argparse.ArgumentParser) -> None:
    """serve 与 snapshot 共用的截屏/画布参数（默认 None = 交给环境变量和默认值）。"""
    parser.add_argument(
        "--source",
        choices=["screen", "synthetic"],
        help="画面来源：screen 截屏（默认）；synthetic 合成画面（时间戳 + 计数器，不需要屏幕录制权限）",
    )
    parser.add_argument("--monitor", type=int, help="mss 显示器下标：1 = 主屏（默认），0 = 所有显示器拼在一起")
    parser.add_argument(
        "--region",
        type=_argtype(_region_text, "region"),
        metavar="L,T,W,H",
        help="只截显示器内的一块区域（相对显示器左上角，macOS 为逻辑点）；none = 全屏",
    )
    parser.add_argument(
        "--fit", dest="fit_mode", choices=["fit", "crop"], help="fit：等比缩放加黑边（默认）；crop：居中裁剪铺满"
    )
    parser.add_argument(
        "--canvas",
        type=_argtype(parse_canvas, "canvas"),
        metavar="WxH",
        help=f"输出画布，默认 {CANVAS[0]}x{CANVAS[1]}（S1 一屏预算，research.md §2.3）",
    )
    parser.add_argument("--jpeg-quality", type=int, metavar="Q", help="JPEG 质量 1～100，默认 70")
    parser.add_argument(
        "--jpeg-target-kb",
        type=float,
        metavar="KB",
        help="每张图不超过这么多 KB，取能做到的最高质量（--jpeg-quality 变成上限，research.md §9.22）；默认 0 = 固定质量",
    )
    parser.add_argument(
        "--jpeg-encoder",
        choices=["auto", "mozjpeg", "pillow"],
        help="JPEG 编码器：auto（默认，装了 mozjpeg 就用，同等观感体积小 16～25%%，research.md §9.21）/ mozjpeg / pillow",
    )
    parser.add_argument(
        "--color",
        dest="color_mode",
        choices=["gray", "rgb"],
        help="输出色彩：gray 8 位灰度（默认；眼镜屏幕是单色绿，体积更小，research.md §9.2 E6）/ rgb 彩色",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="glasscast",
        description=(
            "把电脑屏幕投到千问 AI 眼镜，MCP 控制。两种模式都是眼镜页面轮询灰度 JPEG："
            "image（普通，cast-image 页面，每秒 1 帧）与 fast（流畅，cast-emit 页面用 emit:// 事件倒数换图，"
            "档位不等于真实帧数，默认 4 档时真机约每秒 1.5 帧，research.md §9.19、§9.20）。"
        ),
    )
    parser.add_argument("--version", action="version", version=f"glasscast {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    # ---- serve
    env_lines = "\n".join(f"  {var}" for var in ENV_VARS.values())
    serve = sub.add_parser(
        "serve",
        help="启动 HTTP + MCP 服务",
        description="启动投屏服务：眼镜页面不断拉 /s/<key>/screen.jpg/<任意>.jpg，平台调 /mcp。",
        epilog=f"所有参数都可用环境变量设置（命令行优先）：\n{env_lines}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    serve.add_argument(
        "--public-url",
        metavar="URL",
        help="对外可达的 HTTPS 根地址（必填，也可用 GLASSCAST_PUBLIC_URL），例如 https://cast.example.com",
    )
    serve.add_argument("--host", help="监听地址，默认 127.0.0.1（经隧道/反代访问，不要监听 0.0.0.0）")
    serve.add_argument("--port", type=int, help="监听端口，默认 8766")
    serve.add_argument(
        "--token",
        help="MCP Bearer Token。建议改用环境变量 GLASSCAST_TOKEN（命令行参数会出现在进程列表里）；都没有时自动生成",
    )
    serve.add_argument(
        "--cast-key", metavar="KEY", help="图片地址里的随机段（8～128 位字母数字-_）；默认每次启动随机生成 16 位"
    )
    _add_capture_options(serve)
    serve.add_argument(
        "--image-fps",
        type=float,
        metavar="F",
        help="image 模式截帧频率，默认 1。页面每秒只拉一次；调高只能缩短拉到的画面年龄（平均约 0.5/F 秒），"
        "代价是 CPU；要更高帧率用 fast 模式",
    )
    serve.add_argument(
        "--fast-fps",
        type=float,
        metavar="F",
        help=f"fast（流畅）模式的档位（{FAST_FPS_RANGE_TEXT}，可带小数），默认 4。电脑每秒截 F 张；cast-emit 页面每换一张图倒数 hops × 3 步，"
        "hops 按 --emit-hop-ms 换算、经 start_cast 出参下发。真机帧数低于 F：4 档约每秒 1.5 帧，是实测稳定上限，"
        "4.5 以上可能卡住；不稳时调低（例如 2 档，约每秒 1 帧）",
    )
    serve.add_argument(
        "--emit-hop-ms",
        type=float,
        metavar="MS",
        help="cast-emit 页面 hops 的换算系数（毫秒，默认 20），不是真机每跳耗时（真机每跳 3 步，约 39 ms）。"
        "start_cast 出参 hops = round(1000/fast_fps/MS)；"
        "要微调真机换图节拍时按日志里的图片请求间隔调整（research.md §9.8）",
    )
    serve.add_argument(
        "--gif-levels",
        type=int,
        choices=[0, 2, 4, 8, 16],
        help="每帧另出一张这么多级的灰度 GIF（…/screen.jpg/<stamp>.gif，research.md §9.24）；默认 0 = 不出",
    )
    serve.add_argument(
        "--gif-delay-ms",
        type=int,
        metavar="MS",
        help="GIF 帧时长（毫秒，10 的倍数，默认 100）：<gif repeat-count=1> 每张至少显示这么久",
    )
    serve.add_argument(
        "--autostart",
        choices=["off", "image", "fast"],
        help="启动后立即开始采集的模式；默认 off（等 start_cast）",
    )
    serve.add_argument(
        "--allowed-host",
        dest="allowed_hosts",
        action="append",
        metavar="HOST",
        help="MCP 额外放行的 Host 头（可重复）；public-url 的主机和本机回环已自动放行",
    )
    serve.add_argument(
        "--no-dns-rebinding-protection",
        dest="dns_rebinding_protection",
        action="store_const",
        const=False,
        help="关闭 MCP 的 Host/Origin 校验（只在反代改写了 Host 又无法修改时使用）",
    )
    serve.add_argument(
        "--log-level", choices=["debug", "info", "warning", "error"], default="info", help="日志级别，默认 info"
    )
    serve.add_argument("--no-access-log", action="store_true", help="不打印访问日志（默认每个请求一行，key 已打码）")
    serve.add_argument("--no-probe", action="store_true", help="启动时不试截一张（默认会试截，用来提前触发权限弹窗）")
    serve.set_defaults(handler=cmd_serve)

    # ---- snapshot
    snapshot = sub.add_parser(
        "snapshot",
        help="截一张并按画布处理（调分辨率 / 裁剪区域）",
        description="截一张屏幕，缩放/裁剪到画布后保存。扩展名 .png 存 PNG，其余存 JPEG。",
    )
    snapshot.add_argument("out", metavar="OUT", type=Path, help="输出文件，例如 out.jpg")
    _add_capture_options(snapshot)
    snapshot.add_argument("--raw", action="store_true", help="保存原始截屏（不缩放到画布），用来量 --region 坐标")
    snapshot.set_defaults(handler=cmd_snapshot)

    # ---- monitors
    monitors = sub.add_parser("monitors", help="列出显示器（坐标给 --monitor / --region 用）")
    monitors.add_argument("--json", action="store_true", help="输出 JSON")
    monitors.set_defaults(handler=cmd_monitors)

    # ---- fset
    fset = sub.add_parser("fset", help=".fset 帧合集工具：build / inspect / extract")
    fset_sub = fset.add_subparsers(dest="fset_command", metavar="ACTION")

    build = fset_sub.add_parser(
        "build",
        help="把若干图片打包成 .fset",
        description="把图片打包成 .fset。默认把每张图解码后重新编码成 8-bit RGBA PNG（与官方样本一致，research.md §4）。",
    )
    build.add_argument("out", metavar="OUT.fset", type=Path)
    build.add_argument("images", metavar="IMG", type=Path, nargs="+")
    build.add_argument(
        "--canvas", type=_argtype(parse_canvas, "canvas"), metavar="WxH", help="先把每张图缩放到画布（默认保持原尺寸）"
    )
    build.add_argument("--fit", dest="fit_mode", choices=["fit", "crop"], default="fit", help="配合 --canvas 使用")
    build.add_argument(
        "--format",
        dest="frame_format",
        choices=["png", "jpeg", "raw"],
        default="png",
        help="帧编码：png（默认，RGBA）/ jpeg / raw（原样打包文件字节，不解码）",
    )
    build.add_argument("--jpeg-quality", type=int, default=70, metavar="Q", help="--format jpeg 时的质量，默认 70")
    build.set_defaults(handler=cmd_fset_build)

    inspect = fset_sub.add_parser("inspect", help="查看 .fset 结构")
    inspect.add_argument("file", metavar="FILE.fset", type=Path)
    inspect.add_argument("--json", action="store_true", help="输出 JSON")
    inspect.add_argument("--frames", action="store_true", help="逐帧列出大小和尺寸")
    inspect.add_argument("--no-strict", action="store_true", help="允许尾部多余字节和 reserved 非零")
    inspect.set_defaults(handler=cmd_fset_inspect)

    extract = fset_sub.add_parser("extract", help="把 .fset 的每一帧存成单独文件")
    extract.add_argument("file", metavar="FILE.fset", type=Path)
    extract.add_argument("outdir", metavar="OUTDIR", type=Path)
    extract.add_argument("--prefix", default="frame_", help="文件名前缀，默认 frame_")
    extract.add_argument("--no-strict", action="store_true", help="允许尾部多余字节和 reserved 非零")
    extract.set_defaults(handler=cmd_fset_extract)

    fset.set_defaults(handler=lambda args: _print_help(fset))

    # ---- set-fps / config（本机管理接口，不重启）
    set_fps = sub.add_parser(
        "set-fps",
        help=f"不重启地修改流畅模式档位（{FAST_FPS_RANGE_TEXT}）",
        description=(
            f"把正在运行的 glasscast serve 的流畅模式档位改成 FPS（{FAST_FPS_RANGE_TEXT}，可以带小数，例如 2.5），"
            "调用本机的 POST /admin/fps。档位不是真实帧数：4 档真机约每秒 1.5 帧。电脑端截帧立即按新档位进行；"
            "眼镜上已打开的流畅模式页面要等下一次 "
            "start_cast（对眼镜说「投屏调到 N 档」或「切到流畅模式」，模型会带新的 hops 再打开一次页面）才按新节拍拉图。"
        ),
    )
    set_fps.add_argument(
        "fps", metavar="FPS", type=_argtype(_fps_arg, "fps"), help=f"档位，{FAST_FPS_RANGE_TEXT}，可以带小数"
    )
    _add_admin_options(set_fps)
    set_fps.set_defaults(handler=cmd_set_fps)

    config = sub.add_parser(
        "config",
        help="查看正在运行的 glasscast 的当前配置（模式、档位、色彩、画布）",
        description="调用本机的 GET /admin/config，显示运行中的模式、流畅模式档位和真机帧数、普通模式截帧频率、帧时长、色彩和画布。",
    )
    _add_admin_options(config)
    config.set_defaults(handler=cmd_config)
    return parser


def _print_help(parser: argparse.ArgumentParser) -> int:
    parser.print_help()
    return EXIT_USAGE


def _cli_overrides(args: argparse.Namespace, names: Sequence[str]) -> dict[str, Any]:
    """从 argparse 结果里取出 config 字段（None = 命令行没给）。"""
    return {name: getattr(args, name, None) for name in names}


_SERVE_FIELDS = (
    "public_url",
    "host",
    "port",
    "token",
    "cast_key",
    "source",
    "monitor",
    "region",
    "fit_mode",
    "canvas",
    "image_fps",
    "fast_fps",
    "emit_hop_ms",
    "jpeg_quality",
    "jpeg_encoder",
    "jpeg_target_kb",
    "gif_levels",
    "gif_delay_ms",
    "color_mode",
    "autostart",
    "allowed_hosts",
    "dns_rebinding_protection",
)
_CAPTURE_FIELDS = ("source", "monitor", "region", "fit_mode", "canvas", "jpeg_quality", "jpeg_encoder", "jpeg_target_kb", "color_mode")
_COLOR_LABELS = {"gray": "灰度", "rgb": "彩色"}


# --------------------------------------------------------------------------- 公共工具


def configure_logging(level: str = "info") -> None:
    numeric = getattr(logging, level.upper(), logging.INFO)
    # 时间戳带毫秒：要从访问日志里核对 cast-emit 的换图间隔（真机 4 档约 630～720 ms）。
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
    if numeric > logging.DEBUG:
        # mcp SDK 每个请求都打 INFO（"Terminating session: None" 等），只保留警告
        for name in ("mcp", "httpx", "httpx2"):
            logging.getLogger(name).setLevel(logging.WARNING)


def make_source(settings: Settings) -> FrameSource:
    """按配置创建帧源。``screen`` 构造时不碰 mss，第一次 grab 才截屏（frames.ScreenSource）。"""
    if settings.source == "synthetic":
        return SyntheticSource(settings.canvas)
    return ScreenSource(settings.monitor, settings.region)


def probe_source(source: FrameSource) -> tuple[tuple[int, int] | None, str | None, bool]:
    """试截一张：返回 ``(原始尺寸, 错误信息, 是否全黑)``。全黑通常意味着 macOS 没给屏幕录制权限。"""
    try:
        im = source.grab()
    except Exception as exc:  # noqa: BLE001 - 只用于提示
        return None, f"{type(exc).__name__}: {exc}", False
    extrema = im.convert("RGB").getextrema()
    all_black = all(high == 0 for _low, high in extrema)  # type: ignore[union-attr]
    return im.size, None, all_black


def _display_width(text: str) -> int:
    """终端显示宽度：全角/宽字符（中文）算 2 列。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _describe_jpeg_quality(settings: Settings) -> str:
    if settings.jpeg_target_kb > 0:
        return f"每张 ≤ {settings.jpeg_target_kb:g} KB 取最高质量（上限 {settings.jpeg_quality}）"
    return f"质量 {settings.jpeg_quality}"


def _describe_jpeg_encoder(encoder: str) -> str:
    """启动信息里的编码器：auto 时写出实际用的那个。"""
    if encoder == "pillow":
        return "Pillow"
    found = find_mozjpeg()
    if encoder == "auto":
        return "mozjpeg（auto）" if found else "Pillow（auto，没找到 mozjpeg）"
    return "mozjpeg" if found else "mozjpeg（没找到！brew install mozjpeg）"


def _describe_source(settings: Settings) -> str:
    if settings.source == "synthetic":
        return "合成画面（synthetic，不截屏）"
    region = "全屏" if settings.region is None else "区域 {},{} {}×{}".format(*settings.region)
    return f"屏幕（显示器 {settings.monitor}，{region}）"


def _token_origin(args: argparse.Namespace, settings: Settings, env: Mapping[str, str]) -> str:
    if settings.token_generated:
        return "generated"
    if getattr(args, "token", None):
        return "cli"
    if env.get(ENV_VARS["token"], "").strip():
        return "env"
    return "unknown"


def print_banner(
    settings: Settings,
    *,
    token_origin: str,
    probe: tuple[tuple[int, int] | None, str | None, bool] | None = None,
    out: Any = None,
) -> None:
    """启动信息：完整地址、MCP、Token（仅自动生成时打印）、页面参数、权限提示（实现指南 §4.4）。"""
    out = out or sys.stdout
    width, height = settings.canvas

    def line(label: str, value: str) -> None:
        print(f"  {_pad(label, 11)}{value}", file=out)

    print(f"glasscast {__version__} · 电脑投屏到千问 AI 眼镜", file=out)
    source = _describe_source(settings)
    if probe is not None and probe[0] is not None:
        source += f"；截屏 {probe[0][0]}×{probe[0][1]} → 画布 {width}×{height}（{settings.fit_mode}）"
    line("画面来源", source)
    line("输出", f"{width}×{height} {_COLOR_LABELS.get(settings.color_mode, settings.color_mode)} JPEG，{_describe_jpeg_quality(settings)}，编码器 {_describe_jpeg_encoder(settings.jpeg_encoder)}")
    line("本地监听", settings.local_url)
    line("公网地址", settings.public_url)
    line("图片地址", f"{settings.image_url}    ← 页面的 url（image / fast 共用）")
    line("定时器", f"{settings.timer_url}    ← 仅 cast-fast 参考页面（本地模拟器）用")
    line("状态页", settings.status_url)
    line("MCP", f"{settings.mcp_url}    （streamable HTTP，认证方式 Bearer Token）")
    if token_origin == "generated":
        line("Token", f"{settings.token}")
        line("", "↑ 自动生成，只显示这一次；重启会变。用环境变量 GLASSCAST_TOKEN 固定后再填到平台插件里")
    elif token_origin == "cli":
        line("Token", "来自 --token（不显示）")
    else:
        line("Token", f"来自环境变量 {ENV_VARS['token']}（不显示）")
    if settings.cast_key_generated:
        line("cast_key", f"随机生成（每次启动都变；用 {ENV_VARS['cast_key']} 固定）")
    line("普通模式", f"image：截帧 {settings.image_fps:g} fps，页面 <tick> 每秒 1 帧")
    hops = emit_hops_for(settings.fast_fps, settings.emit_hop_ms)
    line(
        "流畅模式",
        f"fast：{settings.fast_fps:g} 档，真机约每秒 {device_fps_for(hops):g} 帧；电脑每秒截 {settings.fast_fps:g} 张，"
        f"cast-emit 页面每 {hops} 跳换一张（hops 按换算系数 {settings.emit_hop_ms:g} 算出，它不是真机每跳耗时，由 start_cast 出参下发，research.md §9.8）",
    )
    line(
        "调档位",
        f"运行中可调（{FAST_FPS_RANGE_TEXT}）：对眼镜说「投屏调到 N 档」，或在电脑上运行 glasscast set-fps N",
    )
    line("自动开始", settings.autostart if settings.autostart != "off" else "否（等平台调用 start_cast）")

    notes: list[str] = []
    if settings.source == "screen":
        notes.append(_MACOS_PERMISSION_HINT)
        if probe is not None:
            if probe[1] is not None:
                notes.append(f"试截屏失败：{probe[1]}（检查屏幕录制权限和 --monitor / --region）")
            elif probe[2]:
                notes.append("试截到的是全黑画面：很可能没有屏幕录制权限，或者屏幕已锁定/休眠")
    notes.extend(settings.warnings())
    if notes:
        print("注意：", file=out)
        for note in notes:
            print(f"  - {note}", file=out)
    out.flush()


def _load(args: argparse.Namespace, names: Sequence[str], **kwargs: Any) -> Settings:
    return load_settings(_cli_overrides(args, names), os.environ, **kwargs)


def _fail(message: str, code: int = EXIT_FAIL) -> int:
    print(f"glasscast: {message}", file=sys.stderr)
    return code


# --------------------------------------------------------------------------- serve


def cmd_serve(args: argparse.Namespace) -> int:
    configure_logging(args.log_level)
    try:
        settings = _load(args, _SERVE_FIELDS)
    except ConfigError as exc:
        return _fail(f"配置错误：\n{exc}", EXIT_USAGE)

    # 延迟导入：serve 以外的子命令不需要加载 starlette / mcp / uvicorn
    import uvicorn

    from .pipeline import CastPipeline

    try:
        from .server import create_app
    except ImportError as exc:
        return _fail(_mcp_import_hint(exc))

    source = make_source(settings)
    probe = None
    if settings.source == "screen" and not args.no_probe:
        probe = probe_source(source)
    pipeline = CastPipeline(source, close_source=True, **settings.pipeline_kwargs())
    try:
        app = create_app(settings, pipeline, access_log=not args.no_access_log)
        print_banner(settings, token_origin=_token_origin(args, settings, os.environ), probe=probe)
        if settings.autostart != "off":
            pipeline.start(settings.autostart)
        config = uvicorn.Config(
            app,
            host=settings.host,
            port=settings.port,
            log_config=None,  # 用上面 basicConfig 的格式
            log_level=args.log_level,
            access_log=False,  # uvicorn 的访问日志会打出含 key 的完整路径；用 AccessLogMiddleware 代替
            server_header=False,
            # 空闲连接保留 2 小时：OpenResty 的 upstream keepalive 池（keepalive_timeout 3600s，deploy/nginx）
            # 经 SSH 隧道连到这里，池里的连接只要热着，一次请求约 0.25 s；uvicorn 默认 5 s 就断，
            # 池每隔几秒就空一次，每个请求都要重新开 SSH 通道（约 0.7 s），平台 3 s 的工具超时很容易撞上。
            # 要比 nginx 那边长，让 nginx 先关、不会拿到刚被这边关掉的连接（tunnel.sh 每 30 s 保温一次）。
            timeout_keep_alive=7200,
            timeout_graceful_shutdown=3,
            lifespan="on",
        )
        server = uvicorn.Server(config)
        # uvicorn 优雅退出后会把捕获的 SIGINT/SIGTERM 重新发给自己（capture_signals）。SIGINT 默认变成
        # KeyboardInterrupt；SIGTERM 默认直接杀进程、跳过下面的 finally，所以临时也换成 KeyboardInterrupt。
        previous_sigterm = _install_sigterm_as_interrupt()
        try:
            server.run()
        except KeyboardInterrupt:
            pass  # Ctrl-C / SIGTERM：uvicorn 已经优雅关闭，属于正常退出
        finally:
            _restore_sigterm(previous_sigterm)
        print("glasscast 已退出", file=sys.stderr)
        return EXIT_OK if server.started else EXIT_FAIL
    finally:
        pipeline.close()


def _mcp_import_hint(exc: ImportError) -> str:
    """serve 需要 mcp SDK 2.x 的 ``mcp.server.mcpserver``（1.x 里叫 ``mcp.server.fastmcp``，实现指南 §4.3）。

    pyproject 要求 ``mcp>=2.3,<3``，uv.lock 钉在 2.3.0，正常安装碰不到 1.x；但绕过依赖解析时（例如
    ``pip install --no-deps``、直接用 PYTHONPATH 跑源码）环境里可能还是 1.x，这里给出能照做的提示，而不是一段
    ImportError 堆栈。
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        installed = f"当前是 {version('mcp')}"
    except PackageNotFoundError:
        installed = "当前没有安装"
    return (
        f"无法加载 MCP 服务（{type(exc).__name__}: {exc}）。\n"
        f"glasscast serve 需要 mcp 2.3 或更新的 2.x 版本（{installed}）。"
        "请在项目目录运行 uv sync（uv.lock 钉在 mcp 2.3.0）后用 uv run glasscast serve 启动；"
        "不要用 pip 装进已有 mcp 1.x 的环境。"
    )


def _raise_interrupt(signum: int, frame: FrameType | None) -> None:
    raise KeyboardInterrupt


def _install_sigterm_as_interrupt() -> Any:
    if threading.current_thread() is not threading.main_thread():
        return None
    return signal.signal(signal.SIGTERM, _raise_interrupt)


def _restore_sigterm(previous: Any) -> None:
    if previous is not None and threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, previous)


# --------------------------------------------------------------------------- snapshot / monitors


def cmd_snapshot(args: argparse.Namespace) -> int:
    configure_logging("warning")
    try:
        settings = _load(args, _CAPTURE_FIELDS, require_public_url=False, generate_secrets=False)
    except ConfigError as exc:
        return _fail(f"配置错误：\n{exc}", EXIT_USAGE)
    source = make_source(settings)
    try:
        raw = source.grab()
    except Exception as exc:  # noqa: BLE001
        msg = f"截屏失败：{type(exc).__name__}: {exc}"
        if settings.source == "screen":
            msg += "\n" + _MACOS_PERMISSION_HINT
        return _fail(msg)
    finally:
        closer = getattr(source, "close", None)
        if callable(closer):
            closer()

    # 与 serve 输出一致：缩放到画布后按 color_mode 转灰度（research.md §9.2 E6）；--raw 保存原始彩色截屏
    im = raw if args.raw else apply_color_mode(fit_to_canvas(raw, settings.canvas, settings.fit_mode), settings.color_mode)
    is_png = args.out.suffix.lower() == ".png"
    if is_png:
        data = encode_png(im)
    elif settings.jpeg_target_kb > 0:
        data, _ = encode_jpeg_target(
            im,
            round(settings.jpeg_target_kb * 1024),
            quality_max=settings.jpeg_quality,
            quality_min=min(10, settings.jpeg_quality),
            encoder=settings.jpeg_encoder,
        )
    else:
        data = encode_jpeg(im, settings.jpeg_quality, encoder=settings.jpeg_encoder)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(data)
    kind = ("灰度 " if im.mode == "L" else "") + ("PNG" if is_png else "JPEG")
    print(f"{_describe_source(settings)}：截屏 {raw.size[0]}×{raw.size[1]}")
    print(f"已保存 {args.out}：{im.size[0]}×{im.size[1]} {kind}，{len(data):,} 字节")
    for warning in check_budget(im.size[0], im.size[1], len(data), bytes_per_pixel=4 if is_png else 3):
        print(f"注意：{warning}")
    if settings.source == "screen" and all(high == 0 for _low, high in raw.getextrema()):  # type: ignore[union-attr]
        print("注意：截到的是全黑画面，很可能没有屏幕录制权限。\n" + _MACOS_PERMISSION_HINT)
    return EXIT_OK


def format_monitors(monitors: Sequence[Mapping[str, int]]) -> str:
    rows = [f"{'#':<4}{'left':>8}{'top':>8}{'width':>8}{'height':>8}"]
    for i, mon in enumerate(monitors):
        note = "  （所有显示器的并集）" if i == 0 else ("  （主屏）" if i == 1 else "")
        rows.append(f"{i:<4}{mon['left']:>8}{mon['top']:>8}{mon['width']:>8}{mon['height']:>8}{note}")
    rows.append("坐标单位与 mss 一致（macOS 为逻辑点；Retina 下实际截图是物理像素，约 2 倍）。")
    return "\n".join(rows)


def cmd_monitors(args: argparse.Namespace) -> int:
    try:
        monitors = list_monitors()
    except Exception as exc:  # noqa: BLE001
        return _fail(f"读取显示器失败：{type(exc).__name__}: {exc}")
    if args.json:
        print(json.dumps(monitors, ensure_ascii=False, indent=2))
    else:
        print(format_monitors(monitors))
    return EXIT_OK


# --------------------------------------------------------------------------- fset


def _read_file(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError as exc:
        _fail(f"读取 {path} 失败：{exc.strerror or exc}")
        return None


def cmd_fset_build(args: argparse.Namespace) -> int:
    if args.frame_format == "jpeg" and not 1 <= args.jpeg_quality <= 100:
        return _fail("--jpeg-quality 应在 1～100", EXIT_USAGE)
    frames: list[bytes] = []
    sizes: set[tuple[int, int]] = set()
    for path in args.images:
        data = _read_file(path)
        if data is None:
            return EXIT_FAIL
        if args.frame_format == "raw":
            if not data:
                return _fail(f"{path} 是空文件")
            frames.append(data)
            continue
        try:
            with Image.open(path) as opened:
                im = opened.convert("RGB")
        except Exception as exc:  # noqa: BLE001 - Pillow 的各种解码错误
            return _fail(f"无法解码图片 {path}：{exc}")
        if args.canvas is not None:
            im = fit_to_canvas(im, args.canvas, args.fit_mode)
        sizes.add(im.size)
        if args.frame_format == "png":
            frames.append(encode_png(im, alpha=True))  # 官方样本帧是 8-bit RGBA PNG（research.md §4）
        else:
            frames.append(encode_jpeg(im, args.jpeg_quality))

    data = build_fset(frames)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(data)
    print(f"已写入 {args.out}：{len(frames)} 帧，{len(data):,} 字节")
    if len(sizes) > 1:
        print("注意：各帧尺寸不一致，页面只能声明一个 width/height；用 --canvas 统一尺寸")
    if len(sizes) == 1:
        (w, h) = next(iter(sizes))
        print(f'页面写法：<frame-set width="{w}" height="{h}" total-frames="{len(frames)}" .../>')
        for warning in check_budget(w, h, len(data)):
            print(f"注意：{warning}")
    return EXIT_OK


def _format_inspect(path: Path, info: Mapping[str, Any], *, per_frame: bool) -> str:
    n = info["frames"]
    sizes = info["frame_sizes"]
    formats = info["formats"]
    dims = info["png_dims"]
    lines = [
        f"文件      {path}",
        f"帧数      {n}",
        f"头部      {info['header_bytes']} 字节（4 + 12×{n}）",
        f"总大小    {info['total_bytes']:,} 字节（{info['total_bytes'] / 1024:.1f} KiB）",
        f"帧大小    最小 {min(sizes):,} / 平均 {sum(sizes) // n:,} / 最大 {max(sizes):,} 字节",
    ]
    fmt_counts: dict[str, int] = {}
    for fmt in formats:
        fmt_counts[fmt] = fmt_counts.get(fmt, 0) + 1
    lines.append("格式      " + "、".join(f"{fmt.upper()} ×{count}" for fmt, count in fmt_counts.items()))
    dim_counts: dict[str, int] = {}
    for dim in dims:
        key = f"{dim[0]}×{dim[1]}" if dim else "未知（非 PNG）"
        dim_counts[key] = dim_counts.get(key, 0) + 1
    lines.append("尺寸      " + "、".join(f"{key} ×{count}" for key, count in dim_counts.items()))
    if per_frame:
        for i, (size, fmt, dim) in enumerate(zip(sizes, formats, dims, strict=True)):
            dim_text = f"{dim[0]}×{dim[1]}" if dim else "-"
            lines.append(f"  #{i:<4} {fmt:<8} {dim_text:<10} {size:>10,} 字节")
    known = {d for d in dims if d}
    if len(known) == 1 and all(dims):
        w, h = next(iter(known))
        lines.append(f'页面写法  <frame-set width="{w}" height="{h}" total-frames="{n}" .../>')
        lines.extend(f"注意      {warning}" for warning in check_budget(w, h, info["total_bytes"]))
    return "\n".join(lines)


def cmd_fset_inspect(args: argparse.Namespace) -> int:
    data = _read_file(args.file)
    if data is None:
        return EXIT_FAIL
    try:
        info = inspect_fset(data, strict=not args.no_strict)
    except FsetError as exc:
        return _fail(f"{args.file} 不是合法的 .fset：{exc}")
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
    else:
        print(_format_inspect(args.file, info, per_frame=args.frames))
    return EXIT_OK


_EXTENSIONS = {"png": ".png", "jpeg": ".jpg", "unknown": ".bin"}


def cmd_fset_extract(args: argparse.Namespace) -> int:
    data = _read_file(args.file)
    if data is None:
        return EXIT_FAIL
    try:
        frames = parse_fset(data, strict=not args.no_strict)
    except FsetError as exc:
        return _fail(f"{args.file} 不是合法的 .fset：{exc}")
    args.outdir.mkdir(parents=True, exist_ok=True)
    width = max(3, len(str(len(frames) - 1)))
    for i, blob in enumerate(frames):
        ext = _EXTENSIONS[detect_frame_format(blob)]
        (args.outdir / f"{args.prefix}{i:0{width}d}{ext}").write_bytes(blob)
    print(f"已导出 {len(frames)} 帧到 {args.outdir}")
    return EXIT_OK


# --------------------------------------------------------------------------- set-fps / config（本机管理接口）

#: 管理接口请求超时（秒）。接口本身不阻塞，几毫秒就返回
_ADMIN_TIMEOUT_S = 5.0
_MODE_LABELS = {"off": "未投屏", "image": "普通模式", "fast": "流畅模式", "clip": "实验分段模式"}


def _num(value: object) -> str:
    """数字按 ``:g`` 显示（2.0 → 2），缺字段或类型不对时显示 ``?``。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{value:g}"
    return "?"


def admin_client(base_url: str) -> httpx.Client:
    """连本机 glasscast 管理接口的 httpx 客户端（测试里可以换成 MockTransport）。

    ``trust_env=False``：只连本机，不走 ``HTTP(S)_PROXY`` 环境变量里的代理。
    """
    import httpx

    return httpx.Client(base_url=base_url, timeout=_ADMIN_TIMEOUT_S, trust_env=False)


def _admin_target(args: argparse.Namespace, env: Mapping[str, str]) -> tuple[str, str | None]:
    """``(base_url, token)``：命令行 > 环境变量 > 默认值。监听全部地址（0.0.0.0 / ::）时改连本机回环。"""
    host = (args.host or env.get(ENV_VARS["host"], "") or DEFAULT_HOST).strip()
    if host in ("", "0.0.0.0"):
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    if args.port is not None:
        port = args.port
    else:
        raw = env.get(ENV_VARS["port"], "").strip()
        try:
            port = int(raw) if raw else DEFAULT_PORT
        except ValueError:
            raise ValueError(f"环境变量 {ENV_VARS['port']} 应为整数: {raw!r}") from None
    if not 1 <= port <= 65535:
        raise ValueError(f"端口应在 1～65535: {port}")
    token = (args.token or env.get(ENV_VARS["token"], "")).strip() or None
    netloc = f"[{host}]" if ":" in host else host
    return f"http://{netloc}:{port}", token


def _admin_call(
    args: argparse.Namespace, method: str, path: str, body: dict[str, Any] | None = None
) -> tuple[dict[str, Any] | None, int, str]:
    """调本机管理接口，返回 ``(响应 JSON, 退出码, base_url)``。失败时已把中文提示打到 stderr、JSON 为 None。"""
    import httpx

    try:
        base, token = _admin_target(args, os.environ)
    except ValueError as exc:
        return None, _fail(str(exc), EXIT_USAGE), ""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        with admin_client(base) as client:
            response = client.request(method, path, json=body, headers=headers)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return None, _fail(
            f"连不上 glasscast（{base}）：请确认 glasscast serve 正在运行，--host / --port 与它一致"
            f"（默认 {DEFAULT_HOST}:{DEFAULT_PORT}）"
        ), base
    except httpx.TimeoutException:
        return None, _fail(f"glasscast（{base}）{_ADMIN_TIMEOUT_S:g} 秒内没有响应，请看一下它的日志"), base
    except httpx.HTTPError as exc:
        return None, _fail(f"请求 glasscast（{base}）失败：{type(exc).__name__}: {exc}"), base

    status = response.status_code
    if status == 401:
        if token:
            hint = f"Token 不对（401）：--token 或环境变量 {ENV_VARS['token']} 要与 glasscast serve 用的 token 一致"
        else:
            hint = f"缺少 Token（401）：用 --token 或环境变量 {ENV_VARS['token']} 传入 glasscast serve 用的 token"
        return None, _fail(hint), base
    if status == 404:
        return None, _fail(
            f"{base}{path} 返回 404：这个端口上的 glasscast 没有管理接口（版本太旧，或者连到了别的服务），"
            "请更新后重启 glasscast serve"
        ), base
    try:
        data = response.json()
    except ValueError:
        data = None
    if status in (400, 413):
        message = data.get("message") if isinstance(data, dict) else None
        return None, _fail(f"glasscast 拒绝了这个请求：{message or response.text[:200]}", EXIT_USAGE), base
    if status != 200 or not isinstance(data, dict):
        return None, _fail(f"glasscast 返回 HTTP {status}：{response.text[:200]}"), base
    return data, EXIT_OK, base


def cmd_set_fps(args: argparse.Namespace) -> int:
    data, code, _base = _admin_call(args, "POST", "/admin/fps", {"fps": args.fps})
    if data is None:
        return code
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return EXIT_OK
    mode = str(data.get("mode", "?"))
    print(
        f"流畅模式档位：{_num(data.get('previous_fast_fps'))} → {_num(data.get('fast_fps'))}"
        f"（真机约每秒 {_num(data.get('previous_device_fps'))} → {_num(data.get('device_fps'))} 帧，"
        f"cast-emit 页面 hops {_num(data.get('previous_hops'))} → {_num(data.get('hops'))}），"
        f"当前{_MODE_LABELS.get(mode, mode)}"
    )
    if data.get("message"):
        print(data["message"])
    for warning in data.get("warnings") or []:
        print(f"注意：{warning}")
    return EXIT_OK


def cmd_config(args: argparse.Namespace) -> int:
    data, code, base = _admin_call(args, "GET", "/admin/config")
    if data is None:
        return code
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return EXIT_OK
    mode = str(data.get("mode", "?"))
    canvas = data.get("canvas") or [None, None]
    fps_range = data.get("fast_fps_range") or [None, None]

    def line(label: str, value: str) -> None:
        print(f"  {_pad(label, 11)}{value}")

    print(f"glasscast 当前配置（{base}）")
    line("模式", f"{mode}（{_MODE_LABELS.get(mode, mode)}）")
    line(
        "流畅档位",
        f"{_num(data.get('fast_fps'))} 档（真机约每秒 {_num(data.get('device_fps'))} 帧），"
        f"frame_duration_ms={_num(data.get('frame_duration_ms'))}"
        f"（启动时 {_num(data.get('initial_fast_fps'))}；可调 {_num(fps_range[0])}～{_num(fps_range[1])}：glasscast set-fps N）",
    )
    line("普通帧率", f"截帧 {_num(data.get('image_fps'))} fps（页面每秒 1 帧）")
    line("色彩", str(data.get("color_mode", "?")))
    line("画布", f"{_num(canvas[0])}×{_num(canvas[1])}")
    return EXIT_OK


# --------------------------------------------------------------------------- 入口


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler: Callable[[argparse.Namespace], int] | None = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return EXIT_USAGE
    try:
        return handler(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
