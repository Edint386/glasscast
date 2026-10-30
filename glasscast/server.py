"""HTTP 服务：眼镜拉图，加上挂在 ``/mcp`` 的 MCP 控制面（实现指南 §4.2、§9.1）。

路由：

==================================  ==========================================================
``GET /healthz``                    ``200 ok``
``GET /s/{key}/screen.jpg``         image / fast 模式最新一张（灰度）JPEG；没有帧时返回同尺寸占位 JPEG
``GET /s/{key}/screen.jpg/{stamp}`` 同上，``stamp`` 任意（页面写 ``…/screen.jpg/<时间戳>_<序号>.jpg``）：真机/穿戴 App 的
                                   下载器按 URL 后缀判断图片类型，带 ``?ts=`` 查询串的地址下载成功却不显示
                                   （research.md §9.6），所以缓存破坏改放在路径里、地址以 .jpg 结尾
``GET /s/{key}/timer.fset``         cast-fast 参考页面（仅本地模拟器）的定时器：内容固定的 2 帧 8×8 灰度 PNG `.fset`（research.md §9.2 E4）
``GET /s/{key}/clip.fset``          实验接口（clip 分段流已下线，research.md §2.4）：内部 clip 模式的最新一段，
                                    其余时候是补齐到 ``clip_frames`` 帧的占位段
``GET /s/{key}/clip/{n}.fset``      同上，``n`` 只用于防缓存，服务端忽略
``GET /s/{key}/status.json``        ``pipeline.stats()`` + 当前地址 + 页面参数（顶层 ``fast_fps`` 是运行时当前值）
``/mcp``                            MCP streamable HTTP（stateless + JSON 响应），Bearer 鉴权
``POST /admin/fps``                 电脑本机改流畅模式帧率（``{"fps": 6}``），不用重启；Bearer 鉴权（同 /mcp）
``GET /admin/config``               当前模式、流畅/普通帧率、帧时长、色彩、画布；Bearer 鉴权（同 /mcp）
==================================  ==========================================================

平台约束：

- 眼镜发不了请求头，图片地址只能靠路径里的随机 key 鉴权（实现指南 §1）；key 不对一律 404，
  错误响应和访问日志里都不出现 key。
- 远程下载按完整 URL 缓存，地址必须每次变化（research.md §2.2、§9.2 E7）：页面自己改路径的最后一段
  （``…/screen.jpg/<会话>_<序号>.jpg``）。这里仍**忽略 query string**（路由只匹配路径，处理函数从不读取
  ``request.query_params``，任何参数名、空值、非法百分号编码都不影响响应），并且所有响应都带
  ``Cache-Control: no-store``。
- ``Access-Control-Allow-Origin: *`` 和 ``Cross-Origin-Resource-Policy: cross-origin`` 给本地 WASM 模拟器用：
  模拟器页面是 cross-origin isolated（COOP/COEP），跨源资源必须带 CORP 头才能加载（research.md §3）；
  内核用 XHR（CORS 模式）下载，缺 ``Access-Control-Allow-Origin`` 就下载失败（research.md §9.2 E8）。
- `.fset` 文件头里没有帧尺寸和帧时长，全靠页面 XML 声明（research.md §4）：timer.fset 恒为
  :data:`~glasscast.frames.TIMER_FRAMES` 帧 :data:`~glasscast.frames.TIMER_SIZE`，clip.fset 恒为 ``clip_frames`` 帧。
- MCP 的 DNS 重绑定保护必须放行公网 Host（平台经 OpenResty 访问时 Host 为 ``cast.example.com``），
  否则平台收到 421（实现指南 §4.3）；放行列表见 :meth:`~glasscast.config.Settings.mcp_allowed_hosts`。
- ``/admin/*`` 是给电脑本机 ``glasscast set-fps`` / ``glasscast config`` 用的管理接口：与 ``/mcp`` 同一个
  Bearer Token、同一套校验（:class:`McpGuardMiddleware`），公网 vhost 显式 404（deploy/nginx，docs/deploy.md）。
  改帧率只改电脑端（截帧节拍立即变）；眼镜上已打开页面的 ``hops`` 在打开时写入（research.md §9.8），
  下一次 ``start_cast`` 重新打开页面后才按新节拍换图，所以返回的 message 会说明这一点。
"""

from __future__ import annotations

import contextlib
import hmac
import json
import logging
import re
import threading
import time
from collections.abc import AsyncIterator
from typing import Any, Literal

import anyio
from PIL import Image
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import __version__
from .config import Settings
from .frames import TIMER_FRAMES, TIMER_SIZE, build_timer_fset, placeholder_image
from .mcp_tools import DEFAULT_FIRST_FRAME_TIMEOUT_S, build_mcp_server
from .probe import PROBE_NAME_RE, build_probe_file, probe_media_type
from .pipeline import (
    FAST_FPS_MAX,
    FAST_FPS_MIN,
    IMAGE_FRAME_DURATION_MS,
    SNAPSHOT_MODES,
    CastPipeline,
    Clip,
    FastFpsError,
    Snapshot,
    emit_hops_for,
    fast_fps_warnings,
)

__all__ = [
    "ADMIN_PATH",
    "COMMON_HEADERS",
    "MCP_PATH",
    "AccessLogMiddleware",
    "CommonHeadersMiddleware",
    "McpGuardMiddleware",
    "Placeholders",
    "create_app",
    "redact_path",
]

log = logging.getLogger(__name__)

#: 探针文件名 ``<会话>_<名字>.<后缀>``；名字的种类决定后缀（fj = .fset 字节放在 .jpg 地址后面）
_PROBE_FILE_RE = re.compile(r"^([A-Za-z0-9-]{1,32})_([a-z]{1,2}[0-9]{1,3})\.(fset|gif|jpg)$")
_PROBE_EXT = {"f": "fset", "fj": "jpg", "g": "gif", "i": "jpg"}
#: 探针页用到的文件，启动时预生成（生成一份 .fset 要几百毫秒，别算进真机测得的间隔里）
PROBE_WARM_NAMES = ("f16", "f40", "f80", "f160", "fj20", "g8", "i4", "i12")


def _warm_probe_files() -> None:
    for name in PROBE_WARM_NAMES:
        build_probe_file(name)

access_log = logging.getLogger("glasscast.access")

MCP_PATH = "/mcp"
#: MCP 请求体上限。平台只发 JSON-RPC 小包；OpenResty 侧 client_max_body_size 64k（deploy/nginx）
MCP_MAX_BODY_BYTES = 256 * 1024
#: 本机管理接口的前缀（glasscast set-fps / config 用；公网 vhost 显式 404，deploy/nginx）
ADMIN_PATH = "/admin"
#: /admin 请求体上限：只有 {"fps": 6} 这样的小 JSON
ADMIN_MAX_BODY_BYTES = 4096

#: 所有响应都带的头（实现指南 §1、§4.2）
COMMON_HEADERS: tuple[tuple[str, str], ...] = (
    ("cache-control", "no-store"),
    ("access-control-allow-origin", "*"),
    ("cross-origin-resource-policy", "cross-origin"),
)

PlaceholderReason = Literal["waiting", "not_started", "stopped", "mode_image", "mode_fast", "mode_clip"]

# 占位图文案：message 用中文字体画，detail 用拉丁字体画（只能是 ASCII，见 frames.placeholder_image）。
# mode_image / mode_fast 只出现在实验接口 clip.fset 上；mode_clip 是内部 clip 模式时 screen.jpg 的占位。
_PLACEHOLDER_TEXT: dict[str, tuple[str, str]] = {
    "not_started": ("投屏未开始", "cast not started"),
    "stopped": ("投屏已停止", "cast stopped"),
    "mode_image": ("当前是普通模式", "now casting in image mode"),
    "mode_fast": ("当前是流畅模式", "now casting in fast mode"),
    "mode_clip": ("当前是实验分段模式", "now casting in clip mode (experimental)"),
}

_KEY_IN_PATH = re.compile(r"^/s/[^/]+")
_NOT_FOUND = "Not Found"


def redact_path(path: str) -> str:
    """把路径里的 key 换成 ``…``：``/s/<key>/screen.jpg`` → ``/s/…/screen.jpg``。"""
    return _KEY_IN_PATH.sub("/s/…", path)


# --------------------------------------------------------------------------- 中间件（纯 ASGI）


class CommonHeadersMiddleware:
    """给每个 HTTP 响应补上 :data:`COMMON_HEADERS`（已存在的同名头不覆盖）。"""

    def __init__(self, app: ASGIApp, headers: tuple[tuple[str, str], ...] = COMMON_HEADERS) -> None:
        self.app = app
        self.headers = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                raw = list(message.get("headers", []))
                present = {k.lower() for k, _ in raw}
                raw.extend((k, v) for k, v in self.headers if k not in present)
                message = {**message, "headers": raw}
            await send(message)

        await self.app(scope, receive, send_with_headers)


class AccessLogMiddleware:
    """一行一请求的访问日志（logger ``glasscast.access``），路径里的 key 已打码。

    uvicorn 自带的访问日志会把完整路径（含 key）打出来，所以 CLI 里关掉了它、用这个代替。
    排查页面是否在轮询时看路径里的 ``<序号>`` 是否递增；query 原样保留只为排查旧地址。
    """

    def __init__(self, app: ASGIApp, logger: logging.Logger = access_log) -> None:
        self.app = app
        self.logger = logger

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        t0 = time.perf_counter()
        status = 0
        nbytes = 0

        async def send_and_count(message: Message) -> None:
            nonlocal status, nbytes
            if message["type"] == "http.response.start":
                status = int(message["status"])
            elif message["type"] == "http.response.body":
                nbytes += len(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, receive, send_and_count)
        finally:
            if self.logger.isEnabledFor(logging.INFO):
                headers = Headers(scope=scope)
                query = scope.get("query_string", b"").decode("latin-1")
                target = redact_path(scope.get("path", "")) + (f"?{query}" if query else "")
                client = headers.get("x-real-ip") or (scope.get("client") or ("-",))[0]
                agent = headers.get("user-agent", "-")[:80]
                self.logger.info(
                    '%s %s %s %s %.1fms %s "%s"',
                    scope.get("method", "-"),
                    target,
                    status or "-",
                    _human_bytes(nbytes),
                    (time.perf_counter() - t0) * 1000,
                    client,
                    agent,
                )


def _human_bytes(n: int) -> str:
    return f"{n}B" if n < 1024 else f"{n / 1024:.1f}KB"


class McpGuardMiddleware:
    """只作用于 ``path``（默认 ``/mcp``）及其子路径：校验 Bearer Token，并在 stateless 模式下拒绝 GET。

    同一个类也挂在 ``/admin`` 上（``reject_get=False``）：本机管理接口复用 MCP 的 Token 和校验逻辑。
    ``label`` / ``hint`` 只影响 401 时的日志文案。

    - 平台注册插件时选 Bearer Token，每个请求头自动加 ``Authorization: Bearer <token>``（官方文档第 37 节 §4.2）。
      常见误填是在平台的 Token 栏里也写了 ``Bearer ``，结果变成 ``Bearer Bearer <token>``；
      这里宽松处理（也接受不带前缀的裸 token），因为判断依据始终是 token 本身，并在日志里提示一次。
    - 不匹配 → ``401`` + ``WWW-Authenticate: Bearer``。用 :func:`hmac.compare_digest` 做常量时间比较。
    - stateless 模式没有会话，GET 打开的 SSE 流永远收不到东西、会一直挂着占连接；
      按 MCP 规范返回 ``405``，客户端会改为只用 POST。
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        token: str,
        path: str = MCP_PATH,
        reject_get: bool = True,
        label: str = "MCP",
        hint: str = "检查平台插件里的 Bearer Token 是否等于 GLASSCAST_TOKEN",
    ) -> None:
        if not token:
            raise ValueError("MCP token 不能为空")
        self.app = app
        self.token = token.encode("utf-8")
        self.path = path
        self.reject_get = reject_get
        self.label = label
        self.hint = hint
        self._lock = threading.Lock()
        self._hinted_nonstandard = False
        self._last_reject_log = 0.0

    def _applies(self, path: str) -> bool:
        return path == self.path or path.startswith(self.path + "/")

    @staticmethod
    def extract_token(header: str | None) -> tuple[str, bool]:
        """从 ``Authorization`` 头取 token，返回 ``(token, 是否标准写法)``。"""
        value = (header or "").strip()
        standard = True
        parts = value.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            value = parts[1].strip()
            inner = value.split(None, 1)
            if len(inner) == 2 and inner[0].lower() == "bearer":
                value, standard = inner[1].strip(), False
        elif value:
            standard = False
        return value, standard

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self._applies(scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        token, standard = self.extract_token(headers.get("authorization"))
        if not token or not hmac.compare_digest(token.encode("utf-8"), self.token):
            self._log_reject("缺少 Authorization 头" if not headers.get("authorization") else "Bearer Token 不匹配")
            response = JSONResponse(
                {"error": "unauthorized", "message": "缺少或错误的 Bearer Token"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="glasscast"'},
            )
            await response(scope, receive, send)
            return
        if not standard:
            self._hint_nonstandard()
        if self.reject_get and scope.get("method") == "GET":
            response = PlainTextResponse(
                "Method Not Allowed: stateless MCP endpoint, use POST", status_code=405, headers={"Allow": "POST"}
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)

    def _log_reject(self, reason: str) -> None:
        now = time.monotonic()
        with self._lock:
            if now - self._last_reject_log < 10.0:
                return
            self._last_reject_log = now
        log.warning("拒绝 %s 请求（401）：%s。%s", self.label, reason, self.hint)

    def _hint_nonstandard(self) -> None:
        with self._lock:
            if self._hinted_nonstandard:
                return
            self._hinted_nonstandard = True
        log.warning(
            "%s 请求的 Authorization 头不是标准的 'Bearer <token>'（多了或少了 Bearer 前缀），已放行；"
            "建议在平台插件里只填 token 本体",
            self.label,
        )


# --------------------------------------------------------------------------- 占位图


class Placeholders:
    """没有可用画面时返回的占位 JPEG / `.fset`（尺寸恒为画布，`.fset` 帧数恒为 ``clip_frames``）。

    ``waiting`` 直接用 pipeline 自带的"等待画面"占位；其余文案在这里画，结果缓存。线程安全。
    编码交给 pipeline（:meth:`~glasscast.pipeline.CastPipeline.make_placeholder_snapshot` /
    ``make_placeholder_clip``），所以占位与真实画面的色彩（默认灰度，research.md §9.2 E6）、
    JPEG 质量和 `.fset` 帧格式完全一致。
    """

    def __init__(self, pipeline: CastPipeline) -> None:
        self._pipeline = pipeline
        self._lock = threading.Lock()
        self._snapshots: dict[str, Snapshot] = {}
        self._clips: dict[str, Clip] = {}

    def snapshot(self, reason: PlaceholderReason) -> Snapshot:
        if reason == "waiting":
            return self._pipeline.placeholder_snapshot()
        with self._lock:
            cached = self._snapshots.get(reason)
            if cached is None:
                cached = self._pipeline.make_placeholder_snapshot(self._image(reason))
                self._snapshots[reason] = cached
            return cached

    def clip(self, reason: PlaceholderReason) -> Clip:
        if reason == "waiting":
            return self._pipeline.placeholder_clip()
        with self._lock:
            cached = self._clips.get(reason)
            if cached is None:
                cached = self._pipeline.make_placeholder_clip(self._image(reason))
                self._clips[reason] = cached
            return cached

    def _image(self, reason: str) -> Image.Image:
        message, detail = _PLACEHOLDER_TEXT[reason]
        return placeholder_image(message, self._pipeline.canvas, detail=detail)

    def warm(self) -> None:
        """预先生成全部占位（启动时在线程里调用，避免第一次请求在事件循环里画图）。"""
        for reason in ("waiting", *_PLACEHOLDER_TEXT):
            self.snapshot(reason)  # type: ignore[arg-type]
            self.clip(reason)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- 应用


def create_app(
    settings: Settings,
    pipeline: CastPipeline,
    *,
    mcp_server: MCPServer | None = None,
    first_frame_timeout_s: float = DEFAULT_FIRST_FRAME_TIMEOUT_S,
    access_log: bool = True,
    close_pipeline_on_shutdown: bool = False,
) -> Starlette:
    """创建 Starlette 应用。

    - ``pipeline`` 由调用方创建并负责关闭（``close_pipeline_on_shutdown=True`` 时由应用在退出时关闭）。
      本函数不会启动采集；``--autostart`` 由 CLI 处理。
    - ``mcp_server`` 默认用 :func:`~glasscast.mcp_tools.build_mcp_server` 新建。同一个 MCPServer 只能挂到
      一个应用上（SDK 的 session manager 每个实例只能 ``run()`` 一次），所以每次都新建应用即可。
    - 必须在 ASGI lifespan 里运行（uvicorn 默认会；测试用 ``with TestClient(app)``），
      否则 MCP 的 session manager 没启动，``/mcp`` 返回 500。
    """
    settings.validate(require_public_url=False)
    if not settings.token or not settings.cast_key:
        raise ValueError("create_app 需要非空的 token 和 cast_key（用 load_settings 生成）")

    server = mcp_server or build_mcp_server(settings, pipeline, first_frame_timeout_s=first_frame_timeout_s)
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=settings.dns_rebinding_protection,
        allowed_hosts=settings.mcp_allowed_hosts(),
        allowed_origins=settings.mcp_allowed_origins(),
    )
    # stateless + JSON 响应：平台每次调用都是独立的 POST，不依赖会话；电脑端重启后平台无需重新握手
    # （实现指南 §4.3）。返回的子应用只有一个 Route("/mcp")，直接并进主路由表；它的 lifespan 不会被
    # Starlette 自动执行，下面的 lifespan 里手动运行 session_manager。
    mcp_app = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        json_response=True,
        stateless_http=True,
        transport_security=security,
        host=settings.host,
        max_request_body_size=MCP_MAX_BODY_BYTES,
    )
    session_manager = server.session_manager
    placeholders = Placeholders(pipeline)
    # 定时器内容固定（research.md §9.2 E4），启动时生成一次，之后每次原样返回
    timer_data = build_timer_fset()
    started_at = time.time()

    def key_ok(request: Request) -> bool:
        key = request.path_params.get("key", "")
        return hmac.compare_digest(str(key).encode("utf-8"), settings.cast_key.encode("utf-8"))

    def not_found() -> Response:
        # 与 Starlette 默认 404 相同，既不泄露 key 也不区分"key 错"与"路径不存在"
        return PlainTextResponse(_NOT_FOUND, status_code=404)

    def off_reason() -> PlaceholderReason:
        # 采集线程在第一次 start 时创建，之后常驻：据此区分"从没开始"和"已停止"
        return "stopped" if pipeline.running or pipeline.closed else "not_started"

    def pick_snapshot() -> tuple[Snapshot, str | None]:
        mode = pipeline.mode
        if mode in SNAPSHOT_MODES:  # image 与 fast 共用 snapshot，只是截帧频率不同
            snap = pipeline.latest_snapshot()
            return (snap, None) if snap is not None else (placeholders.snapshot("waiting"), "waiting")
        if mode == "clip":
            # 不返回上一轮 image/fast 模式残留的旧截图：那会被当成"现在的屏幕"
            return placeholders.snapshot("mode_clip"), "mode_clip"
        reason = off_reason()
        return placeholders.snapshot(reason), reason

    def pick_clip() -> tuple[Clip, str | None]:
        mode = pipeline.mode
        if mode == "clip":
            clip = pipeline.latest_clip()
            return (clip, None) if clip is not None else (placeholders.clip("waiting"), "waiting")
        if mode == "image":
            return placeholders.clip("mode_image"), "mode_image"
        if mode == "fast":
            return placeholders.clip("mode_fast"), "mode_fast"
        # 停止后不再循环播放最后一段：那看起来像实时画面
        reason = off_reason()
        return placeholders.clip(reason), reason

    def frame_headers(seq: int, ts: float, placeholder: str | None) -> dict[str, str]:
        headers = {
            "Cache-Control": "no-store",
            "X-Glasscast-Seq": str(seq),
            "X-Glasscast-Mode": pipeline.mode,
        }
        if placeholder is None:
            headers["ETag"] = f'"{seq}"'
            headers["X-Glasscast-Age"] = f"{max(0.0, time.time() - ts):.3f}"
        else:
            headers["ETag"] = f'"0-{placeholder}"'
            headers["X-Glasscast-Age"] = "0"
            headers["X-Glasscast-Placeholder"] = placeholder
        return headers

    async def healthz(request: Request) -> Response:
        return PlainTextResponse("ok")

    async def screen_jpg(request: Request) -> Response:
        if not key_ok(request):
            return not_found()
        snap, placeholder = pick_snapshot()
        return Response(snap.data, media_type="image/jpeg", headers=frame_headers(snap.seq, snap.ts, placeholder))

    async def screen_gif(request: Request) -> Response:
        # 同一帧的灰度 GIF，给 <gif repeat-count="1" on-completed> 自定节奏的测试页（research.md §9.24）；没开 gif_levels 时 404
        if not key_ok(request):
            return not_found()
        snap, placeholder = pick_snapshot()
        if snap.gif is None:
            return not_found()
        headers = frame_headers(snap.seq, snap.ts, placeholder)
        headers["ETag"] = headers["ETag"][:-1] + '-gif"'
        return Response(snap.gif, media_type="image/gif", headers=headers)

    async def probe_file(request: Request) -> Response:
        # 真机探针（research.md §9.25）：…/screen.jpg/probe/<会话>_<名字>.<后缀>，立即返回固定内容；靠访问日志看请求间隔
        if not key_ok(request):
            return not_found()
        m = _PROBE_FILE_RE.match(request.path_params["file"])
        name_m = PROBE_NAME_RE.match(m.group(2)) if m else None
        if not m or not name_m or _PROBE_EXT[name_m.group(1)] != m.group(3):
            return not_found()
        try:
            data = await anyio.to_thread.run_sync(build_probe_file, m.group(2))
        except ValueError:
            return not_found()
        return Response(data, media_type=probe_media_type(name_m.group(1)), headers={"Cache-Control": "no-store"})

    async def timer_fset(request: Request) -> Response:
        # 内容与模式无关、永远相同：页面只在帧合集节点创建时下载一次（src 只求值一次，research.md §9.2 E2）。
        # 实现指南 §9.1 允许省掉 no-store，这里仍保留：通用头统一，且几十字节的文件不值得缓存。
        if not key_ok(request):
            return not_found()
        headers = {
            "Cache-Control": "no-store",
            "X-Glasscast-Frames": str(TIMER_FRAMES),
            "X-Glasscast-Frame-Size": f"{TIMER_SIZE[0]}x{TIMER_SIZE[1]}",
            "X-Glasscast-Frame-Duration-Ms": str(pipeline.fast_frame_duration_ms),
            "X-Glasscast-Frame-Format": "png",
        }
        return Response(timer_data, media_type="application/octet-stream", headers=headers)

    async def clip_fset(request: Request) -> Response:
        if not key_ok(request):
            return not_found()
        clip, placeholder = pick_clip()
        headers = frame_headers(clip.seq, clip.ts, placeholder)
        headers["X-Glasscast-Frames"] = str(clip.frames)
        headers["X-Glasscast-Frame-Duration-Ms"] = str(clip.frame_duration_ms)
        headers["X-Glasscast-Frame-Format"] = clip.kind
        return Response(clip.data, media_type="application/octet-stream", headers=headers)

    async def status_json(request: Request) -> Response:
        if not key_ok(request):
            return not_found()
        # stats 里的 fast_fps / fast_frame_duration_ms 是运行时当前值（start_cast(fps=…)、POST /admin/fps 会改），
        # 同一把锁下取出、彼此一致；下面 config 里的是启动参数（初始值）
        stats = pipeline.stats()
        width, height = pipeline.canvas
        body: dict[str, Any] = {
            **stats,
            "glasscast": __version__,
            "source": settings.source,
            "uptime_s": round(time.time() - started_at, 1),
            "server_time": round(time.time(), 3),
            "urls": settings.urls(),
            # 两个眼镜页面需要的参数（与 start_cast 出参一致，实现指南 §9.1）。fast 页面的 <frame-set>
            # width/height/total-frames 必须等于 timer.fset 的帧尺寸和帧数（research.md §4、§9.2 E4）
            "page": {
                "width": width,
                "height": height,
                "image": {"template": "cast-image", "frame_duration_ms": IMAGE_FRAME_DURATION_MS},
                # 流畅模式页面已改为 cast-emit（emit 事件总线自环，research.md §9.8）：hops 与 start_cast 出参一致
                "fast": {
                    "template": "cast-emit",
                    "hops": emit_hops_for(stats["fast_fps"], settings.emit_hop_ms),
                    "emit_hop_ms": settings.emit_hop_ms,
                },
            },
            "config": settings.public_dict(),
        }
        return JSONResponse(body)

    # ------------------------------------------------------------------ /admin（本机管理接口，Bearer 已由中间件校验）

    def fast_page_note(mode: str, fps: float) -> str:
        # 页面的跳数 hops 只经 start_cast 出参下发（research.md §9.8）：已打开的 cast-emit 不会跟着变
        if mode == "fast":
            return (
                f"眼镜上已打开的流畅模式页面仍按旧跳数拉图；对眼镜说「投屏调到 {fps:g} 帧」，"
                "页面会拿到新的 hops 并按新节拍刷新"
            )
        return "下次切到流畅模式时按这个帧率投屏"

    async def admin_config(request: Request) -> Response:
        fast_fps, fast_duration = pipeline.fast_settings()
        width, height = pipeline.canvas
        return JSONResponse(
            {
                "success": True,
                "mode": pipeline.mode,
                "fast_fps": fast_fps,
                # 流畅模式页面定时器每帧毫秒数 round(1000/fast_fps/2)，与 start_cast(mode=fast) 出参同名字段一致
                "frame_duration_ms": fast_duration,
                "image_fps": pipeline.image_fps,
                "color_mode": pipeline.color_mode,
                "canvas": [width, height],
                "fast_fps_range": [FAST_FPS_MIN, FAST_FPS_MAX],
                "initial_fast_fps": settings.fast_fps,
                # cast-emit 页面跳数（与 start_cast 出参一致）和估算用的每跳毫秒数（research.md §9.8）
                "hops": emit_hops_for(fast_fps, settings.emit_hop_ms),
                "emit_hop_ms": settings.emit_hop_ms,
            }
        )

    def admin_error(status: int, error: str, message: str) -> Response:
        return JSONResponse(
            {"success": False, "error": error, "message": message, "fast_fps": pipeline.fast_fps}, status_code=status
        )

    async def admin_fps(request: Request) -> Response:
        body = await request.body()
        if len(body) > ADMIN_MAX_BODY_BYTES:
            return admin_error(413, "too_large", "请求体太大，只需要 {\"fps\": 6} 这样的 JSON")
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):  # RecursionError：防御极深的嵌套
            payload = None
        if not isinstance(payload, dict) or "fps" not in payload:
            return admin_error(400, "bad_request", "请求体应为 JSON 对象，例如 {\"fps\": 6}")
        previous_fps, previous_duration = pipeline.fast_settings()
        try:
            fps, duration = pipeline.set_fast_fps(payload["fps"])
        except FastFpsError as exc:
            return admin_error(400, "invalid_fps", str(exc))
        mode = pipeline.mode
        return JSONResponse(
            {
                "success": True,
                "mode": mode,
                "fast_fps": fps,
                "frame_duration_ms": duration,
                "previous_fast_fps": previous_fps,
                "previous_frame_duration_ms": previous_duration,
                "hops": emit_hops_for(fps, settings.emit_hop_ms),
                "previous_hops": emit_hops_for(previous_fps, settings.emit_hop_ms),
                "warnings": fast_fps_warnings(fps),
                "message": (
                    f"流畅模式帧率已改为每秒 {fps:g} 张（hops={emit_hops_for(fps, settings.emit_hop_ms)}），电脑端截帧立即生效；"
                    + fast_page_note(mode, fps)
                ),
            }
        )

    async def internal_error(request: Request, exc: Exception) -> Response:
        # Starlette 把 ServerErrorMiddleware 放在所有用户中间件外层，处理函数抛异常时 500 响应直接经原始
        # send 发出，绕过 CommonHeadersMiddleware；所以通用头必须写在这个响应自己身上（实现指南 §1、§4.2：
        # 所有响应都带）。缺 CORP 的响应会被 COEP require-corp 的模拟器页面直接拦截，连状态码都看不到。
        # 正文固定，不回显异常信息，也不含路径（路径里有 key）。堆栈由 Starlette 随后重新抛出、uvicorn 记录，
        # 这里只补一行打码后的路径。
        log.error("处理 %s %s 时出错：%s: %s", request.method, redact_path(request.url.path), type(exc).__name__, exc)
        return PlainTextResponse("Internal Server Error", status_code=500, headers=dict(COMMON_HEADERS))

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        try:
            await anyio.to_thread.run_sync(placeholders.warm)
            await anyio.to_thread.run_sync(_warm_probe_files)
        except Exception:  # noqa: BLE001 - 预热失败不影响服务，按需再生成
            log.warning("预生成占位图失败", exc_info=True)
        try:
            async with session_manager.run():
                yield
        finally:
            if close_pipeline_on_shutdown:
                await anyio.to_thread.run_sync(pipeline.close)

    routes = [
        Route("/healthz", healthz, methods=["GET"]),
        Route("/s/{key}/screen.jpg", screen_jpg, methods=["GET"]),
        # 真机：地址必须以 .jpg 结尾才会显示（research.md §9.6）；stamp 只是缓存破坏，服务端忽略
        # 以 .gif 结尾的给 GIF（穿戴 App 按后缀认图片类型），必须排在通用的 {stamp} 前面
        Route("/s/{key}/screen.jpg/{stamp}.gif", screen_gif, methods=["GET"]),
        Route("/s/{key}/screen.jpg/{stamp}", screen_jpg, methods=["GET"]),
        Route("/s/{key}/screen.jpg/probe/{file}", probe_file, methods=["GET"]),
        Route("/s/{key}/timer.fset", timer_fset, methods=["GET"]),
        Route("/s/{key}/clip.fset", clip_fset, methods=["GET"]),
        Route("/s/{key}/clip/{n}.fset", clip_fset, methods=["GET"]),
        Route("/s/{key}/status.json", status_json, methods=["GET"]),
        Route(f"{ADMIN_PATH}/fps", admin_fps, methods=["POST"]),
        Route(f"{ADMIN_PATH}/config", admin_config, methods=["GET"]),
        *mcp_app.routes,
    ]
    middleware = [Middleware(CommonHeadersMiddleware)]
    if access_log:
        middleware.append(Middleware(AccessLogMiddleware))
    middleware.append(Middleware(McpGuardMiddleware, token=settings.token, path=MCP_PATH, reject_get=True))
    # 本机管理接口：同一个 token、同一套校验；GET /admin/config 要放行，所以不拒绝 GET
    middleware.append(
        Middleware(
            McpGuardMiddleware,
            token=settings.token,
            path=ADMIN_PATH,
            reject_get=False,
            label=ADMIN_PATH,
            hint="检查 glasscast set-fps / config 的 --token 或环境变量 GLASSCAST_TOKEN 是否与 glasscast serve 一致",
        )
    )

    app = Starlette(
        routes=routes,
        middleware=middleware,
        lifespan=lifespan,
        exception_handlers={Exception: internal_error},
    )
    app.state.settings = settings
    app.state.pipeline = pipeline
    app.state.mcp_server = server
    app.state.placeholders = placeholders
    return app
