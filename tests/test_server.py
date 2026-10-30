"""glasscast.server 的测试：Starlette TestClient 覆盖全部路由与错误分支。

不真截屏：画面来自 :class:`SyntheticSource`，需要"画面暂时停住"时用 :class:`GateSource`
（每次 grab 都要测试代码放行）。等待一律用 pipeline 的条件变量（``wait_for_*``）加超时，不靠 sleep 猜时间。
"""

from __future__ import annotations

import io
import json
import logging
import threading
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from PIL import Image
from starlette.testclient import TestClient

from glasscast.config import Settings, load_settings
from glasscast.frames import CANVAS, TIMER_FRAMES, TIMER_SIZE, StaticSource, SyntheticSource, build_timer_fset
from glasscast.fset import inspect_fset, parse_fset
from glasscast.pipeline import CastPipeline
from glasscast.server import (
    COMMON_HEADERS,
    McpGuardMiddleware,
    Placeholders,
    create_app,
    redact_path,
)

TIMEOUT = 10.0
PUBLIC = "https://cast.example.com"
TOKEN = "test-token-0123456789abcdef"
KEY = "TestKey_0123-abc"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}


class GateSource:
    """受控帧源：每次 grab 消耗一个许可，没有许可就阻塞（关闭后抛异常让采集线程退出）。"""

    def __init__(self, allow: int = 0) -> None:
        self._sem = threading.Semaphore(allow)
        self._closed = threading.Event()
        self._inner = SyntheticSource(CANVAS)
        self.entered = threading.Event()

    def grab(self) -> Image.Image:
        self.entered.set()
        while not self._sem.acquire(timeout=0.02):
            if self._closed.is_set():
                raise RuntimeError("GateSource 已关闭")
        return self._inner.grab()

    def release(self, n: int = 1) -> None:
        for _ in range(n):
            self._sem.release()

    def close(self) -> None:
        self._closed.set()


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"public_url": PUBLIC, "token": TOKEN, "cast_key": KEY}
    values.update(overrides)
    return load_settings(values, {})


@pytest.fixture
def make_client() -> Iterator[Callable[..., tuple[TestClient, CastPipeline]]]:
    """工厂：``make_client(source=..., settings=..., **pipeline_kwargs)`` → (已进入 lifespan 的 client, pipeline)。"""
    cleanups: list[Callable[[], None]] = []

    def factory(
        source: Any = None,
        *,
        settings: Settings | None = None,
        app_kwargs: dict[str, Any] | None = None,
        **pipeline_kwargs: Any,
    ) -> tuple[TestClient, CastPipeline]:
        src = source if source is not None else SyntheticSource(CANVAS)
        kwargs: dict[str, Any] = {"image_fps": 50.0, "fast_fps": 50.0, "clip_fps": 100.0, "clip_frames": 4}
        kwargs.update(pipeline_kwargs)
        pipeline = CastPipeline(src, **kwargs)
        app = create_app(settings or make_settings(), pipeline, **(app_kwargs or {}))
        client = TestClient(app, base_url=PUBLIC)
        client.__enter__()

        def cleanup() -> None:
            client.__exit__(None, None, None)
            closer = getattr(src, "close", None)
            if callable(closer):
                closer()
            pipeline.close()

        cleanups.append(cleanup)
        return client, pipeline

    yield factory
    for cleanup in reversed(cleanups):
        cleanup()


def jpeg_size(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as im:
        assert im.format == "JPEG"
        return im.size


def jpeg_mode(data: bytes) -> str:
    with Image.open(io.BytesIO(data)) as im:
        assert im.format == "JPEG"
        return im.mode


def assert_common_headers(headers: Any) -> None:
    for name, value in COMMON_HEADERS:
        assert headers.get(name) == value, name


# --------------------------------------------------------------------------- /healthz 与通用头


def test_healthz(make_client: Callable[..., tuple[TestClient, CastPipeline]]) -> None:
    client, _ = make_client()
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.text == "ok"
    assert r.headers["content-type"].startswith("text/plain")
    assert_common_headers(r.headers)
    assert "server" not in r.headers or "uvicorn" not in r.headers["server"].lower()


def test_head_healthz(make_client: Callable[..., tuple[TestClient, CastPipeline]]) -> None:
    client, _ = make_client()
    r = client.head("/healthz")
    assert r.status_code == 200
    assert_common_headers(r.headers)


def test_common_headers_on_errors(make_client: Callable[..., tuple[TestClient, CastPipeline]]) -> None:
    client, _ = make_client()
    for response in (
        client.get("/nope"),
        client.get(f"/s/{KEY}/nope"),
        client.post("/mcp", json=TOOLS_LIST, headers=MCP_HEADERS),
        client.post(f"/s/{KEY}/screen.jpg"),
    ):
        assert response.status_code in (401, 404, 405)
        assert_common_headers(response.headers)


def test_internal_error_keeps_common_headers(caplog: pytest.LogCaptureFixture) -> None:
    """处理函数抛异常时的 500 也要带通用头（ServerErrorMiddleware 在用户中间件外层，绕过 CommonHeadersMiddleware），
    且正文和日志都不泄露 key。"""
    pipeline = CastPipeline(SyntheticSource(CANVAS))

    def broken_stats() -> dict[str, Any]:
        raise RuntimeError("stats exploded")

    pipeline.stats = broken_stats  # type: ignore[method-assign]
    app = create_app(make_settings(), pipeline)
    try:
        with caplog.at_level(logging.ERROR, logger="glasscast.server"):
            with TestClient(app, base_url=PUBLIC, raise_server_exceptions=False) as client:
                r = client.get(f"/s/{KEY}/status.json")
                assert r.status_code == 500
                assert r.text == "Internal Server Error"
                assert_common_headers(r.headers)
                assert KEY not in r.text and "exploded" not in r.text
                assert client.get("/healthz").status_code == 200  # 一次 500 不影响后续请求
        assert any("/s/…/status.json" in rec.getMessage() for rec in caplog.records)
        assert all(KEY not in rec.getMessage() for rec in caplog.records)
    finally:
        pipeline.close()


# --------------------------------------------------------------------------- key 校验


@pytest.mark.parametrize("path", ["screen.jpg", "timer.fset", "clip.fset", "clip/3.fset", "status.json"])
@pytest.mark.parametrize("bad_key", ["wrong", KEY[:-1], KEY + "x", KEY.lower(), "%20"])
def test_wrong_key_is_404_without_leaking(
    make_client: Callable[..., tuple[TestClient, CastPipeline]], path: str, bad_key: str
) -> None:
    client, _ = make_client()
    r = client.get(f"/s/{bad_key}/{path}?t=1")
    assert r.status_code == 404
    assert r.text == "Not Found"
    assert KEY not in r.text and bad_key not in r.text
    assert "x-glasscast-seq" not in r.headers


def test_non_get_methods_rejected(make_client: Callable[..., tuple[TestClient, CastPipeline]]) -> None:
    client, _ = make_client()
    assert client.post(f"/s/{KEY}/screen.jpg").status_code == 405
    assert client.put(f"/s/{KEY}/clip.fset").status_code == 405
    assert client.delete(f"/s/{KEY}/status.json").status_code == 405


# --------------------------------------------------------------------------- screen.jpg


def test_screen_placeholder_before_start(make_client: Callable[..., tuple[TestClient, CastPipeline]]) -> None:
    client, pipeline = make_client()
    r = client.get(f"/s/{KEY}/screen.jpg")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-glasscast-seq"] == "0"
    assert r.headers["x-glasscast-placeholder"] == "not_started"
    assert r.headers["x-glasscast-mode"] == "off"
    assert r.headers["etag"] == '"0-not_started"'
    assert r.headers["x-glasscast-age"] == "0"
    assert jpeg_size(r.content) == CANVAS
    assert jpeg_mode(r.content) == "L"  # 占位也是灰度（research.md §9.2 E6）
    assert not pipeline.running  # 取占位不会启动采集


def test_screen_serves_latest_snapshot(make_client: Callable[..., tuple[TestClient, CastPipeline]]) -> None:
    # image_fps 很低：第一帧立即截，之后 1 秒内不会有新帧，便于逐字节比较
    client, pipeline = make_client(StaticSource(Image.new("RGB", (1280, 800), (200, 30, 30))), image_fps=0.2)
    pipeline.start("image")
    snap = pipeline.wait_for_snapshot(0, TIMEOUT)
    assert snap is not None
    r = client.get(f"/s/{KEY}/screen.jpg?t=3599")
    assert r.status_code == 200
    assert r.content == snap.data
    assert r.headers["x-glasscast-seq"] == str(snap.seq)
    assert r.headers["etag"] == f'"{snap.seq}"'
    assert r.headers["x-glasscast-mode"] == "image"
    assert "x-glasscast-placeholder" not in r.headers
    assert 0.0 <= float(r.headers["x-glasscast-age"]) < TIMEOUT
    assert jpeg_size(r.content) == CANVAS
    assert jpeg_mode(r.content) == "L"
    assert_common_headers(r.headers)


def test_screen_ignores_query_string(make_client: Callable[..., tuple[TestClient, CastPipeline]]) -> None:
    client, pipeline = make_client(image_fps=0.2)
    pipeline.start("image")
    assert pipeline.wait_for_snapshot(0, TIMEOUT) is not None
    a = client.get(f"/s/{KEY}/screen.jpg?t=1")
    b = client.get(f"/s/{KEY}/screen.jpg?t=2&foo=bar&key=x")
    c = client.get(f"/s/{KEY}/screen.jpg")
    assert a.content == b.content == c.content
    assert a.headers["x-glasscast-seq"] == b.headers["x-glasscast-seq"] == c.headers["x-glasscast-seq"]


def test_screen_updates_with_new_frames(make_client: Callable[..., tuple[TestClient, CastPipeline]]) -> None:
    client, pipeline = make_client()
    pipeline.start("image")
    first = pipeline.wait_for_snapshot(0, TIMEOUT)
    assert first is not None
    later = pipeline.wait_for_snapshot(first.seq, TIMEOUT)
    assert later is not None
    r = client.get(f"/s/{KEY}/screen.jpg")
    assert int(r.headers["x-glasscast-seq"]) >= later.seq > first.seq


def test_screen_waiting_placeholder_until_first_frame(
    make_client: Callable[..., tuple[TestClient, CastPipeline]],
) -> None:
    source = GateSource()
    client, pipeline = make_client(source)
    pipeline.start("image")
    assert source.entered.wait(TIMEOUT)  # 采集线程已经卡在第一帧
    r = client.get(f"/s/{KEY}/screen.jpg")
    assert r.headers["x-glasscast-placeholder"] == "waiting"
    assert r.headers["x-glasscast-seq"] == "0"
    assert r.content == pipeline.placeholder_snapshot().data
    source.release()
    snap = pipeline.wait_for_snapshot(0, TIMEOUT)
    assert snap is not None
    r = client.get(f"/s/{KEY}/screen.jpg")
    assert r.headers["x-glasscast-seq"] == str(snap.seq)
    assert "x-glasscast-placeholder" not in r.headers


def test_screen_in_clip_mode_is_placeholder(make_client: Callable[..., tuple[TestClient, CastPipeline]]) -> None:
    client, pipeline = make_client()
    pipeline.start("image")
    assert pipeline.wait_for_snapshot(0, TIMEOUT) is not None
    pipeline.start("clip")
    r = client.get(f"/s/{KEY}/screen.jpg")
    # 不把上一轮 image 模式残留的旧截图当成"现在的屏幕"
    assert r.headers["x-glasscast-placeholder"] == "mode_clip"
    assert r.headers["x-glasscast-mode"] == "clip"
    assert jpeg_size(r.content) == CANVAS


@pytest.mark.parametrize("clear", [False, True])
def test_screen_after_stop_is_stopped_placeholder(
    make_client: Callable[..., tuple[TestClient, CastPipeline]], clear: bool
) -> None:
    client, pipeline = make_client()
    pipeline.start("image")
    snap = pipeline.wait_for_snapshot(0, TIMEOUT)
    assert snap is not None
    pipeline.stop(clear=clear)
    r = client.get(f"/s/{KEY}/screen.jpg")
    assert r.status_code == 200
    assert r.headers["x-glasscast-placeholder"] == "stopped"
    assert r.headers["x-glasscast-seq"] == "0"
    assert r.content != snap.data
    assert jpeg_size(r.content) == CANVAS


# --------------------------------------------------------------------------- clip.fset


def assert_valid_clip(data: bytes, frames: int, size: tuple[int, int] = CANVAS) -> None:
    info = inspect_fset(data)
    assert info["frames"] == frames
    assert info["formats"] == ["png"] * frames
    assert info["png_dims"] == [size] * frames


def test_clip_placeholder_before_start(make_client: Callable[..., tuple[TestClient, CastPipeline]]) -> None:
    client, pipeline = make_client(clip_frames=10, clip_fps=5.0)
    r = client.get(f"/s/{KEY}/clip.fset")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-glasscast-frames"] == "10"
    assert r.headers["x-glasscast-frame-duration-ms"] == "200"
    assert r.headers["x-glasscast-placeholder"] == "not_started"
    assert_valid_clip(r.content, 10)


def test_clip_waiting_placeholder_has_full_frame_count(
    make_client: Callable[..., tuple[TestClient, CastPipeline]],
) -> None:
    source = GateSource()
    client, pipeline = make_client(source, clip_frames=7)
    pipeline.start("clip")
    assert source.entered.wait(TIMEOUT)
    r = client.get(f"/s/{KEY}/clip.fset")
    assert r.headers["x-glasscast-placeholder"] == "waiting"
    assert r.headers["x-glasscast-frames"] == "7"
    # 页面 total-frames 恒成立：占位段也补齐到 clip_frames 帧（实现指南 §4.2）
    assert_valid_clip(r.content, 7)
    assert r.content == pipeline.placeholder_clip().data


def test_clip_serves_latest_segment(make_client: Callable[..., tuple[TestClient, CastPipeline]]) -> None:
    source = GateSource(allow=4)  # 正好够攒一段，之后采集卡住，latest_clip 不再变化
    client, pipeline = make_client(source, clip_frames=4, clip_fps=100.0)
    pipeline.start("clip")
    clip = pipeline.wait_for_clip(0, TIMEOUT)
    assert clip is not None
    for path in ("clip.fset", "clip.fset?i=0", "clip/1.fset", "clip/12345.fset?i=12345", "clip/abc.fset"):
        r = client.get(f"/s/{KEY}/{path}")
        assert r.status_code == 200, path
        assert r.content == clip.data, path
        assert r.headers["x-glasscast-seq"] == str(clip.seq)
        assert r.headers["etag"] == f'"{clip.seq}"'
        assert r.headers["x-glasscast-frames"] == "4"
        assert r.headers["x-glasscast-frame-duration-ms"] == str(clip.frame_duration_ms) == "10"
        assert r.headers["x-glasscast-frame-format"] == "png"
        assert "x-glasscast-placeholder" not in r.headers
    assert_valid_clip(clip.data, 4)
    assert len(parse_fset(clip.data)) == 4


def test_clip_in_image_mode_is_placeholder(make_client: Callable[..., tuple[TestClient, CastPipeline]]) -> None:
    client, pipeline = make_client(clip_frames=5)
    pipeline.start("image")
    r = client.get(f"/s/{KEY}/clip/9.fset")
    assert r.headers["x-glasscast-placeholder"] == "mode_image"
    assert_valid_clip(r.content, 5)


def test_clip_after_stop_is_stopped_placeholder(make_client: Callable[..., tuple[TestClient, CastPipeline]]) -> None:
    client, pipeline = make_client(clip_frames=3)
    pipeline.start("clip")
    clip = pipeline.wait_for_clip(0, TIMEOUT)
    assert clip is not None
    pipeline.stop()  # 不清空也不再循环播放最后一段（那看起来像实时画面）
    r = client.get(f"/s/{KEY}/clip.fset")
    assert r.headers["x-glasscast-placeholder"] == "stopped"
    assert r.content != clip.data
    assert_valid_clip(r.content, 3)


def test_clip_jpeg_format_placeholders(make_client: Callable[..., tuple[TestClient, CastPipeline]]) -> None:
    client, pipeline = make_client(clip_format="jpeg", clip_frames=3)
    r = client.get(f"/s/{KEY}/clip.fset")
    info = inspect_fset(r.content)
    assert info["frames"] == 3
    assert info["formats"] == ["jpeg"] * 3
    assert r.headers["x-glasscast-frame-format"] == "jpeg"


# --------------------------------------------------------------------------- status.json


def test_status_json(make_client: Callable[..., tuple[TestClient, CastPipeline]]) -> None:
    settings = make_settings()
    client, pipeline = make_client(settings=settings)
    pipeline.start("image")
    assert pipeline.wait_for_snapshot(0, TIMEOUT) is not None
    r = client.get(f"/s/{KEY}/status.json?x=1")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert_common_headers(r.headers)
    body = r.json()
    for key in pipeline.stats():
        assert key in body, key
    assert body["mode"] == "image"
    assert body["urls"] == settings.urls()
    assert body["urls"]["image"] == f"{PUBLIC}/s/{KEY}/screen.jpg"
    assert body["urls"]["timer"] == f"{PUBLIC}/s/{KEY}/timer.fset"
    assert "clip" not in body["urls"]  # clip 分段流已下线，不再对外列出
    assert body["page"] == {
        "width": 640,
        "height": 290,
        "image": {"template": "cast-image", "frame_duration_ms": 1000},
        # 流畅模式页面 cast-emit：hops 与 start_cast 出参一致。fixture 的 50 fps → round(1000/50/20) = 1 跳
        "fast": {"template": "cast-emit", "hops": 1, "emit_hop_ms": 20.0},
    }
    assert body["source"] == "screen"
    assert body["fast_fps"] == pipeline.fast_fps == 50.0  # 运行时当前值（fixture 的高帧率）
    assert body["config"]["fast_fps"] == 4.0  # config 是启动参数（初始值）
    assert body["config"]["canvas"] == [640, 290]
    assert body["config"]["color_mode"] == "gray"
    assert TOKEN not in r.text  # token 绝不出现在状态页


# --------------------------------------------------------------------------- /mcp 鉴权


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": ""},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer wrong-token"},
        {"Authorization": f"Bearer {TOKEN}x"},
        {"Authorization": f"Basic {TOKEN}"},
        {"Authorization": f"Bearer {TOKEN[:-1]}"},
    ],
)
def test_mcp_rejects_missing_or_wrong_bearer(
    make_client: Callable[..., tuple[TestClient, CastPipeline]], headers: dict[str, str]
) -> None:
    client, _ = make_client()
    r = client.post("/mcp", json=TOOLS_LIST, headers={**MCP_HEADERS, **headers})
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Bearer")
    assert r.json()["error"] == "unauthorized"
    assert TOKEN not in r.text
    assert_common_headers(r.headers)


@pytest.mark.parametrize(
    "authorization",
    [f"Bearer {TOKEN}", f"bearer {TOKEN}", f"BEARER   {TOKEN}  ", f"Bearer Bearer {TOKEN}", TOKEN],
)
def test_mcp_accepts_valid_bearer(
    make_client: Callable[..., tuple[TestClient, CastPipeline]], authorization: str
) -> None:
    client, _ = make_client()
    r = client.post("/mcp", json=TOOLS_LIST, headers={**MCP_HEADERS, "Authorization": authorization})
    assert r.status_code == 200
    names = {tool["name"] for tool in r.json()["result"]["tools"]}
    assert names == {"start_cast", "stop_cast", "cast_status"}


def test_mcp_subpaths_also_require_auth(make_client: Callable[..., tuple[TestClient, CastPipeline]]) -> None:
    client, _ = make_client()
    assert client.post("/mcp/", json=TOOLS_LIST, headers=MCP_HEADERS).status_code == 401
    assert client.get("/mcp/anything").status_code == 401


def test_mcp_get_is_405_in_stateless_mode(make_client: Callable[..., tuple[TestClient, CastPipeline]]) -> None:
    client, _ = make_client()
    assert client.get("/mcp", headers={"Accept": "text/event-stream"}).status_code == 401  # 先鉴权
    r = client.get("/mcp", headers={**AUTH, "Accept": "text/event-stream"})
    assert r.status_code == 405
    assert r.headers["allow"] == "POST"


def test_mcp_tool_call_without_initialize(make_client: Callable[..., tuple[TestClient, CastPipeline]]) -> None:
    """stateless：平台选"无状态 HTTP"时可能直接 tools/call，不先 initialize。"""
    client, pipeline = make_client()
    call = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "start_cast", "arguments": {}}}
    r = client.post("/mcp", json=call, headers={**MCP_HEADERS, **AUTH})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    result = r.json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["success"] is True
    assert result["structuredContent"]["url"] == f"{PUBLIC}/s/{KEY}/screen.jpg"
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    assert pipeline.mode == "image"
    assert "mcp-session-id" not in r.headers  # stateless：不发会话 ID


def test_mcp_rejects_unknown_host(make_client: Callable[..., tuple[TestClient, CastPipeline]]) -> None:
    client, _ = make_client()
    r = client.post("http://evil.example/mcp", json=TOOLS_LIST, headers={**MCP_HEADERS, **AUTH})
    assert r.status_code == 421  # DNS 重绑定保护（实现指南 §4.3）


@pytest.mark.parametrize(
    "base", ["https://cast.example.com", "http://127.0.0.1:18766", "http://localhost:8766", "http://127.0.0.1"]
)
def test_mcp_allows_public_and_loopback_hosts(
    make_client: Callable[..., tuple[TestClient, CastPipeline]], base: str
) -> None:
    client, _ = make_client()
    r = client.post(f"{base}/mcp", json=TOOLS_LIST, headers={**MCP_HEADERS, **AUTH})
    assert r.status_code == 200


def test_mcp_allowed_hosts_and_disable_protection(
    make_client: Callable[..., tuple[TestClient, CastPipeline]],
) -> None:
    client, _ = make_client(settings=make_settings(allowed_hosts=("extra.example",)))
    assert client.post("http://extra.example/mcp", json=TOOLS_LIST, headers={**MCP_HEADERS, **AUTH}).status_code == 200
    assert client.post("http://evil.example/mcp", json=TOOLS_LIST, headers={**MCP_HEADERS, **AUTH}).status_code == 421

    client2, _ = make_client(settings=make_settings(dns_rebinding_protection=False))
    r = client2.post("http://evil.example/mcp", json=TOOLS_LIST, headers={**MCP_HEADERS, **AUTH})
    assert r.status_code == 200


def test_mcp_public_url_with_port_and_path(make_client: Callable[..., tuple[TestClient, CastPipeline]]) -> None:
    settings = make_settings(public_url="https://app.example.com:8443/cast")
    client, _ = make_client(settings=settings)
    r = client.post(
        "https://app.example.com:8443/mcp", json=TOOLS_LIST, headers={**MCP_HEADERS, **AUTH}
    )
    assert r.status_code == 200
    assert settings.image_url == f"https://app.example.com:8443/cast/s/{KEY}/screen.jpg"


# --------------------------------------------------------------------------- 访问日志


def test_access_log_redacts_key(
    make_client: Callable[..., tuple[TestClient, CastPipeline]], caplog: pytest.LogCaptureFixture
) -> None:
    client, _ = make_client()
    with caplog.at_level(logging.INFO, logger="glasscast.access"):
        client.get(f"/s/{KEY}/clip.fset?i=3")
        client.get("/s/some-wrong-key/screen.jpg")
        client.post("/mcp", json=TOOLS_LIST, headers={**MCP_HEADERS, **AUTH})
        client.get(f"/s/{KEY}/screen.jpg?ts=1729999999999&i=7")
        client.get(f"/s/{KEY}/timer.fset")
    messages = [r.getMessage() for r in caplog.records if r.name == "glasscast.access"]
    assert len(messages) == 5
    assert "GET /s/…/clip.fset?i=3 200" in messages[0]
    assert "/s/…/screen.jpg 404" in messages[1]
    assert "POST /mcp 200" in messages[2]
    assert "GET /s/…/screen.jpg?ts=1729999999999&i=7 200" in messages[3]  # fast 页面的防缓存参数原样保留
    assert "GET /s/…/timer.fset 200" in messages[4]
    joined = "\n".join(messages)
    assert KEY not in joined and "some-wrong-key" not in joined and TOKEN not in joined


def test_access_log_can_be_disabled(
    make_client: Callable[..., tuple[TestClient, CastPipeline]], caplog: pytest.LogCaptureFixture
) -> None:
    client, _ = make_client(app_kwargs={"access_log": False})
    with caplog.at_level(logging.INFO, logger="glasscast.access"):
        client.get("/healthz")
    assert not [r for r in caplog.records if r.name == "glasscast.access"]


def test_redact_path() -> None:
    assert redact_path(f"/s/{KEY}/screen.jpg") == "/s/…/screen.jpg"
    assert redact_path(f"/s/{KEY}/clip/3.fset") == "/s/…/clip/3.fset"
    assert redact_path("/s/") == "/s/"
    assert redact_path("/healthz") == "/healthz"
    assert redact_path("/mcp") == "/mcp"


def test_extract_token() -> None:
    extract = McpGuardMiddleware.extract_token
    assert extract(f"Bearer {TOKEN}") == (TOKEN, True)
    assert extract(f"bearer   {TOKEN} ") == (TOKEN, True)
    assert extract(f"Bearer Bearer {TOKEN}") == (TOKEN, False)
    assert extract(TOKEN) == (TOKEN, False)
    assert extract(None) == ("", True)
    assert extract("") == ("", True)
    assert extract("Basic abc") == ("Basic abc", False)


# --------------------------------------------------------------------------- create_app 本身


def test_create_app_requires_secrets() -> None:
    pipeline = CastPipeline(SyntheticSource())
    try:
        base = make_settings()
        for broken in (
            Settings(public_url=PUBLIC, token="", cast_key=KEY),
            Settings(public_url=PUBLIC, token=TOKEN, cast_key=""),
        ):
            with pytest.raises(ValueError):
                create_app(broken, pipeline)
        assert create_app(base, pipeline) is not None
    finally:
        pipeline.close()


def test_app_does_not_start_or_close_pipeline_by_default() -> None:
    pipeline = CastPipeline(SyntheticSource())
    try:
        app = create_app(make_settings(), pipeline)
        with TestClient(app, base_url=PUBLIC) as client:
            assert client.get("/healthz").status_code == 200
        assert pipeline.mode == "off"
        assert not pipeline.closed
    finally:
        pipeline.close()


def test_close_pipeline_on_shutdown() -> None:
    pipeline = CastPipeline(SyntheticSource(), image_fps=50.0)
    app = create_app(make_settings(), pipeline, close_pipeline_on_shutdown=True)
    with TestClient(app, base_url=PUBLIC):
        pipeline.start("image")
        assert pipeline.wait_for_snapshot(0, TIMEOUT) is not None
    assert pipeline.closed
    assert not pipeline.running


def test_each_app_gets_its_own_mcp_server() -> None:
    """SDK 的 session manager 每个实例只能 run 一次：同一个 pipeline 可以先后挂到两个新建的应用上。"""
    pipeline = CastPipeline(SyntheticSource())
    try:
        for _ in range(2):
            app = create_app(make_settings(), pipeline)
            with TestClient(app, base_url=PUBLIC) as client:
                r = client.post("/mcp", json=TOOLS_LIST, headers={**MCP_HEADERS, **AUTH})
                assert r.status_code == 200
    finally:
        pipeline.close()


# --------------------------------------------------------------------------- 占位图


@pytest.mark.parametrize(("color_mode", "png_mode"), [("gray", "L"), ("rgb", "RGBA")])
@pytest.mark.parametrize("clip_format", ["png", "jpeg"])
def test_placeholders_warm_and_cache(clip_format: str, color_mode: str, png_mode: str) -> None:
    pipeline = CastPipeline(
        SyntheticSource(), clip_frames=6, clip_format=clip_format, color_mode=color_mode  # type: ignore[arg-type]
    )
    try:
        placeholders = Placeholders(pipeline)
        placeholders.warm()
        reasons = ("waiting", "not_started", "stopped", "mode_image", "mode_fast", "mode_clip")
        snapshots = [placeholders.snapshot(r) for r in reasons]  # type: ignore[arg-type]
        assert len({s.data for s in snapshots}) == len(reasons)  # 每种文案不同
        for snap in snapshots:
            assert snap.seq == 0
            assert jpeg_size(snap.data) == CANVAS
            assert jpeg_mode(snap.data) == ("L" if color_mode == "gray" else "RGB")
        for reason in reasons:
            clip = placeholders.clip(reason)  # type: ignore[arg-type]
            assert clip is placeholders.clip(reason)  # type: ignore[arg-type]  # 有缓存
            info = inspect_fset(clip.data)
            assert info["frames"] == clip.frames == 6
            assert info["formats"] == [clip_format] * 6
            if clip_format == "png":
                assert info["png_dims"] == [CANVAS] * 6
                # 与 pipeline 的段帧编码一致：gray → 8 位灰度（research.md §9.2 E6），rgb → 官方样本的 RGBA（§4）
                with Image.open(io.BytesIO(parse_fset(clip.data)[0])) as frame:
                    assert frame.mode == png_mode
    finally:
        pipeline.close()


def test_placeholder_canvas_follows_pipeline() -> None:
    pipeline = CastPipeline(SyntheticSource((320, 145)), canvas=(320, 145), clip_frames=2)
    try:
        placeholders = Placeholders(pipeline)
        assert jpeg_size(placeholders.snapshot("stopped").data) == (320, 145)
        assert inspect_fset(placeholders.clip("stopped").data)["png_dims"] == [(320, 145)] * 2
    finally:
        pipeline.close()


@pytest.mark.parametrize("version", ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"])
def test_mcp_initialize_with_legacy_protocol_versions(
    make_client: Callable[..., tuple[TestClient, CastPipeline]], version: str
) -> None:
    """平台网关用的 MCP 协议版本未知：几个握手版本都要能 initialize，后续请求带版本头也能用。"""
    client, _ = make_client()
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "okhttp", "version": "4"}},
    }
    r = client.post("/mcp", json=initialize, headers={**MCP_HEADERS, **AUTH})
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["protocolVersion"] == version
    assert result["serverInfo"]["name"] == "glasscast"
    assert "tools" in result["capabilities"]

    notified = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={**MCP_HEADERS, **AUTH, "MCP-Protocol-Version": version},
    )
    assert notified.status_code == 202
    status = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "cast_status", "arguments": {}}}
    r = client.post("/mcp", json=status, headers={**MCP_HEADERS, **AUTH, "MCP-Protocol-Version": version})
    assert r.status_code == 200
    assert r.json()["result"]["structuredContent"]["success"] is True


def test_mcp_json_only_accept_header(make_client: Callable[..., tuple[TestClient, CastPipeline]]) -> None:
    """JSON 响应模式下只要求客户端接受 application/json（有的 Java 客户端只发这一种）。"""
    client, _ = make_client()
    r = client.post("/mcp", json=TOOLS_LIST, headers={**AUTH, "Accept": "application/json"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")


# --------------------------------------------------------------------------- timer.fset 与 fast 模式（实现指南 §9.1）


def test_timer_fset(make_client: Callable[..., tuple[TestClient, CastPipeline]]) -> None:
    client, pipeline = make_client(fast_fps=4.0)
    r = client.get(f"/s/{KEY}/timer.fset")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/octet-stream"
    assert_common_headers(r.headers)  # no-store / CORS / CORP（模拟器用 XHR 跨源下载，research.md §9.2 E8）
    assert r.headers["x-glasscast-frames"] == str(TIMER_FRAMES) == "2"
    assert r.headers["x-glasscast-frame-size"] == "8x8"
    assert r.headers["x-glasscast-frame-duration-ms"] == "125"  # round(1000 / 4 / 2)
    assert r.headers["x-glasscast-frame-format"] == "png"
    assert r.content == build_timer_fset()
    info = inspect_fset(r.content)
    assert info["frames"] == 2 and info["formats"] == ["png", "png"]
    assert info["png_dims"] == [TIMER_SIZE] * 2 == [(8, 8), (8, 8)]
    for blob in parse_fset(r.content):
        with Image.open(io.BytesIO(blob)) as im:
            assert im.mode == "L"
    assert not pipeline.running  # 取定时器不会启动采集
    assert client.head(f"/s/{KEY}/timer.fset").status_code == 200
    assert client.post(f"/s/{KEY}/timer.fset").status_code == 405


def test_timer_fset_is_identical_in_every_mode(make_client: Callable[..., tuple[TestClient, CastPipeline]]) -> None:
    client, pipeline = make_client()
    bodies = [client.get(f"/s/{KEY}/timer.fset").content]
    for mode in ("image", "fast", "clip"):
        pipeline.start(mode)  # type: ignore[arg-type]
        bodies.append(client.get(f"/s/{KEY}/timer.fset?i=3").content)
    pipeline.stop(clear=True)
    bodies.append(client.get(f"/s/{KEY}/timer.fset").content)
    assert len(set(bodies)) == 1


def test_screen_in_fast_mode_serves_latest_snapshot(
    make_client: Callable[..., tuple[TestClient, CastPipeline]],
) -> None:
    client, pipeline = make_client(StaticSource(Image.new("RGB", (1280, 800), (30, 200, 30))), fast_fps=0.2)
    pipeline.start("fast")
    snap = pipeline.wait_for_snapshot(0, TIMEOUT)
    assert snap is not None
    r = client.get(f"/s/{KEY}/screen.jpg?ts=1729999999999&i=0")
    assert r.status_code == 200
    assert r.content == snap.data
    assert r.headers["x-glasscast-mode"] == "fast"
    assert r.headers["x-glasscast-seq"] == str(snap.seq)
    assert "x-glasscast-placeholder" not in r.headers
    assert jpeg_size(r.content) == CANVAS and jpeg_mode(r.content) == "L"


def test_screen_waiting_placeholder_in_fast_mode(make_client: Callable[..., tuple[TestClient, CastPipeline]]) -> None:
    source = GateSource()
    client, pipeline = make_client(source)
    pipeline.start("fast")
    assert source.entered.wait(TIMEOUT)
    r = client.get(f"/s/{KEY}/screen.jpg")
    assert r.headers["x-glasscast-placeholder"] == "waiting"
    assert r.headers["x-glasscast-mode"] == "fast"
    source.release()
    snap = pipeline.wait_for_snapshot(0, TIMEOUT)
    assert snap is not None
    assert "x-glasscast-placeholder" not in client.get(f"/s/{KEY}/screen.jpg").headers


def test_screen_switch_image_to_fast_has_no_placeholder_gap(
    make_client: Callable[..., tuple[TestClient, CastPipeline]],
) -> None:
    source = GateSource(allow=1)
    client, pipeline = make_client(source)
    pipeline.start("image")
    snap = pipeline.wait_for_snapshot(0, TIMEOUT)
    assert snap is not None
    pipeline.start("fast")  # 新一帧还卡在 grab 里
    r = client.get(f"/s/{KEY}/screen.jpg")
    assert r.content == snap.data  # image ↔ fast 共用 snapshot，眼镜不会闪"等待画面"
    assert "x-glasscast-placeholder" not in r.headers
    assert r.headers["x-glasscast-mode"] == "fast"


def test_clip_in_fast_mode_is_placeholder(make_client: Callable[..., tuple[TestClient, CastPipeline]]) -> None:
    client, pipeline = make_client(clip_frames=3)
    pipeline.start("fast")
    r = client.get(f"/s/{KEY}/clip.fset")
    assert r.headers["x-glasscast-placeholder"] == "mode_fast"
    assert_valid_clip(r.content, 3)


QUERIES = [
    "",
    "?ts=1729999999999&i=0",
    "?ts=1729999999999&t=86399",
    "?ts=&i=&t=",
    "?t=86400",
    "?ts=1&amp;i=2",  # 页面误写实体时服务端收到的样子（research.md §2.2）
    "?i=abc&t=%zz&ts=%",  # 非法百分号编码
    "?ts=1&ts=2&i=-1&i=99999999999999999999",
    "?key=wrong&s=x&mode=clip",
    "?" + "x" * 4000,
]


@pytest.mark.parametrize("query", QUERIES)
def test_query_string_is_ignored_on_every_route(
    make_client: Callable[..., tuple[TestClient, CastPipeline]], query: str
) -> None:
    """页面在地址后追加 ts / i / t 防缓存（research.md §9.2 E7），服务端必须对任何 query 一视同仁。"""
    client, pipeline = make_client(StaticSource(SyntheticSource().grab()), image_fps=0.2, fast_fps=0.2)
    pipeline.start("fast")
    snap = pipeline.wait_for_snapshot(0, TIMEOUT)
    assert snap is not None

    screen = client.get(f"/s/{KEY}/screen.jpg{query}")
    assert screen.status_code == 200 and screen.content == snap.data
    assert screen.headers["x-glasscast-seq"] == str(snap.seq)
    timer = client.get(f"/s/{KEY}/timer.fset{query}")
    assert timer.status_code == 200 and timer.content == build_timer_fset()
    clip = client.get(f"/s/{KEY}/clip.fset{query}")
    assert clip.status_code == 200 and clip.headers["x-glasscast-placeholder"] == "mode_fast"
    status = client.get(f"/s/{KEY}/status.json{query}")
    assert status.status_code == 200 and status.json()["mode"] == "fast"
    for r in (screen, timer, clip, status):
        assert_common_headers(r.headers)
    assert client.get(f"/healthz{query}").text == "ok"
    # query 不能绕过 key 校验
    assert client.get(f"/s/wrong-key/timer.fset{query}").status_code == 404


# --------------------------------------------------------------------------- /admin（本机调帧率，不重启）

ADMIN_BAD_AUTH = [
    {},
    {"Authorization": ""},
    {"Authorization": "Bearer wrong-token"},
    {"Authorization": f"Basic {TOKEN}"},
    {"Authorization": f"Bearer {TOKEN}x"},
]


@pytest.mark.parametrize("headers", ADMIN_BAD_AUTH)
def test_admin_requires_bearer(make_client: Callable[..., tuple[TestClient, CastPipeline]], headers: dict[str, str]) -> None:
    """/admin 复用 /mcp 的 Bearer 校验：无 / 错 token 一律 401，且 401 先于参数校验（不泄露接口细节）。"""
    client, pipeline = make_client(fast_fps=4.0)
    responses = [
        client.get("/admin/config", headers=headers),
        client.post("/admin/fps", json={"fps": 6}, headers=headers),
        client.post("/admin/fps", content=b"not json", headers=headers),
        client.get("/admin/fps", headers=headers),
    ]
    for r in responses:
        assert r.status_code == 401
        assert r.headers["www-authenticate"].startswith("Bearer")
        assert r.json()["error"] == "unauthorized"
        assert TOKEN not in r.text
        assert_common_headers(r.headers)
    assert pipeline.fast_fps == 4.0


def test_admin_config(make_client: Callable[..., tuple[TestClient, CastPipeline]]) -> None:
    client, pipeline = make_client(fast_fps=4.0, image_fps=1.0)
    r = client.get("/admin/config?x=1", headers=AUTH)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert_common_headers(r.headers)
    assert r.json() == {
        "success": True,
        "mode": "off",
        "fast_fps": 4.0,
        "frame_duration_ms": 125,  # 流畅模式：round(1000 / 4 / 2)
        "image_fps": 1.0,
        "color_mode": "gray",
        "canvas": [640, 290],
        "fast_fps_range": [1.0, 10.0],
        "initial_fast_fps": 4.0,
        "hops": 12,  # cast-emit 页面跳数 round(1000/4/20)
        "emit_hop_ms": 20.0,
    }
    pipeline.start("fast")
    assert client.get("/admin/config", headers=AUTH).json()["mode"] == "fast"
    assert client.post("/admin/config", headers=AUTH).status_code == 405
    assert client.get("/admin/fps", headers=AUTH).status_code == 405


def test_admin_set_fps_takes_effect_without_restart(make_client: Callable[..., tuple[TestClient, CastPipeline]]) -> None:
    client, pipeline = make_client(fast_fps=4.0)
    pipeline.start("fast")
    r = client.post("/admin/fps", json={"fps": 2}, headers=AUTH)
    assert r.status_code == 200
    assert_common_headers(r.headers)
    body = r.json()
    assert body["success"] is True and body["mode"] == "fast"
    assert (body["fast_fps"], body["frame_duration_ms"]) == (2.0, 250)
    assert (body["previous_fast_fps"], body["previous_frame_duration_ms"]) == (4.0, 125)
    assert body["warnings"] == []
    assert (body["hops"], body["previous_hops"]) == (25, 12)  # round(1000/2/20)、round(1000/4/20)
    # 已打开的页面要等模型带新的 hops 再打开一次才按新节拍拉图
    assert "hops=25" in body["message"] and "新的 hops" in body["message"]
    assert (pipeline.fast_fps, pipeline.fast_frame_duration_ms, pipeline.mode) == (2.0, 250, "fast")

    # 立即反映到 config、status.json、timer.fset 的头和 MCP 出参上
    config = client.get("/admin/config", headers=AUTH).json()
    assert (config["fast_fps"], config["frame_duration_ms"], config["initial_fast_fps"]) == (2.0, 250, 4.0)
    status = client.get(f"/s/{KEY}/status.json").json()
    assert (status["fast_fps"], status["fast_frame_duration_ms"]) == (2.0, 250)
    assert status["page"]["fast"]["hops"] == 25
    assert config["hops"] == 25
    assert status["config"]["fast_fps"] == 4.0  # 启动参数不变
    assert client.get(f"/s/{KEY}/timer.fset").headers["x-glasscast-frame-duration-ms"] == "250"
    call = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "start_cast", "arguments": {"mode": "fast"}},
    }
    result = client.post("/mcp", json=call, headers={**MCP_HEADERS, **AUTH}).json()["result"]["structuredContent"]
    assert (result["frame_duration_ms"], result["fps"], result["fast_fps"]) == (250, 2.0, 2.0)
    status_call = {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "cast_status", "arguments": {}}}
    status_result = client.post("/mcp", json=status_call, headers={**MCP_HEADERS, **AUTH}).json()["result"]
    assert status_result["structuredContent"]["fast_fps"] == 2.0

    # 小数、数字字符串都行；大于 4 带提醒（真机实测稳定上限，research.md §9.20）
    r = client.post("/admin/fps", json={"fps": "2.5"}, headers=AUTH)
    assert r.status_code == 200 and r.json()["frame_duration_ms"] == 200
    r = client.post("/admin/fps", json={"fps": 4.5}, headers=AUTH)
    assert r.status_code == 200 and any("堆积" in w for w in r.json()["warnings"])

    pipeline.stop()
    r = client.post("/admin/fps", json={"fps": 3}, headers=AUTH)
    assert r.json()["mode"] == "off" and "下次切到流畅模式" in r.json()["message"]
    assert pipeline.mode == "off"  # 改帧率不会开始投屏


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ({"fps": 0.5}, "invalid_fps"),
        ({"fps": 11}, "invalid_fps"),
        ({"fps": 0}, "invalid_fps"),
        ({"fps": "abc"}, "invalid_fps"),
        ({"fps": None}, "invalid_fps"),
        ({"fps": True}, "invalid_fps"),
        ({"fps": [6]}, "invalid_fps"),
        ({}, "bad_request"),
        ({"rate": 6}, "bad_request"),
        ([6], "bad_request"),
        ("6", "bad_request"),
    ],
)
def test_admin_set_fps_rejects_bad_values(
    make_client: Callable[..., tuple[TestClient, CastPipeline]], body: object, error: str
) -> None:
    client, pipeline = make_client(fast_fps=4.0)
    r = client.post("/admin/fps", json=body, headers=AUTH)
    assert r.status_code == 400
    assert_common_headers(r.headers)
    data = r.json()
    assert data["success"] is False and data["error"] == error
    assert data["message"] and data["fast_fps"] == 4.0
    if error == "invalid_fps":
        assert "1～10" in data["message"]  # 中文说明范围
    assert pipeline.fast_fps == 4.0  # 不改、不钳制


def test_admin_set_fps_rejects_non_json_and_oversized(
    make_client: Callable[..., tuple[TestClient, CastPipeline]],
) -> None:
    client, pipeline = make_client(fast_fps=4.0)
    for content in (b"", b"fps=6", b"\xff\xfe", b"NaN", b"[" * 2000 + b"]" * 2000):  # 最后一个：深层嵌套的数组
        r = client.post("/admin/fps", content=content, headers=AUTH)
        assert r.status_code == 400, content
        assert "fps" in r.json()["message"]
    r = client.post("/admin/fps", content=b'{"fps": 6' + b" " * 5000 + b"}", headers=AUTH)
    assert r.status_code == 413
    assert_common_headers(r.headers)
    assert pipeline.fast_fps == 4.0


# --------------------------------------------------------------------------- screen.jpg/<stamp>（真机要求 .jpg 结尾）


def test_screen_jpg_stamp_suffix_serves_same_frame(make_client: Callable[..., tuple[TestClient, CastPipeline]]) -> None:
    """页面写 …/screen.jpg/<时间戳>_<序号>.jpg（research.md §9.6）：与 screen.jpg 同一张图、同样的头。"""
    client, _pipeline = make_client()
    base = client.get(f"/s/{KEY}/screen.jpg")
    r = client.get(f"/s/{KEY}/screen.jpg/1792829202066_85206.jpg")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.headers.get("cache-control") == "no-store"
    assert r.content == base.content
    assert r.headers.get("x-glasscast-seq") == base.headers.get("x-glasscast-seq")


def test_screen_gif_stamp_serves_gray_gif_of_same_frame(make_client: Callable[..., tuple[TestClient, CastPipeline]]) -> None:
    """…/screen.jpg/<stamp>.gif：同一帧的 4 级灰度 GIF（research.md §9.24）；.jpg 结尾的仍是 JPEG。"""
    client, pipeline = make_client(gif_levels=4, gif_delay_ms=120)
    pipeline.start("image")
    snap = pipeline.wait_for_snapshot(0, TIMEOUT)
    assert snap is not None and snap.gif is not None
    r = client.get(f"/s/{KEY}/screen.jpg/1025131622_7.gif")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/gif"
    assert r.headers.get("cache-control") == "no-store"
    assert r.headers["etag"].endswith('-gif"')
    with Image.open(io.BytesIO(r.content)) as im:
        assert (im.format, im.size, im.info.get("duration")) == ("GIF", CANVAS, 120)
        assert len(set(im.convert("L").get_flattened_data())) <= 4
    assert client.get(f"/s/{KEY}/screen.jpg/1025131622_7.jpg").headers["content-type"] == "image/jpeg"
    assert client.get("/s/wrongkey/screen.jpg/1.gif").status_code == 404


def test_screen_gif_is_404_when_gif_off(make_client: Callable[..., tuple[TestClient, CastPipeline]]) -> None:
    client, pipeline = make_client()
    pipeline.start("image")
    assert pipeline.wait_for_snapshot(0, TIMEOUT) is not None
    assert client.get(f"/s/{KEY}/screen.jpg/1_2.gif").status_code == 404
    assert client.get(f"/s/{KEY}/screen.jpg/1_2.jpg").status_code == 200


@pytest.mark.parametrize(
    ("file", "ctype", "magic"),
    [("q1_f16.fset", "application/octet-stream", b"\x00"), ("q1_fj20.jpg", "application/octet-stream", b"\x00"),
     ("q1_g8.gif", "image/gif", b"GIF89a"), ("q1_i4.jpg", "image/jpeg", b"\xff\xd8\xff")],
)
def test_probe_files(make_client: Callable[..., tuple[TestClient, CastPipeline]], file: str, ctype: str, magic: bytes) -> None:
    """真机探针（research.md §9.25）：…/screen.jpg/probe/<会话>_<名字>.<后缀>，内容固定、立即返回。"""
    client, _pipeline = make_client()
    r = client.get(f"/s/{KEY}/screen.jpg/probe/{file}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(ctype)
    assert r.headers.get("cache-control") == "no-store"
    if file.endswith((".fset", "_fj20.jpg")):
        assert inspect_fset(r.content)["frames"] == 20
    else:
        assert r.content.startswith(magic)
    assert client.get(f"/s/{KEY}/screen.jpg/probe/{file}").content == r.content  # 内容固定


@pytest.mark.parametrize("file", ["q1_f16.gif", "q1_fj20.fset", "q1_i4.gif", "q1_x4.jpg", "q1_f999.fset", "bad/x_f16.fset", "q_1_f16"])
def test_probe_files_reject_bad_names(make_client: Callable[..., tuple[TestClient, CastPipeline]], file: str) -> None:
    client, _pipeline = make_client()
    assert client.get(f"/s/{KEY}/screen.jpg/probe/{file}").status_code == 404
    assert client.get("/s/wrongkey/screen.jpg/probe/q1_f16.fset").status_code == 404


def test_screen_jpg_stamp_suffix_rejects_wrong_key(make_client: Callable[..., tuple[TestClient, CastPipeline]]) -> None:
    client, _pipeline = make_client()
    assert client.get("/s/wrongkey/screen.jpg/1.jpg").status_code == 404
    assert client.post(f"/s/{KEY}/screen.jpg/1.jpg").status_code == 405
