"""MCP 控制面测试。

- 端到端：mcp 2.x 的 streamable HTTP 客户端（``streamable_http_client`` + ``ClientSession``）经
  ``httpx2.ASGITransport`` 直接打到 :func:`glasscast.server.create_app` 的 ASGI 应用，走一遍
  initialize → tools/list → tools/call，与平台的调用路径一致（实现指南 §4.3、§8）。
  ASGITransport 不跑 lifespan，所以手动进入 ``app.router.lifespan_context``（里面启动 MCP session manager）。
- 单元：:class:`CastController` 的消息分支、模式归一化、异常兜底。
- 对外模式只有 image / fast（实现指南 §9.1）；旧的 clip 说法归一化到 fast。
- 不真截屏：画面来自 SyntheticSource / 受控的假帧源。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import Any, TypeVar

import httpx2
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from PIL import Image
from starlette.applications import Starlette

from glasscast import __version__
from glasscast.config import Settings, load_settings
from glasscast.frames import CANVAS, SyntheticSource
from glasscast.mcp_tools import (
    DEFAULT_FIRST_FRAME_TIMEOUT_S,
    MODE_LABELS,
    TOOL_NAMES,
    CastController,
    build_mcp_server,
    normalize_mode,
)
from glasscast.pipeline import CastPipeline
from glasscast.server import create_app

T = TypeVar("T")

TIMEOUT = 10.0
PUBLIC = "https://cast.example.com"
TOKEN = "mcp-test-token-0123456789"
KEY = "McpTestKey_01234"
#: 平台对工具统一 3000 ms 超时，glasscast 要求 1 s 内返回（实现指南 §4.3）
TOOL_BUDGET_S = 1.0


def run(coro_factory: Callable[[], Awaitable[T]], timeout: float = 30.0) -> T:
    """在新事件循环里跑一个协程，整体超时则失败（不依赖 pytest 异步插件）。"""

    async def wrapper() -> T:
        return await asyncio.wait_for(coro_factory(), timeout)

    return asyncio.run(wrapper())


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"public_url": PUBLIC, "token": TOKEN, "cast_key": KEY}
    values.update(overrides)
    return load_settings(values, {})


@contextlib.asynccontextmanager
async def mcp_session(app: Starlette, *, token: str = TOKEN, base: str = PUBLIC) -> AsyncIterator[ClientSession]:
    """带 Bearer 的 MCP 客户端会话（已 initialize 之前的状态）。"""
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app),
            base_url=base,
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT,
        ) as http:
            async with streamable_http_client(f"{base}/mcp", http_client=http) as streams:
                read_stream, write_stream = streams[0], streams[1]
                async with ClientSession(read_stream, write_stream) as session:
                    yield session


async def call(session: ClientSession, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """调用工具并检查：1 s 内返回、不是 isError、structuredContent 与文本 JSON 一致。"""
    t0 = time.monotonic()
    result = await session.call_tool(name, arguments or {})
    elapsed = time.monotonic() - t0
    assert elapsed < TOOL_BUDGET_S, f"{name} 用了 {elapsed:.3f}s"
    assert result.is_error is False
    structured = result.structured_content
    assert isinstance(structured, dict)
    # 平台读哪一份还没验证（ui_schema 条件读工具出参），两份必须一致
    assert json.loads(result.content[0].text) == structured  # type: ignore[union-attr]
    return structured


class GateSource:
    """第一次 grab 之前一直阻塞，直到 :meth:`release`（模拟很慢的第一帧）。"""

    def __init__(self) -> None:
        self._open = threading.Event()
        self._closed = threading.Event()
        self._inner = SyntheticSource(CANVAS)

    def grab(self) -> Image.Image:
        while not self._open.wait(0.02):
            if self._closed.is_set():
                raise RuntimeError("closed")
        return self._inner.grab()

    def release(self) -> None:
        self._open.set()

    def close(self) -> None:
        self._closed.set()


class FailingSource:
    """模拟 macOS 没给屏幕录制权限：每次截屏都抛异常。"""

    def __init__(self) -> None:
        self.grabs = 0

    def grab(self) -> Image.Image:
        self.grabs += 1
        raise PermissionError("screen capture not permitted")


@pytest.fixture
def make_app() -> Iterator[Callable[..., tuple[Starlette, CastPipeline, Settings]]]:
    pipelines: list[tuple[CastPipeline, Any]] = []

    def factory(
        source: Any = None,
        *,
        settings: Settings | None = None,
        first_frame_timeout_s: float = DEFAULT_FIRST_FRAME_TIMEOUT_S,
        **pipeline_kwargs: Any,
    ) -> tuple[Starlette, CastPipeline, Settings]:
        src = source if source is not None else SyntheticSource(CANVAS)
        kwargs: dict[str, Any] = {"image_fps": 20.0, "clip_fps": 50.0}
        kwargs.update(pipeline_kwargs)
        pipeline = CastPipeline(src, **kwargs)
        pipelines.append((pipeline, src))
        s = settings or make_settings()
        return create_app(s, pipeline, first_frame_timeout_s=first_frame_timeout_s), pipeline, s

    yield factory
    for pipeline, src in pipelines:
        closer = getattr(src, "close", None)
        if callable(closer):
            closer()
        pipeline.close()


# --------------------------------------------------------------------------- 端到端（SDK 客户端）


def test_initialize_and_list_tools(make_app: Callable[..., tuple[Starlette, CastPipeline, Settings]]) -> None:
    app, _, _ = make_app()

    async def scenario() -> None:
        async with mcp_session(app) as session:
            init = await session.initialize()
            assert init.server_info.name == "glasscast"
            assert init.server_info.version == __version__
            assert init.instructions and "投屏" in init.instructions

            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert set(tools) == set(TOOL_NAMES) == {"start_cast", "stop_cast", "cast_status"}
            for tool in tools.values():
                # 平台把描述交给大模型选工具（官方文档第 37 节 §5.4）：中文、写清触发场景、没有缩进残留
                assert tool.description and "投屏" in tool.description
                assert "何时调用" in tool.description
                assert "\n    " not in tool.description
                assert tool.title

            schema = tools["start_cast"].input_schema
            mode = schema["properties"]["mode"]
            assert mode["enum"] == ["image", "fast"]  # clip 不再对外（实现指南 §9.1）
            assert mode["default"] == "image"
            assert mode["type"] == "string"
            assert "description" in mode
            assert schema.get("required", []) == []
            # fps：number 且可为 null，描述里写范围；不写 minimum/maximum，超范围由工具回 success=false
            fps = schema["properties"]["fps"]
            assert fps["type"] == "number" and fps["nullable"] is True
            assert "1～10" in fps["description"] and "mode=fast" in fps["description"]
            assert "minimum" not in fps and "maximum" not in fps
            assert tools["stop_cast"].input_schema["properties"] == {}
            assert tools["cast_status"].input_schema["properties"] == {}
            assert tools["cast_status"].annotations is not None
            assert tools["cast_status"].annotations.read_only_hint is True
            for tool in tools.values():
                assert tool.output_schema is not None and tool.output_schema["type"] == "object"
                # TypedDict 返回类型 → 逐字段 outputSchema；success / message 两种结果都有
                assert set(tool.output_schema["required"]) == {"success", "message"}
                assert tool.output_schema["properties"]["success"]["type"] == "boolean"
            start_props = tools["start_cast"].output_schema["properties"]  # type: ignore[index]
            for field in ("mode", "url", "timer_url", "width", "height", "frame_duration_ms", "error"):
                assert field in start_props, field
            assert start_props["frame_duration_ms"]["type"] == "integer"
            assert start_props["fps"]["type"] == "number" and start_props["fast_fps"]["type"] == "number"
            assert start_props["device_fps"]["type"] == "number"
            assert "clip_url" not in start_props and "clip_frames" not in start_props
            assert "previous_mode" in tools["stop_cast"].output_schema["properties"]  # type: ignore[index]
            status_props = tools["cast_status"].output_schema["properties"]  # type: ignore[index]
            for field in ("active", "mode", "last_frame_age_s", "seq", "consecutive_errors", "frame_duration_ms"):
                assert field in status_props, field
            assert status_props["fast_fps"]["type"] == "number"
            assert status_props["device_fps"]["type"] == "number"
            assert status_props["fast_frame_duration_ms"]["type"] == "integer"
            assert "fast" in tools["start_cast"].description and "流畅" in tools["start_cast"].description
            # 描述交给大模型选工具（官方文档第 37 节 §5.4）：fps 的含义、范围、"换档后带新 hops 再打开一次页面"都要写
            start_description = tools["start_cast"].description
            assert "fps" in start_description and "1～10" in start_description
            assert "再打开一次" in start_description and "快一点" in start_description
            assert "cast-emit" in start_description and "自动打开" not in start_description
            assert "fast_fps" in tools["cast_status"].description
            assert "device_fps" in tools["cast_status"].description and "device_fps" in start_description
            # 档位不是帧数，要说清楚；用户说的"N 帧""N 档"都要能触发调档
            assert "档位不是帧数" in start_description and "档位 4 真机约每秒 1.5 帧" in start_description
            assert "投屏调到 N 档" in start_description and "投屏调到 N 帧" in start_description
            assert "档位不是帧数" in fps["description"] and "档位 4 真机约每秒 1.5 帧" in fps["description"]
            assert "投屏调到 N 档" in init.instructions and "投屏调到 N 帧" in init.instructions

    run(scenario)


def test_full_cast_flow(make_app: Callable[..., tuple[Starlette, CastPipeline, Settings]]) -> None:
    app, pipeline, settings = make_app(clip_frames=4)

    async def scenario() -> None:
        async with mcp_session(app) as session:
            await session.initialize()

            status = await call(session, "cast_status")
            assert status["success"] is True
            assert status["active"] is False
            assert status["mode"] == "off"
            assert status["message"] == "现在没有在投屏"

            started = await call(session, "start_cast", {"mode": "image"})
            assert started == {
                "success": True,
                "mode": "image",
                "url": f"{PUBLIC}/s/{KEY}/screen.jpg",
                "timer_url": f"{PUBLIC}/s/{KEY}/timer.fset",
                "width": 640,
                "height": 290,
                "frame_duration_ms": 1000,  # image：页面 <tick> 每秒一次
                "fps": 4.0,  # 流畅模式的当前档位（image 模式也带上，fps 参数被忽略）
                "fast_fps": 4.0,
                "device_fps": 1.5,  # 4 档估算的真机帧数（image 模式也报流畅模式的值）
                "hops": 12,  # cast-emit 页面跳数 round(1000/4/20)（research.md §9.8）
                "page_fps": 4.2,  # 旧估算 1000/(12×20)
                "previous_mode": "off",
                "first_frame": True,
                "message": "投屏已开始，每秒刷新一次",
            }
            assert started["url"] == settings.image_url
            assert pipeline.mode == "image"
            # first_frame=True 意味着返回时页面第一次拉取就能拿到真实画面
            assert pipeline.latest_snapshot() is not None

            status = await call(session, "cast_status")
            assert status["active"] is True
            assert status["mode"] == "image"
            assert status["last_frame_age_s"] is not None and status["last_frame_age_s"] < 5
            assert status["seq"] >= 1
            assert status["consecutive_errors"] == 0
            assert status["message"] == "正在投屏，普通模式，画面刚刚更新"

            again = await call(session, "start_cast", {"mode": "image"})
            assert again["success"] is True
            assert again["message"] == "投屏已在进行中（普通模式）"

            switched = await call(session, "start_cast", {"mode": "fast"})
            assert switched["success"] is True
            assert switched["mode"] == "fast"
            assert switched["previous_mode"] == "image"
            assert switched["message"] == "已切换到流畅模式"
            assert switched["frame_duration_ms"] == 125  # round(1000 / 4 / 2)，默认 fast_fps=4
            assert switched["timer_url"] == settings.timer_url
            assert switched["first_frame"] is True  # image ↔ fast 共用 snapshot，切换后立即有画面
            assert pipeline.mode == "fast"

            status = await call(session, "cast_status")
            assert status["mode"] == "fast"
            assert status["message"] == "正在投屏，流畅模式，画面刚刚更新"
            assert status["frame_duration_ms"] == 125
            assert status["device_fps"] == 1.5

            back = await call(session, "start_cast", {"mode": "image"})
            assert back["message"] == "已切换到普通模式" and back["frame_duration_ms"] == 1000

            stopped = await call(session, "stop_cast")
            assert stopped == {"success": True, "previous_mode": "image", "message": "投屏已停止"}
            assert pipeline.mode == "off"
            assert pipeline.latest_snapshot() is None and pipeline.latest_clip() is None  # 停止即清空（隐私）

            stopped_again = await call(session, "stop_cast")
            assert stopped_again["success"] is True
            assert stopped_again["message"] == "投屏本来就没有开启"

            status = await call(session, "cast_status")
            assert status["active"] is False

    run(scenario)


@pytest.mark.parametrize(
    ("fast_fps", "frame_duration_ms", "hops", "page_fps", "device_fps"),
    # hops = round(1000/fast_fps/20)，限制在 1～25；page_fps = 1000/(hops×20) 是旧估算；device_fps 按 hops 估算真机帧数。
    # 2 档正好是 25 跳；更低的档位 hops 不再变，只降低电脑截帧频率
    [(4.0, 125, 12, 4.2, 1.5), (5.0, 100, 10, 5.0, 1.5), (2.0, 250, 25, 2.0, 1.0), (0.25, 1000, 25, 2.0, 1.0)],
)
def test_start_fast_from_off(
    make_app: Callable[..., tuple[Starlette, CastPipeline, Settings]],
    fast_fps: float,
    frame_duration_ms: int,
    hops: int,
    page_fps: float,
    device_fps: float,
) -> None:
    app, pipeline, settings = make_app(fast_fps=fast_fps)

    async def scenario() -> dict[str, Any]:
        async with mcp_session(app) as session:
            await session.initialize()
            return await call(session, "start_cast", {"mode": "fast"})

    started = run(scenario)
    assert started == {
        "success": True,
        "mode": "fast",
        "url": f"{PUBLIC}/s/{KEY}/screen.jpg",
        "timer_url": f"{PUBLIC}/s/{KEY}/timer.fset",
        "width": 640,
        "height": 290,
        # 页面 cast-fast 的 frame-duration 绑定它：2 帧一轮，round(1000/fast_fps/2)，上限 1000（实现指南 §9.1）
        "frame_duration_ms": frame_duration_ms,
        "fps": fast_fps,  # 本次实际生效的流畅模式档位
        "fast_fps": fast_fps,
        "device_fps": device_fps,
        "hops": hops,  # cast-emit 页面跳数（research.md §9.8）
        "page_fps": page_fps,
        "previous_mode": "off",
        "first_frame": True,
        "message": f"投屏已开始（流畅模式），{fast_fps:g} 档，每秒约 {device_fps:g} 帧",
    }
    assert started["timer_url"] == settings.timer_url
    assert pipeline.mode == "fast"


def test_start_cast_clip_request_becomes_fast(
    make_app: Callable[..., tuple[Starlette, CastPipeline, Settings]],
) -> None:
    """旧版 SKILL.md 可能还传 clip：clip 已下线，按"想要流畅"处理成 fast，mode 回传实际模式。"""
    app, pipeline, _ = make_app()

    async def scenario() -> dict[str, Any]:
        async with mcp_session(app) as session:
            await session.initialize()
            return await call(session, "start_cast", {"mode": "clip"})

    result = run(scenario)
    assert result["success"] is True and result["mode"] == "fast"
    assert pipeline.mode == "fast"
    assert pipeline.latest_clip() is None


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (None, "image"),
        ("视频", "fast"),
        ("Clip", "fast"),
        (" IMAGE ", "image"),
        ("流畅", "fast"),
        ("", "image"),
        ("FAST", "fast"),
        ("快速模式", "fast"),
    ],
)
def test_start_cast_mode_aliases(
    make_app: Callable[..., tuple[Starlette, CastPipeline, Settings]], given: str | None, expected: str
) -> None:
    app, pipeline, _ = make_app()

    async def scenario() -> dict[str, Any]:
        async with mcp_session(app) as session:
            await session.initialize()
            return await call(session, "start_cast", {} if given is None else {"mode": given})

    result = run(scenario)
    assert result["success"] is True
    assert result["mode"] == expected
    assert pipeline.mode == expected


def test_start_cast_accepts_null_mode(make_app: Callable[..., tuple[Starlette, CastPipeline, Settings]]) -> None:
    """大模型常给可选参数填 null：不能被 SDK 参数校验拦成 isError 纯文本，要按默认 image 处理。"""
    app, pipeline, _ = make_app()

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        async with mcp_session(app) as session:
            await session.initialize()
            # call() 里断言了 is_error 为 False、structuredContent 与文本 JSON 一致
            null_mode = await call(session, "start_cast", {"mode": None})
            number_mode = await call(session, "start_cast", {"mode": 1})
            return null_mode, number_mode

    null_mode, number_mode = run(scenario)
    assert null_mode["success"] is True
    assert null_mode["mode"] == "image"
    assert pipeline.mode == "image"
    # 非字符串：不是 isError，而是 success=false 的 JSON 对象，带可以直接播报的 message
    assert number_mode["success"] is False
    assert number_mode["message"] and "1" in number_mode["error"]
    assert pipeline.mode == "image"  # 无效调用不影响正在进行的投屏


def test_start_cast_rejects_unknown_mode(make_app: Callable[..., tuple[Starlette, CastPipeline, Settings]]) -> None:
    app, pipeline, _ = make_app()

    async def scenario() -> dict[str, Any]:
        async with mcp_session(app) as session:
            await session.initialize()
            return await call(session, "start_cast", {"mode": "4k-hdr"})

    result = run(scenario)
    assert result["success"] is False
    assert result["message"]  # 失败时眼镜会原样念 message：一句中文，不含技术细节
    assert "4k-hdr" not in result["message"]
    assert "4k-hdr" in result["error"]
    assert pipeline.mode == "off"


def test_start_cast_reports_capture_failure(
    make_app: Callable[..., tuple[Starlette, CastPipeline, Settings]],
) -> None:
    source = FailingSource()
    app, pipeline, _ = make_app(source, first_frame_timeout_s=0.8)

    async def scenario() -> dict[str, Any]:
        async with mcp_session(app) as session:
            await session.initialize()
            return await call(session, "start_cast", {"mode": "image"})

    result = run(scenario)
    assert result["success"] is False  # ui_schema 的 Hook 条件 success == true 不成立，不弹页面
    assert result["mode"] == "image"
    assert "屏幕录制" in result["message"]
    assert "PermissionError" in result["error"]
    assert pipeline.mode == "off"  # 第一帧就失败：关掉采集，不空转刷错误
    assert source.grabs >= 1


def test_start_cast_returns_before_slow_first_frame(
    make_app: Callable[..., tuple[Starlette, CastPipeline, Settings]],
) -> None:
    source = GateSource()
    app, pipeline, _ = make_app(source, first_frame_timeout_s=0.2)

    async def scenario() -> tuple[dict[str, Any], float]:
        async with mcp_session(app) as session:
            await session.initialize()
            t0 = time.monotonic()
            result = await call(session, "start_cast", {"mode": "image"})
            return result, time.monotonic() - t0

    result, elapsed = run(scenario)
    assert result["success"] is True
    assert result["first_frame"] is False  # 超时只说明第一帧慢，不算失败
    assert 0.15 <= elapsed < TOOL_BUDGET_S
    assert pipeline.mode == "image"
    source.release()
    assert pipeline.wait_for_snapshot(0, TIMEOUT) is not None


def test_start_cast_same_mode_reports_real_first_frame_state(
    make_app: Callable[..., tuple[Starlette, CastPipeline, Settings]],
) -> None:
    """同模式重复调用不等第一帧，但 first_frame 要如实反映"是否已经截到画面"。"""
    source = GateSource()
    app, pipeline, _ = make_app(source, first_frame_timeout_s=0.2)

    async def scenario() -> list[dict[str, Any]]:
        async with mcp_session(app) as session:
            await session.initialize()
            first = await call(session, "start_cast", {"mode": "image"})
            second = await call(session, "start_cast", {"mode": "image"})
            source.release()
            await anyio_wait(lambda: pipeline.latest_snapshot() is not None)
            third = await call(session, "start_cast", {"mode": "image"})
            return [first, second, third]

    first, second, third = run(scenario)
    assert first["first_frame"] is False
    assert second["message"] == "投屏已在进行中（普通模式）"
    assert second["first_frame"] is False  # 还没有任何画面，服务端仍在回"等待画面"占位
    assert third["first_frame"] is True


async def anyio_wait(predicate: Callable[[], bool], timeout: float = TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "等待条件超时"
        await asyncio.sleep(0.005)


class StaleFailureSource:
    """第 2 次 grab 先卡 ``hold_s`` 秒再抛一次 OSError，其余都正常：模拟切换模式时旧模式那一帧失败。"""

    def __init__(self, hold_s: float = 0.15) -> None:
        self.hold_s = hold_s
        self.grabs = 0
        self.second_entered = threading.Event()
        self._inner = SyntheticSource(CANVAS)
        self._lock = threading.Lock()

    def grab(self) -> Image.Image:
        with self._lock:
            self.grabs += 1
            n = self.grabs
        if n == 2:
            self.second_entered.set()
            time.sleep(self.hold_s)
            raise OSError("transient")
        return self._inner.grab()


def test_switch_ignores_failure_of_previous_mode_frame(
    make_app: Callable[..., tuple[Starlette, CastPipeline, Settings]],
) -> None:
    """切到 fast 时旧 image 帧（旧代号）失败：不能算到新模式头上、关掉整个投屏、还播报权限问题。"""
    source = StaleFailureSource()
    app, pipeline, _ = make_app(source, image_fps=20.0, error_backoff_s=5.0, error_backoff_max_s=5.0)

    async def scenario() -> dict[str, Any]:
        async with mcp_session(app) as session:
            await session.initialize()
            started = await call(session, "start_cast", {"mode": "image"})
            assert started["success"] is True
            await anyio_wait(source.second_entered.is_set)  # 旧 image 帧正在 grab 里，随后会失败
            return await call(session, "start_cast", {"mode": "fast"})

    switched = run(scenario)
    assert switched["success"] is True, switched
    assert switched["mode"] == "fast"
    assert switched["message"] == "已切换到流畅模式"
    assert switched["first_frame"] is True  # 没有被 5 s 退避耽误：旧帧失败不退避
    assert pipeline.mode == "fast"
    stats = pipeline.stats()
    assert stats["errors"] == 0 and stats["consecutive_errors"] == 0 and stats["last_error"] is None
    assert stats["discarded_frames"] >= 1


class FlakySource:
    """前 ``failures`` 次截屏失败，之后正常（模拟刚授予屏幕录制权限）。"""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.grabs = 0
        self._inner = SyntheticSource(CANVAS)
        self._lock = threading.Lock()

    def grab(self) -> Image.Image:
        with self._lock:
            self.grabs += 1
            failing = self.grabs <= self.failures
        if failing:
            raise PermissionError("screen capture not permitted")
        return self._inner.grab()


def wait_for_errors(pipeline: CastPipeline, count: int) -> None:
    deadline = time.monotonic() + TIMEOUT
    while pipeline.stats()["consecutive_errors"] < count:
        assert time.monotonic() < deadline, "等待采集失败超时"
        time.sleep(0.005)


def test_start_cast_retry_with_fps_reports_level_and_rate(
    make_app: Callable[..., tuple[Starlette, CastPipeline, Settings]],
) -> None:
    source = FlakySource(failures=1)
    app, pipeline, _ = make_app(source, error_backoff_s=5.0, error_backoff_max_s=5.0, first_frame_timeout_s=0.8)
    pipeline.start("fast")
    wait_for_errors(pipeline, 1)

    async def scenario() -> dict[str, Any]:
        async with mcp_session(app) as session:
            await session.initialize()
            return await call(session, "start_cast", {"mode": "fast", "fps": 2.5})

    result = run(scenario)
    assert result["success"] is True
    assert result["message"] == "投屏已重新开始（流畅模式），2.5 档，每秒约 1.3 帧"


def test_start_cast_same_mode_retries_after_errors(
    make_app: Callable[..., tuple[Starlette, CastPipeline, Settings]],
) -> None:
    """--autostart 时还没权限、之后授权：再说"开始投屏"要立即重试，而不是等退避或直接回"进行中"。"""
    source = FlakySource(failures=1)
    # 退避 5 s：不打断退避的话 0.6 s 内不可能出新帧
    app, pipeline, _ = make_app(source, error_backoff_s=5.0, error_backoff_max_s=5.0, first_frame_timeout_s=0.8)
    pipeline.start("image")
    wait_for_errors(pipeline, 1)

    async def scenario() -> dict[str, Any]:
        async with mcp_session(app) as session:
            await session.initialize()
            return await call(session, "start_cast", {"mode": "image"})

    result = run(scenario)
    assert result["success"] is True
    assert result["first_frame"] is True
    assert result["message"] == "投屏已重新开始（普通模式）"
    assert pipeline.latest_snapshot() is not None
    assert pipeline.stats()["consecutive_errors"] == 0


def test_start_cast_same_mode_still_failing(
    make_app: Callable[..., tuple[Starlette, CastPipeline, Settings]],
) -> None:
    source = FailingSource()
    app, pipeline, _ = make_app(source, error_backoff_s=5.0, error_backoff_max_s=5.0, first_frame_timeout_s=0.8)
    pipeline.start("fast")
    wait_for_errors(pipeline, 1)

    async def scenario() -> dict[str, Any]:
        async with mcp_session(app) as session:
            await session.initialize()
            return await call(session, "start_cast", {"mode": "fast"})

    result = run(scenario)
    assert result["success"] is False
    assert "屏幕录制" in result["message"]
    assert pipeline.mode == "off"
    assert source.grabs >= 2  # 确实重试了一次


def test_synthetic_source_failure_message(make_app: Callable[..., tuple[Starlette, CastPipeline, Settings]]) -> None:
    app, _, _ = make_app(FailingSource(), settings=make_settings(source="synthetic"), first_frame_timeout_s=0.8)

    async def scenario() -> dict[str, Any]:
        async with mcp_session(app) as session:
            await session.initialize()
            return await call(session, "start_cast", {"mode": "fast"})

    result = run(scenario)
    assert result["success"] is False
    assert "屏幕录制" not in result["message"]


def test_closed_pipeline_returns_failure(make_app: Callable[..., tuple[Starlette, CastPipeline, Settings]]) -> None:
    app, pipeline, _ = make_app()
    pipeline.close()

    async def scenario() -> dict[str, Any]:
        async with mcp_session(app) as session:
            await session.initialize()
            return await call(session, "start_cast", {"mode": "image"})

    result = run(scenario)
    assert result["success"] is False
    assert "RuntimeError" not in result["message"]


def test_unauthenticated_requests_get_401(make_app: Callable[..., tuple[Starlette, CastPipeline, Settings]]) -> None:
    app, pipeline, _ = make_app()
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
    }
    call_start = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "start_cast", "arguments": {}}}
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}

    async def scenario() -> list[int]:
        codes: list[int] = []
        async with app.router.lifespan_context(app):
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app), base_url=PUBLIC) as http:
                for body in (initialize, call_start):
                    codes.append((await http.post("/mcp", json=body, headers=headers)).status_code)
                    bad = {**headers, "Authorization": "Bearer nope"}
                    codes.append((await http.post("/mcp", json=body, headers=bad)).status_code)
                ok = {**headers, "Authorization": f"Bearer {TOKEN}"}
                codes.append((await http.post("/mcp", json=initialize, headers=ok)).status_code)
        return codes

    assert run(scenario) == [401, 401, 401, 401, 200]
    assert pipeline.mode == "off"  # 被拒的 tools/call 没有生效


def test_sdk_client_with_wrong_token_fails(make_app: Callable[..., tuple[Starlette, CastPipeline, Settings]]) -> None:
    app, _, _ = make_app()

    async def scenario() -> None:
        async with mcp_session(app, token="wrong") as session:
            await session.initialize()

    with pytest.raises(BaseException) as excinfo:  # SDK 把 401 包装成 MCPError，外面再套几层 ExceptionGroup
        run(scenario, timeout=15.0)
    assert not isinstance(excinfo.value, (asyncio.TimeoutError, TimeoutError))
    names = {type(exc).__name__ for exc in _flatten(excinfo.value)}
    assert "MCPError" in names, names


def _flatten(exc: BaseException) -> Iterator[BaseException]:
    yield exc
    for inner in getattr(exc, "exceptions", ()):
        yield from _flatten(inner)


def test_loopback_host_through_sdk(make_app: Callable[..., tuple[Starlette, CastPipeline, Settings]]) -> None:
    """本地 curl / 隧道未透传 Host 时（127.0.0.1:端口）也能用。"""
    app, _, _ = make_app()

    async def scenario() -> set[str]:
        async with mcp_session(app, base="http://127.0.0.1:8766") as session:
            await session.initialize()
            return {tool.name for tool in (await session.list_tools()).tools}

    assert run(scenario) == set(TOOL_NAMES)


# --------------------------------------------------------------------------- CastController 单元测试


class FakePipeline:
    """只提供 cast_status 用到的 stats()，用来覆盖各种消息分支。"""

    def __init__(self, **stats: Any) -> None:
        self._stats: dict[str, Any] = {
            "mode": "image",
            "closed": False,
            "last_frame_age_s": 0.4,
            "seq": 5,
            "frames_captured": 5,
            "capture_fps": 1.0,
            "consecutive_errors": 0,
            "errors": 0,
            "last_error": None,
            "clip_frames": 10,
            "frame_duration_ms": 200,
            "fast_fps": 4.0,
            "fast_frame_duration_ms": 125,
        }
        self._stats.update(stats)

    def stats(self) -> dict[str, Any]:
        return dict(self._stats)


@pytest.mark.parametrize(
    ("stats", "active", "message"),
    [
        ({"mode": "off"}, False, "现在没有在投屏"),
        ({"mode": "image", "closed": True}, False, "现在没有在投屏"),
        ({"mode": "image", "last_frame_age_s": None}, True, "投屏已开启，但电脑端还没有截到画面"),
        ({"mode": "image", "last_frame_age_s": 0.44}, True, "正在投屏，普通模式，画面刚刚更新"),
        ({"mode": "fast", "last_frame_age_s": 1.2}, True, "正在投屏，流畅模式，画面刚刚更新"),
        ({"mode": "clip", "last_frame_age_s": 1.2}, True, "正在投屏，实验分段模式，画面刚刚更新"),
        (
            {"mode": "fast", "last_frame_age_s": 12.6},
            True,
            "正在投屏，但画面已经 13 秒没更新了，请看一下电脑上的投屏程序",
        ),
        (
            {"mode": "image", "last_frame_age_s": None, "consecutive_errors": 3, "errors": 3},
            True,
            "投屏已开启，但电脑截屏已经连续失败 3 次，请看一下电脑上的投屏程序",
        ),
        (
            {"mode": "image", "last_frame_age_s": 30.0, "consecutive_errors": 7},
            True,
            "投屏已开启，但电脑截屏已经连续失败 7 次，请看一下电脑上的投屏程序",
        ),
    ],
)
def test_cast_status_messages(stats: dict[str, Any], active: bool, message: str) -> None:
    controller = CastController(make_settings(), FakePipeline(**stats))  # type: ignore[arg-type]
    result = controller.cast_status()
    assert result["success"] is True
    assert result["active"] is active
    assert result["message"] == message
    assert result["mode"] == stats["mode"]


class SwitchableSource:
    """合成画面；:attr:`open` 被清掉时 grab 阻塞（模拟某一轮第一帧很慢）。"""

    def __init__(self) -> None:
        self.open = threading.Event()
        self.open.set()
        self._closed = threading.Event()
        self._inner = SyntheticSource(CANVAS)

    def grab(self) -> Image.Image:
        while not self.open.wait(0.02):
            if self._closed.is_set():
                raise RuntimeError("closed")
        return self._inner.grab()

    def close(self) -> None:
        self._closed.set()


def test_cast_status_after_restart_reports_waiting_not_stale(
    make_app: Callable[..., tuple[Starlette, CastPipeline, Settings]],
) -> None:
    """停止后重新开始、第一帧还没出来：要说"还没有截到画面"，不能拿上一轮的截帧时间说"N 秒没更新"。"""
    source = SwitchableSource()
    _, pipeline, settings = make_app(source)
    controller = CastController(settings, pipeline, first_frame_timeout_s=0.2)
    try:
        first = run(lambda: controller.start_cast("image"))
        assert first["success"] is True and first["first_frame"] is True
        assert controller.stop_cast()["success"] is True
        source.open.clear()  # 新一轮的第一帧卡住
        second = run(lambda: controller.start_cast("image"))
        assert second["success"] is True and second["first_frame"] is False
        status = controller.cast_status()
        assert status["active"] is True
        assert status["last_frame_age_s"] is None
        assert status["consecutive_errors"] == 0
        assert status["message"] == "投屏已开启，但电脑端还没有截到画面"
    finally:
        source.open.set()


def test_tool_exceptions_become_failure_objects() -> None:
    class Broken(CastController):
        def cast_status(self) -> dict[str, Any]:
            raise RuntimeError("boom")

        def stop_cast(self) -> dict[str, Any]:
            raise ValueError("bad")

    pipeline = CastPipeline(SyntheticSource())
    try:
        settings = make_settings()
        server = build_mcp_server(settings, pipeline, controller=Broken(settings, pipeline))

        async def scenario() -> list[Any]:
            return [await server.call_tool("cast_status", {}), await server.call_tool("stop_cast", {})]

        for result in run(scenario):
            assert result.is_error is False  # 返回的是 JSON 对象，平台 Hook/Skill 可以按 success 判断
            assert result.structured_content["success"] is False
            assert result.structured_content["message"]
            assert "boom" in result.structured_content["error"] or "bad" in result.structured_content["error"]
    finally:
        pipeline.close()


def test_direct_tool_call_without_http() -> None:
    """不经 HTTP 直接调用 MCPServer（退化方案，实现指南 §4.4）。"""
    pipeline = CastPipeline(SyntheticSource(), image_fps=20.0)
    try:
        server = build_mcp_server(make_settings(), pipeline)

        async def scenario() -> Any:
            return await server.call_tool("start_cast", {"mode": "image"})

        result = run(scenario)
        assert result.structured_content["success"] is True
        assert pipeline.mode == "image"
    finally:
        pipeline.close()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("image", "image"),
        ("fast", "fast"),
        ("clip", "fast"),
        ("Video", "fast"),
        ("smooth", "fast"),
        ("普通模式", "image"),
        ("流畅模式", "fast"),
        ("视频模式", "fast"),
        ("高帧率", "fast"),
        (None, "image"),
        ("  ", "image"),
        ("4k", None),
        (3, None),
    ],
)
def test_normalize_mode(value: object, expected: str | None) -> None:
    assert normalize_mode(value) == expected


def test_controller_rejects_bad_timeouts() -> None:
    pipeline = CastPipeline(SyntheticSource())
    try:
        with pytest.raises(ValueError):
            CastController(make_settings(), pipeline, first_frame_timeout_s=-1)
        with pytest.raises(ValueError):
            CastController(make_settings(), pipeline, poll_interval_s=0)
    finally:
        pipeline.close()


def test_default_first_frame_timeout_fits_platform_budget() -> None:
    # 平台 3 s 超时，工具设计为 1 s 内返回（research.md §2.5、实现指南 §4.3）
    assert 0 < DEFAULT_FIRST_FRAME_TIMEOUT_S < TOOL_BUDGET_S


# --------------------------------------------------------------------------- 第二轮（fast 模式、TypedDict 返回体）


def test_mode_labels() -> None:
    assert MODE_LABELS["image"] == "普通模式"
    assert MODE_LABELS["fast"] == "流畅模式"
    assert MODE_LABELS["clip"] != MODE_LABELS["fast"]  # 内部 clip 不能和流畅模式混淆


@pytest.mark.parametrize(("mode", "expected"), [("fast", 125), ("image", 1000), ("off", 200), ("clip", 200)])
def test_cast_status_frame_duration_follows_mode(mode: str, expected: int) -> None:
    """第一版的字段都保留；frame_duration_ms 与 start_cast 出参同义（fast 定时器帧时长 / image 1000）。"""
    result = CastController(make_settings(), FakePipeline(mode=mode)).cast_status()  # type: ignore[arg-type]
    assert result["frame_duration_ms"] == expected
    assert set(result) == {
        "success",
        "active",
        "mode",
        "last_frame_age_s",
        "seq",
        "frames_captured",
        "capture_fps",
        "consecutive_errors",
        "errors",
        "last_error",
        "clip_frames",
        "frame_duration_ms",
        "fast_fps",
        "device_fps",
        "fast_frame_duration_ms",
        "message",
    }
    # 流畅模式的档位、真机帧数估算和帧时长与当前模式无关（"快一点 / 慢一点"据此加减）
    assert (result["fast_fps"], result["device_fps"], result["fast_frame_duration_ms"]) == (4.0, 1.5, 125)


def test_structured_content_matches_text_for_every_shape() -> None:
    """SDK 按 TypedDict 校验并只保留声明过的键：成功、失败、异常兜底三种形状都必须完整出现在 structuredContent。"""

    class Broken(CastController):
        async def start_cast(self, mode: object = "image", fps: object = None) -> Any:
            raise RuntimeError("kaboom")

    pipeline = CastPipeline(SyntheticSource(), image_fps=20.0, fast_fps=20.0)
    try:
        settings = make_settings()
        good = build_mcp_server(settings, pipeline)
        broken = build_mcp_server(settings, pipeline, controller=Broken(settings, pipeline))

        async def scenario() -> list[Any]:
            return [
                await good.call_tool("start_cast", {"mode": "fast"}),
                await good.call_tool("start_cast", {"mode": "fast"}),  # 同模式重复调用
                await good.call_tool("start_cast", {"mode": "bogus"}),
                await good.call_tool("cast_status", {}),
                await good.call_tool("stop_cast", {}),
                await broken.call_tool("start_cast", {"mode": "image"}),
            ]

        results = run(scenario)
        for result in results:
            assert result.is_error is False
            assert json.loads(result.content[0].text) == result.structured_content
        assert results[0].structured_content["frame_duration_ms"] == 25  # round(1000 / 20 / 2)
        assert results[1].structured_content["message"] == "投屏已在进行中（流畅模式）"
        assert results[2].structured_content["success"] is False
        assert results[5].structured_content["success"] is False
        assert "kaboom" in results[5].structured_content["error"]
    finally:
        pipeline.close()


def test_descriptions_follow_fast_fps() -> None:
    """工具描述交给大模型选工具（官方文档第 37 节 §5.4）：fast 的档位和真机帧数按实际 --fast-fps 写，不留占位符。"""
    pipeline = CastPipeline(SyntheticSource(), fast_fps=2.5)
    try:
        server = build_mcp_server(make_settings(), pipeline)

        async def scenario() -> list[Any]:
            return list(await server.list_tools())

        start = next(tool for tool in run(scenario) if tool.name == "start_cast")
        rate = "默认 2.5 档，每秒约 1.3 帧"
        assert rate in start.description
        mode = start.input_schema["properties"]["mode"]
        assert rate in mode["description"]
        assert rate in (server.instructions or "")
        for text in (start.description, mode["description"], server.instructions or ""):
            assert "{fast_fps}" not in text and "{device_fps}" not in text
    finally:
        pipeline.close()


# --------------------------------------------------------------------------- start_cast 的 fps 参数（语音调档位）


def test_start_cast_fps_sets_runtime_fast_fps(make_app: Callable[..., tuple[Starlette, CastPipeline, Settings]]) -> None:
    """fps 合法：先写入运行时 fast_fps 再开始；之后不带 fps 的调用、停止后重开都沿用新档位（运行时持久）。"""
    app, pipeline, settings = make_app()

    async def scenario() -> dict[str, dict[str, Any]]:
        async with mcp_session(app) as session:
            await session.initialize()
            out: dict[str, dict[str, Any]] = {}
            out["first"] = await call(session, "start_cast", {"mode": "fast", "fps": 6})
            out["status"] = await call(session, "cast_status")
            out["again"] = await call(session, "start_cast", {"mode": "fast"})
            out["null"] = await call(session, "start_cast", {"mode": "fast", "fps": None})
            out["retune"] = await call(session, "start_cast", {"mode": "fast", "fps": 2.5})
            out["text"] = await call(session, "start_cast", {"mode": "fast", "fps": "3"})
            out["stop"] = await call(session, "stop_cast")
            out["reopen"] = await call(session, "start_cast", {"mode": "fast"})
            out["to_image"] = await call(session, "start_cast", {"mode": "image"})
            out["back"] = await call(session, "start_cast", {"mode": "fast", "fps": 5})
            return out

    out = run(scenario)
    first = out["first"]
    assert first["success"] is True and first["mode"] == "fast"
    assert first["frame_duration_ms"] == 83  # round(1000 / 6 / 2)
    assert first["fps"] == first["fast_fps"] == 6.0
    assert first["message"] == "投屏已开始（流畅模式），6 档，每秒约 1.5 帧"
    assert first["timer_url"] == settings.timer_url and first["first_frame"] is True
    status = out["status"]
    assert (status["fast_fps"], status["fast_frame_duration_ms"], status["frame_duration_ms"]) == (6.0, 83, 83)
    # 不带 fps / fps=null：沿用运行时的 6，不回到启动值 4
    for name in ("again", "null"):
        assert out[name]["success"] is True and out[name]["frame_duration_ms"] == 83 and out[name]["fps"] == 6.0
    assert out["again"]["message"] == "投屏已在进行中（流畅模式）"
    # 已在流畅模式时换档：成功（模型随后带新的 hops 再打开一次 cast-emit），message 说新档位和真机帧数
    assert out["retune"]["frame_duration_ms"] == 200 and out["retune"]["fps"] == 2.5
    assert out["retune"]["message"] == "流畅模式已调到 2.5 档，每秒约 1.3 帧"
    # 每次出参的 hops 和 device_fps 都按调用之后生效的档位重算：round(1000/fps/20)
    assert [out[k]["hops"] for k in ("first", "again", "retune", "text", "back")] == [8, 8, 20, 17, 10]
    assert [out[k]["device_fps"] for k in ("first", "again", "retune", "text", "back")] == [1.5, 1.5, 1.3, 1.5, 1.5]
    assert status["device_fps"] == 1.5
    assert out["text"]["fps"] == 3.0 and out["text"]["frame_duration_ms"] == 167  # 数字字符串也接受
    assert out["stop"]["success"] is True
    assert out["reopen"]["fps"] == 3.0 and out["reopen"]["frame_duration_ms"] == 167  # 停止后重开仍是 3
    assert out["to_image"]["frame_duration_ms"] == 1000 and out["to_image"]["fps"] == 3.0
    assert out["back"]["message"] == "已切换到流畅模式，5 档，每秒约 1.5 帧" and out["back"]["frame_duration_ms"] == 100
    assert (pipeline.fast_fps, pipeline.fast_frame_duration_ms, pipeline.mode) == (5.0, 100, "fast")


def test_start_cast_hops_follow_emit_hop_ms(make_app: Callable[..., tuple[Starlette, CastPipeline, Settings]]) -> None:
    """GLASSCAST_EMIT_HOP_MS 改了每跳毫秒数：hops 和 page_fps 跟着变（真机校准用，research.md §9.8）。"""
    app, _, _ = make_app(settings=make_settings(emit_hop_ms=40))

    async def scenario() -> dict[str, Any]:
        async with mcp_session(app) as session:
            await session.initialize()
            return await call(session, "start_cast", {"mode": "fast", "fps": 4})

    result = run(scenario)
    assert (result["hops"], result["page_fps"]) == (6, 4.2)  # round(1000/4/40) = 6；1000/(6×40) ≈ 4.2


def test_device_fps_follows_hops_not_the_level(make_app: Callable[..., tuple[Starlette, CastPipeline, Settings]]) -> None:
    """device_fps 按实际下发的 hops 估算：改了 emit_hop_ms，同样的 4 档真机帧数也跟着变。"""
    app, _, _ = make_app(settings=make_settings(emit_hop_ms=10))

    async def scenario() -> list[dict[str, Any]]:
        async with mcp_session(app) as session:
            await session.initialize()
            return [await call(session, "start_cast", {"mode": "fast", "fps": 4}), await call(session, "cast_status")]

    started, status = run(scenario)
    assert (started["hops"], started["device_fps"], status["device_fps"]) == (25, 1.0, 1.0)  # round(1000/4/10) 截到 25
    assert started["message"] == "投屏已开始（流畅模式），4 档，每秒约 1 帧"


@pytest.mark.parametrize(
    ("fps", "frame_duration_ms", "device_fps", "rate"),
    [
        (1, 500, 1.0, "1 档，每秒约 1 帧"),
        (2, 250, 1.0, "2 档，每秒约 1 帧"),
        (2.5, 200, 1.3, "2.5 档，每秒约 1.3 帧"),
        (4, 125, 1.5, "4 档，每秒约 1.5 帧"),
        (6, 83, 1.5, "6 档，每秒约 1.5 帧"),
        (10, 50, 1.5, "10 档，每秒约 1.5 帧"),
    ],
)
def test_start_cast_fps_frame_duration_mapping(
    make_app: Callable[..., tuple[Starlette, CastPipeline, Settings]],
    fps: float,
    frame_duration_ms: int,
    device_fps: float,
    rate: str,
) -> None:
    """frame_duration_ms = round(1000/fps/2)，页面 cast-fast 的 frame-duration 绑定它（research.md §9.2 E9）。"""
    app, pipeline, _ = make_app()

    async def scenario() -> dict[str, Any]:
        async with mcp_session(app) as session:
            await session.initialize()
            return await call(session, "start_cast", {"mode": "fast", "fps": fps})

    result = run(scenario)
    assert result["success"] is True
    assert result["frame_duration_ms"] == frame_duration_ms == pipeline.fast_frame_duration_ms
    assert result["fps"] == result["fast_fps"] == float(fps)
    assert result["device_fps"] == device_fps
    assert result["message"] == f"投屏已开始（流畅模式），{rate}"


@pytest.mark.parametrize("bad", [0, 0.5, 11, 100, -2, "abc", True, [6]])
def test_start_cast_rejects_bad_fps(make_app: Callable[..., tuple[Starlette, CastPipeline, Settings]], bad: object) -> None:
    """fps 非法：success=false、中文 message 说明范围、error 带细节；不开始投屏、不改档位、不钳制。"""
    app, pipeline, _ = make_app()

    async def scenario() -> dict[str, Any]:
        async with mcp_session(app) as session:
            await session.initialize()
            return await call(session, "start_cast", {"mode": "fast", "fps": bad})

    result = run(scenario)
    assert result["success"] is False  # ui_schema 的 Hook 不命中，不重开页面
    assert result["mode"] == "fast"
    assert result["message"] == "档位只能调到 1 到 10，可以带小数，现在是 4 档"
    assert result["error"] and "1～10" in result["error"]
    assert result["fast_fps"] == result["fps"] == 4.0
    assert result["device_fps"] == 1.5
    assert pipeline.mode == "off"
    assert pipeline.fast_fps == 4.0


def test_start_cast_bad_fps_keeps_running_cast(
    make_app: Callable[..., tuple[Starlette, CastPipeline, Settings]],
) -> None:
    app, pipeline, _ = make_app()

    async def scenario() -> list[dict[str, Any]]:
        async with mcp_session(app) as session:
            await session.initialize()
            started = await call(session, "start_cast", {"mode": "fast"})
            rejected = await call(session, "start_cast", {"mode": "fast", "fps": 12})
            return [started, rejected]

    started, rejected = run(scenario)
    assert started["success"] is True and rejected["success"] is False
    assert pipeline.mode == "fast" and pipeline.fast_fps == 4.0  # 正在进行的投屏不受影响


@pytest.mark.parametrize("fps", [6, 99, "abc"])
def test_start_cast_image_mode_ignores_fps(
    make_app: Callable[..., tuple[Starlette, CastPipeline, Settings]], fps: object
) -> None:
    """mode=image 时忽略 fps（合法与否都不看），返回体仍带流畅模式的当前档位。"""
    app, pipeline, _ = make_app()

    async def scenario() -> dict[str, Any]:
        async with mcp_session(app) as session:
            await session.initialize()
            return await call(session, "start_cast", {"mode": "image", "fps": fps})

    result = run(scenario)
    assert result["success"] is True and result["mode"] == "image"
    assert result["frame_duration_ms"] == 1000
    assert result["fps"] == result["fast_fps"] == 4.0
    assert result["device_fps"] == 1.5
    assert result["message"] == "投屏已开始，每秒刷新一次（档位只对流畅模式有效，普通模式固定每秒 1 帧）"
    assert pipeline.fast_fps == 4.0 and pipeline.mode == "image"


def test_start_cast_fps_with_default_mode_is_image(
    make_app: Callable[..., tuple[Starlette, CastPipeline, Settings]],
) -> None:
    """不传 mode 时按规格是 image（默认），fps 被忽略；SKILL.md 要求调档位时显式传 mode=fast。"""
    app, pipeline, _ = make_app()

    async def scenario() -> dict[str, Any]:
        async with mcp_session(app) as session:
            await session.initialize()
            return await call(session, "start_cast", {"fps": 6})

    result = run(scenario)
    assert result["mode"] == "image" and result["fps"] == 4.0
    assert pipeline.fast_fps == 4.0


def test_cast_status_reports_runtime_fast_fps(make_app: Callable[..., tuple[Starlette, CastPipeline, Settings]]) -> None:
    """电脑上 set-fps（这里直接改 pipeline）之后，cast_status 立即报新值，"快一点/慢一点"据此加减。"""
    app, pipeline, _ = make_app()
    pipeline.fast_fps = 2

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        async with mcp_session(app) as session:
            await session.initialize()
            status = await call(session, "cast_status")
            started = await call(session, "start_cast", {"mode": "fast"})
            return status, started

    status, started = run(scenario)
    assert status["fast_fps"] == 2.0 and status["fast_frame_duration_ms"] == 250 and status["device_fps"] == 1.0
    assert status["mode"] == "off"  # 与当前模式无关：没在投屏时也报流畅模式的当前档位
    assert started["frame_duration_ms"] == 250 and started["fps"] == 2.0 and started["device_fps"] == 1.0
