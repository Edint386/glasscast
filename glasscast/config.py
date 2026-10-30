"""glasscast 的运行配置。

来源优先级：命令行参数 > 环境变量 ``GLASSCAST_<字段名大写>`` > 默认值（实现指南 §4.1）。

环境变量与字段一一对应，例如 ``GLASSCAST_PUBLIC_URL``、``GLASSCAST_TOKEN``、``GLASSCAST_CAST_KEY``、
``GLASSCAST_FAST_FPS``、``GLASSCAST_COLOR_MODE=rgb``、``GLASSCAST_REGION=0,0,1280,580``、``GLASSCAST_CANVAS=640x290``、
``GLASSCAST_ALLOWED_HOSTS=a.example.com,b.example.com``、``GLASSCAST_DNS_REBINDING_PROTECTION=0``。
完整列表见 :data:`ENV_VARS`。

与平台约束相关的几项：

- ``public_url`` 必须是眼镜和平台都能访问的公网 HTTPS 根地址：真机经手机网络下载图片，编辑器模拟器经平台代理
  拉取，平台从云端调用 MCP，三者都看不到本机 localhost（research.md §1 第 4 条、§2.5）。
  它同时决定 MCP 的 DNS 重绑定保护放行哪个 ``Host``（实现指南 §4.3），填错平台会收到 421。
- ``token`` 是 MCP 的 Bearer Token；眼镜发不了请求头，所以图片地址只能靠路径里的随机 ``cast_key`` 鉴权
  （实现指南 §1）。两者为空时启动时自动生成（token 只打印一次）。
- 对外只有 ``image``（普通，1 fps）和 ``fast``（流畅，``fast_fps`` 是档位，默认 4）两种模式（实现指南 §9.1）。
  fast 页面 ``cast-emit`` 的跳数 ``hops = round(1000 / fast_fps / emit_hop_ms)`` 由 ``start_cast`` 下发
  （research.md §9.8），所以改 ``fast_fps`` 不用改页面（``frame_duration_ms`` 只给 ``cast-fast`` 参考页面用）。
  clip 分段流已下线（research.md §2.4、§9.3），
  它的参数不再出现在配置里（流水线用内部默认值，只服务 ``clip.fset`` 实验接口）。
- ``fast_fps`` 只是**初始值**：运行中可用 MCP ``start_cast(mode=fast, fps=N)`` 或 ``glasscast set-fps N``
  （``POST /admin/fps``）修改，当前值以 :attr:`~glasscast.pipeline.CastPipeline.fast_fps` 为准。启动值与运行中的值
  共用同一个合法范围 1～10（:func:`~glasscast.pipeline.parse_fast_fps`），超出直接报错，不再静默截到 1000 ms。
- ``color_mode`` 默认 ``gray``：眼镜屏幕是单色绿，灰度图显示正常且体积更小（research.md §9.2 E6）。
"""

from __future__ import annotations

import math
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields, replace
from typing import Any, Literal, get_args
from urllib.parse import urlsplit

from .frames import CANVAS, GIF_LEVELS, ColorMode, FitMode, JpegEncoder, check_budget
from .pipeline import (
    DEFAULT_EMIT_HOP_MS,
    DEFAULT_FAST_FPS,
    EMIT_HOP_MS_MAX,
    EMIT_HOP_MS_MIN,
    IMAGE_FRAME_DURATION_MS,
    FastFpsError,
    fast_fps_warnings,
    fast_frame_duration_for,
    parse_fast_fps,
)

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "ENV_PREFIX",
    "ENV_VARS",
    "AutostartMode",
    "ConfigError",
    "Settings",
    "SourceKind",
    "generate_cast_key",
    "generate_token",
    "load_settings",
    "normalize_public_url",
    "parse_bool",
    "parse_canvas",
    "parse_hosts",
    "parse_region",
]

ENV_PREFIX = "GLASSCAST_"
DEFAULT_HOST = "127.0.0.1"
#: 默认端口；deploy/tunnel.sh 把服务器 127.0.0.1:18766 反向转发到这里（实现指南 §6）
DEFAULT_PORT = 8766

SourceKind = Literal["screen", "synthetic"]
#: ``--autostart`` 的取值：对外模式（image / fast）加 off；clip 不再对外（实现指南 §9.1）
AutostartMode = Literal["off", "image", "fast"]
#: image_fps 超过它就提醒。image 页面 <tick> 每秒拉一次（research.md §2.2），拉取时刻与采集相位无关，
#: 拉到的画面平均已过去约 0.5/F 秒（research.md §5 延迟估算里的"等刻度"）：调到 2 能把这一项从约 0.5 s
#: 降到约 0.25 s，再往上收益很小，CPU 却按 F 线性增长
_IMAGE_FPS_WARN_ABOVE = 2.0

#: 图片路径里的 key：只允许 URL 安全字符，避免出现在路径里需要转义
_CAST_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
#: token 放在 HTTP 头里：只允许可见 ASCII，不含空白
_TOKEN_RE = re.compile(r"^[\x21-\x7e]+$")
_MIN_TOKEN_LEN = 16
#: 真机能显示的画布上限（不含）：640×290 在真机上下载成功但不显示（research.md §9.7）
DEVICE_CANVAS_LIMIT: tuple[int, int] = (640, 290)
_LOCAL_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1"})


class ConfigError(ValueError):
    """配置不合法。消息是给人看的中文说明，可能包含多条问题（每行一条）。"""


# --------------------------------------------------------------------------- 解析函数（CLI 与环境变量共用）


def parse_bool(text: str) -> bool:
    """``1/true/yes/on`` → True，``0/false/no/off`` → False（不区分大小写）。"""
    value = text.strip().lower()
    if value in ("1", "true", "yes", "on", "y"):
        return True
    if value in ("0", "false", "no", "off", "n"):
        return False
    raise ValueError(f"无法识别的布尔值 {text!r}（用 1/0、true/false、yes/no、on/off）")


def parse_region(text: str) -> tuple[int, int, int, int] | None:
    """``"L,T,W,H"`` → ``(left, top, width, height)``；空串、``none``、``full`` 表示全屏（None）。

    坐标相对所选显示器左上角，单位与 mss 一致（macOS 为逻辑点，见 :class:`~glasscast.frames.ScreenSource`）。
    """
    value = text.strip()
    if value.lower() in ("", "none", "full", "all"):
        return None
    parts = [p.strip() for p in re.split(r"[,\s]+", value) if p.strip()]
    if len(parts) != 4:
        raise ValueError(f"region 应为 L,T,W,H 四个整数: {text!r}")
    try:
        left, top, width, height = (int(p) for p in parts)
    except ValueError:
        raise ValueError(f"region 应为 L,T,W,H 四个整数: {text!r}") from None
    if width <= 0 or height <= 0:
        raise ValueError(f"region 的宽高必须为正: {text!r}")
    return (left, top, width, height)


def parse_canvas(text: str) -> tuple[int, int]:
    """``"640x290"``（也接受 ``640×290``、``640,290``）→ ``(640, 290)``。"""
    parts = [p for p in re.split(r"[x×X,*\s]+", text.strip()) if p]
    if len(parts) != 2:
        raise ValueError(f"canvas 应为 宽x高，例如 640x290: {text!r}")
    try:
        width, height = int(parts[0]), int(parts[1])
    except ValueError:
        raise ValueError(f"canvas 应为 宽x高，例如 640x290: {text!r}") from None
    if width <= 0 or height <= 0:
        raise ValueError(f"canvas 的宽高必须为正: {text!r}")
    return (width, height)


def parse_hosts(text: str) -> tuple[str, ...]:
    """逗号或空白分隔的 Host 列表，例如 ``"a.example.com, 10.0.0.2:8766"``。"""
    return tuple(h.strip().lower() for h in re.split(r"[,\s]+", text) if h.strip())


def _parse_int(text: str) -> int:
    try:
        return int(text.strip())
    except ValueError:
        raise ValueError(f"应为整数: {text!r}") from None


def _parse_float(text: str) -> float:
    try:
        value = float(text.strip())
    except ValueError:
        raise ValueError(f"应为数字: {text!r}") from None
    return value


def _choice(*options: str) -> Callable[[str], str]:
    def parse(text: str) -> str:
        value = text.strip().lower()
        if value not in options:
            raise ValueError(f"应为 {' / '.join(options)} 之一: {text!r}")
        return value

    return parse


def normalize_public_url(url: str) -> str:
    """校验并规范化对外根地址：去掉末尾 ``/``；只允许 http/https；不能带 query 或 fragment。

    允许带路径前缀（例如 ``https://app.example.com/cast``，docs/deploy.md 方案 B：反代剥掉前缀后转发）。
    """
    value = url.strip()
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"public_url 必须是 http(s)://主机[:端口][/路径] 形式: {url!r}")
    if parts.query or parts.fragment:
        raise ValueError(f"public_url 不能带 ? 查询参数或 # 片段: {url!r}")
    if parts.username or parts.password:
        raise ValueError("public_url 里不要放用户名/密码")
    try:
        parts.port  # noqa: B018 - 触发端口合法性检查
    except ValueError:
        raise ValueError(f"public_url 的端口不合法: {url!r}") from None
    netloc = parts.netloc.lower()
    path = parts.path.rstrip("/")
    return f"{parts.scheme}://{netloc}{path}"


def generate_token() -> str:
    """随机 MCP Bearer Token（43 个 URL 安全字符，256 bit）。"""
    return secrets.token_urlsafe(32)


def generate_cast_key() -> str:
    """随机图片路径 key：12 字节 → 恰好 16 个 URL 安全字符（实现指南 §4.1）。"""
    return secrets.token_urlsafe(12)


# --------------------------------------------------------------------------- Settings


@dataclass(frozen=True)
class Settings:
    """一次 ``glasscast serve`` 的全部配置。用 :func:`load_settings` 构造并校验。"""

    #: 对外可达的根地址（HTTPS，末尾无 ``/``），例如 ``https://cast.example.com``
    public_url: str = ""
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    #: MCP Bearer Token；为空时 :func:`load_settings` 自动生成（``token_generated=True``）
    token: str = field(default="", repr=False)
    #: 图片路径里的随机段 ``/s/<cast_key>/…``；为空时自动生成 16 位
    cast_key: str = field(default="", repr=False)
    monitor: int = 1
    region: tuple[int, int, int, int] | None = None
    fit_mode: FitMode = "fit"
    canvas: tuple[int, int] = CANVAS
    #: image 模式（普通，页面 ``<tick>`` 每秒刷新）的截帧频率
    image_fps: float = 1.0
    #: fast 模式（流畅，cast-emit 事件倒数换图）档位的**初始值**：电脑每秒截这么多张，真机帧数低于它（4 档约每秒 1.5 帧）。
    #: 合法范围 1～10；运行中的当前值在 pipeline 上（start_cast(fps=…) / glasscast set-fps 会改它）
    fast_fps: float = DEFAULT_FAST_FPS
    #: cast-emit 页面 hops 的换算系数（毫秒），不是真机每跳耗时，决定 start_cast 出参 hops（research.md §9.8）
    emit_hop_ms: float = DEFAULT_EMIT_HOP_MS
    jpeg_quality: int = 70
    #: JPEG 编码器：auto（装了 mozjpeg 就用，research.md §9.21）/ mozjpeg / pillow
    jpeg_encoder: JpegEncoder = "auto"
    #: > 0 时每张图取体积不超过这么多 KB 的最高质量（jpeg_quality 变成上限，research.md §9.22）；0 = 固定质量
    jpeg_target_kb: float = 0.0
    #: > 0 时每帧另出一张这么多级的灰度 GIF（2/4/8/16），给 ``…/screen.jpg/<stamp>.gif``（research.md §9.24）；0 = 不出
    gif_levels: int = 0
    #: GIF 帧时长（毫秒，10 的倍数，0 = 不写）：``<gif repeat-count="1">`` 每张至少显示这么久才触发 on-completed
    gif_delay_ms: int = 100
    #: 输出色彩：gray（默认，眼镜屏幕单色绿，research.md §9.2 E6）/ rgb
    color_mode: ColorMode = "gray"
    source: SourceKind = "screen"
    #: 启动后立即进入的模式（``off`` = 等 MCP 的 start_cast）
    autostart: AutostartMode = "off"
    #: 除 public_url 的主机外，MCP 额外放行的 Host 头（DNS 重绑定保护，实现指南 §4.3）
    allowed_hosts: tuple[str, ...] = ()
    #: 关闭后 MCP 不再校验 Host/Origin（只在反代改写了 Host 又无法配置时使用）
    dns_rebinding_protection: bool = True
    token_generated: bool = False
    cast_key_generated: bool = False

    # ------------------------------------------------------------------ 校验

    def problems(self, *, require_public_url: bool = True) -> list[str]:
        """返回所有不合法项（中文），空列表表示可用。"""
        errors: list[str] = []
        if self.public_url:
            try:
                if normalize_public_url(self.public_url) != self.public_url:
                    errors.append(f"public_url 未规范化（末尾不要 /）: {self.public_url!r}")
            except ValueError as exc:
                errors.append(str(exc))
        elif require_public_url:
            errors.append(
                "缺少对外地址：用 --public-url 或环境变量 GLASSCAST_PUBLIC_URL 指定，"
                "例如 https://cast.example.com（本地联调可用 http://127.0.0.1:8766）"
            )
        if not self.host.strip():
            errors.append("host 不能为空")
        if not 1 <= self.port <= 65535:
            errors.append(f"port 应在 1～65535: {self.port}")
        if self.token and not _TOKEN_RE.match(self.token):
            errors.append("token 只能包含可见 ASCII 字符，不能有空格或中文（它要放进 HTTP 请求头）")
        if self.cast_key and not _CAST_KEY_RE.match(self.cast_key):
            errors.append("cast_key 应为 8～128 个字母、数字、- 或 _（它是图片地址路径的一段）")
        if self.monitor < 0:
            errors.append(f"monitor 必须 ≥ 0（0 = 所有显示器拼在一起，1 = 主屏）: {self.monitor}")
        if self.region is not None and (len(self.region) != 4 or self.region[2] <= 0 or self.region[3] <= 0):
            errors.append(f"region 应为 (left, top, width, height) 且宽高为正: {self.region!r}")
        if self.fit_mode not in get_args(FitMode):
            errors.append(f"fit_mode 应为 fit / crop: {self.fit_mode!r}")
        if len(self.canvas) != 2 or self.canvas[0] <= 0 or self.canvas[1] <= 0:
            errors.append(f"canvas 宽高必须为正: {self.canvas!r}")
        if not (isinstance(self.image_fps, (int, float)) and math.isfinite(self.image_fps) and self.image_fps > 0):
            errors.append(f"image_fps 必须是正数: {self.image_fps!r}")
        try:
            # 与运行中修改（start_cast(fps=…)、POST /admin/fps）同一个范围和帧时长下限，不钳制
            parse_fast_fps(self.fast_fps)
        except FastFpsError as exc:
            errors.append(f"fast_fps: {exc}")
        if not (
            isinstance(self.emit_hop_ms, (int, float))
            and math.isfinite(self.emit_hop_ms)
            and EMIT_HOP_MS_MIN <= self.emit_hop_ms <= EMIT_HOP_MS_MAX
        ):
            errors.append(f"emit_hop_ms 应在 {EMIT_HOP_MS_MIN:g}～{EMIT_HOP_MS_MAX:g}: {self.emit_hop_ms!r}")
        if not 1 <= self.jpeg_quality <= 100:
            errors.append(f"jpeg_quality 应在 1～100: {self.jpeg_quality}")
        if self.gif_levels and self.gif_levels not in GIF_LEVELS:
            errors.append(f"gif_levels 应为 0（不出 GIF）或 {' / '.join(map(str, GIF_LEVELS))}: {self.gif_levels!r}")
        if not (0 <= self.gif_delay_ms <= 655_350 and self.gif_delay_ms % 10 == 0):
            errors.append(f"gif_delay_ms 应为 0～655350 之间 10 的倍数: {self.gif_delay_ms!r}")
        if not 0 <= self.jpeg_target_kb <= 1024:
            errors.append(f"jpeg_target_kb 应在 0～1024（0 = 固定质量）: {self.jpeg_target_kb!r}")
        if self.jpeg_encoder not in get_args(JpegEncoder):
            errors.append(f"jpeg_encoder 应为 auto / mozjpeg / pillow: {self.jpeg_encoder!r}")
        if self.color_mode not in get_args(ColorMode):
            errors.append(f"color_mode 应为 gray / rgb: {self.color_mode!r}")
        if self.source not in get_args(SourceKind):
            errors.append(f"source 应为 screen / synthetic: {self.source!r}")
        if self.autostart not in get_args(AutostartMode):
            errors.append(f"autostart 应为 off / image / fast: {self.autostart!r}")
        return errors

    def validate(self, *, require_public_url: bool = True) -> Settings:
        """不合法时抛 :class:`ConfigError`（列出全部问题），否则返回自身。"""
        errors = self.problems(require_public_url=require_public_url)
        if errors:
            raise ConfigError("\n".join(errors))
        return self

    def warnings(self) -> list[str]:
        """能跑但大概率不是你想要的配置（启动时打印）。"""
        notes: list[str] = []
        parts = urlsplit(self.public_url) if self.public_url else None
        if parts is not None and parts.scheme != "https" and parts.hostname not in _LOCAL_HOSTNAMES:
            notes.append("public_url 不是 HTTPS：眼镜和千问平台只能访问公网 HTTPS 地址（research.md §1、§2.5）")
        if parts is not None and parts.hostname in _LOCAL_HOSTNAMES:
            notes.append("public_url 是本机地址，只能本地联调；眼镜和平台访问不到（research.md §1 第 4 条）")
        if self.host in ("0.0.0.0", "::", ""):
            notes.append(f"监听 {self.host} 会把服务暴露到局域网；经隧道/反代访问时保持 127.0.0.1 即可")
        if self.token and len(self.token) < _MIN_TOKEN_LEN:
            notes.append(f"token 只有 {len(self.token)} 个字符，建议至少 {_MIN_TOKEN_LEN} 个")
        if not self.dns_rebinding_protection:
            notes.append("已关闭 MCP 的 DNS 重绑定保护（Host/Origin 校验），仅靠 Bearer Token 鉴权")
        if self.image_fps > _IMAGE_FPS_WARN_ABOVE:
            notes.append(
                f"image_fps={self.image_fps:g}：image 页面每秒只拉一次（research.md §2.2），"
                f"提高 image_fps 只能缩短拉到的画面年龄（平均约 0.5/F 秒，现在约 {0.5 / self.image_fps:.2f} 秒），"
                "代价是 CPU；要更高帧率请用 fast 模式（fast_fps）"
            )
        if self.fast_fps <= self.image_fps:
            notes.append(
                f"fast_fps={self.fast_fps:g}（档位）不高于 image_fps={self.image_fps:g}：流畅模式的电脑截帧频率不比普通模式高"
            )
        # 大于 4 档的提醒与运行中 set-fps 共用文案（research.md §9.20）
        notes.extend(fast_fps_warnings(self.fast_fps))
        if self.color_mode != "gray":
            notes.append(
                "color_mode=rgb：眼镜屏幕是单色绿，颜色本身显示不出来，彩色 JPEG 约比灰度大 40%（research.md §9.2 E6）；"
                "真机实测 rgb 480×218 质量 40 可以显示（research.md §9.7）"
            )
        # 真机实测（research.md §9.7）：640×290（正好等于文档的 742 400 B 解码预算）下载成功但不显示，
        # 480×218 / 320×145 都能显示。模拟器不受影响，所以只提醒、不拒绝。
        if self.canvas[0] * self.canvas[1] >= DEVICE_CANVAS_LIMIT[0] * DEVICE_CANVAS_LIMIT[1]:
            notes.append(
                f"画布 {self.canvas[0]}×{self.canvas[1]}：真机实测 640×290 及以上下载成功但不显示，"
                "上真机请用 --canvas 480x218（或环境变量 GLASSCAST_CANVAS=480x218，research.md §9.7）"
            )
        # image / fast 都是 JPEG：文档折算约 640×386，按 3 字节/像素算（research.md §2.3）
        for warning in check_budget(*self.canvas, 0, bytes_per_pixel=3):
            notes.append(f"画布超预算（JPEG）：{warning}（research.md §2.3）")
        return notes

    # ------------------------------------------------------------------ 派生值

    @property
    def fast_frame_duration_ms(self) -> int:
        """fast 页面定时器每帧时长 ``round(1000 / fast_fps / 2)``，限制在 1～1000（实现指南 §9.1）。"""
        return fast_frame_duration_for(self.fast_fps)

    def frame_duration_ms_for(self, mode: str) -> int:
        """start_cast 出参 ``frame_duration_ms``：fast → 定时器帧时长；image → 1000（每秒一次）。"""
        if mode == "fast":
            return self.fast_frame_duration_ms
        if mode == "image":
            return IMAGE_FRAME_DURATION_MS
        raise ValueError(f"mode 应为 image / fast: {mode!r}")

    @property
    def cast_prefix(self) -> str:
        """图片路径前缀 ``/s/<cast_key>``。"""
        return f"/s/{self.cast_key}"

    @property
    def image_url(self) -> str:
        """image / fast 页面拉取的图片地址；页面在后面拼 ``/<会话>_<序号>.jpg``（地址必须以 .jpg 结尾，服务端忽略这一段）。"""
        return f"{self.public_url}{self.cast_prefix}/screen.jpg"

    @property
    def timer_url(self) -> str:
        """fast 页面帧合集定时器的地址：内容固定的 2 帧 8×8 灰度 PNG `.fset`（research.md §9.2 E4）。"""
        return f"{self.public_url}{self.cast_prefix}/timer.fset"

    @property
    def clip_url(self) -> str:
        """clip 分段流的实验接口地址（已下线，不再对外下发，research.md §2.4）。"""
        return f"{self.public_url}{self.cast_prefix}/clip.fset"

    @property
    def status_url(self) -> str:
        return f"{self.public_url}{self.cast_prefix}/status.json"

    @property
    def mcp_url(self) -> str:
        return f"{self.public_url}/mcp"

    @property
    def local_url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"

    def urls(self) -> dict[str, str]:
        """所有对外地址（含 key，不含 token）。"""
        return {
            "public": self.public_url,
            "image": self.image_url,
            "timer": self.timer_url,
            "status": self.status_url,
            "mcp": self.mcp_url,
        }

    def mcp_allowed_hosts(self) -> list[str]:
        """MCP DNS 重绑定保护放行的 ``Host`` 头（实现指南 §4.3）。

        - public_url 的主机：平台经 OpenResty 访问时 ``Host`` 透传为 ``cast.example.com``
          （deploy/nginx 的 ``proxy_set_header Host $host``）；带端口和不带端口都放行。
        - 本机回环（任意端口）：本地 curl / 测试，以及反代没有透传 Host 时的 ``127.0.0.1:18766``。
        - ``allowed_hosts`` 里额外配置的主机。
        """
        hosts: list[str] = []

        def add(host: str) -> None:
            # 不带端口的主机同时放行"无端口"和"任意端口"两种 Host 头；带端口的只放行这一个
            host = host.strip().lower()
            if not host:
                return
            if host.startswith("["):  # [IPv6] 或 [IPv6]:端口
                candidates = [host] if "]:" in host else [host, f"{host}:*"]
            elif host.count(":") == 0:
                candidates = [host, f"{host}:*"]
            elif host.count(":") == 1:
                candidates = [host]
            else:  # 没加方括号的 IPv6
                candidates = [f"[{host}]", f"[{host}]:*"]
            for candidate in candidates:
                if candidate not in hosts:
                    hosts.append(candidate)

        if self.public_url:
            parts = urlsplit(self.public_url)
            hostname = parts.hostname or ""
            name = f"[{hostname}]" if ":" in hostname else hostname
            add(name)
            if parts.port is not None:
                add(f"{name}:{parts.port}")
        for local in ("127.0.0.1", "localhost", "[::1]"):
            add(local)
        if self.host not in ("0.0.0.0", "::", ""):
            add(f"[{self.host}]" if ":" in self.host else self.host)
        for extra in self.allowed_hosts:
            add(extra)
        return hosts

    def mcp_allowed_origins(self) -> list[str]:
        """MCP 放行的 ``Origin`` 头。平台云端调用不带 Origin（不校验）；这里只为浏览器调试工具留口子。"""
        origins: list[str] = []
        if self.public_url:
            parts = urlsplit(self.public_url)
            origins.append(f"{parts.scheme}://{parts.netloc}")
        for host in self.mcp_allowed_hosts():
            if host.endswith(":*"):
                base = host[:-2]
                origins.extend([f"http://{base}:*", f"https://{base}:*"])
            else:
                origins.extend([f"http://{host}", f"https://{host}"])
        seen: list[str] = []
        for origin in origins:
            if origin not in seen:
                seen.append(origin)
        return seen

    def pipeline_kwargs(self) -> dict[str, Any]:
        """传给 :class:`~glasscast.pipeline.CastPipeline` 的关键字参数。"""
        return {
            "canvas": self.canvas,
            "fit_mode": self.fit_mode,
            "image_fps": self.image_fps,
            "fast_fps": self.fast_fps,
            "jpeg_quality": self.jpeg_quality,
            "jpeg_encoder": self.jpeg_encoder,
            "jpeg_target_bytes": round(self.jpeg_target_kb * 1024),
            "gif_levels": self.gif_levels,
            "gif_delay_ms": self.gif_delay_ms,
            "color_mode": self.color_mode,
        }

    def public_dict(self) -> dict[str, Any]:
        """不含 token 的配置快照（status.json 用；cast_key 已体现在地址里）。

        这是**启动参数**：``fast_fps`` / ``fast_frame_duration_ms`` 是启动时的初始值，运行中被
        ``start_cast(fps=…)`` / ``glasscast set-fps`` 改过之后，当前值看 status.json 顶层的同名字段（来自 pipeline）。
        """
        return {
            "public_url": self.public_url,
            "source": self.source,
            "monitor": self.monitor,
            "region": list(self.region) if self.region else None,
            "fit_mode": self.fit_mode,
            "canvas": list(self.canvas),
            "image_fps": self.image_fps,
            "fast_fps": self.fast_fps,
            "fast_frame_duration_ms": self.fast_frame_duration_ms,
            "emit_hop_ms": self.emit_hop_ms,
            "jpeg_quality": self.jpeg_quality,
            "jpeg_encoder": self.jpeg_encoder,
            "jpeg_target_kb": self.jpeg_target_kb,
            "gif_levels": self.gif_levels,
            "gif_delay_ms": self.gif_delay_ms,
            "color_mode": self.color_mode,
        }


# --------------------------------------------------------------------------- 加载

#: 字段 → 字符串解析函数（环境变量和 CLI 的字符串值都经过它）
_PARSERS: dict[str, Callable[[str], Any]] = {
    "public_url": lambda s: normalize_public_url(s) if s.strip() else "",
    "host": lambda s: s.strip(),
    "port": _parse_int,
    "token": lambda s: s.strip(),
    "cast_key": lambda s: s.strip(),
    "monitor": _parse_int,
    "region": parse_region,
    "fit_mode": _choice(*get_args(FitMode)),
    "canvas": parse_canvas,
    "image_fps": _parse_float,
    "fast_fps": _parse_float,
    "emit_hop_ms": _parse_float,
    "jpeg_quality": _parse_int,
    "jpeg_encoder": _choice(*get_args(JpegEncoder)),
    "jpeg_target_kb": _parse_float,
    "gif_levels": _parse_int,
    "gif_delay_ms": _parse_int,
    "color_mode": _choice(*get_args(ColorMode)),
    "source": _choice(*get_args(SourceKind)),
    "autostart": _choice(*get_args(AutostartMode)),
    "allowed_hosts": parse_hosts,
    "dns_rebinding_protection": parse_bool,
}

#: 支持的环境变量：字段名 → 变量名
ENV_VARS: dict[str, str] = {name: ENV_PREFIX + name.upper() for name in _PARSERS}


def _coerce(name: str, value: Any) -> Any:
    """把 CLI/调用方给的值转成字段类型；字符串走统一解析，其余做基本类型规整。"""
    if isinstance(value, str):
        return _PARSERS[name](value)
    if name == "public_url":
        return normalize_public_url(str(value))
    if name in ("port", "monitor", "jpeg_quality", "gif_levels", "gif_delay_ms"):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"应为整数: {value!r}")
        return value
    if name in ("image_fps", "fast_fps", "emit_hop_ms", "jpeg_target_kb"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"应为数字: {value!r}")
        return float(value)
    if name == "region":
        return None if value is None else tuple(int(v) for v in value)
    if name == "canvas":
        return tuple(int(v) for v in value)
    if name == "allowed_hosts":
        return tuple(str(h).strip().lower() for h in value if str(h).strip())
    if name == "dns_rebinding_protection":
        return bool(value)
    return value


def load_settings(
    cli: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    *,
    require_public_url: bool = True,
    generate_secrets: bool = True,
) -> Settings:
    """按"CLI > 环境变量 > 默认值"合并配置并校验。

    - ``cli``：字段名 → 值；值为 ``None`` 表示命令行没给（不覆盖环境变量）。
      ``allowed_hosts`` 会与环境变量里的合并（两处都给时都放行）。
    - ``env``：通常传 ``os.environ``；只读取 :data:`ENV_VARS` 里的变量，空字符串视为未设置。
    - ``generate_secrets``：token / cast_key 为空时自动生成，并把 ``token_generated`` /
      ``cast_key_generated`` 置真（CLI 据此决定是否打印 token）。

    任何不合法项都汇总成一个 :class:`ConfigError` 抛出。
    """
    cli = dict(cli or {})
    env = env or {}
    values: dict[str, Any] = {}
    errors: list[str] = []

    for name, var in ENV_VARS.items():
        raw = env.get(var)
        if raw is None or not raw.strip():
            continue
        try:
            values[name] = _PARSERS[name](raw)
        except ValueError as exc:
            errors.append(f"环境变量 {var}: {exc}")

    unknown = sorted(set(cli) - set(_PARSERS))
    if unknown:
        raise TypeError(f"未知配置项: {', '.join(unknown)}")
    for name, value in cli.items():
        if value is None:
            continue
        try:
            coerced = _coerce(name, value)
        except (TypeError, ValueError) as exc:
            errors.append(f"参数 {name}: {exc}")
            continue
        if name == "allowed_hosts":
            coerced = tuple(dict.fromkeys((*values.get("allowed_hosts", ()), *coerced)))
        values[name] = coerced

    if errors:
        raise ConfigError("\n".join(errors))

    settings = Settings(**values)
    if generate_secrets:
        if not settings.token:
            settings = replace(settings, token=generate_token(), token_generated=True)
        if not settings.cast_key:
            settings = replace(settings, cast_key=generate_cast_key(), cast_key_generated=True)
    return settings.validate(require_public_url=require_public_url)


def settings_field_names() -> list[str]:
    """Settings 的所有字段名（文档/测试用）。"""
    return [f.name for f in fields(Settings)]
