#!/usr/bin/env python3
"""glasscast 本地模拟器服务器。

只用标准库（Pillow 可选，仅用于把演示图编码成 JPEG）。职责：

1. 静态提供 ``index.html`` / ``harness.js``，并把 ``/assets/`` 映射到 ``fetch_assets.sh`` 的下载目录。
2. 所有响应加 ``Cross-Origin-Opener-Policy: same-origin`` 与
   ``Cross-Origin-Embedder-Policy: require-corp``：waft-lite 内核用 WASM pthreads，页面必须
   cross-origin isolated，否则 ``createWaftLite`` 直接抛错（research.md §3；内核入口
   ``index.blob-worker.js`` 里的 ``crossOriginIsolated === false`` 检查）。
3. ``/pages/`` 只读映射仓库的 ``pages/``（投屏页面模板），供 harness 一键载入。
   可选 ``--samples DIR``：把自己的一批 XML 页面映射到 ``/samples/``，harness 里出现为 ``sample:<name>`` 预设；
   不给就没有 ``/samples/``，harness 里也没有这类预设。
4. ``/demo/*``：不依赖 glasscast 服务也能验证两个投屏页面的演示资源——每次请求都换内容的
   640×290 图（research.md §2.3 一屏预算）和 10 帧 PNG ``.fset``（格式见 research.md §4）。
5. ``/proxy?url=``：可选的同源代理（默认关闭，``--allow-proxy`` 打开），给没有 CORS 头的远程图用。
   内核下载走 XHR（CORS 模式，research.md §3），远程地址不带 ``Access-Control-Allow-Origin`` 时
   只能经同源代理，这里的 ``/proxy`` 就是这个角色。
6. ``/api/info``（资源自检、可用页面）与 ``/api/requests``（最近的 demo/proxy 请求，用来观察
   眼镜页面实际拉取节奏，对应 research.md §8 待验证 1、2）。

启动：``uv run python tools/simulator/serve.py``，浏览器打开 http://127.0.0.1:8770/ 。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import email.utils
import http.client
import ipaddress
import json
import re
import shutil
import socket
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections import deque
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

SIM_DIR: Path = Path(__file__).resolve().parent
PROJECT_ROOT: Path = SIM_DIR.parent.parent
DEFAULT_PORT: int = 8770
# S1 一屏：640×290（research.md §2.3），远程图解码后 ≤ 742 400 B、文件 ≤ 1 MiB。
CANVAS: tuple[int, int] = (640, 290)
DISPLAY_BUDGET_BYTES: int = 742_400

# 内核资源：相对 assets 目录的路径。入口 import './dist/…'，所以必须保留 dist/ 子目录。
REQUIRED_ASSETS: tuple[str, ...] = (
    "index.blob-worker.js",
    "dist/waft-lite-wasm.blob-worker.js",
    "dist/waft-lite-wasm.wasm",
    "dist/waft-lite-wasm.data",
)
OPTIONAL_ASSETS: tuple[str, ...] = ("dist/build-info.json", "index.js", "dist/waft-lite-wasm.js")

# 只公开这几个本目录文件（避免把 serve.py、测试等也当静态文件放出去）。
STATIC_FILES: dict[str, str] = {
    "/": "index.html",
    "/index.html": "index.html",
    "/harness.js": "harness.js",
    "/README.md": "README.md",
}

# 模块脚本必须是 JS MIME；.wasm 必须是 application/wasm 才能 instantiateStreaming。
MIME_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".wasm": "application/wasm",
    ".data": "application/octet-stream",
    ".json": "application/json; charset=utf-8",
    ".xml": "application/xml; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".fset": "application/octet-stream",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
}

PROXY_MAX_BYTES: int = 8 * 1024 * 1024
PROXY_TIMEOUT_S: float = 10.0
LOOPBACK_NAMES: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})


# ---------------------------------------------------------------------------
# 配置与运行时状态
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SimulatorConfig:
    """服务器配置。全部由 CLI 决定，运行期不变。"""

    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    assets_dir: Path = SIM_DIR / "assets"
    pages_dir: Path = PROJECT_ROOT / "pages"
    #: 可选的示例 XML 目录（--samples）；None = 不提供 /samples/
    samples_dir: Path | None = None
    allow_proxy: bool = False
    proxy_allow_private: bool = False
    extra_hosts: tuple[str, ...] = ()
    demo_frames: int = 10
    demo_size: tuple[int, int] = CANVAS
    quiet: bool = False


@dataclass
class SimulatorState:
    """线程安全的计数器 + 最近请求环形缓冲（挂在 server 对象上，不用模块级全局）。"""

    max_records: int = 300
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _counters: dict[str, int] = field(default_factory=dict)
    _records: deque[dict[str, Any]] = field(default_factory=deque)

    def next_seq(self, name: str) -> int:
        with self._lock:
            value = self._counters.get(name, 0) + 1
            self._counters[name] = value
            return value

    def record(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self._records.append(entry)
            while len(self._records) > self.max_records:
                self._records.popleft()

    def recent(self, since: float = 0.0) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._records if r.get("ts", 0.0) > since]


# ---------------------------------------------------------------------------
# 资源自检
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AssetReport:
    ok: bool
    files: dict[str, int]
    missing: list[str]
    invalid: dict[str, str]
    build_info: dict[str, Any] | None

    def to_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "files": self.files,
            "missing": self.missing,
            "invalid": self.invalid,
            "build_info": self.build_info,
        }


def check_assets(assets_dir: Path) -> AssetReport:
    """粗检内核资源是否齐全（与 fetch_assets.sh 的校验同口径，但更宽松，只为给出友好提示）。"""
    files: dict[str, int] = {}
    missing: list[str] = []
    invalid: dict[str, str] = {}
    for rel in REQUIRED_ASSETS + OPTIONAL_ASSETS:
        path = assets_dir / rel
        if not path.is_file():
            if rel in REQUIRED_ASSETS:
                missing.append(rel)
            continue
        size = path.stat().st_size
        files[rel] = size
        with path.open("rb") as fh:
            head = fh.read(8)
        if head.startswith(b"<?xml") or head.lstrip().startswith(b"<"):
            invalid[rel] = "内容是 XML/HTML（多半是 CDN/OSS 的错误页）"
        elif rel.endswith(".wasm") and not head.startswith(b"\x00asm"):
            invalid[rel] = "缺少 \\0asm 魔数"
    build_info: dict[str, Any] | None = None
    bi_path = assets_dir / "dist" / "build-info.json"
    if bi_path.is_file() and "dist/build-info.json" not in invalid:
        try:
            loaded = json.loads(bi_path.read_text(encoding="utf-8"))
            build_info = loaded if isinstance(loaded, dict) else None
        except (OSError, ValueError):
            build_info = None
    required_bad = [r for r in invalid if r in REQUIRED_ASSETS]
    return AssetReport(
        ok=not missing and not required_bad,
        files=files,
        missing=missing,
        invalid=invalid,
        build_info=build_info,
    )


def resolve_under(root: Path, rel: str) -> Path | None:
    """把 URL 路径安全地解析到 root 之下的一个现有文件；越界、隐藏文件或不存在返回 None。"""
    if "\x00" in rel or "\\" in rel:
        return None
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." or p.startswith(".") for p in parts):
        return None
    try:
        root_resolved = root.resolve()
        candidate = root_resolved.joinpath(*parts).resolve()
    except (OSError, RuntimeError):
        return None
    if root_resolved != candidate and root_resolved not in candidate.parents:
        return None
    return candidate if candidate.is_file() else None


def list_pages(pages_dir: Path) -> list[dict[str, str]]:
    """列出 pages/<name>/template.xml，供 harness 预设使用。"""
    result: list[dict[str, str]] = []
    if not pages_dir.is_dir():
        return result
    for tpl in sorted(pages_dir.glob("*/template.xml")):
        name = tpl.parent.name
        if name.startswith("."):
            continue
        entry = {"name": name, "template": f"/pages/{urllib.parse.quote(name)}/template.xml"}
        if (tpl.parent / "data.json").is_file():
            entry["data"] = f"/pages/{urllib.parse.quote(name)}/data.json"
        result.append(entry)
    return result


def list_samples(samples_dir: Path | None) -> list[str]:
    """``--samples`` 目录里的 ``*.xml``；没配置或目录不存在时为空列表（harness 就不显示 sample: 预设）。"""
    if samples_dir is None or not samples_dir.is_dir():
        return []
    return [f"/samples/{urllib.parse.quote(p.name)}" for p in sorted(samples_dir.glob("*.xml"))]


# ---------------------------------------------------------------------------
# 演示内容：纯标准库画图 + PNG/.fset 编码
# ---------------------------------------------------------------------------

Color = tuple[int, int, int, int]

# 七段数码管：a 上、b 右上、c 右下、d 下、e 左下、f 左上、g 中。
_SEGMENTS: dict[str, str] = {
    "0": "abcdef", "1": "bc", "2": "abged", "3": "abgcd", "4": "fgbc",
    "5": "afgcd", "6": "afgedc", "7": "abc", "8": "abcdefg", "9": "abcdfg", "-": "g", " ": "",
}
_BACKGROUNDS: tuple[Color, ...] = (
    (16, 32, 72, 255), (64, 16, 48, 255), (16, 56, 40, 255),
    (72, 48, 8, 255), (40, 16, 72, 255), (8, 56, 64, 255),
)


class Raster:
    """极简 RGBA 位图：只支持填充矩形，够画数码管、进度条和边框。"""

    def __init__(self, width: int, height: int, fill: Color) -> None:
        if width <= 0 or height <= 0:
            raise ValueError("width/height 必须为正")
        self.width = width
        self.height = height
        row = bytes(fill) * width
        self.rows: list[bytearray] = [bytearray(row) for _ in range(height)]

    def fill_rect(self, x: int, y: int, w: int, h: int, color: Color) -> None:
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(self.width, x + w), min(self.height, y + h)
        if x0 >= x1 or y0 >= y1:
            return
        pixels = bytes(color) * (x1 - x0)
        for row in self.rows[y0:y1]:
            row[x0 * 4 : x1 * 4] = pixels

    def outline(self, thickness: int, color: Color) -> None:
        t = thickness
        self.fill_rect(0, 0, self.width, t, color)
        self.fill_rect(0, self.height - t, self.width, t, color)
        self.fill_rect(0, 0, t, self.height, color)
        self.fill_rect(self.width - t, 0, t, self.height, color)

    def draw_digit(self, ch: str, x: int, y: int, w: int, h: int, t: int, color: Color) -> None:
        if ch == ":":
            self.fill_rect(x + (w - t) // 2, y + h // 3 - t // 2, t, t, color)
            self.fill_rect(x + (w - t) // 2, y + 2 * h // 3 - t // 2, t, t, color)
            return
        half = h // 2
        boxes = {
            "a": (x, y, w, t),
            "b": (x + w - t, y, t, half),
            "c": (x + w - t, y + half, t, h - half),
            "d": (x, y + h - t, w, t),
            "e": (x, y + half, t, h - half),
            "f": (x, y, t, half),
            "g": (x, y + half - t // 2, w, t),
        }
        for seg in _SEGMENTS.get(ch, ""):
            self.fill_rect(*boxes[seg], color)

    def draw_text(self, text: str, x: int, y: int, w: int, h: int, color: Color) -> int:
        """画一串七段字符，返回占用宽度。"""
        t = max(2, h // 9)
        gap = max(2, w // 4)
        cx = x
        for ch in text:
            cw = max(t * 2, w // 3) if ch == ":" else w
            self.draw_digit(ch, cx, y, cw, h, t, color)
            cx += cw + gap
        return cx - x

    def raw_rgba(self) -> bytes:
        return b"".join(bytes(r) for r in self.rows)

    def to_png(self) -> bytes:
        return encode_png_rgba(self.width, self.height, self.rows)


def encode_png_rgba(width: int, height: int, rows: Sequence[bytes | bytearray]) -> bytes:
    """8-bit RGBA 非隔行 PNG（与资源包 .fset 样本同色彩类型，research.md §4）。"""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes(r) for r in rows)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")


def build_fset(frames: Sequence[bytes]) -> bytes:
    """按 research.md §4 的布局拼 .fset：uint32 N + N×(offset,size,0) + 帧数据首尾相接。

    这里刻意不 import glasscast.fset，让模拟器工具不依赖核心库也能独立运行。
    """
    if not frames:
        raise ValueError("frames 不能为空")
    n = len(frames)
    offset = 4 + 12 * n
    header = [struct.pack("<I", n)]
    for blob in frames:
        header.append(struct.pack("<III", offset, len(blob), 0))
        offset += len(blob)
    return b"".join(header) + b"".join(frames)


def render_demo_frame(
    width: int,
    height: int,
    seq: int,
    *,
    frame: int | None = None,
    frames: int | None = None,
    now: float | None = None,
) -> Raster:
    """演示画面：大号序号 + 时钟 + 进度条，背景色随序号轮换，肉眼一眼能看出是否刷新。"""
    ts = time.time() if now is None else now
    bg = _BACKGROUNDS[seq % len(_BACKGROUNDS)]
    r = Raster(width, height, bg)
    r.outline(max(2, min(width, height) // 72), (255, 200, 0, 255))
    pad = max(4, height // 24)
    big_h = max(10, int(height * 0.36))
    big_w = max(6, big_h // 2)
    r.draw_text(f"{seq % 100000}", pad * 2, pad * 2, big_w, big_h, (255, 255, 255, 255))
    clock = _dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S")
    small_h = max(8, int(height * 0.2))
    small_w = max(5, small_h // 2)
    r.draw_text(clock, pad * 2, pad * 3 + big_h, small_w, small_h, (120, 230, 120, 255))
    bar_y = height - pad * 2 - max(4, height // 20)
    bar_h = max(4, height // 20)
    if frame is not None and frames:
        # .fset 帧：右上角帧号 + 进度条按帧推进
        r.draw_text(f"{frame:02d}", width - pad * 2 - 2 * small_w - 2 * max(2, small_w // 4), pad * 2,
                    small_w, small_h, (255, 160, 64, 255))
        r.fill_rect(pad * 2, bar_y, width - pad * 4, bar_h, (40, 40, 40, 255))
        r.fill_rect(pad * 2, bar_y, (width - pad * 4) * (frame + 1) // frames, bar_h, (255, 160, 64, 255))
    else:
        # 单图：一个滑块随序号移动
        span = max(1, width - pad * 4 - bar_h * 4)
        r.fill_rect(pad * 2 + (seq * 37) % span, bar_y, bar_h * 4, bar_h, (255, 160, 64, 255))
    return r


def encode_demo_image(raster: Raster, want: str, *, gray: bool = False) -> tuple[bytes, str]:
    """want='jpeg' 时尽量用 Pillow 编 JPEG；没有 Pillow 就退回 PNG（内容类型如实返回）。

    gray=True 时输出 8 位灰度（JPEG 单通道 / PNG 色彩类型 0），需要 Pillow：模拟器实测屏幕是
    单色绿（只保留亮度），灰度图可以省带宽，这里用来验证内核能否解码灰度图。
    """
    if want == "jpeg" or gray:
        try:
            import io

            from PIL import Image  # 可选依赖：glasscast 的 venv 里有

            im = Image.frombytes("RGBA", (raster.width, raster.height), raster.raw_rgba())
            im = im.convert("L" if gray else "RGB")
            buf = io.BytesIO()
            if want == "jpeg":
                im.save(buf, "JPEG", quality=70, progressive=False)
                return buf.getvalue(), "image/jpeg"
            im.save(buf, "PNG", compress_level=6)
            return buf.getvalue(), "image/png"
        except ImportError:
            pass
    return raster.to_png(), "image/png"


def pillow_available() -> bool:
    try:
        import PIL  # noqa: F401
    except ImportError:
        return False
    return True


# ---------------------------------------------------------------------------
# 代理
# ---------------------------------------------------------------------------


class ProxyError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


_ENCODED_SCHEME_RE = re.compile(r"^https?%3a", re.IGNORECASE)


def extract_proxy_target(raw_query: str) -> str | None:
    """从原始 query 取 url 参数。

    内核 ``waft_download_set_cors_proxy(prefix)`` 之后怎么拼接远程地址没有公开（可能是
    ``prefix + url`` 原样拼，也可能 ``prefix + encodeURIComponent(url)``），所以这里不走
    parse_qs（原样拼时目标地址里的 ``&`` 会被切断），而是取 ``url=`` 之后的全部内容，
    若看起来是整体百分号编码过的（``https%3A…``）再解码一次。
    """
    if raw_query.startswith("url="):
        value = raw_query[4:]
    else:
        idx = raw_query.find("&url=")
        if idx < 0:
            return None
        value = raw_query[idx + 5 :]
    if _ENCODED_SCHEME_RE.match(value):
        value = urllib.parse.unquote(value)
    return value or None


def _is_public_address(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global


def is_loopback_client(addr: str) -> bool:
    """请求方地址是否本机回环（含 IPv4 映射的 ``::ffff:127.0.0.1`` 和带 zone 的 IPv6）。"""
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def validate_proxy_target(
    url: str,
    *,
    allow_private: bool,
    resolver: Callable[[str, int], Iterable[str]] | None = None,
) -> str:
    """校验代理目标，返回规范化后的 URL；不合格抛 ProxyError。

    - 只允许 http/https，不允许 userinfo；
    - 默认拒绝解析到本机/内网/链路本地等非公网地址（防止被当成跳板访问内网），
      ``--proxy-allow-private`` 可放开（比如经代理访问本机 glasscast 做对比）。
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ProxyError(HTTPStatus.BAD_REQUEST, "只允许 http/https 地址")
    if not parts.hostname:
        raise ProxyError(HTTPStatus.BAD_REQUEST, "地址缺少主机名")
    if "@" in parts.netloc:
        raise ProxyError(HTTPStatus.BAD_REQUEST, "不允许在地址里携带用户名/密码")
    if not allow_private:
        port = parts.port or (443 if parts.scheme == "https" else 80)
        resolve = resolver or _resolve_host
        try:
            addresses = list(resolve(parts.hostname, port))
        except OSError as exc:
            raise ProxyError(HTTPStatus.BAD_GATEWAY, f"域名解析失败：{exc}") from exc
        if not addresses or not all(_is_public_address(a) for a in addresses):
            raise ProxyError(
                HTTPStatus.FORBIDDEN,
                "目标解析到本机/内网地址，默认拒绝（需要时加 --proxy-allow-private）",
            )
    return urllib.parse.urlunsplit(parts)


def _resolve_host(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    return [str(info[4][0]) for info in infos]


def _guarded_create_connection(
    address: tuple[str, int],
    timeout: Any = socket._GLOBAL_DEFAULT_TIMEOUT,  # type: ignore[attr-defined]  # 与 socket.create_connection 一致
    source_address: tuple[str, int] | None = None,
) -> socket.socket:
    """解析一次、校验、再直连校验过的 IP，代替 ``socket.create_connection``。

    ``validate_proxy_target`` 的解析与 http.client 建连时的解析是两次独立查询，DNS 重绑定可以让
    第一次返回公网地址、第二次返回 127.0.0.1（TOCTOU）。这里把"校验"和"连接"绑在同一次解析结果上：
    任何一个地址不是公网就拒绝，通过后按 IP 字面量连接，不再二次解析。HTTPS 的 SNI 和证书校验仍用
    原主机名（HTTPSConnection 在本函数返回后用 ``self.host`` 包 TLS）。
    """
    host, port = address
    try:
        addresses = list(_resolve_host(host, port))
    except OSError as exc:
        raise ProxyError(HTTPStatus.BAD_GATEWAY, f"域名解析失败：{exc}") from exc
    if not addresses or not all(_is_public_address(a) for a in addresses):
        raise ProxyError(
            HTTPStatus.FORBIDDEN,
            "目标解析到本机/内网地址，默认拒绝（需要时加 --proxy-allow-private）",
        )
    last_exc: OSError | None = None
    for ip in addresses:
        try:
            return socket.create_connection((ip, port), timeout, source_address)
        except OSError as exc:
            last_exc = exc
    assert last_exc is not None
    raise last_exc


class _PinnedConnectionMixin:
    """给 urllib 的 HTTP(S)Handler 换上 :func:`_guarded_create_connection`（只在不允许内网时）。

    覆盖 ``do_open`` 而不是 ``http_open`` / ``https_open``：后两者在不同 Python 版本里给连接类传的
    参数不同（3.12 去掉了 check_hostname），包一层连接类工厂与版本无关。
    """

    _allow_private: bool = False

    def do_open(
        self,
        http_class: Callable[..., http.client.HTTPConnection],
        req: urllib.request.Request,
        **http_conn_args: Any,
    ) -> http.client.HTTPResponse:
        allow_private = self._allow_private

        def factory(*args: Any, **kwargs: Any) -> http.client.HTTPConnection:
            conn = http_class(*args, **kwargs)
            if not allow_private:
                # http.client 的建连入口（HTTPConnection.connect 调用 self._create_connection）
                conn._create_connection = _guarded_create_connection
            return conn

        return super().do_open(factory, req, **http_conn_args)  # type: ignore[misc]


class _PinnedHTTPHandler(_PinnedConnectionMixin, urllib.request.HTTPHandler):
    def __init__(self, allow_private: bool) -> None:
        super().__init__()
        self._allow_private = allow_private


class _PinnedHTTPSHandler(_PinnedConnectionMixin, urllib.request.HTTPSHandler):
    def __init__(self, allow_private: bool) -> None:
        super().__init__()
        self._allow_private = allow_private


class _GuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """跟随重定向前重新校验目标，避免公网地址 302 到内网绕过检查。"""

    def __init__(self, validate: Callable[[str], str]) -> None:
        super().__init__()
        self._validate = validate

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        self._validate(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_via_proxy(url: str, *, allow_private: bool) -> tuple[int, bytes, str]:
    """取远程资源，返回 (状态码, 内容, Content-Type)。"""

    def validate(u: str) -> str:
        return validate_proxy_target(u, allow_private=allow_private)

    target = validate(url)  # 先按 URL 校验一遍，给出清楚的 400/403；建连时还会再按实际连接的 IP 校验
    opener = urllib.request.build_opener(
        # 不走环境变量 / macOS 系统代理：经上游代理时连接的是代理地址，目标由代理自己解析，校验形同虚设
        urllib.request.ProxyHandler({}),
        _PinnedHTTPHandler(allow_private),
        _PinnedHTTPSHandler(allow_private),
        _GuardedRedirectHandler(validate),
    )
    req = urllib.request.Request(
        target,
        headers={"User-Agent": "glasscast-simulator-proxy/0.1", "Accept": "*/*"},
        method="GET",
    )
    try:
        with opener.open(req, timeout=PROXY_TIMEOUT_S) as resp:
            body = resp.read(PROXY_MAX_BYTES + 1)
            status = int(resp.status)
            ctype = resp.headers.get("Content-Type", "application/octet-stream")
    except urllib.error.HTTPError as exc:
        body = exc.read(64 * 1024) if exc.fp is not None else b""
        return int(exc.code), body, exc.headers.get("Content-Type", "text/plain") if exc.headers else "text/plain"
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, ProxyError):
            raise reason
        raise ProxyError(HTTPStatus.BAD_GATEWAY, f"上游请求失败：{reason}") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise ProxyError(HTTPStatus.GATEWAY_TIMEOUT, "上游超时") from exc
    if len(body) > PROXY_MAX_BYTES:
        raise ProxyError(HTTPStatus.BAD_GATEWAY, f"上游内容超过 {PROXY_MAX_BYTES} 字节上限")
    return status, body, ctype


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class SimulatorServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, config: SimulatorConfig, state: SimulatorState | None = None) -> None:
        self.config = config
        self.state = state or SimulatorState()
        if ":" in config.host:
            self.address_family = socket.AF_INET6
        super().__init__((config.host, config.port), SimulatorHandler)

    @property
    def url(self) -> str:
        host, port = self.server_address[0], self.server_address[1]
        host_s = str(host)
        if host_s in ("0.0.0.0", "::"):
            host_s = "127.0.0.1"
        if ":" in host_s:
            host_s = f"[{host_s}]"
        return f"http://{host_s}:{port}"


class SimulatorHandler(BaseHTTPRequestHandler):
    server: SimulatorServer
    server_version = "glasscast-simulator/0.1"
    protocol_version = "HTTP/1.1"

    # ---- 通用 ----

    def end_headers(self) -> None:
        # 所有响应（含错误页）都带隔离头：harness 页面和它加载的一切都要 cross-origin isolated。
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002  (签名沿用基类)
        if not self.server.config.quiet:
            super().log_message(format, *args)

    def _host_allowed(self) -> bool:
        """校验 Host 头，挡住 DNS 重绑定（外部网页借浏览器访问本机服务）。"""
        host = self.headers.get("Host")
        if not host:
            # 浏览器总会带 Host；没有 Host 的只可能是手工客户端，一律拒绝，免得绕过下面的白名单
            return False
        host = host.strip().lower()
        if host.startswith("["):
            name = host[1 : host.find("]")] if "]" in host else host
        else:
            name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        cfg = self.server.config
        allowed = set(LOOPBACK_NAMES) | {h.lower() for h in cfg.extra_hosts}
        if cfg.host not in ("0.0.0.0", "::", ""):
            allowed.add(cfg.host.lower())
        return name in allowed

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        headers: dict[str, str] | None = None,
        head_only: bool = False,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _send_json(self, status: int, payload: Any, *, head_only: bool = False) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8",
                         headers={"Cache-Control": "no-store"}, head_only=head_only)

    def _send_text(self, status: int, message: str, *, head_only: bool = False) -> None:
        self._send_bytes(status, (message + "\n").encode("utf-8"), "text/plain; charset=utf-8",
                         headers={"Cache-Control": "no-store"}, head_only=head_only)

    def _send_file(self, path: Path, *, cache: str, head_only: bool) -> None:
        stat = path.stat()
        etag = f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
        ctype = MIME_TYPES.get(path.suffix.lower(), "application/octet-stream")
        if self.headers.get("If-None-Match") == etag:
            self.send_response(HTTPStatus.NOT_MODIFIED)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", cache)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(stat.st_size))
        self.send_header("ETag", etag)
        self.send_header("Last-Modified", email.utils.formatdate(stat.st_mtime, usegmt=True))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        if head_only:
            return
        with path.open("rb") as fh:
            shutil.copyfileobj(fh, self.wfile, 256 * 1024)

    # ---- 路由 ----

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch(head_only=False)

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch(head_only=True)

    def do_POST(self) -> None:  # noqa: N802
        self._send_text(HTTPStatus.METHOD_NOT_ALLOWED, "只支持 GET/HEAD")

    def _dispatch(self, *, head_only: bool) -> None:
        if not self._host_allowed():
            self._send_text(HTTPStatus.FORBIDDEN, "Host 不在允许列表（用 --allow-host 添加）", head_only=head_only)
            return
        split = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(split.path)
        cfg = self.server.config
        try:
            if path in STATIC_FILES:
                file_path = SIM_DIR / STATIC_FILES[path]
                if not file_path.is_file():
                    self._send_text(HTTPStatus.NOT_FOUND, "文件不存在", head_only=head_only)
                    return
                self._send_file(file_path, cache="no-store", head_only=head_only)
            elif path.startswith("/assets/"):
                self._serve_under(cfg.assets_dir, path[len("/assets/") :], cache="public, max-age=3600",
                                  head_only=head_only, hint="内核资源缺失：先运行 tools/simulator/fetch_assets.sh")
            elif path.startswith("/pages/"):
                self._serve_under(cfg.pages_dir, path[len("/pages/") :], cache="no-store", head_only=head_only)
            elif path.startswith("/samples/"):
                if cfg.samples_dir is None:
                    self._send_text(HTTPStatus.NOT_FOUND, "没有配置示例目录（启动时加 --samples DIR）", head_only=head_only)
                    return
                self._serve_under(cfg.samples_dir, path[len("/samples/") :], cache="no-store", head_only=head_only)
            elif path == "/api/info":
                self._send_json(HTTPStatus.OK, self._info(), head_only=head_only)
            elif path == "/api/requests":
                self._serve_requests(split.query, head_only=head_only)
            elif path.startswith("/demo/"):
                self._serve_demo(path, split.query, head_only=head_only)
            elif path == "/proxy":
                self._serve_proxy(split.query, head_only=head_only)
            elif path == "/favicon.ico":
                self._send_bytes(HTTPStatus.NO_CONTENT, b"", "image/x-icon", head_only=head_only)
            else:
                self._send_text(HTTPStatus.NOT_FOUND, "未找到", head_only=head_only)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _serve_under(self, root: Path, rel: str, *, cache: str, head_only: bool, hint: str = "未找到") -> None:
        target = resolve_under(root, rel)
        if target is None:
            self._send_text(HTTPStatus.NOT_FOUND, hint, head_only=head_only)
            return
        self._send_file(target, cache=cache, head_only=head_only)

    def _info(self) -> dict[str, Any]:
        cfg = self.server.config
        w, h = cfg.demo_size
        return {
            "assets": check_assets(cfg.assets_dir).to_json(),
            "proxy": {"enabled": cfg.allow_proxy, "prefix": "/proxy?url=", "allow_private": cfg.proxy_allow_private},
            "pillow": pillow_available(),
            "demo": {
                "width": w,
                "height": h,
                "frames": cfg.demo_frames,
                "image": "/demo/screen.jpg",
                "image_png": "/demo/screen.png",
                "clip": "/demo/clip.fset",
                "clip_jpeg": "/demo/clip-jpeg.fset",
                "clip_gray": "/demo/clip-gray.fset",
                "image_gray": "/demo/screen-gray.jpg",
                "over_budget": "/demo/big.png",
            },
            "pages": list_pages(cfg.pages_dir),
            "samples": list_samples(cfg.samples_dir),
            "server_time": time.time(),
        }

    def _serve_requests(self, query: str, *, head_only: bool) -> None:
        params = urllib.parse.parse_qs(query)
        try:
            since = float(params.get("since", ["0"])[0])
        except ValueError:
            since = 0.0
        self._send_json(HTTPStatus.OK, {"now": time.time(), "items": self.server.state.recent(since)},
                        head_only=head_only)

    # ---- /demo ----

    def _serve_demo(self, path: str, query: str, *, head_only: bool) -> None:
        cfg = self.server.config
        state = self.server.state
        w, h = cfg.demo_size
        # 与 glasscast 一致：no-store + 允许跨源读取（CORS + CORP），模拟真实投屏服务的响应头。
        common = {
            "Cache-Control": "no-store",
            "Access-Control-Allow-Origin": "*",
            "Cross-Origin-Resource-Policy": "cross-origin",
        }
        # 页面地址以 .jpg 结尾（…/screen.jpg/<时间戳>_<序号>.jpg，research.md §9.6）：去掉尾段后按原路径处理
        path = re.sub(r"^(/demo/screen(?:-gray)?\.(?:jpg|png))/[^/]+\.(?:jpg|png)$", r"\1", path)
        if path in ("/demo/screen.jpg", "/demo/screen.png", "/demo/screen-gray.jpg", "/demo/screen-gray.png"):
            gray = "-gray" in path
            if gray and not pillow_available():
                self._send_text(HTTPStatus.NOT_IMPLEMENTED, "灰度图需要 Pillow", head_only=head_only)
                return
            seq = state.next_seq("screen")
            raster = render_demo_frame(w, h, seq)
            body, ctype = encode_demo_image(raster, "jpeg" if path.endswith(".jpg") else "png", gray=gray)
            headers = {**common, "X-Demo-Seq": str(seq)}
        elif path == "/demo/big.png":
            # 故意超预算：(w+60)×(h+10)×4 > 742 400 B，用来确认模拟器会拒绝并告警（research.md §2.3）。
            seq = state.next_seq("big")
            raster = render_demo_frame(w + 60, h + 10, seq)
            body, ctype = raster.to_png(), "image/png"
            headers = {**common, "X-Demo-Seq": str(seq)}
        elif path == "/demo/clip.fset" or re.fullmatch(r"/demo/clip/[^/]{1,64}\.fset", path):
            seq = state.next_seq("clip")
            n = cfg.demo_frames
            now = time.time()
            frames = [render_demo_frame(w, h, seq, frame=i, frames=n, now=now).to_png() for i in range(n)]
            body, ctype = build_fset(frames), "application/octet-stream"
            headers = {**common, "X-Demo-Seq": str(seq), "X-Demo-Frames": str(n), "X-Demo-Frame-Format": "png"}
        elif m := re.fullmatch(r"/demo/clip-(jpeg|gray)(?:\.fset|/[^/]{1,64}\.fset)", path):
            # 实验帧格式（需要 Pillow）：jpeg = JPEG 帧（research.md §4、§8 待验证 3）；
            # gray = 8 位灰度 PNG 帧（屏幕是单色，看能否省体积）。
            if not pillow_available():
                self._send_text(HTTPStatus.NOT_IMPLEMENTED, "实验帧格式需要 Pillow", head_only=head_only)
                return
            variant = m.group(1)
            seq = state.next_seq(f"clip-{variant}")
            n = cfg.demo_frames
            now = time.time()
            frames = [
                encode_demo_image(render_demo_frame(w, h, seq, frame=i, frames=n, now=now),
                                  "jpeg" if variant == "jpeg" else "png", gray=variant == "gray")[0]
                for i in range(n)
            ]
            body, ctype = build_fset(frames), "application/octet-stream"
            headers = {**common, "X-Demo-Seq": str(seq), "X-Demo-Frames": str(n),
                       "X-Demo-Frame-Format": "jpeg" if variant == "jpeg" else "png-gray"}
        else:
            self._send_text(HTTPStatus.NOT_FOUND, "未知演示资源", head_only=head_only)
            return
        state.record({
            "ts": time.time(), "kind": "demo", "path": path, "query": query[:200],
            "status": 200, "bytes": len(body), "seq": seq, "method": self.command,
        })
        self._send_bytes(HTTPStatus.OK, body, ctype, headers=headers, head_only=head_only)

    # ---- /proxy ----

    def _serve_proxy(self, query: str, *, head_only: bool) -> None:
        cfg = self.server.config
        if not cfg.allow_proxy:
            self._send_text(HTTPStatus.FORBIDDEN, "代理未开启：启动时加 --allow-proxy", head_only=head_only)
            return
        # 只服务本机：下面的同源检查只对带 Sec-Fetch-Site / Origin 的浏览器请求有效，Host 也由客户端决定；
        # 用 --host 0.0.0.0 监听时，局域网里的非浏览器客户端可以什么都不带，把这里当开放代理用。
        if not is_loopback_client(self.client_address[0]):
            self._send_text(HTTPStatus.FORBIDDEN, "代理只接受本机请求", head_only=head_only)
            return
        # 只服务本页面自己的请求：浏览器跨站请求带 Sec-Fetch-Site: cross-site / Origin，直接拒绝，
        # 免得任意网页借本机代理读别人的内容。
        site = self.headers.get("Sec-Fetch-Site")
        if site is not None and site not in ("same-origin", "none"):
            self._send_text(HTTPStatus.FORBIDDEN, "代理只接受同源请求", head_only=head_only)
            return
        origin = self.headers.get("Origin")
        if origin is not None and origin != "null":
            o = urllib.parse.urlsplit(origin)
            allowed = LOOPBACK_NAMES | {cfg.host.lower()} | {h.lower() for h in cfg.extra_hosts}
            if o.hostname is None or o.hostname.lower() not in allowed:
                self._send_text(HTTPStatus.FORBIDDEN, "代理只接受同源请求", head_only=head_only)
                return
        target = extract_proxy_target(query)
        if not target:
            self._send_text(HTTPStatus.BAD_REQUEST, "缺少 url 参数", head_only=head_only)
            return
        started = time.time()
        try:
            status, body, ctype = fetch_via_proxy(target, allow_private=cfg.proxy_allow_private)
        except ProxyError as exc:
            self.server.state.record({
                "ts": time.time(), "kind": "proxy", "path": "/proxy", "query": query[:300], "target": target[:300],
                "status": int(exc.status), "error": exc.message, "bytes": 0,
            })
            self._send_text(int(exc.status), exc.message, head_only=head_only)
            return
        self.server.state.record({
            "ts": time.time(), "kind": "proxy", "path": "/proxy", "query": query[:300], "target": target[:300],
            "status": status, "bytes": len(body), "ms": round((time.time() - started) * 1000),
        })
        # 代理结果对 harness 是同源资源；CORP 写 same-origin 已满足 COEP require-corp，
        # 且不给 ACAO，避免别的源借它读取。
        self._send_bytes(status, body, ctype, headers={
            "Cache-Control": "no-store",
            "Cross-Origin-Resource-Policy": "same-origin",
        }, head_only=head_only)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_size(text: str) -> tuple[int, int]:
    m = re.fullmatch(r"\s*(\d+)\s*[xX×]\s*(\d+)\s*", text)
    if not m:
        raise argparse.ArgumentTypeError("尺寸格式应为 宽x高，例如 640x290")
    w, h = int(m.group(1)), int(m.group(2))
    if not (1 <= w <= 4096 and 1 <= h <= 4096):
        raise argparse.ArgumentTypeError("尺寸超出范围 1..4096")
    return w, h


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="glasscast 本地模拟器服务器：加 COOP/COEP 头提供 waft-lite 内核与 harness 页面。",
    )
    p.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1，只给本机用）")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"端口（默认 {DEFAULT_PORT}；0 = 随机）")
    p.add_argument("--assets", type=Path, default=SIM_DIR / "assets", help="fetch_assets.sh 的下载目录")
    p.add_argument("--pages", type=Path, default=PROJECT_ROOT / "pages", help="映射到 /pages/ 的模板目录")
    p.add_argument("--samples", type=Path, default=None, metavar="DIR",
                   help="可选：把这个目录里的 *.xml 映射到 /samples/，harness 里出现为 sample:<name> 预设（默认不提供）")
    p.add_argument("--allow-proxy", action="store_true", help="打开 /proxy?url= 同源代理（默认关闭）")
    p.add_argument("--proxy-allow-private", action="store_true",
                   help="允许代理访问本机/内网地址（默认拒绝；仅在 --allow-proxy 时有意义）")
    p.add_argument("--allow-host", action="append", default=[], metavar="NAME",
                   help="额外允许的 Host 名（默认只认 127.0.0.1/localhost/::1 与 --host）")
    p.add_argument("--demo-frames", type=int, default=10, help="/demo/clip.fset 的帧数（默认 10，与 cast_clip 模板一致）")
    p.add_argument("--demo-size", type=_parse_size, default=CANVAS, help="演示图尺寸（默认 640x290）")
    p.add_argument("--quiet", action="store_true", help="不打印逐条访问日志")
    p.add_argument("--open", action="store_true", help="启动后用默认浏览器打开 harness")
    return p


def config_from_args(args: argparse.Namespace) -> SimulatorConfig:
    if not 1 <= args.demo_frames <= 240:
        raise SystemExit("--demo-frames 必须在 1..240")
    return SimulatorConfig(
        host=args.host,
        port=args.port,
        assets_dir=args.assets.expanduser().resolve(),
        pages_dir=args.pages.expanduser().resolve(),
        samples_dir=args.samples.expanduser().resolve() if args.samples is not None else None,
        allow_proxy=bool(args.allow_proxy),
        proxy_allow_private=bool(args.proxy_allow_private),
        extra_hosts=tuple(args.allow_host),
        demo_frames=args.demo_frames,
        demo_size=args.demo_size,
        quiet=bool(args.quiet),
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    config = config_from_args(args)
    # 后台运行/重定向到文件时 stdout 是块缓冲，启动横幅会迟迟不出现；改成行缓冲。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    try:
        server = SimulatorServer(config)
    except OSError as exc:
        print(f"无法监听 {config.host}:{config.port}：{exc}", file=sys.stderr)
        return 1
    report = check_assets(config.assets_dir)
    print(f"glasscast 模拟器：{server.url}/")
    print(f"  内核资源目录：{config.assets_dir}")
    if report.ok:
        bi = report.build_info or {}
        print(f"  内核资源齐全（waft-lite v{bi.get('version', '?')} {bi.get('variant', '?')}/{bi.get('channel', '?')}）")
    else:
        print("  [警告] 内核资源不完整：", ", ".join(report.missing + list(report.invalid)) or "?")
        print("         先运行：tools/simulator/fetch_assets.sh")
    print(f"  演示资源：{server.url}/demo/screen.jpg  {server.url}/demo/clip.fset"
          f"（{config.demo_frames} 帧 {config.demo_size[0]}x{config.demo_size[1]}）")
    if config.samples_dir is not None:
        count = len(list_samples(config.samples_dir))
        note = f"{count} 个 XML" if config.samples_dir.is_dir() else "[警告] 目录不存在，忽略"
        print(f"  示例目录：{config.samples_dir}（{note}）")
    if config.allow_proxy:
        scope = "含本机/内网" if config.proxy_allow_private else "仅公网目标"
        print(f"  代理已开启：{server.url}/proxy?url=…（{scope}，只接受本机的同源请求）")
    else:
        print("  代理关闭（需要时加 --allow-proxy）")
    if config.host not in LOOPBACK_NAMES:
        print(f"  [注意] 监听在 {config.host}，局域网内其他机器也能访问")
    if args.open:
        import webbrowser

        webbrowser.open(server.url + "/")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
