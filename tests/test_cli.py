"""glasscast.cli 与 glasscast.config 的测试。

- 子进程：``--help``、``fset inspect fixture``，以及真正的 ``serve --source synthetic``（只监听 127.0.0.1，
  ``--autostart image`` 和 ``--autostart fast`` 各一次，用 HTTP 打一遍全部路由和 MCP，最后 SIGINT 退出），
  对应实现指南 §8 / §9.1 的验收命令。
- 进程内：直接调 :func:`glasscast.cli.main`，覆盖 fset build/inspect/extract、snapshot（合成画面或注入的
  假帧源）、monitors（注入的假显示器列表）、配置解析与优先级。
- 不真截屏：snapshot 只用 ``--source synthetic`` 或打桩的 ``make_source``。
"""

from __future__ import annotations

import io
import json
import os
import signal
import socket
import subprocess
import sys
import time
import contextlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from PIL import Image

from glasscast import __version__, cli
from glasscast.config import (
    ENV_VARS,
    ConfigError,
    Settings,
    generate_cast_key,
    generate_token,
    load_settings,
    normalize_public_url,
    parse_bool,
    parse_canvas,
    parse_hosts,
    parse_region,
)
from glasscast.frames import CANVAS, StaticSource, build_timer_fset
from glasscast.fset import build_fset, inspect_fset, parse_fset
from glasscast.pipeline import CastPipeline

ROOT = Path(__file__).resolve().parents[1]
#: 自制夹具，形状与官方样本 genie_ball.fset 相同（48 帧 32×30 RGBA PNG，见 tests/fixtures/make_sample_fset.py）
FIXTURE = ROOT / "tests" / "fixtures" / "sample.fset"
FIXTURE_BYTES = 9_673
PYTHON = sys.executable


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试不受开发者自己 shell 里的 GLASSCAST_* 影响。"""
    for var in list(os.environ):
        if var.startswith("GLASSCAST_"):
            monkeypatch.delenv(var, raising=False)


def child_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GLASSCAST_")}
    env["PYTHONUNBUFFERED"] = "1"
    env.update(extra)
    return env


def run_cli(*args: str, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [PYTHON, "-m", "glasscast.cli", *args],
        cwd=ROOT,
        env=child_env(),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def make_png(path: Path, color: tuple[int, int, int], size: tuple[int, int] = (40, 20)) -> Path:
    Image.new("RGB", size, color).save(path, format="PNG")
    return path


# =========================================================================== 子进程


def test_help_subprocess() -> None:
    result = run_cli("--help")
    assert result.returncode == 0, result.stderr
    for command in ("serve", "snapshot", "monitors", "fset", "set-fps", "config"):
        assert command in result.stdout

    serve = run_cli("serve", "--help")
    assert serve.returncode == 0, serve.stderr
    for option in (
        "--public-url",
        "--port",
        "--host",
        "--token",
        "--source",
        "--monitor",
        "--region",
        "--fit",
        "--image-fps",
        "--fast-fps",
        "--jpeg-quality",
        "--color",
        "--autostart",
    ):
        assert option in serve.stdout, option
    # clip 分段流已下线：不再是对外选项（实现指南 §9.1）
    for removed in ("--clip-fps", "--clip-frames", "--clip-format", "GLASSCAST_CLIP"):
        assert removed not in serve.stdout, removed
    assert "{off,image,fast}" in serve.stdout
    assert "{gray,rgb}" in serve.stdout
    assert "GLASSCAST_TOKEN" in serve.stdout
    assert "GLASSCAST_PUBLIC_URL" in serve.stdout
    assert "GLASSCAST_FAST_FPS" in serve.stdout and "GLASSCAST_COLOR_MODE" in serve.stdout

    for sub in (("fset", "build", "--help"), ("fset", "inspect", "--help"), ("snapshot", "--help")):
        assert run_cli(*sub).returncode == 0
    set_fps = run_cli("set-fps", "--help")
    assert set_fps.returncode == 0
    for option in ("--port", "--host", "--token", "GLASSCAST_TOKEN", "1～10", "档位"):
        assert option in set_fps.stdout, option
    assert "每秒张数" not in set_fps.stdout  # 档位不是每秒张数的承诺，真实帧数低得多
    assert run_cli("config", "--help").returncode == 0


def test_version_subprocess() -> None:
    result = run_cli("--version")
    assert result.returncode == 0
    assert __version__ in result.stdout


def test_console_script_installed() -> None:
    script = Path(PYTHON).parent / "glasscast"
    if not script.exists():
        pytest.skip("venv 里没有 glasscast 入口脚本（未以 uv sync 安装）")
    result = subprocess.run([str(script), "--version"], capture_output=True, text=True, timeout=60, env=child_env())
    assert result.returncode == 0
    assert __version__ in result.stdout


def test_fset_inspect_fixture_subprocess() -> None:
    result = run_cli("fset", "inspect", str(FIXTURE))
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "帧数      48" in out
    assert "580 字节" in out
    assert f"{FIXTURE_BYTES:,} 字节" in out
    assert "PNG ×48" in out
    assert "32×30 ×48" in out
    assert 'total-frames="48"' in out


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.contextmanager
def serve_process(tmp_path: Path, autostart: str) -> Iterator[dict[str, Any]]:
    """真实启动 ``glasscast serve --source synthetic --autostart <autostart>``（只监听 127.0.0.1）。"""
    port = free_port()
    token = "subprocess-token-0123456789"
    key = "SubprocessKey_01"
    base = f"http://127.0.0.1:{port}"
    stdout_path, stderr_path = tmp_path / "stdout.txt", tmp_path / "stderr.txt"
    with stdout_path.open("w") as out, stderr_path.open("w") as err:
        proc = subprocess.Popen(
            [
                PYTHON,
                "-m",
                "glasscast.cli",
                "serve",
                "--source",
                "synthetic",
                "--public-url",
                base,
                "--port",
                str(port),
                "--autostart",
                autostart,
                "--cast-key",
                key,
            ],
            cwd=ROOT,
            env=child_env(GLASSCAST_TOKEN=token),
            stdout=out,
            stderr=err,
        )
    info = {"proc": proc, "base": base, "token": token, "key": key, "stdout": stdout_path, "stderr": stderr_path}
    try:
        deadline = time.monotonic() + 60
        while True:
            if proc.poll() is not None:
                pytest.fail(f"serve 提前退出（{proc.returncode}）：\n{stderr_path.read_text()}")
            try:
                if httpx.get(f"{base}/healthz", timeout=1.0).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                pytest.fail(f"serve 60 秒内没有就绪：\n{stderr_path.read_text()}")
            time.sleep(0.1)
        yield info
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


@pytest.fixture
def served(tmp_path: Path) -> Iterator[dict[str, Any]]:
    with serve_process(tmp_path, "image") as info:
        yield info


def poll(fetch: Any, predicate: Any, timeout: float = 15.0) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = fetch()
        if predicate(value):
            return value
        if time.monotonic() > deadline:
            pytest.fail("等待条件超时")
        time.sleep(0.05)


def test_serve_end_to_end(served: dict[str, Any]) -> None:
    base, key, token = served["base"], served["key"], served["token"]
    mcp_headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    auth = {**mcp_headers, "Authorization": f"Bearer {token}"}

    with httpx.Client(base_url=base, timeout=5.0) as client:
        r = client.get("/healthz")
        assert r.text == "ok"
        assert r.headers["cache-control"] == "no-store"
        assert "server" not in r.headers  # server_header=False，不暴露 uvicorn

        # --autostart image：很快就有真实画面（不是占位），默认灰度
        r = poll(lambda: client.get(f"/s/{key}/screen.jpg?t=3600"), lambda r: "x-glasscast-placeholder" not in r.headers)
        assert r.headers["content-type"] == "image/jpeg"
        assert int(r.headers["x-glasscast-seq"]) > 0
        with Image.open(io.BytesIO(r.content)) as im:
            assert (im.format, im.size, im.mode) == ("JPEG", CANVAS, "L")

        # 定时器 .fset：2 帧 8×8 PNG，内容固定（research.md §9.2 E4）
        r = client.get(f"/s/{key}/timer.fset?ts=1&i=2")
        assert r.content == build_timer_fset()
        info = inspect_fset(r.content)
        assert (info["frames"], set(info["formats"]), set(info["png_dims"])) == (2, {"png"}, {(8, 8)})

        # 实验接口 clip.fset 仍在：image 模式下是 10 帧 PNG 640×290 的占位段
        r = client.get(f"/s/{key}/clip.fset")
        info = inspect_fset(r.content)
        assert (info["frames"], set(info["formats"]), set(info["png_dims"])) == (10, {"png"}, {CANVAS})

        assert client.get(f"/s/{key}/status.json").json()["mode"] == "image"
        assert client.get("/s/wrong-key/screen.jpg").status_code == 404

        # MCP：无 Bearer 401；有 Bearer 能列出 3 个工具
        tools_list = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        assert client.post("/mcp", json=tools_list, headers=mcp_headers).status_code == 401
        r = client.post("/mcp", json=tools_list, headers=auth)
        assert r.status_code == 200
        assert {t["name"] for t in r.json()["result"]["tools"]} == {"start_cast", "stop_cast", "cast_status"}

        # start_cast(fast) → 返回定时器地址与帧时长，图片地址照常出画面
        start = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "start_cast", "arguments": {"mode": "fast"}}}
        t0 = time.monotonic()
        r = client.post("/mcp", json=start, headers=auth)
        assert time.monotonic() - t0 < 1.0
        result = r.json()["result"]["structuredContent"]
        assert result["success"] is True and result["mode"] == "fast"
        assert result["url"] == f"{base}/s/{key}/screen.jpg"
        assert result["timer_url"] == f"{base}/s/{key}/timer.fset"
        assert result["frame_duration_ms"] == 125
        r = client.get(f"/s/{key}/screen.jpg?ts=1729999999999&i=1")
        assert r.headers["x-glasscast-mode"] == "fast" and "x-glasscast-placeholder" not in r.headers

    proc = served["proc"]
    proc.send_signal(signal.SIGINT)
    assert proc.wait(timeout=15) == 0

    stdout = served["stdout"].read_text()
    assert f"{base}/s/{key}/screen.jpg" in stdout
    assert f"{base}/s/{key}/timer.fset" in stdout
    assert "clip.fset" not in stdout  # 已下线的接口不再出现在启动信息里
    assert "cast-emit 页面每 12 跳换一张" in stdout  # 流畅模式改用 emit 自环页（research.md §9.8）
    assert f"{base}/mcp" in stdout
    assert "来自环境变量 GLASSCAST_TOKEN" in stdout
    assert token not in stdout  # 来自环境变量的 token 不打印
    stderr = served["stderr"].read_text()
    assert "GET /s/…/screen.jpg?t=3600 200" in stderr  # 访问日志打码 key、保留 query
    assert key not in stderr
    assert token not in stderr


def test_serve_autostart_fast_subprocess(tmp_path: Path) -> None:
    """实现指南 §8 / §9.1 的验收：``--autostart fast`` 后图片地址出灰度画面，timer.fset 为 2 帧 8×8 PNG。"""
    with serve_process(tmp_path, "fast") as info:
        base, key = info["base"], info["key"]
        with httpx.Client(base_url=base, timeout=5.0) as client:
            first = poll(lambda: client.get(f"/s/{key}/screen.jpg?ts=1&i=0"), lambda r: "x-glasscast-placeholder" not in r.headers)
            assert first.headers["x-glasscast-mode"] == "fast"
            with Image.open(io.BytesIO(first.content)) as im:
                assert (im.format, im.size, im.mode) == ("JPEG", CANVAS, "L")
            seq = int(first.headers["x-glasscast-seq"])
            # fast_fps 默认 4：很快就有更新的一帧（不靠 sleep 猜时间，轮询带超时）
            poll(lambda: client.get(f"/s/{key}/screen.jpg?ts=2&i=1"), lambda r: int(r.headers["x-glasscast-seq"]) > seq)
            timer = client.get(f"/s/{key}/timer.fset")
            assert timer.headers["x-glasscast-frame-duration-ms"] == "125"
            assert timer.headers["access-control-allow-origin"] == "*"
            assert timer.headers["cross-origin-resource-policy"] == "cross-origin"
            assert timer.headers["cache-control"] == "no-store"
        timer_path = tmp_path / "timer.fset"
        timer_path.write_bytes(timer.content)
        result = run_cli("fset", "inspect", str(timer_path), "--json")
        assert result.returncode == 0, result.stderr
        parsed = json.loads(result.stdout)
        assert parsed["frames"] == 2 and parsed["formats"] == ["png", "png"] and parsed["png_dims"] == [[8, 8], [8, 8]]
    assert "自动开始   fast" in info["stdout"].read_text()


def test_serve_prints_generated_token(tmp_path: Path) -> None:
    """没有 GLASSCAST_TOKEN 时自动生成并打印一次（用 --no-probe 之外的参数快速失败退出）。"""
    settings = load_settings({"public_url": "https://cast.example.com"}, {})
    buf = io.StringIO()
    cli.print_banner(settings, token_origin="generated", out=buf)
    text = buf.getvalue()
    assert settings.token_generated and settings.token in text
    assert "GLASSCAST_TOKEN" in text
    assert settings.image_url in text and settings.mcp_url in text
    assert "屏幕录制" in text  # 默认 source=screen，提示 macOS 权限


# =========================================================================== serve 参数错误


def test_serve_requires_public_url(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["serve", "--no-probe"]) == 2
    err = capsys.readouterr().err
    assert "--public-url" in err and "GLASSCAST_PUBLIC_URL" in err


@pytest.mark.parametrize(
    "args",
    [
        ["serve", "--region", "1,2,3"],
        ["serve", "--canvas", "640"],
        ["serve", "--source", "camera"],
        ["serve", "--autostart", "video"],
        ["serve", "--autostart", "clip"],  # clip 不再对外（实现指南 §9.1）
        ["serve", "--color", "cmyk"],
        ["serve", "--clip-fps", "5"],
        ["serve", "--fast-fps", "fast"],
        ["serve", "--port", "abc"],
    ],
)
def test_serve_rejects_bad_arguments(args: list[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(args)
    assert excinfo.value.code == 2


@pytest.mark.parametrize(
    ("args", "fragment"),
    [
        (["--public-url", "ftp://x"], "public_url"),
        (["--public-url", "https://x.example/?a=1"], "查询参数"),
        (["--public-url", "https://x.example", "--jpeg-quality", "0"], "jpeg_quality"),
        (["--public-url", "https://x.example", "--fast-fps", "0"], "fast_fps"),
        (["--public-url", "https://x.example", "--cast-key", "bad key!"], "cast_key"),
        (["--public-url", "https://x.example", "--fast-fps", "-1"], "fast_fps"),
        (["--public-url", "https://x.example", "--fast-fps", "12"], "1～10"),
        (["--public-url", "https://x.example", "--image-fps", "nan"], "image_fps"),
    ],
)
def test_serve_config_errors(capsys: pytest.CaptureFixture[str], args: list[str], fragment: str) -> None:
    assert cli.main(["serve", "--no-probe", *args]) == 2
    assert fragment in capsys.readouterr().err


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == 2
    assert "serve" in capsys.readouterr().out
    assert cli.main(["fset"]) == 2


# =========================================================================== fset


def test_fset_inspect_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["fset", "inspect", str(FIXTURE), "--json"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["frames"] == 48
    assert info["header_bytes"] == 580
    assert info["total_bytes"] == FIXTURE_BYTES
    assert info["formats"] == ["png"] * 48
    assert info["png_dims"] == [[32, 30]] * 48


def test_fset_inspect_frames(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["fset", "inspect", str(FIXTURE), "--frames"]) == 0
    out = capsys.readouterr().out
    assert sum(1 for line in out.splitlines() if line.strip().startswith("#")) == 48


def test_fset_inspect_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    broken = tmp_path / "broken.fset"
    broken.write_bytes(b"\x02\x00\x00\x00garbage")
    assert cli.main(["fset", "inspect", str(broken)]) == 1
    assert "不是合法的 .fset" in capsys.readouterr().err

    assert cli.main(["fset", "inspect", str(tmp_path / "missing.fset")]) == 1
    assert "读取" in capsys.readouterr().err

    # 尾部多余字节：严格模式报错，--no-strict 放行
    padded = tmp_path / "padded.fset"
    padded.write_bytes(FIXTURE.read_bytes() + b"\x00\x00")
    assert cli.main(["fset", "inspect", str(padded)]) == 1
    capsys.readouterr()
    assert cli.main(["fset", "inspect", str(padded), "--no-strict"]) == 0


def test_fset_build_png_default(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    images = [make_png(tmp_path / f"{i}.png", (i * 60, 0, 0)) for i in range(3)]
    out = tmp_path / "out" / "clip.fset"
    assert cli.main(["fset", "build", str(out), *map(str, images)]) == 0
    stdout = capsys.readouterr().out
    assert "3 帧" in stdout and 'total-frames="3"' in stdout
    info = inspect_fset(out.read_bytes())
    assert info["frames"] == 3
    assert info["formats"] == ["png"] * 3
    assert info["png_dims"] == [(40, 20)] * 3
    with Image.open(io.BytesIO(parse_fset(out.read_bytes())[0])) as frame:
        assert frame.mode == "RGBA"  # 与官方样本一致（research.md §4）
        assert frame.getpixel((0, 0)) == (0, 0, 0, 255)


def test_fset_build_with_canvas_and_jpeg(tmp_path: Path) -> None:
    images = [make_png(tmp_path / f"{i}.png", (0, i * 80, 0), size=(1000, 300)) for i in range(2)]
    out = tmp_path / "c.fset"
    assert cli.main(["fset", "build", str(out), *map(str, images), "--canvas", "64x29", "--fit", "crop"]) == 0
    assert inspect_fset(out.read_bytes())["png_dims"] == [(64, 29)] * 2

    jpg = tmp_path / "j.fset"
    assert cli.main(["fset", "build", str(jpg), *map(str, images), "--format", "jpeg", "--jpeg-quality", "50"]) == 0
    assert inspect_fset(jpg.read_bytes())["formats"] == ["jpeg"] * 2


def test_fset_build_mixed_sizes_warns(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    a = make_png(tmp_path / "a.png", (1, 2, 3), size=(10, 10))
    b = make_png(tmp_path / "b.png", (1, 2, 3), size=(20, 10))
    assert cli.main(["fset", "build", str(tmp_path / "m.fset"), str(a), str(b)]) == 0
    assert "尺寸不一致" in capsys.readouterr().out


def test_fset_build_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    junk = tmp_path / "junk.png"
    junk.write_bytes(b"not an image")
    assert cli.main(["fset", "build", str(tmp_path / "x.fset"), str(junk)]) == 1
    assert "无法解码" in capsys.readouterr().err
    assert cli.main(["fset", "build", str(tmp_path / "x.fset"), str(tmp_path / "missing.png")]) == 1
    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"")
    assert cli.main(["fset", "build", str(tmp_path / "x.fset"), str(empty), "--format", "raw"]) == 1
    good = make_png(tmp_path / "g.png", (0, 0, 0))
    assert cli.main(["fset", "build", str(tmp_path / "x.fset"), str(good), "--format", "jpeg", "--jpeg-quality", "0"]) == 2


def test_fset_extract_and_raw_rebuild_roundtrip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    outdir = tmp_path / "frames"
    assert cli.main(["fset", "extract", str(FIXTURE), str(outdir)]) == 0
    assert "48 帧" in capsys.readouterr().out
    files = sorted(outdir.iterdir())
    assert [f.name for f in files[:2]] == ["frame_000.png", "frame_001.png"]
    assert len(files) == 48
    original = FIXTURE.read_bytes()
    assert [f.read_bytes() for f in files] == parse_fset(original)

    # raw 打包回去与原文件逐字节相同（research.md §4 的布局）
    rebuilt = tmp_path / "rebuilt.fset"
    assert cli.main(["fset", "build", str(rebuilt), *map(str, files), "--format", "raw"]) == 0
    assert rebuilt.read_bytes() == original


def test_fset_extract_jpeg_extension(tmp_path: Path) -> None:
    pipeline = CastPipeline(StaticSource(Image.new("RGB", (8, 8))), clip_format="jpeg", clip_frames=2)
    try:
        data = pipeline.placeholder_clip().data
    finally:
        pipeline.close()
    src = tmp_path / "j.fset"
    src.write_bytes(data)
    assert cli.main(["fset", "extract", str(src), str(tmp_path / "o"), "--prefix", "f"]) == 0
    assert sorted(p.name for p in (tmp_path / "o").iterdir()) == ["f000.jpg", "f001.jpg"]


def test_fset_extract_invalid(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "bad.fset"
    bad.write_bytes(build_fset([b"a", b"b"])[:-1])
    assert cli.main(["fset", "extract", str(bad), str(tmp_path / "o")]) == 1
    assert "不是合法的 .fset" in capsys.readouterr().err


# =========================================================================== snapshot / monitors


def test_snapshot_synthetic_jpeg(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "shots" / "a.jpg"
    assert cli.main(["snapshot", str(out), "--source", "synthetic"]) == 0
    with Image.open(out) as im:
        assert (im.format, im.size, im.mode) == ("JPEG", CANVAS, "L")  # 与 serve 一样默认灰度
    assert "640×290 灰度 JPEG" in capsys.readouterr().out

    color = tmp_path / "c.jpg"
    assert cli.main(["snapshot", str(color), "--source", "synthetic", "--color", "rgb"]) == 0
    with Image.open(color) as im:
        assert im.mode == "RGB"


def test_snapshot_png_and_canvas(tmp_path: Path) -> None:
    out = tmp_path / "a.png"
    assert cli.main(["snapshot", str(out), "--source", "synthetic", "--canvas", "320x145"]) == 0
    with Image.open(out) as im:
        assert (im.format, im.size, im.mode) == ("PNG", (320, 145), "L")


def test_snapshot_uses_env_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GLASSCAST_SOURCE", "synthetic")
    monkeypatch.setenv("GLASSCAST_CANVAS", "200x100")
    out = tmp_path / "e.jpg"
    assert cli.main(["snapshot", str(out)]) == 0
    with Image.open(out) as im:
        assert im.size == (200, 100)


def test_snapshot_screen_path_with_fake_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[Settings] = []

    def fake_make_source(settings: Settings) -> StaticSource:
        seen.append(settings)
        return StaticSource(Image.new("RGB", (2880, 1800), (0, 0, 0)))  # 全黑：模拟没有屏幕录制权限

    monkeypatch.setattr(cli, "make_source", fake_make_source)
    out = tmp_path / "s.jpg"
    assert cli.main(["snapshot", str(out), "--monitor", "2", "--region", "10,20,300,200", "--fit", "crop"]) == 0
    assert seen[0].source == "screen"
    assert seen[0].monitor == 2
    assert seen[0].region == (10, 20, 300, 200)
    assert seen[0].fit_mode == "crop"
    text = capsys.readouterr().out
    assert "2880×1800" in text
    assert "全黑" in text and "屏幕录制" in text
    with Image.open(out) as im:
        assert im.size == CANVAS

    raw = tmp_path / "raw.png"
    assert cli.main(["snapshot", str(raw), "--raw"]) == 0
    with Image.open(raw) as im:
        assert im.size == (2880, 1800)


def test_snapshot_capture_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Failing:
        closed = False

        def grab(self) -> Image.Image:
            raise PermissionError("denied")

        def close(self) -> None:
            Failing.closed = True

    monkeypatch.setattr(cli, "make_source", lambda settings: Failing())
    assert cli.main(["snapshot", str(tmp_path / "x.jpg")]) == 1
    err = capsys.readouterr().err
    assert "PermissionError" in err and "屏幕录制" in err
    assert Failing.closed
    assert not (tmp_path / "x.jpg").exists()


def test_monitors(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    fake = [
        {"left": 0, "top": 0, "width": 3000, "height": 900},
        {"left": 0, "top": 0, "width": 1440, "height": 900},
        {"left": 1440, "top": 0, "width": 1560, "height": 900},
    ]
    monkeypatch.setattr(cli, "list_monitors", lambda: fake)
    assert cli.main(["monitors"]) == 0
    out = capsys.readouterr().out
    assert "1440" in out and "1560" in out and "主屏" in out
    assert cli.main(["monitors", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == fake

    def boom() -> list[dict[str, int]]:
        raise RuntimeError("no display")

    monkeypatch.setattr(cli, "list_monitors", boom)
    assert cli.main(["monitors"]) == 1
    assert "no display" in capsys.readouterr().err


# =========================================================================== serve 辅助函数


def test_probe_source() -> None:
    size, error, black = cli.probe_source(StaticSource(Image.new("RGB", (30, 20), (0, 0, 0))))
    assert (size, error, black) == ((30, 20), None, True)
    size, error, black = cli.probe_source(StaticSource(Image.new("RGB", (30, 20), (9, 0, 0))))
    assert (size, error, black) == ((30, 20), None, False)

    class Failing:
        def grab(self) -> Image.Image:
            raise OSError("nope")

    size, error, black = cli.probe_source(Failing())  # type: ignore[arg-type]
    assert size is None and error is not None and "nope" in error and black is False


def test_make_source() -> None:
    from glasscast.frames import ScreenSource, SyntheticSource

    synthetic = cli.make_source(load_settings({"source": "synthetic", "canvas": "100x50"}, {}, require_public_url=False))
    assert isinstance(synthetic, SyntheticSource)
    assert synthetic.grab().size == (100, 50)
    screen = cli.make_source(load_settings({"monitor": 2, "region": "1,2,3,4"}, {}, require_public_url=False))
    assert isinstance(screen, ScreenSource)  # 构造时不碰 mss，不会截屏
    assert screen.monitor == 2 and screen.region == (1, 2, 3, 4)


def test_banner_variants() -> None:
    settings = load_settings({"public_url": "https://cast.example.com", "token": "x" * 32}, {})
    buf = io.StringIO()
    cli.print_banner(settings, token_origin="env", probe=((2880, 1800), None, True), out=buf)
    text = buf.getvalue()
    assert "x" * 32 not in text
    assert "来自环境变量 GLASSCAST_TOKEN" in text
    assert "2880×1800" in text
    assert "全黑" in text
    assert "timer.fset" in text and "cast-emit 页面每 12 跳换一张" in text
    assert "换算系数 20" in text  # 帧合集定时器页已不再是流畅模式页面，横幅改讲 emit 跳数
    assert "fast：4 档，真机约每秒 1.5 帧；电脑每秒截 4 张" in text
    assert "「投屏调到 N 档」" in text and "glasscast set-fps N" in text
    assert "灰度 JPEG" in text
    assert "clip" not in text

    buf = io.StringIO()
    cli.print_banner(settings, token_origin="cli", probe=(None, "ScreenShotError: denied", False), out=buf)
    text = buf.getvalue()
    assert "来自 --token" in text
    assert "试截屏失败" in text and "denied" in text

    synthetic = load_settings(
        {
            "public_url": "http://127.0.0.1:8766",
            "token": "y" * 32,
            "source": "synthetic",
            "autostart": "fast",
            "fast_fps": 5,
            "color_mode": "rgb",
        },
        {},
    )
    buf = io.StringIO()
    cli.print_banner(synthetic, token_origin="env", out=buf)
    text = buf.getvalue()
    assert "屏幕录制" not in text  # 合成画面不需要权限提示
    assert "本机地址" in text  # public_url 是 localhost 的警告
    assert "自动开始   fast" in text
    assert "每 10 跳换一张" in text  # round(1000/5/20) = 10
    assert "fast：5 档，真机约每秒 1.5 帧；电脑每秒截 5 张" in text
    assert "彩色 JPEG" in text and "单色绿" in text  # rgb 的提醒


# =========================================================================== config


def test_defaults_match_guide() -> None:
    s = load_settings({"public_url": "https://cast.example.com"}, {})
    assert (s.host, s.port) == ("127.0.0.1", 8766)
    assert s.monitor == 1 and s.region is None
    assert s.fit_mode == "fit" and s.canvas == (640, 290)
    assert (s.image_fps, s.fast_fps, s.jpeg_quality) == (1.0, 4.0, 70)
    assert s.color_mode == "gray"
    assert s.fast_frame_duration_ms == 125  # round(1000 / 4 / 2)（实现指南 §9.1）
    assert s.frame_duration_ms_for("fast") == 125 and s.frame_duration_ms_for("image") == 1000
    assert not hasattr(s, "clip_fps") and not hasattr(s, "clip_frames")  # clip 参数不再对外
    assert s.source == "screen" and s.autostart == "off"
    assert s.dns_rebinding_protection is True
    # 默认画布 640×290 只适合模拟器：真机实测不显示，启动时提醒（research.md §9.7）
    assert len(s.warnings()) == 1 and "480x218" in s.warnings()[0]


def test_precedence_cli_over_env_over_default() -> None:
    env = {"GLASSCAST_PUBLIC_URL": "https://env.example", "GLASSCAST_PORT": "9000", "GLASSCAST_FAST_FPS": "5"}
    s = load_settings({"port": 9100, "fast_fps": None}, env)
    assert s.public_url == "https://env.example"  # 只有环境变量
    assert s.port == 9100  # CLI 覆盖环境变量
    assert s.fast_fps == 5.0  # CLI 为 None = 没给，用环境变量
    assert s.jpeg_quality == 70  # 默认值
    assert s.fast_frame_duration_ms == 100
    assert load_settings({"fast_fps": 2, "color_mode": "rgb"}, env).fast_fps == 2.0


def test_region_none_on_cli_overrides_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--region none/full 要盖过 GLASSCAST_REGION（命令行 > 环境变量；none = 全屏），serve 与 snapshot 一样。"""
    monkeypatch.setenv("GLASSCAST_REGION", "0,0,1280,580")
    parser = cli.build_parser()

    def serve_region(*extra: str) -> tuple[int, int, int, int] | None:
        args = parser.parse_args(["serve", "--public-url", "https://x.example.com", *extra])
        return cli._load(args, cli._SERVE_FIELDS).region

    assert serve_region() == (0, 0, 1280, 580)  # 没给 --region：用环境变量
    for text in ("none", "full", "NONE", "all"):
        assert serve_region("--region", text) is None
    assert serve_region("--region", "10,20,300,200") == (10, 20, 300, 200)

    snap = parser.parse_args(["snapshot", str(tmp_path / "x.jpg"), "--region", "none"])
    assert cli._load(snap, cli._CAPTURE_FIELDS, require_public_url=False, generate_secrets=False).region is None

    # 走完整的 main：snapshot 收到的 Settings 也是全屏
    seen: list[Settings] = []

    def fake_make_source(settings: Settings) -> StaticSource:
        seen.append(settings)
        return StaticSource(Image.new("RGB", (64, 32), (40, 80, 120)))

    monkeypatch.setattr(cli, "make_source", fake_make_source)
    assert cli.main(["snapshot", str(tmp_path / "y.jpg"), "--region", "none"]) == 0
    assert seen[0].region is None


def test_serve_explains_missing_mcp2(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """mcp 1.x 没有 mcp.server.mcpserver：serve 给出能照做的提示（pyproject 要求 mcp>=2.3，绕过依赖解析时才会碰到 1.x），而不是堆栈。"""
    monkeypatch.setitem(sys.modules, "glasscast.server", None)  # 让 from .server import … 抛 ImportError
    assert cli.main(["serve", "--no-probe", "--public-url", "https://x.example.com", "--source", "synthetic"]) == 1
    err = capsys.readouterr().err
    assert "mcp 2.3" in err and "uv sync" in err


def test_every_field_has_env_var() -> None:
    env = {
        "GLASSCAST_PUBLIC_URL": "https://e.example/",
        "GLASSCAST_HOST": "127.0.0.2",
        "GLASSCAST_PORT": "8800",
        "GLASSCAST_TOKEN": "tok-0123456789abcdef",
        "GLASSCAST_CAST_KEY": "KeyFromEnv_0001",
        "GLASSCAST_MONITOR": "2",
        "GLASSCAST_REGION": "0, 0, 1280, 580",
        "GLASSCAST_FIT_MODE": "CROP",
        "GLASSCAST_CANVAS": "320×145",
        "GLASSCAST_IMAGE_FPS": "0.5",
        "GLASSCAST_FAST_FPS": "2.5",
        "GLASSCAST_EMIT_HOP_MS": "40",
        "GLASSCAST_JPEG_QUALITY": "80",
        "GLASSCAST_JPEG_ENCODER": "Pillow",
        "GLASSCAST_JPEG_TARGET_KB": "2.5",
        "GLASSCAST_GIF_LEVELS": "4",
        "GLASSCAST_GIF_DELAY_MS": "120",
        "GLASSCAST_COLOR_MODE": "RGB",
        "GLASSCAST_SOURCE": "synthetic",
        "GLASSCAST_AUTOSTART": "fast",
        "GLASSCAST_ALLOWED_HOSTS": "a.example, b.example:9",
        "GLASSCAST_DNS_REBINDING_PROTECTION": "off",
    }
    assert set(env) == set(ENV_VARS.values())
    s = load_settings({}, env)
    assert s.public_url == "https://e.example"
    assert (s.host, s.port, s.token, s.cast_key) == ("127.0.0.2", 8800, "tok-0123456789abcdef", "KeyFromEnv_0001")
    assert (s.monitor, s.region, s.fit_mode, s.canvas) == (2, (0, 0, 1280, 580), "crop", (320, 145))
    assert (s.image_fps, s.fast_fps, s.jpeg_quality, s.color_mode) == (0.5, 2.5, 80, "rgb")
    assert s.emit_hop_ms == 40.0
    assert (s.jpeg_encoder, s.jpeg_target_kb) == ("pillow", 2.5)
    assert s.pipeline_kwargs()["jpeg_target_bytes"] == 2560
    assert (s.gif_levels, s.gif_delay_ms) == (4, 120)
    assert (s.source, s.autostart) == ("synthetic", "fast")
    assert s.allowed_hosts == ("a.example", "b.example:9")
    assert s.dns_rebinding_protection is False
    assert not s.token_generated and not s.cast_key_generated


def test_empty_env_values_are_ignored() -> None:
    s = load_settings({"public_url": "https://x.example"}, {"GLASSCAST_PORT": "", "GLASSCAST_TOKEN": "  "})
    assert s.port == 8766
    assert s.token_generated


def test_env_errors_are_aggregated() -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_settings({}, {"GLASSCAST_PORT": "x", "GLASSCAST_FIT_MODE": "zoom", "GLASSCAST_REGION": "1,2"})
    message = str(excinfo.value)
    assert "GLASSCAST_PORT" in message and "GLASSCAST_FIT_MODE" in message and "GLASSCAST_REGION" in message


def test_validation_errors() -> None:
    base = {"public_url": "https://x.example"}
    for overrides, fragment in [
        ({"port": 0}, "port"),
        ({"port": 70000}, "port"),
        ({"monitor": -1}, "monitor"),
        ({"jpeg_quality": 101}, "jpeg_quality"),
        ({"image_fps": 0}, "image_fps"),
        ({"fast_fps": 0}, "fast_fps"),
        ({"fast_fps": float("inf")}, "fast_fps"),
        ({"fast_fps": 0.5}, "1～10"),  # 启动值与运行中修改共用 1～10（pipeline.parse_fast_fps）
        ({"fast_fps": 11}, "1～10"),
        ({"color_mode": "cmyk"}, "color_mode"),
        ({"autostart": "clip"}, "autostart"),
        ({"token": "has space"}, "token"),
        ({"token": "中文token"}, "token"),
        ({"cast_key": "short"}, "cast_key"),
        ({"cast_key": "has/slash00"}, "cast_key"),
    ]:
        with pytest.raises(ConfigError) as excinfo:
            load_settings({**base, **overrides}, {})
        assert fragment in str(excinfo.value), overrides


def test_unknown_cli_key_is_type_error() -> None:
    with pytest.raises(TypeError):
        load_settings({"pubic_url": "https://x.example"}, {})


def test_generated_secrets() -> None:
    s = load_settings({"public_url": "https://x.example"}, {})
    assert s.token_generated and s.cast_key_generated
    assert len(s.token) == 43
    assert len(s.cast_key) == 16
    assert all(c.isalnum() or c in "-_" for c in s.cast_key)
    other = load_settings({"public_url": "https://x.example"}, {})
    assert other.token != s.token and other.cast_key != s.cast_key
    assert len(generate_cast_key()) == 16 and len(generate_token()) == 43
    # 不生成时允许为空（snapshot 等子命令用）
    bare = load_settings({}, {}, require_public_url=False, generate_secrets=False)
    assert bare.token == "" and bare.cast_key == ""


def test_token_not_in_repr() -> None:
    s = load_settings({"public_url": "https://x.example", "token": "super-secret-token-123"}, {})
    assert "super-secret-token-123" not in repr(s)
    assert s.cast_key not in repr(s)


def test_urls_and_public_dict() -> None:
    s = load_settings({"public_url": "https://cast.example.com/", "cast_key": "AbcdEfgh12345678"}, {})
    assert s.urls() == {
        "public": "https://cast.example.com",
        "image": "https://cast.example.com/s/AbcdEfgh12345678/screen.jpg",
        "timer": "https://cast.example.com/s/AbcdEfgh12345678/timer.fset",
        "status": "https://cast.example.com/s/AbcdEfgh12345678/status.json",
        "mcp": "https://cast.example.com/mcp",
    }
    assert s.local_url == "http://127.0.0.1:8766"
    assert s.timer_url == "https://cast.example.com/s/AbcdEfgh12345678/timer.fset"
    assert s.clip_url.endswith("/clip.fset")  # 实验接口地址仍可算出，但不在 urls() 里
    public = s.public_dict()
    assert s.token not in json.dumps(public)
    assert public["fast_frame_duration_ms"] == 125
    assert public["color_mode"] == "gray" and public["fast_fps"] == 4.0


def test_pipeline_kwargs_are_accepted() -> None:
    s = load_settings(
        {"public_url": "https://x.example", "fast_fps": 5, "canvas": "320x145", "color_mode": "rgb", "image_fps": 0.5}, {}
    )
    pipeline = CastPipeline(StaticSource(Image.new("RGB", (10, 10))), **s.pipeline_kwargs())
    try:
        assert pipeline.canvas == (320, 145)
        assert pipeline.color_mode == "rgb"
        assert (pipeline.image_fps, pipeline.fast_fps) == (0.5, 5.0)
        assert pipeline.fast_frame_duration_ms == s.fast_frame_duration_ms == 100
        assert pipeline.page_frame_duration_ms("image") == s.frame_duration_ms_for("image")
    finally:
        pipeline.close()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://cast.example.com/", "https://cast.example.com"),
        ("HTTPS://Cast.Example.COM", "https://cast.example.com"),
        ("https://app.example.com/cast/", "https://app.example.com/cast"),
        ("http://127.0.0.1:8766", "http://127.0.0.1:8766"),
        ("  https://a.example:8443//  ", "https://a.example:8443"),
    ],
)
def test_normalize_public_url(raw: str, expected: str) -> None:
    assert normalize_public_url(raw) == expected


@pytest.mark.parametrize(
    "raw", ["cast.example.com", "ftp://x.example", "https://", "https://x.example/?a=1", "https://x.example/#f",
            "https://u:p@x.example", "https://x.example:99999"]
)
def test_normalize_public_url_rejects(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_public_url(raw)


def test_parsers() -> None:
    assert parse_region("0,0,1280,580") == (0, 0, 1280, 580)
    assert parse_region(" 10 20 30 40 ") == (10, 20, 30, 40)
    assert parse_region("-100,0,50,50") == (-100, 0, 50, 50)
    for empty in ("", "none", "FULL"):
        assert parse_region(empty) is None
    for bad in ("1,2,3", "a,b,c,d", "0,0,0,10", "0,0,10,-1"):
        with pytest.raises(ValueError):
            parse_region(bad)
    assert parse_canvas("640x290") == parse_canvas("640×290") == parse_canvas("640,290") == (640, 290)
    for bad in ("640", "0x10", "axb", "1x2x3"):
        with pytest.raises(ValueError):
            parse_canvas(bad)
    assert parse_bool("YES") is True and parse_bool("0") is False
    with pytest.raises(ValueError):
        parse_bool("maybe")
    assert parse_hosts(" A.example, b.example:9  c ") == ("a.example", "b.example:9", "c")


def test_allowed_hosts_for_mcp() -> None:
    s = load_settings({"public_url": "https://cast.example.com", "allowed_hosts": ["Extra.example", "10.0.0.2:8766"]}, {})
    hosts = s.mcp_allowed_hosts()
    for expected in (
        "cast.example.com",
        "cast.example.com:*",
        "127.0.0.1:*",
        "localhost:*",
        "[::1]:*",
        "extra.example",
        "extra.example:*",
        "10.0.0.2:8766",
    ):
        assert expected in hosts, expected
    assert "10.0.0.2:8766:*" not in hosts
    assert "https://cast.example.com" in s.mcp_allowed_origins()

    with_port = load_settings({"public_url": "https://h.example:8443/p"}, {})
    assert "h.example:8443" in with_port.mcp_allowed_hosts()

    merged = load_settings({"public_url": "https://h.example", "allowed_hosts": ["b"]}, {"GLASSCAST_ALLOWED_HOSTS": "a"})
    assert merged.allowed_hosts == ("a", "b")


def test_warnings() -> None:
    def warnings(**values: Any) -> str:
        return "\n".join(load_settings({"public_url": "https://x.example", **values}, {}).warnings())

    assert "HTTPS" in warnings(public_url="http://x.example")
    assert "本机地址" in warnings(public_url="http://localhost:8766")
    assert "0.0.0.0" in warnings(host="0.0.0.0")
    assert "建议至少" in warnings(token="short")
    assert "DNS 重绑定" in warnings(dns_rebinding_protection=False)
    assert "image_fps" in warnings(image_fps=5)
    assert "0.5/F" in warnings(image_fps=5)  # 调高只缩短画面年龄，不是"没有意义"
    assert "image_fps" not in warnings(image_fps=2)  # 2 fps 把平均画面年龄从约 0.5 s 降到约 0.25 s，不提醒
    assert "不高于" in warnings(fast_fps=1)
    assert "档位 4.5 偏高" in warnings(fast_fps=4.5)  # 大于 4 提醒：4 档是真机实测稳定上限（research.md §9.20）
    assert "偏高" not in warnings(fast_fps=4)
    # 超出 1～10 不再静默截到 1000 ms，而是直接报错（与运行中 start_cast(fps=…) / set-fps 同一个范围）
    for bad in (0.25, 0.5, 10.5, 20):
        with pytest.raises(ConfigError, match="fast_fps"):
            warnings(fast_fps=bad)
    assert "单色绿" in warnings(color_mode="rgb")  # research.md §9.2 E6
    assert "超预算" in warnings(canvas=(1280, 720))
    assert warnings(canvas=(320, 145)) == ""  # 页面宽高都是 100%，换画布不需要改页面
    assert "超预算" not in warnings(canvas=(640, 386))  # JPEG 允许约 640×386（research.md §2.3）
    assert "480x218" in warnings(canvas=(640, 290))  # 真机实测 640×290 不显示（research.md §9.7）
    assert warnings(canvas=(480, 218)) == ""


def test_serve_sigterm_exits_cleanly(served: dict[str, Any]) -> None:
    proc = served["proc"]
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=15) == 0
    assert "glasscast 已退出" in served["stderr"].read_text()


def test_log_timestamps_have_milliseconds() -> None:
    """访问日志时间戳带毫秒：核对流畅模式换图间隔（4 档约 630～720 ms）要用（research.md §9.20）。"""
    import logging
    import re

    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        cli.configure_logging("info")
        formatter = root.handlers[0].formatter
        assert formatter is not None
        record = logging.LogRecord("glasscast.access", logging.INFO, __file__, 1, "GET /s/…/screen.jpg", None, None)
        line = formatter.format(record)
        assert re.match(r"^\d\d:\d\d:\d\d\.\d{3} INFO\s+glasscast\.access: GET ", line), line
    finally:
        for h in root.handlers[:]:
            root.removeHandler(h)
        for h in saved_handlers:
            root.addHandler(h)
        root.setLevel(saved_level)


# =========================================================================== set-fps / config（不重启调档位）


def _mcp_call(client: httpx.Client, token: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    r = client.post("/mcp", json=body, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["result"]["structuredContent"]


def test_set_fps_and_config_against_real_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """真实启动 serve（--autostart fast），用 set-fps / config 子命令改档位并核对 MCP、status.json（实现指南 §8 风格）。"""
    with serve_process(tmp_path, "fast") as info:
        base, key, token = info["base"], info["key"], info["token"]
        port = base.rsplit(":", 1)[1]

        # 没给 token / token 不对：401 → 可读的中文提示，退出码 1
        assert cli.main(["config", "--port", port]) == 1
        assert "缺少 Token" in capsys.readouterr().err
        assert cli.main(["set-fps", "2", "--port", port, "--token", "wrong-token-0123456789"]) == 1
        err = capsys.readouterr().err
        assert "Token 不对" in err and "401" in err

        assert cli.main(["config", "--port", port, "--token", token, "--json"]) == 0
        config = json.loads(capsys.readouterr().out)
        assert (config["mode"], config["fast_fps"], config["frame_duration_ms"]) == ("fast", 4.0, 125)
        assert config["device_fps"] == 1.5
        assert config["canvas"] == list(CANVAS) and config["color_mode"] == "gray"

        # token 来自环境变量 GLASSCAST_TOKEN
        monkeypatch.setenv("GLASSCAST_TOKEN", token)
        assert cli.main(["set-fps", "2", "--port", port]) == 0
        out = capsys.readouterr().out
        assert "流畅模式档位：4 → 2（真机约每秒 1.5 → 1 帧，cast-emit 页面 hops 12 → 25），当前流畅模式" in out
        assert "新的 hops" in out

        with httpx.Client(base_url=base, timeout=5.0) as client:
            status = client.get(f"/s/{key}/status.json").json()
            assert (status["fast_fps"], status["fast_frame_duration_ms"]) == (2.0, 250)
            status_result = _mcp_call(client, token, "cast_status", {})
            assert (status_result["fast_fps"], status_result["device_fps"]) == (2.0, 1.0)
            # 不带 fps：用电脑上刚改的 2 → 250；带 fps=6 → 83
            assert _mcp_call(client, token, "start_cast", {"mode": "fast"})["frame_duration_ms"] == 250
            assert _mcp_call(client, token, "start_cast", {"mode": "fast", "fps": 6})["frame_duration_ms"] == 83

        assert cli.main(["config", "--port", port]) == 0
        out = capsys.readouterr().out
        assert "流畅档位" in out and "640×290" in out
        assert "6 档（真机约每秒 1.5 帧），frame_duration_ms=83（启动时 4；可调 1～10：glasscast set-fps N）" in out

        # 越界在本地就拒绝（与服务端同一个规则），argparse 退出码 2
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["set-fps", "11", "--port", port])
        assert excinfo.value.code == 2
        assert "1～10" in capsys.readouterr().err

    # 服务停了：连不上 → 中文提示，退出码 1
    assert cli.main(["config", "--port", port]) == 1
    assert "连不上 glasscast" in capsys.readouterr().err


def test_admin_cli_with_mock_transport(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """不起服务：用 httpx.MockTransport 覆盖地址/端口/token 的来源、服务端 400 / 404 / 500 和成功输出。"""
    seen: list[httpx.Request] = []
    replies: dict[str, httpx.Response] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return replies[request.url.path]

    monkeypatch.setattr(
        cli, "admin_client", lambda base: httpx.Client(base_url=base, transport=httpx.MockTransport(handler))
    )
    monkeypatch.setenv("GLASSCAST_HOST", "0.0.0.0")  # serve 监听全部地址时改连本机回环
    monkeypatch.setenv("GLASSCAST_PORT", "9123")
    monkeypatch.setenv("GLASSCAST_TOKEN", "env-token")

    replies["/admin/fps"] = httpx.Response(400, json={"success": False, "message": "服务端说不行"})
    assert cli.main(["set-fps", "5"]) == 2
    assert "服务端说不行" in capsys.readouterr().err
    assert str(seen[-1].url) == "http://127.0.0.1:9123/admin/fps"
    assert seen[-1].headers["authorization"] == "Bearer env-token"
    assert json.loads(seen[-1].content) == {"fps": 5.0}

    replies["/admin/fps"] = httpx.Response(
        200,
        json={
            "success": True,
            "mode": "image",
            "fast_fps": 5.0,
            "device_fps": 1.5,
            "frame_duration_ms": 100,
            "previous_fast_fps": 4.0,
            "previous_device_fps": 1.5,
            "previous_frame_duration_ms": 125,
            "hops": 10,
            "previous_hops": 12,
            "warnings": ["提醒一下"],
            "message": "下次切到流畅模式时按这个档位投屏",
        },
    )
    assert cli.main(["set-fps", "5", "--host", "localhost", "--port", "9200", "--token", "cli-token"]) == 0
    out = capsys.readouterr().out
    assert "流畅模式档位：4 → 5（真机约每秒 1.5 → 1.5 帧，cast-emit 页面 hops 12 → 10），当前普通模式" in out
    assert "下次切到流畅模式时按这个档位投屏" in out and "注意：提醒一下" in out
    assert str(seen[-1].url) == "http://localhost:9200/admin/fps"
    assert seen[-1].headers["authorization"] == "Bearer cli-token"  # 命令行优先于环境变量

    replies["/admin/config"] = httpx.Response(404, text="Not Found")
    assert cli.main(["config"]) == 1
    assert "404" in capsys.readouterr().err
    replies["/admin/config"] = httpx.Response(500, text="Internal Server Error")
    assert cli.main(["config"]) == 1
    assert "HTTP 500" in capsys.readouterr().err
    replies["/admin/config"] = httpx.Response(200, json={"success": True})  # 字段缺失也不崩
    assert cli.main(["config"]) == 0
    assert "?" in capsys.readouterr().out

    monkeypatch.setenv("GLASSCAST_PORT", "abc")
    assert cli.main(["config"]) == 2
    assert "GLASSCAST_PORT" in capsys.readouterr().err


def test_emit_hop_ms_range() -> None:
    from glasscast.config import ConfigError, load_settings

    assert load_settings({"public_url": "https://x.example", "emit_hop_ms": 33}, {}).emit_hop_ms == 33.0
    for bad in (1, 1000):
        with pytest.raises(ConfigError, match="emit_hop_ms"):
            load_settings({"public_url": "https://x.example", "emit_hop_ms": bad}, {})
