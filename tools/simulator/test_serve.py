"""tools/simulator/serve.py 的测试。

不需要浏览器、不访问公网、不截屏：服务器起在 127.0.0.1 的随机端口，内核资源用假文件代替，
代理的上游也是本机临时起的 HTTP 服务。

运行：uv run pytest tools/simulator/test_serve.py
（pyproject 的 testpaths 只含 tests/，所以这里要显式给路径。）
"""

from __future__ import annotations

import http.client
import importlib.util
import json
import struct
import sys
import threading
import urllib.parse
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SIM_DIR = Path(__file__).resolve().parent


def _load_serve() -> ModuleType:
    spec = importlib.util.spec_from_file_location("glasscast_simulator_serve", SIM_DIR / "serve.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclass 解析注解时需要模块已登记
    spec.loader.exec_module(module)
    return module


serve = _load_serve()


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def png_info(data: bytes) -> tuple[int, int, int]:
    """返回 (宽, 高, 色彩类型)。"""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert data[12:16] == b"IHDR"
    w, h = struct.unpack(">II", data[16:24])
    color_type = data[25]
    assert data.endswith(b"IEND\xaeB`\x82")
    return w, h, color_type


def parse_fset(data: bytes) -> list[bytes]:
    """按 research.md §4 解析，并校验连续性（测试里独立实现，不依赖 glasscast.fset）。"""
    (n,) = struct.unpack_from("<I", data, 0)
    assert n > 0
    expected = 4 + 12 * n
    frames: list[bytes] = []
    for i in range(n):
        off, size, reserved = struct.unpack_from("<III", data, 4 + 12 * i)
        assert off == expected and reserved == 0
        frames.append(data[off : off + size])
        expected = off + size
    assert expected == len(data)
    return frames


class Response:
    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


def request(port: int, path: str, *, method: str = "GET", headers: dict[str, str] | None = None) -> Response:
    """用 http.client 发原始路径（不做 ../ 归一化），方便测路径穿越。"""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        hdrs = {"Host": f"127.0.0.1:{port}", **(headers or {})}
        for k, v in hdrs.items():
            conn.putheader(k, v)
        conn.endheaders()
        resp = conn.getresponse()
        body = resp.read()
        return Response(resp.status, {k.lower(): v for k, v in resp.getheaders()}, body)
    finally:
        conn.close()


@pytest.fixture()
def fake_tree(tmp_path: Path) -> dict[str, Path]:
    assets = tmp_path / "assets"
    (assets / "dist").mkdir(parents=True)
    (assets / "index.blob-worker.js").write_text("import x from './dist/waft-lite-wasm.blob-worker.js';\n", encoding="utf-8")
    (assets / "dist" / "waft-lite-wasm.blob-worker.js").write_text("// glue\n", encoding="utf-8")
    (assets / "dist" / "waft-lite-wasm.wasm").write_bytes(b"\x00asm\x01\x00\x00\x00")
    (assets / "dist" / "waft-lite-wasm.data").write_bytes(b"\x00" * 64)
    (assets / "dist" / "build-info.json").write_text(json.dumps({"version": "0.0.0-test"}), encoding="utf-8")
    (assets / ".secret").write_text("x", encoding="utf-8")
    pages = tmp_path / "pages"
    (pages / "cast-image").mkdir(parents=True)
    (pages / "cast-image" / "template.xml").write_text("<view/>", encoding="utf-8")
    (pages / "cast-image" / "data.json").write_text('{"url": "x"}', encoding="utf-8")
    samples = tmp_path / "samples"
    samples.mkdir()
    (samples / "demo.xml").write_text("<view/>", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("不应被读到", encoding="utf-8")
    return {"root": tmp_path, "assets": assets, "pages": pages, "samples": samples}


def _start(config: Any) -> Any:
    server = serve.SimulatorServer(config)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    return server


@pytest.fixture()
def make_server(fake_tree: dict[str, Path]) -> Iterator[Any]:
    servers: list[Any] = []

    def factory(**overrides: Any) -> Any:
        kwargs: dict[str, Any] = {
            "host": "127.0.0.1",
            "port": 0,
            "assets_dir": fake_tree["assets"],
            "pages_dir": fake_tree["pages"],
            "samples_dir": fake_tree["samples"],
            "quiet": True,
        }
        kwargs.update(overrides)
        cfg = serve.SimulatorConfig(**kwargs)
        srv = _start(cfg)
        servers.append(srv)
        return srv

    yield factory
    for srv in servers:
        srv.shutdown()
        srv.server_close()


def port_of(server: Any) -> int:
    return int(server.server_address[1])


# ---------------------------------------------------------------------------
# 静态资源与隔离头
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/harness.js", "/assets/index.blob-worker.js", "/nope", "/api/info", "/demo/screen.png"])
def test_isolation_headers_on_every_response(make_server: Any, path: str) -> None:
    port = port_of(make_server())
    r = request(port, path)
    assert r.headers["cross-origin-opener-policy"] == "same-origin"
    assert r.headers["cross-origin-embedder-policy"] == "require-corp"


def test_mime_types(make_server: Any) -> None:
    port = port_of(make_server())
    assert request(port, "/").headers["content-type"].startswith("text/html")
    assert request(port, "/harness.js").headers["content-type"].startswith("text/javascript")
    assert request(port, "/assets/index.blob-worker.js").headers["content-type"].startswith("text/javascript")
    wasm = request(port, "/assets/dist/waft-lite-wasm.wasm")
    assert wasm.status == 200 and wasm.headers["content-type"] == "application/wasm"
    assert wasm.body.startswith(b"\x00asm")
    data = request(port, "/assets/dist/waft-lite-wasm.data")
    assert data.headers["content-type"] == "application/octet-stream"
    assert len(data.body) == 64


def test_head_has_no_body(make_server: Any) -> None:
    port = port_of(make_server())
    r = request(port, "/assets/dist/waft-lite-wasm.data", method="HEAD")
    assert r.status == 200 and r.body == b"" and r.headers["content-length"] == "64"


@pytest.mark.parametrize(
    "path",
    [
        "/serve.py",
        "/test_serve.py",
        "/assets/../serve.py",
        "/assets/%2e%2e/serve.py",
        "/assets/..%2foutside.txt",
        "/pages/../outside.txt",
        "/pages/%2e%2e/outside.txt",
        "/samples/../outside.txt",
        "/samples/%2e%2e/outside.txt",
        "/assets/.secret",
        "/assets/",
        "/assets/dist",
    ],
)
def test_no_traversal_or_unlisted_files(make_server: Any, path: str) -> None:
    port = port_of(make_server())
    r = request(port, path)
    assert r.status == 404, (path, r.status)
    assert "不应被读到".encode() not in r.body


def test_etag_conditional_get(make_server: Any) -> None:
    port = port_of(make_server())
    first = request(port, "/assets/dist/waft-lite-wasm.wasm")
    etag = first.headers["etag"]
    second = request(port, "/assets/dist/waft-lite-wasm.wasm", headers={"If-None-Match": etag})
    assert second.status == 304 and second.body == b""


def test_host_header_checked(make_server: Any) -> None:
    port = port_of(make_server())
    assert request(port, "/", headers={"Host": "evil.example"}).status == 403
    assert request(port, "/", headers={"Host": f"evil.example:{port}"}).status == 403
    assert request(port, "/", headers={"Host": f"localhost:{port}"}).status == 200
    assert request(port, "/", headers={"Host": f"[::1]:{port}"}).status == 200


def test_missing_host_header_rejected(make_server: Any) -> None:
    port = port_of(make_server())
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.putrequest("GET", "/", skip_host=True, skip_accept_encoding=True)
        conn.endheaders()
        resp = conn.getresponse()
        resp.read()  # 读完再关，免得服务端读下一个请求时遇到 RST
        assert resp.status == 403
    finally:
        conn.close()


def test_extra_allowed_host(make_server: Any) -> None:
    port = port_of(make_server(extra_hosts=("mybox.local",)))
    assert request(port, "/", headers={"Host": f"mybox.local:{port}"}).status == 200


def test_post_rejected(make_server: Any) -> None:
    port = port_of(make_server())
    assert request(port, "/", method="POST").status == 405


# ---------------------------------------------------------------------------
# /api
# ---------------------------------------------------------------------------


def test_api_info(make_server: Any) -> None:
    port = port_of(make_server())
    info = request(port, "/api/info").json()
    assert info["assets"]["ok"] is True
    assert info["assets"]["build_info"] == {"version": "0.0.0-test"}
    assert info["proxy"]["enabled"] is False
    assert info["demo"]["width"] == 640 and info["demo"]["height"] == 290 and info["demo"]["frames"] == 10
    assert info["pages"] == [
        {"name": "cast-image", "template": "/pages/cast-image/template.xml", "data": "/pages/cast-image/data.json"}
    ]
    assert info["samples"] == ["/samples/demo.xml"]
    assert request(port, "/pages/cast-image/template.xml").body == b"<view/>"
    assert request(port, "/samples/demo.xml").body == b"<view/>"


@pytest.mark.parametrize("samples", ["unset", "missing"])
def test_samples_are_optional(make_server: Any, fake_tree: dict[str, Path], samples: str) -> None:
    """示例目录是可选的：不配置（默认）或目录不存在时 samples 为空、/samples/ 404，其余照常。"""
    assert serve.SimulatorConfig().samples_dir is None
    samples_dir = None if samples == "unset" else fake_tree["root"] / "no-such-dir"
    port = port_of(make_server(samples_dir=samples_dir))
    info = request(port, "/api/info").json()
    assert info["samples"] == []
    assert info["assets"]["ok"] is True and info["pages"][0]["name"] == "cast-image"
    r = request(port, "/samples/demo.xml")
    assert r.status == 404
    if samples == "unset":
        assert "--samples" in r.body.decode("utf-8")
    assert serve.list_samples(None) == []


def test_check_assets_reports_missing_and_error_pages(tmp_path: Path) -> None:
    report = serve.check_assets(tmp_path)
    assert report.ok is False
    assert set(report.missing) == set(serve.REQUIRED_ASSETS)
    (tmp_path / "dist").mkdir()
    for rel in serve.REQUIRED_ASSETS:
        (tmp_path / rel).write_bytes(b"<?xml version='1.0'?><Error>NoSuchKey</Error>")
    report = serve.check_assets(tmp_path)
    assert report.ok is False and not report.missing
    assert "dist/waft-lite-wasm.wasm" in report.invalid


def test_requests_log_records_demo_fetches(make_server: Any) -> None:
    port = port_of(make_server())
    request(port, "/demo/screen.png?t=3600")
    request(port, "/demo/clip/abc.fset?i=2")
    items = request(port, "/api/requests").json()["items"]
    assert [(i["path"], i["query"]) for i in items] == [("/demo/screen.png", "t=3600"), ("/demo/clip/abc.fset", "i=2")]
    since = items[0]["ts"]
    later = request(port, f"/api/requests?since={since}").json()["items"]
    assert [i["path"] for i in later] == ["/demo/clip/abc.fset"]


# ---------------------------------------------------------------------------
# /demo
# ---------------------------------------------------------------------------


def test_demo_screen_png(make_server: Any) -> None:
    port = port_of(make_server())
    r1 = request(port, "/demo/screen.png")
    r2 = request(port, "/demo/screen.png?t=1")
    assert r1.status == 200 and r1.headers["content-type"] == "image/png"
    assert png_info(r1.body) == (640, 290, 6)
    assert r1.headers["cache-control"] == "no-store"
    assert r1.headers["access-control-allow-origin"] == "*"
    assert r1.headers["cross-origin-resource-policy"] == "cross-origin"
    assert int(r2.headers["x-demo-seq"]) == int(r1.headers["x-demo-seq"]) + 1
    assert r1.body != r2.body  # 每次请求内容都变（背景色随序号轮换）
    # research.md §2.3：解码后 ≤ 742 400 B，文件 ≤ 1 MiB
    assert 640 * 290 * 4 <= serve.DISPLAY_BUDGET_BYTES and len(r1.body) <= 1_048_576


def test_demo_screen_jpeg(make_server: Any) -> None:
    port = port_of(make_server())
    r = request(port, "/demo/screen.jpg")
    assert r.status == 200
    if serve.pillow_available():
        assert r.headers["content-type"] == "image/jpeg" and r.body[:2] == b"\xff\xd8"
        from PIL import Image
        import io

        im = Image.open(io.BytesIO(r.body))
        assert im.size == (640, 290) and im.mode == "RGB"
        gray = request(port, "/demo/screen-gray.jpg")
        assert Image.open(io.BytesIO(gray.body)).mode == "L"
    else:
        assert r.headers["content-type"] == "image/png"


def test_demo_clip_fset(make_server: Any) -> None:
    port = port_of(make_server())
    for path in ("/demo/clip.fset", "/demo/clip/17.fset?i=3"):
        r = request(port, path)
        assert r.status == 200 and r.headers["content-type"] == "application/octet-stream"
        assert r.headers["x-demo-frames"] == "10"
        frames = parse_fset(r.body)
        assert len(frames) == 10
        assert all(png_info(f) == (640, 290, 6) for f in frames)
        assert len(set(frames)) == 10  # 帧号/进度条不同


def test_demo_clip_custom_frames_and_size(make_server: Any) -> None:
    port = port_of(make_server(demo_frames=3, demo_size=(64, 29)))
    frames = parse_fset(request(port, "/demo/clip.fset").body)
    assert len(frames) == 3 and all(png_info(f)[:2] == (64, 29) for f in frames)


@pytest.mark.skipif(not serve.pillow_available(), reason="实验帧格式需要 Pillow")
def test_demo_experimental_clip_formats(make_server: Any) -> None:
    port = port_of(make_server())
    jpeg = parse_fset(request(port, "/demo/clip-jpeg.fset").body)
    assert all(f[:2] == b"\xff\xd8" for f in jpeg)
    gray = parse_fset(request(port, "/demo/clip-gray/1.fset").body)
    assert all(png_info(f) == (640, 290, 0) for f in gray)


def test_demo_big_png_is_over_budget(make_server: Any) -> None:
    port = port_of(make_server())
    w, h, _ = png_info(request(port, "/demo/big.png").body)
    assert w * h * 4 > serve.DISPLAY_BUDGET_BYTES


def test_unknown_demo_is_404(make_server: Any) -> None:
    port = port_of(make_server())
    assert request(port, "/demo/whatever.png").status == 404
    assert request(port, "/demo/clip/a/b.fset").status == 404


def test_build_fset_layout() -> None:
    data = serve.build_fset([b"ab", b"cde"])
    assert struct.unpack_from("<I", data, 0) == (2,)
    assert struct.unpack_from("<III", data, 4) == (28, 2, 0)
    assert struct.unpack_from("<III", data, 16) == (30, 3, 0)
    assert data[28:] == b"abcde"
    with pytest.raises(ValueError):
        serve.build_fset([])


@pytest.mark.parametrize("size", [(1, 1), (32, 30), (640, 290), (1280, 120)])
def test_render_demo_frame_any_size(size: tuple[int, int]) -> None:
    w, h = size
    png = serve.render_demo_frame(w, h, 123456, frame=4, frames=10, now=0.0).to_png()
    assert png_info(png) == (w, h, 6)


# ---------------------------------------------------------------------------
# /proxy
# ---------------------------------------------------------------------------


def test_proxy_disabled_by_default(make_server: Any) -> None:
    port = port_of(make_server())
    r = request(port, "/proxy?url=https://example.com/a.png")
    assert r.status == 403


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("url=https://a.example/x.png?t=1&k=2", "https://a.example/x.png?t=1&k=2"),  # 原样拼接
        ("url=https%3A%2F%2Fa.example%2Fx.png%3Ft%3D1%26k%3D2", "https://a.example/x.png?t=1&k=2"),  # 内核实测：整体编码
        ("url=HTTP%3a%2F%2Fa.example%2F", "HTTP://a.example/"),
        ("x=1&url=https://a.example/y", "https://a.example/y"),
        ("url=", None),
        ("u=https://a.example/", None),
        ("", None),
    ],
)
def test_extract_proxy_target(query: str, expected: str | None) -> None:
    assert serve.extract_proxy_target(query) == expected


def test_validate_proxy_target() -> None:
    public = lambda host, port: ["93.184.216.34"]  # noqa: E731
    private = lambda host, port: ["127.0.0.1"]  # noqa: E731
    mixed = lambda host, port: ["93.184.216.34", "10.0.0.5"]  # noqa: E731
    mapped = lambda host, port: ["::ffff:192.168.1.1"]  # noqa: E731
    ok = serve.validate_proxy_target("https://a.example/x.png", allow_private=False, resolver=public)
    assert ok == "https://a.example/x.png"
    for bad_url in ("ftp://a.example/x", "file:///etc/passwd", "https:///nohost", "https://user:pw@a.example/"):
        with pytest.raises(serve.ProxyError) as ei:
            serve.validate_proxy_target(bad_url, allow_private=False, resolver=public)
        assert ei.value.status == 400
    for resolver in (private, mixed, mapped):
        with pytest.raises(serve.ProxyError) as ei:
            serve.validate_proxy_target("http://a.example/", allow_private=False, resolver=resolver)
        assert ei.value.status == 403
    # 显式放开内网时不做解析
    assert serve.validate_proxy_target("http://127.0.0.1:8766/s/k/screen.jpg", allow_private=True) == (
        "http://127.0.0.1:8766/s/k/screen.jpg"
    )


class _Upstream(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", "http://localhost:1/never")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path.startswith("/missing"):
            body = b"nope"
            self.send_response(404)
        else:
            body = b"\x89PNG-fake:" + self.path.encode()
            self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie", "secret=1")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass


@pytest.fixture()
def upstream() -> Iterator[int]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield int(srv.server_address[1])
    srv.shutdown()
    srv.server_close()


def test_proxy_end_to_end(make_server: Any, upstream: int) -> None:
    port = port_of(make_server(allow_proxy=True, proxy_allow_private=True))
    target = f"http://127.0.0.1:{upstream}/img.png?t=1&k=2"
    for q in (urllib.parse.quote(target, safe=""), target):
        r = request(port, f"/proxy?url={q}", headers={"Sec-Fetch-Site": "same-origin"})
        assert r.status == 200
        assert r.body == b"\x89PNG-fake:/img.png?t=1&k=2"
        assert r.headers["content-type"] == "image/png"
        assert r.headers["cross-origin-resource-policy"] == "same-origin"
        assert r.headers["cache-control"] == "no-store"
        assert "access-control-allow-origin" not in r.headers
        assert "set-cookie" not in r.headers
    missing = request(port, f"/proxy?url=http://127.0.0.1:{upstream}/missing")
    assert missing.status == 404
    items = request(port, "/api/requests").json()["items"]
    assert items[0]["kind"] == "proxy" and items[0]["target"] == target


def test_proxy_rejects_cross_site_callers(make_server: Any, upstream: int) -> None:
    port = port_of(make_server(allow_proxy=True, proxy_allow_private=True))
    url = f"/proxy?url=http://127.0.0.1:{upstream}/img.png"
    assert request(port, url, headers={"Sec-Fetch-Site": "cross-site"}).status == 403
    assert request(port, url, headers={"Sec-Fetch-Site": "same-site"}).status == 403
    assert request(port, url, headers={"Origin": "https://evil.example"}).status == 403
    assert request(port, url, headers={"Origin": f"http://127.0.0.1:{port}"}).status == 200


def test_proxy_origin_check_ignores_case(make_server: Any, upstream: int) -> None:
    """--allow-host 写成大小写混合时，Host 校验能过，代理的 Origin 校验也必须能过。"""
    port = port_of(make_server(allow_proxy=True, proxy_allow_private=True, extra_hosts=("MyBox.local",)))
    url = f"/proxy?url=http://127.0.0.1:{upstream}/img.png"
    r = request(port, url, headers={"Host": f"mybox.local:{port}", "Origin": f"http://mybox.local:{port}"})
    assert r.status == 200


def test_proxy_only_serves_loopback_clients(
    make_server: Any, upstream: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--host 0.0.0.0 时局域网客户端可以不带 Origin / Sec-Fetch-Site、伪造 Host：代理必须按来源地址拒绝。"""
    port = port_of(make_server(allow_proxy=True, proxy_allow_private=True))
    url = f"/proxy?url=http://127.0.0.1:{upstream}/img.png"
    assert request(port, url).status == 200
    monkeypatch.setattr(serve, "is_loopback_client", lambda addr: False)  # 模拟来自 192.168.x.x 的连接
    r = request(port, url)
    assert r.status == 403 and "本机" in r.body.decode("utf-8")


@pytest.mark.parametrize(
    ("addr", "expected"),
    [("127.0.0.1", True), ("127.8.9.10", True), ("::1", True), ("::ffff:127.0.0.1", True),
     ("192.168.1.50", False), ("10.0.0.1", False), ("fe80::1%en0", False), ("not-an-ip", False)],
)
def test_is_loopback_client(addr: str, expected: bool) -> None:
    assert serve.is_loopback_client(addr) is expected


def test_proxy_rejects_private_targets_by_default(make_server: Any, upstream: int) -> None:
    port = port_of(make_server(allow_proxy=True))
    r = request(port, f"/proxy?url=http://127.0.0.1:{upstream}/img.png")
    assert r.status == 403
    assert request(port, "/proxy?url=ftp://example.com/x").status == 400
    assert request(port, "/proxy").status == 400


def test_proxy_redirect_is_revalidated(upstream: int, monkeypatch: pytest.MonkeyPatch) -> None:
    # 允许内网时重定向照常跟随（localhost:1 连不上 → 502）。
    with pytest.raises(serve.ProxyError) as ei:
        serve.fetch_via_proxy(f"http://127.0.0.1:{upstream}/redirect", allow_private=True)
    assert ei.value.status == 502
    # 不允许内网：把 127.0.0.1 当成"公网"让首跳通过（建连按解析结果直连，所以这里真的连到本机上游），
    # 重定向到 localhost（解析成 127.0.0.2，不算公网）时必须被重新校验并拒绝。
    fake = {"public.test": ["127.0.0.1"], "localhost": ["127.0.0.2"]}
    monkeypatch.setattr(serve, "_resolve_host", lambda host, port: fake[host])
    monkeypatch.setattr(serve, "_is_public_address", lambda addr: addr == "127.0.0.1")
    assert serve.fetch_via_proxy(f"http://public.test:{upstream}/img.png", allow_private=False)[0] == 200
    with pytest.raises(serve.ProxyError) as ei:
        serve.fetch_via_proxy(f"http://public.test:{upstream}/redirect", allow_private=False)
    assert ei.value.status == 403


def test_proxy_resists_dns_rebinding(upstream: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """第一次解析给公网地址、第二次给 127.0.0.1：校验和建连必须用同一次解析结果，不能连到本机。"""
    calls: list[str] = []

    def rebinding(host: str, port: int) -> list[str]:
        calls.append(host)
        return ["93.184.216.34"] if len(calls) == 1 else ["127.0.0.1"]

    monkeypatch.setattr(serve, "_resolve_host", rebinding)
    with pytest.raises(serve.ProxyError) as ei:
        serve.fetch_via_proxy(f"http://rebind.test:{upstream}/img.png", allow_private=False)
    assert ei.value.status == 403
    assert calls == ["rebind.test", "rebind.test"]  # URL 校验一次 + 建连一次，没有第三次（建连不再自己解析）


def test_proxy_ignores_environment_proxies(upstream: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量里的上游代理会让目标由代理去解析、绕过校验：必须直连。"""
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")  # 连不上的代理：若被使用就会 502
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    status, body, _ = serve.fetch_via_proxy(f"http://127.0.0.1:{upstream}/img.png", allow_private=True)
    assert status == 200 and body.startswith(b"\x89PNG-fake:")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_parsing(tmp_path: Path) -> None:
    args = serve.build_arg_parser().parse_args(
        ["--port", "0", "--assets", str(tmp_path), "--allow-proxy", "--demo-size", "320x145", "--allow-host", "a.local"]
    )
    cfg = serve.config_from_args(args)
    assert cfg.allow_proxy is True and cfg.proxy_allow_private is False
    assert cfg.demo_size == (320, 145) and cfg.extra_hosts == ("a.local",)
    assert cfg.assets_dir == tmp_path.resolve()
    assert cfg.samples_dir is None  # 默认不提供 /samples/
    cfg2 = serve.config_from_args(serve.build_arg_parser().parse_args(["--samples", str(tmp_path)]))
    assert cfg2.samples_dir == tmp_path.resolve()
    with pytest.raises(SystemExit):
        serve.build_arg_parser().parse_args(["--demo-size", "abc"])
    with pytest.raises(SystemExit):
        serve.config_from_args(serve.build_arg_parser().parse_args(["--demo-frames", "0"]))
