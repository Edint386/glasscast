"""glasscast.pipeline 的测试。

等待一律用"条件 + 超时"：:meth:`CastPipeline.wait_for_snapshot` / ``wait_for_clip``（条件变量）
或 :func:`wait_until` 轮询，不靠 sleep 猜时间。需要精确控制采集节奏的用 :class:`StepSource`：
每次 grab 都要测试代码发一个"许可"才返回。

流水线默认输出灰度（research.md §9.2 E6）。按"第 n 帧是纯色 (n, 0, 0)"核对帧来源和顺序的测试
（:func:`_red`）显式用 ``color_mode="rgb"``，否则红色通道会被折算成亮度。clip 模式只是内部实验
（research.md §2.4、§9.3），这里的 clip 测试保留，用来守住内部代码。
"""

from __future__ import annotations

import io
import json
import logging
import random
import threading
import time
from collections.abc import Callable, Iterator

import pytest
from PIL import Image

import glasscast.pipeline as pipeline_module
from glasscast.frames import CANVAS, StaticSource, SyntheticSource, encode_png
from glasscast.fset import build_fset, inspect_fset, parse_fset
from glasscast.pipeline import (
    CLIP_COLOR_LADDER,
    CLIP_RELAX_AFTER_SEGMENTS,
    DEFAULT_FAST_FPS,
    FAST_FPS_MAX,
    FAST_FPS_MIN,
    IMAGE_FRAME_DURATION_MS,
    MAX_FRAME_DURATION_MS,
    MIN_FRAME_DURATION_MS,
    PUBLIC_MODES,
    SNAPSHOT_MODES,
    CastPipeline,
    Clip,
    FastFpsError,
    Snapshot,
    fast_fps_warnings,
    fast_frame_duration_for,
    frame_duration_for,
    parse_fast_fps,
)

TIMEOUT = 10.0


def wait_until(predicate: Callable[[], bool], timeout: float = TIMEOUT, interval: float = 0.002) -> None:
    """轮询直到条件成立，超时则让测试失败。"""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            pytest.fail(f"等待条件超时（{timeout}s）")
        time.sleep(interval)


class StepSource:
    """受控帧源：每次 grab 先登记 ``entered``，再等测试 :meth:`release` 一个许可。

    第 n 次成功返回的帧是纯色 (n, 0, 0)，便于从段里核对帧的来源和顺序。
    """

    def __init__(self) -> None:
        self._sem = threading.Semaphore(0)
        self._lock = threading.Lock()
        self._unblocked = False
        self.entered = 0
        self.returned = 0

    def grab(self) -> Image.Image:
        with self._lock:
            self.entered += 1
        if not self._unblocked and not self._sem.acquire(timeout=TIMEOUT):
            raise TimeoutError("测试没有发放许可")
        with self._lock:
            self.returned += 1
            n = self.returned
        return Image.new("RGB", CANVAS, (n % 256, 0, 0))

    def release(self, n: int = 1) -> None:
        for _ in range(n):
            self._sem.release()

    def unblock(self) -> None:
        self._unblocked = True
        self.release(1000)


class FlakySource:
    """前 ``failures`` 次 grab 失败（抛异常或返回垃圾），之后委托给合成源。"""

    def __init__(self, failures: int, *, garbage: bool = False) -> None:
        self.failures = failures
        self.garbage = garbage
        self.calls = 0
        self._inner = SyntheticSource()

    def grab(self) -> Image.Image:
        self.calls += 1
        if self.calls <= self.failures:
            if self.garbage:
                return None  # type: ignore[return-value]
            raise RuntimeError(f"boom #{self.calls}")
        return self._inner.grab()


class AlwaysFailing:
    def __init__(self) -> None:
        self.calls = 0
        self.closed = 0

    def grab(self) -> Image.Image:
        self.calls += 1
        raise OSError("屏幕录制权限被拒绝（模拟）")

    def close(self) -> None:
        self.closed += 1


@pytest.fixture
def make_pipeline() -> Iterator[Callable[..., CastPipeline]]:
    """创建流水线并在测试结束时可靠回收（先放行受控帧源，再 close）。"""
    created: list[CastPipeline] = []
    sources: list[object] = []

    def factory(source: object, **kwargs: object) -> CastPipeline:
        pipeline = CastPipeline(source, **kwargs)  # type: ignore[arg-type]
        created.append(pipeline)
        sources.append(source)
        return pipeline

    yield factory
    for source in sources:
        if isinstance(source, StepSource):
            source.unblock()
    for pipeline in created:
        pipeline.close(timeout=5)
        assert not pipeline.running


def _red(blob: bytes) -> int:
    with Image.open(io.BytesIO(blob)) as im:
        return im.convert("RGB").getpixel((0, 0))[0]  # type: ignore[index]


def _mode_of(blob: bytes) -> str:
    with Image.open(io.BytesIO(blob)) as im:
        return im.mode


# ----------------------------------------------------------------- image 模式


def test_image_mode_publishes_jpeg_snapshots(make_pipeline: Callable[..., CastPipeline]) -> None:
    p = make_pipeline(SyntheticSource(), image_fps=200)
    assert p.latest_snapshot() is None
    p.start("image")
    assert p.mode == "image"
    first = p.wait_for_snapshot(0, TIMEOUT)
    assert isinstance(first, Snapshot)
    second = p.wait_for_snapshot(first.seq, TIMEOUT)
    assert second is not None and second.seq > first.seq and second.ts >= first.ts
    assert second.kind == "jpeg" and (second.width, second.height) == CANVAS
    with Image.open(io.BytesIO(second.data)) as im:
        assert im.format == "JPEG" and im.size == CANVAS
        assert im.mode == "L"  # 默认灰度（research.md §9.2 E6）
    assert p.latest_snapshot() is not None
    assert p.stats()["frames_captured"] >= 2


def test_image_mode_jpeg_target_bytes(make_pipeline: Callable[..., CastPipeline]) -> None:
    p = make_pipeline(SyntheticSource(), image_fps=200, jpeg_quality=80, jpeg_target_bytes=4000, skip_unchanged=False)
    p.start("image")
    snap = p.wait_for_snapshot(0, TIMEOUT)
    assert snap is not None and len(snap.data) <= 4000
    stats = p.stats()
    assert stats["jpeg_target_bytes"] == 4000
    assert 10 <= stats["last_jpeg_quality"] <= 80
    with pytest.raises(ValueError):
        CastPipeline(SyntheticSource(), jpeg_target_bytes=-1)


def test_image_mode_crop_uses_canvas(make_pipeline: Callable[..., CastPipeline]) -> None:
    src = StaticSource(Image.new("RGB", (100, 1000), (255, 255, 255)))
    p = make_pipeline(src, image_fps=200, fit_mode="crop", canvas=(320, 145))
    p.start("image")
    snap = p.wait_for_snapshot(0, TIMEOUT)
    assert snap is not None and (snap.width, snap.height) == (320, 145)
    with Image.open(io.BytesIO(snap.data)) as im:
        assert im.size == (320, 145)
        assert im.convert("L").getextrema()[0] > 200  # crop 没有黑边


def test_skip_unchanged_reuses_jpeg_but_bumps_seq(make_pipeline: Callable[..., CastPipeline]) -> None:
    p = make_pipeline(StaticSource(SyntheticSource().grab()), image_fps=200)
    p.start("image")
    s1 = p.wait_for_snapshot(0, TIMEOUT)
    assert s1 is not None
    s2 = p.wait_for_snapshot(s1.seq, TIMEOUT)
    assert s2 is not None
    s3 = p.wait_for_snapshot(s2.seq, TIMEOUT)
    assert s3 is not None
    assert s1.seq < s2.seq < s3.seq
    assert s1.data is s2.data is s3.data  # 同一份编码结果
    stats = p.stats()
    assert stats["reused_frames"] >= 2
    assert stats["frames_captured"] - stats["reused_frames"] == 1  # 只编码过一次


def test_skip_unchanged_disabled_reencodes(make_pipeline: Callable[..., CastPipeline]) -> None:
    p = make_pipeline(StaticSource(SyntheticSource().grab()), image_fps=200, skip_unchanged=False)
    p.start("image")
    s1 = p.wait_for_snapshot(0, TIMEOUT)
    assert s1 is not None
    s2 = p.wait_for_snapshot(s1.seq, TIMEOUT)
    assert s2 is not None
    assert s1.data == s2.data and s1.data is not s2.data
    assert p.stats()["reused_frames"] == 0


# ----------------------------------------------------------------- clip 模式


@pytest.mark.parametrize(("color_mode", "frame_mode"), [("gray", "L"), ("rgb", "RGBA")])
def test_clip_mode_builds_parseable_fset(
    make_pipeline: Callable[..., CastPipeline], color_mode: str, frame_mode: str
) -> None:
    p = make_pipeline(SyntheticSource(), clip_fps=200, clip_frames=4, color_mode=color_mode)
    p.start("clip")
    clip = p.wait_for_clip(0, TIMEOUT)
    assert isinstance(clip, Clip)
    assert clip.frames == 4 and clip.frame_duration_ms == 5 and clip.kind == "png"
    assert (clip.width, clip.height) == CANVAS
    frames = parse_fset(clip.data)
    assert len(frames) == 4
    info = inspect_fset(clip.data)
    assert info["formats"] == ["png"] * 4
    assert info["png_dims"] == [CANVAS] * 4
    with Image.open(io.BytesIO(frames[0])) as im:
        # gray：8 位灰度 PNG（research.md §9.2 E6）；rgb：与官方样本一致的 RGBA（research.md §4）
        assert im.mode == frame_mode
    nxt = p.wait_for_clip(clip.seq, TIMEOUT)
    assert nxt is not None and nxt.seq > clip.seq
    latest = p.latest_clip()
    assert latest is not None and latest.seq >= nxt.seq


def test_clip_jpeg_frames(make_pipeline: Callable[..., CastPipeline]) -> None:
    p = make_pipeline(SyntheticSource(), clip_fps=200, clip_frames=3, clip_format="jpeg")
    p.start("clip")
    clip = p.wait_for_clip(0, TIMEOUT)
    assert clip is not None and clip.kind == "jpeg"
    assert inspect_fset(clip.data)["formats"] == ["jpeg"] * 3


def test_clip_png_rgb_option(make_pipeline: Callable[..., CastPipeline]) -> None:
    p = make_pipeline(SyntheticSource(), clip_fps=200, clip_frames=2, clip_png_alpha=False, color_mode="rgb")
    p.start("clip")
    clip = p.wait_for_clip(0, TIMEOUT)
    assert clip is not None
    with Image.open(io.BytesIO(parse_fset(clip.data)[0])) as im:
        assert im.mode == "RGB"


def test_only_latest_clip_is_kept_in_capture_order(make_pipeline: Callable[..., CastPipeline]) -> None:
    src = StepSource()
    p = make_pipeline(src, clip_fps=1000, clip_frames=2, color_mode="rgb")
    p.start("clip")
    src.release(6)
    wait_until(lambda: p.stats()["clips_published"] == 3)
    clip = p.latest_clip()
    assert clip is not None
    # 只保留最新一段：第 5、6 帧；前两段已被覆盖
    assert [_red(f) for f in parse_fset(clip.data)] == [5, 6]
    assert p.stats()["clip_seq"] == clip.seq


def test_clip_with_static_source_still_fixed_length(make_pipeline: Callable[..., CastPipeline]) -> None:
    p = make_pipeline(StaticSource(SyntheticSource().grab()), clip_fps=200, clip_frames=3)
    p.start("clip")
    clip = p.wait_for_clip(0, TIMEOUT)
    assert clip is not None
    frames = parse_fset(clip.data)
    assert len(frames) == 3 and frames[0] == frames[1] == frames[2]
    assert p.stats()["reused_frames"] >= 2


def test_frame_duration() -> None:
    assert frame_duration_for(5) == 200
    assert frame_duration_for(3) == 333
    assert frame_duration_for(24) == 42
    assert frame_duration_for(0.5) == MAX_FRAME_DURATION_MS == 1000  # Waft-DSL 上限
    assert frame_duration_for(5000) == 1
    for bad in (0, -1, float("inf"), float("nan")):
        with pytest.raises(ValueError):
            frame_duration_for(bad)
    p = CastPipeline(SyntheticSource(), clip_fps=0.25)
    assert p.frame_duration_ms == 1000
    p.close()


# ----------------------------------------------------------------- 模式切换 / 停止


def test_stop_halts_updates(make_pipeline: Callable[..., CastPipeline]) -> None:
    p = make_pipeline(SyntheticSource(), image_fps=200)
    p.start("image")
    assert p.wait_for_snapshot(0, TIMEOUT) is not None
    p.stop()
    p.stop()  # 幂等
    after = p.stats()
    assert after["mode"] == "off" and after["running"]  # 线程常驻
    # stop 返回后不应再有任何发布：在 0.3 s 内等新 snapshot 必然等不到
    assert p.wait_for_snapshot(after["seq"], timeout=0.3) is None
    assert p.stats()["frames_captured"] == after["frames_captured"]
    assert p.latest_snapshot() is not None  # 默认保留最后一帧


def test_stop_discards_inflight_frame(make_pipeline: Callable[..., CastPipeline]) -> None:
    src = StepSource()
    p = make_pipeline(src, image_fps=1000)
    p.start("image")
    wait_until(lambda: src.entered == 1)  # 线程正卡在 grab 里
    p.stop()
    src.release()
    wait_until(lambda: p.stats()["discarded_frames"] == 1)
    assert p.latest_snapshot() is None
    assert p.stats()["seq"] == 0


def test_stop_clear_and_clear(make_pipeline: Callable[..., CastPipeline]) -> None:
    p = make_pipeline(SyntheticSource(), image_fps=200, clip_fps=200, clip_frames=2)
    p.start("image")
    assert p.wait_for_snapshot(0, TIMEOUT) is not None
    p.stop(clear=True)
    assert p.latest_snapshot() is None
    p.start("clip")
    assert p.wait_for_clip(0, TIMEOUT) is not None
    p.clear()
    assert p.latest_clip() is None


def test_mode_switch_clears_clip_buffer(make_pipeline: Callable[..., CastPipeline]) -> None:
    src = StepSource()
    p = make_pipeline(src, image_fps=1000, clip_fps=1000, clip_frames=5, color_mode="rgb")
    p.start("clip")
    src.release(3)
    wait_until(lambda: p.stats()["clip_buffered"] == 3)
    wait_until(lambda: src.entered == 4)  # 线程已卡在第 4 次 grab（属于 clip 这一轮）

    p.start("image")
    assert p.stats()["clip_buffered"] == 0  # 切换即清空
    src.release()  # 第 4 帧是切换前开始的，必须被丢弃
    wait_until(lambda: p.stats()["discarded_frames"] == 1)
    wait_until(lambda: src.entered == 5)
    src.release()  # 第 5 帧属于 image 模式
    snap = p.wait_for_snapshot(0, TIMEOUT)
    assert snap is not None and abs(_red(snap.data) - 5) <= 3  # JPEG 有损，粗查
    wait_until(lambda: src.entered == 6)  # 线程又卡在下一次 grab（image 这一轮）

    p.start("clip")
    src.release()  # 第 6 帧同样被丢弃
    wait_until(lambda: p.stats()["discarded_frames"] == 2)
    src.release(4)
    wait_until(lambda: p.stats()["clip_buffered"] == 4)
    assert p.latest_clip() is None  # 第一轮攒的 3 帧没有被发布，也没有混进来
    src.release()
    clip = p.wait_for_clip(0, TIMEOUT)
    assert clip is not None
    assert [_red(f) for f in parse_fset(clip.data)] == [7, 8, 9, 10, 11]


def test_start_same_mode_keeps_buffer(make_pipeline: Callable[..., CastPipeline]) -> None:
    src = StepSource()
    p = make_pipeline(src, clip_fps=1000, clip_frames=4, color_mode="rgb")
    p.start("clip")
    src.release(2)
    wait_until(lambda: p.stats()["clip_buffered"] == 2)
    p.start("clip")  # 重复 start 不打断
    assert p.stats()["clip_buffered"] == 2
    src.release(2)
    clip = p.wait_for_clip(0, TIMEOUT)
    assert clip is not None
    assert [_red(f) for f in parse_fset(clip.data)] == [1, 2, 3, 4]


def test_entering_mode_drops_stale_output(make_pipeline: Callable[..., CastPipeline]) -> None:
    src = StepSource()
    p = make_pipeline(src, image_fps=1000, clip_fps=1000, clip_frames=2)
    p.start("clip")
    src.release(2)
    old_clip = p.wait_for_clip(0, TIMEOUT)
    assert old_clip is not None
    p.stop()
    assert p.latest_clip() is old_clip  # stop 默认保留

    p.start("image")
    assert p.latest_clip() is old_clip  # 进入 image 只影响 snapshot
    src.release(2)  # 第 3 帧（clip 轮，被丢弃）+ 第 4 帧（image）
    old_snap = p.wait_for_snapshot(0, TIMEOUT)
    assert old_snap is not None

    p.start("clip")
    assert p.latest_clip() is None  # 上一轮的旧段不再当"最新"
    assert p.latest_snapshot() is old_snap
    src.unblock()
    new_clip = p.wait_for_clip(0, TIMEOUT)
    assert new_clip is not None and new_clip.seq > old_snap.seq
    p.start("image")
    assert p.latest_snapshot() is None
    assert p.wait_for_snapshot(0, TIMEOUT) is not None


def test_start_off_is_stop(make_pipeline: Callable[..., CastPipeline]) -> None:
    p = make_pipeline(SyntheticSource(), image_fps=200)
    p.start("image")
    p.start("off")
    assert p.mode == "off"


def test_concurrent_mode_switching_is_safe(make_pipeline: Callable[..., CastPipeline]) -> None:
    p = make_pipeline(SyntheticSource((64, 32)), canvas=(64, 32), image_fps=500, clip_fps=500, clip_frames=2)
    rng = random.Random(0)

    def toggler(seed: int) -> None:
        local = random.Random(seed)
        for _ in range(100):
            choice = local.choice(["image", "fast", "clip", "off"])
            if choice == "off":
                p.stop()
            else:
                p.start(choice)  # type: ignore[arg-type]
            p.stats()
            p.latest_snapshot()
            p.latest_clip()

    threads = [threading.Thread(target=toggler, args=(rng.random(),)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(TIMEOUT)
        assert not t.is_alive()
    stats = p.stats()
    assert stats["running"] and stats["errors"] == 0
    p.start("clip")
    seq = stats["seq"]
    clip = p.wait_for_clip(seq, TIMEOUT)
    assert clip is not None and len(parse_fset(clip.data)) == 2


# ----------------------------------------------------------------- 健壮性


def test_exceptions_are_caught_and_retried(make_pipeline: Callable[..., CastPipeline]) -> None:
    src = FlakySource(3)
    p = make_pipeline(src, image_fps=200, error_backoff_s=0.001, error_backoff_max_s=0.002)
    p.start("image")
    snap = p.wait_for_snapshot(0, TIMEOUT)
    assert snap is not None
    stats = p.stats()
    assert stats["errors"] == 3
    assert stats["consecutive_errors"] == 0
    assert "boom #3" in stats["last_error"]
    assert stats["last_error_age_s"] is not None
    assert stats["running"]


def test_garbage_frames_are_errors_not_crashes(make_pipeline: Callable[..., CastPipeline]) -> None:
    p = make_pipeline(FlakySource(2, garbage=True), clip_fps=200, clip_frames=2, error_backoff_s=0.001)
    p.start("clip")
    assert p.wait_for_clip(0, TIMEOUT) is not None
    assert p.stats()["errors"] == 2


def test_always_failing_source_keeps_thread_alive(
    make_pipeline: Callable[..., CastPipeline], caplog: pytest.LogCaptureFixture
) -> None:
    src = AlwaysFailing()
    p = make_pipeline(src, image_fps=200, error_backoff_s=0.001, error_backoff_max_s=0.001, close_source=True)
    with caplog.at_level(logging.WARNING, logger="glasscast.pipeline"):
        p.start("image")
        wait_until(lambda: p.stats()["errors"] >= 5)
    stats = p.stats()
    assert stats["running"] and stats["consecutive_errors"] >= 5
    assert stats["last_frame_age_s"] is None and p.latest_snapshot() is None
    assert "权限" in stats["last_error"]
    # 日志限流：一轮连续失败只打印一次 WARNING（含堆栈）
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "采集失败" in r.getMessage()]
    assert len(warnings) == 1
    p.stop()
    p.close()
    assert src.closed == 1  # close_source=True


class ScriptedSource:
    """按脚本截帧：``ok`` 出一帧、``fail`` 抛异常；脚本用完后每次 grab 都阻塞到 :attr:`gate` 打开（模拟很慢的帧）。

    只有采集线程调用 grab，不需要加锁。
    """

    def __init__(self, script: list[str]) -> None:
        self.script = list(script)
        self.gate = threading.Event()
        self.calls = 0

    def grab(self) -> Image.Image:
        self.calls += 1
        step = self.script.pop(0) if self.script else "block"
        if step == "fail":
            raise OSError("模拟截屏失败")
        if step == "block" and not self.gate.wait(TIMEOUT):
            raise TimeoutError("测试没有放行")
        return Image.new("RGB", CANVAS, (self.calls % 256, 0, 0))


def test_restart_after_stop_resets_per_run_stats(make_pipeline: Callable[..., CastPipeline]) -> None:
    """stop 之后重新 start 是新的一轮：上一轮的 last_frame_age_s、consecutive_errors、capture_fps 不能带过来，
    否则第一帧还没出来时 cast_status 会按旧值报"画面已经 N 秒没更新""连续失败 N 次"。累计计数保留。"""
    src = ScriptedSource(["ok", "ok", "fail", "fail"])
    p = make_pipeline(src, image_fps=200, error_backoff_s=0.001, error_backoff_max_s=0.002)
    try:
        p.start("image")
        wait_until(lambda: p.stats()["consecutive_errors"] >= 2)
        first = p.stats()
        assert first["last_frame_age_s"] is not None and first["capture_fps"] is not None
        assert (first["frames_captured"], first["errors"]) == (2, 2)

        p.stop()
        p.start("image")  # 新一轮：帧源从这里开始一直阻塞
        restarted = p.stats()
        assert restarted["mode"] == "image"
        assert restarted["last_frame_age_s"] is None
        assert restarted["consecutive_errors"] == 0
        assert restarted["capture_fps"] is None
        assert (restarted["frames_captured"], restarted["errors"]) == (2, 2)  # 累计值不清
        assert restarted["last_error"] is not None  # 最近一次错误仍可查（cast_status 只看连续次数）

        seq = restarted["seq"]
        src.gate.set()
        assert p.wait_for_snapshot(seq, TIMEOUT) is not None
        assert p.stats()["last_frame_age_s"] is not None

        # image ↔ fast 互切不经过 off，采集没有中断：不清
        p.start("fast")
        assert p.stats()["last_frame_age_s"] is not None
    finally:
        src.gate.set()


def test_backoff_doubles_up_to_max() -> None:
    p = CastPipeline(SyntheticSource(), error_backoff_s=0.05, error_backoff_max_s=0.2)
    try:
        delays = [p._record_error(RuntimeError("x"), 0) for _ in range(5)]
        assert delays == pytest.approx([0.05, 0.1, 0.2, 0.2, 0.2])
        p._record_success()
        assert p._record_error(RuntimeError("y"), 0) == pytest.approx(0.05)
    finally:
        p.close()


def test_mode_switch_interrupts_backoff(make_pipeline: Callable[..., CastPipeline]) -> None:
    """长退避期间切换模式会立即重试，而不是等满退避时间。"""
    src = FlakySource(1)
    p = make_pipeline(src, image_fps=200, clip_fps=200, clip_frames=2, error_backoff_s=60, error_backoff_max_s=60)
    p.start("image")
    wait_until(lambda: p.stats()["errors"] == 1)
    p.start("clip")
    assert p.wait_for_clip(0, TIMEOUT) is not None


# ----------------------------------------------------------------- 生命周期


def test_close_is_idempotent_and_final(make_pipeline: Callable[..., CastPipeline]) -> None:
    p = make_pipeline(SyntheticSource(), image_fps=200)
    p.start("image")
    assert p.wait_for_snapshot(0, TIMEOUT) is not None
    p.close()
    p.close()
    assert p.closed and not p.running
    p.stop()  # 关闭后 stop 不报错
    with pytest.raises(RuntimeError):
        p.start("image")
    t0 = time.monotonic()
    assert p.wait_for_snapshot(10**9, timeout=5) is None  # 已关闭：立即返回
    assert time.monotonic() - t0 < 1


def test_close_without_start_and_context_manager() -> None:
    p = CastPipeline(SyntheticSource())
    p.close()
    assert not p.running
    with CastPipeline(SyntheticSource(), image_fps=200) as q:
        q.start("image")
        assert q.wait_for_snapshot(0, TIMEOUT) is not None
    assert q.closed and not q.running


def test_close_wakes_waiters(make_pipeline: Callable[..., CastPipeline]) -> None:
    p = make_pipeline(SyntheticSource())
    result: list[Snapshot | None] = []
    waiter = threading.Thread(target=lambda: result.append(p.wait_for_snapshot(0, TIMEOUT)))
    waiter.start()
    p.close()
    waiter.join(TIMEOUT)
    assert not waiter.is_alive() and result == [None]


def test_invalid_arguments() -> None:
    src = SyntheticSource()
    bad_kwargs: list[dict[str, object]] = [
        {"canvas": (0, 290)},
        {"fit_mode": "stretch"},
        {"image_fps": 0},
        {"fast_fps": 0},
        {"fast_fps": float("nan")},
        {"color_mode": "cmyk"},
        {"clip_fps": -1},
        {"jpeg_quality": 0},
        {"jpeg_subsampling": 5},
        {"png_compress_level": 10},
        {"clip_frames": 0},
        {"clip_format": "gif"},
        {"error_backoff_s": 2, "error_backoff_max_s": 1},
    ]
    for kwargs in bad_kwargs:
        with pytest.raises(ValueError):
            CastPipeline(src, **kwargs)  # type: ignore[arg-type]
    p = CastPipeline(src)
    with pytest.raises(ValueError):
        p.start("video")  # type: ignore[arg-type]
    p.close()


# ----------------------------------------------------------------- 状态 / 占位


REQUIRED_STATS = {
    "mode",
    "running",
    "closed",
    "seq",
    "snapshot_seq",
    "clip_seq",
    "frames_captured",
    "snapshots_published",
    "clips_published",
    "reused_frames",
    "discarded_frames",
    "capture_ms_avg",
    "encode_ms_avg",
    "capture_fps",
    "last_frame_age_s",
    "last_snapshot_bytes",
    "last_clip_bytes",
    "errors",
    "consecutive_errors",
    "last_error",
    "last_error_age_s",
    "canvas",
    "fit_mode",
    "color_mode",
    "image_fps",
    "fast_fps",
    "fast_frame_duration_ms",
    "jpeg_quality",
    "skip_unchanged",
    "clip_fps",
    "clip_frames",
    "clip_format",
    "frame_duration_ms",
    "clip_buffered",
    "budget_warnings",
    "clips_over_budget",
    "clip_quantized_colors",
}


def test_stats_fields_before_and_after_running(make_pipeline: Callable[..., CastPipeline]) -> None:
    p = make_pipeline(SyntheticSource(), image_fps=200, clip_fps=200, clip_frames=2)
    idle = p.stats()
    assert REQUIRED_STATS <= idle.keys()
    assert idle["mode"] == "off" and idle["seq"] == 0 and not idle["running"]
    assert idle["last_frame_age_s"] is None and idle["encode_ms_avg"] is None
    json.dumps(idle)

    p.start("image")
    snap = p.wait_for_snapshot(0, TIMEOUT)
    assert snap is not None
    p.start("clip")
    clip = p.wait_for_clip(snap.seq, TIMEOUT)
    assert clip is not None
    stats = p.stats()
    assert REQUIRED_STATS <= stats.keys()
    json.dumps(stats)
    assert stats["mode"] == "clip"
    assert stats["seq"] >= clip.seq > snap.seq  # snapshot 与 clip 共用 seq 计数器
    assert stats["snapshots_published"] >= 1 and stats["clips_published"] >= 1
    assert stats["encode_ms_avg"] > 0 and stats["capture_ms_avg"] >= 0
    assert stats["last_frame_age_s"] >= 0
    assert stats["canvas"] == list(CANVAS)
    assert stats["frame_duration_ms"] == 5 and stats["clip_frames"] == 2
    assert stats["color_mode"] == "gray"
    assert stats["fast_fps"] == DEFAULT_FAST_FPS and stats["fast_frame_duration_ms"] == 125
    assert stats["budget_warnings"] == []


def test_budget_warnings_surface_in_stats(
    make_pipeline: Callable[..., CastPipeline], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="glasscast.pipeline"):
        p = make_pipeline(SyntheticSource((800, 600)), canvas=(800, 600), image_fps=200)
    assert any("画布" in r.getMessage() for r in caplog.records)
    p.start("image")
    assert p.wait_for_snapshot(0, TIMEOUT) is not None
    assert p.stats()["budget_warnings"]


def test_canvas_budget_warning_is_per_mode(caplog: pytest.LogCaptureFixture) -> None:
    """对外的 image/fast 都是 JPEG：640×386 在 JPEG 预算内（research.md §2.3），启动时不告警；
    clip（PNG/ARGB8888）只是内部实验，不在构造时吓唬用户。800×600 则对 JPEG 也超预算。"""
    with caplog.at_level(logging.WARNING, logger="glasscast.pipeline"):
        CastPipeline(SyntheticSource((640, 386)), canvas=(640, 386)).close()
    assert not [r for r in caplog.records if "画布" in r.getMessage()]
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="glasscast.pipeline"):
        CastPipeline(SyntheticSource((800, 600)), canvas=(800, 600)).close()
    messages = [r.getMessage() for r in caplog.records if "画布" in r.getMessage()]
    assert messages and all("image/fast 模式（JPEG）" in m for m in messages)


# --------------------------------------------------------------------------- clip 段超预算时的量化降级

_NOISE_SIZE = (160, 72)  # 小画布：测试快；预算阈值用 monkeypatch 按实际大小设定


def _noise_image(seed: int = 7) -> Image.Image:
    """彩色噪声：全彩 PNG 很大，量化到 16 色明显变小（模拟满屏密集彩色文字）。"""
    rnd = random.Random(seed)
    data = bytes(rnd.getrandbits(8) for _ in range(_NOISE_SIZE[0] * _NOISE_SIZE[1] * 3))
    return Image.frombytes("RGB", _NOISE_SIZE, data)


def _segment_size(im: Image.Image, frames: int, colors: int | None) -> int:
    src = im if colors is None else im.quantize(colors=colors, method=Image.Quantize.FASTOCTREE)
    return len(build_fset([encode_png(src, 3, alpha=True)] * frames))


def _unique_colors(blob: bytes) -> int:
    with Image.open(io.BytesIO(blob)) as im:
        assert im.mode == "RGBA"  # 量化后仍是官方样本那种 8-bit RGBA PNG（research.md §4）
        return len(im.getcolors(maxcolors=1 << 16) or [])


def test_clip_over_budget_is_quantized(make_pipeline: Callable[..., CastPipeline], monkeypatch: pytest.MonkeyPatch) -> None:
    im = _noise_image()
    full, q64, q16 = (_segment_size(im, 3, c) for c in CLIP_COLOR_LADDER)
    assert q16 < q64 < full  # 前提：量化确实能降体积
    monkeypatch.setattr(pipeline_module, "FILE_BUDGET_BYTES", (q16 + q64) // 2)  # 只有 16 色放得下

    p = make_pipeline(StaticSource(im), canvas=_NOISE_SIZE, clip_fps=200, clip_frames=3, color_mode="rgb")
    p.start("clip")
    clip = p.wait_for_clip(0, TIMEOUT)
    assert clip is not None
    assert len(clip.data) <= pipeline_module.FILE_BUDGET_BYTES
    frames = parse_fset(clip.data)
    assert len(frames) == 3
    assert all(_unique_colors(f) <= 16 for f in frames)
    stats = p.stats()
    assert stats["clip_quantized_colors"] == 16
    assert stats["clips_over_budget"] == 0
    # 粘滞：后续段直接按 16 色编码，不用每段都先全彩编码再重来
    nxt = p.wait_for_clip(clip.seq, TIMEOUT)
    assert nxt is not None and all(_unique_colors(f) <= 16 for f in parse_fset(nxt.data))


def test_clip_still_over_budget_is_dropped(
    make_pipeline: Callable[..., CastPipeline], monkeypatch: pytest.MonkeyPatch
) -> None:
    im = _noise_image()
    q16 = _segment_size(im, 3, 16)
    monkeypatch.setattr(pipeline_module, "FILE_BUDGET_BYTES", q16 // 2)  # 量化到底也放不下

    p = make_pipeline(StaticSource(im), canvas=_NOISE_SIZE, clip_fps=200, clip_frames=3, color_mode="rgb")
    p.start("clip")
    wait_until(lambda: p.stats()["clips_over_budget"] >= 2)
    stats = p.stats()
    assert p.latest_clip() is None  # 不发布超预算的段；服务端继续回占位
    assert stats["clips_published"] == 0
    assert stats["clip_quantized_colors"] == CLIP_COLOR_LADDER[-1]
    assert any("已丢弃该段" in w for w in stats["budget_warnings"])


def test_clip_within_budget_is_not_quantized(make_pipeline: Callable[..., CastPipeline]) -> None:
    p = make_pipeline(SyntheticSource(), clip_fps=200, clip_frames=2)
    p.start("clip")
    assert p.wait_for_clip(0, TIMEOUT) is not None
    stats = p.stats()
    assert stats["clip_quantized_colors"] is None and stats["clips_over_budget"] == 0


def test_clip_quantize_level_relaxes_after_streak() -> None:
    """在量化档位上连续 CLIP_RELAX_AFTER_SEGMENTS 段都在预算内就退回上一档；升档立即生效。"""
    p = CastPipeline(SyntheticSource())
    try:
        p._update_clip_colors(None, 64, True)  # 超预算 → 升到 64 色
        assert p._clip_colors == 64
        for _ in range(CLIP_RELAX_AFTER_SEGMENTS - 1):
            p._update_clip_colors(64, 64, True)
            assert p._clip_colors == 64
        p._update_clip_colors(64, 64, True)
        assert p._clip_colors is None  # 退回全彩试试
        p._update_clip_colors(None, 16, False)  # 量化到底仍超：停在最低档
        assert p._clip_colors == 16
        p._update_clip_colors(16, 16, False)
        assert p._clip_colors == 16  # 不达标不累计
    finally:
        p.close()


def test_stale_generation_error_is_not_counted(make_pipeline: Callable[..., CastPipeline]) -> None:
    """模式切换前就开始的那一帧失败：不计入 errors/consecutive_errors，也不让新模式退避。"""

    class HoldThenFail:
        def __init__(self) -> None:
            self.entered = threading.Event()
            self.calls = 0

        def grab(self) -> Image.Image:
            self.calls += 1
            if self.calls == 1:
                self.entered.set()
                time.sleep(0.1)
                raise OSError("stale failure")
            return Image.new("RGB", CANVAS, (9, 9, 9))

    src = HoldThenFail()
    p = make_pipeline(src, image_fps=50, error_backoff_s=5.0, error_backoff_max_s=5.0)
    p.start("image")
    assert src.entered.wait(TIMEOUT)
    p.start("clip")  # 切换时第一帧还卡在 grab 里
    p.start("image")  # 再切回来也一样：旧代号的失败都不算
    snap = p.wait_for_snapshot(0, 2.0)  # 若被当成新模式的失败会退避 5 s，这里就等不到
    assert snap is not None
    stats = p.stats()
    assert stats["errors"] == 0 and stats["consecutive_errors"] == 0 and stats["last_error"] is None
    assert stats["discarded_frames"] >= 1


def test_placeholders(make_pipeline: Callable[..., CastPipeline]) -> None:
    p = make_pipeline(SyntheticSource(), clip_frames=7, clip_fps=4)
    snap = p.placeholder_snapshot()
    assert snap.seq == 0 and snap.kind == "jpeg" and (snap.width, snap.height) == CANVAS
    with Image.open(io.BytesIO(snap.data)) as im:
        assert im.size == CANVAS
    assert p.placeholder_snapshot() is snap  # 缓存

    clip = p.placeholder_clip()
    assert clip.seq == 0 and clip.frames == 7 and clip.frame_duration_ms == 250
    info = inspect_fset(clip.data)
    assert info["frames"] == 7 and info["png_dims"] == [CANVAS] * 7
    assert p.placeholder_clip() is clip
    assert p.latest_snapshot() is None and p.latest_clip() is None  # 占位不算发布

    q = make_pipeline(SyntheticSource(), clip_frames=3, clip_format="jpeg", canvas=(320, 145))
    assert inspect_fset(q.placeholder_clip().data)["formats"] == ["jpeg"] * 3
    assert q.placeholder_snapshot().width == 320


# --------------------------------------------------------------------------- fast 模式（实现指南 §9.1）


def test_public_modes() -> None:
    assert PUBLIC_MODES == ("image", "fast")
    assert SNAPSHOT_MODES == {"image", "fast"}
    assert DEFAULT_FAST_FPS == 4.0
    assert IMAGE_FRAME_DURATION_MS == 1000


def test_fast_frame_duration() -> None:
    # 页面定时器 2 帧一轮，每轮拉一张图：每帧 round(1000 / fast_fps / 2)（research.md §9.2 E4、E9）
    assert fast_frame_duration_for(4) == 125
    assert fast_frame_duration_for(5) == 100
    assert fast_frame_duration_for(2) == 250
    assert fast_frame_duration_for(3) == 167
    assert fast_frame_duration_for(0.5) == MAX_FRAME_DURATION_MS  # 正好 1000
    assert fast_frame_duration_for(0.25) == MAX_FRAME_DURATION_MS  # 超过上限被截断（research.md §2.4）
    assert fast_frame_duration_for(10_000) == 1
    assert fast_frame_duration_for(4, timer_frames=4) == 62  # round(62.5) 银行家舍入
    for bad in (0, -1, float("inf"), float("nan")):
        with pytest.raises(ValueError):
            fast_frame_duration_for(bad)
    with pytest.raises(ValueError):
        fast_frame_duration_for(4, timer_frames=0)


def test_page_frame_duration_per_mode() -> None:
    p = CastPipeline(SyntheticSource(), fast_fps=5, clip_fps=4)
    try:
        assert p.fast_fps == 5.0 and p.fast_frame_duration_ms == 100
        assert p.page_frame_duration_ms("fast") == 100
        assert p.page_frame_duration_ms("image") == 1000
        assert p.page_frame_duration_ms("clip") == p.frame_duration_ms == 250
        with pytest.raises(ValueError):
            p.page_frame_duration_ms("off")
    finally:
        p.close()


def test_fast_mode_publishes_gray_jpeg_snapshots(make_pipeline: Callable[..., CastPipeline]) -> None:
    p = make_pipeline(SyntheticSource(), fast_fps=200)
    p.start("fast")
    assert p.mode == "fast"
    first = p.wait_for_snapshot(0, TIMEOUT)
    assert first is not None
    second = p.wait_for_snapshot(first.seq, TIMEOUT)
    assert second is not None and second.seq > first.seq
    assert second.kind == "jpeg" and (second.width, second.height) == CANVAS
    with Image.open(io.BytesIO(second.data)) as im:
        assert (im.format, im.mode, im.size) == ("JPEG", "L", CANVAS)
    assert p.latest_clip() is None  # fast 只产出 JPEG，不打包 .fset


def test_fast_mode_uses_fast_fps_not_image_fps(make_pipeline: Callable[..., CastPipeline]) -> None:
    """image_fps 极低、fast_fps 很高：fast 模式连续出帧，image 模式第一帧之后就停住。"""
    p = make_pipeline(SyntheticSource(), image_fps=0.01, fast_fps=200)
    p.start("fast")
    seq = 0
    for _ in range(3):
        snap = p.wait_for_snapshot(seq, TIMEOUT)
        assert snap is not None
        seq = snap.seq
    p.start("image")  # 切换后立即截一帧，之后要等 100 s
    snap = p.wait_for_snapshot(seq, TIMEOUT)
    assert snap is not None
    assert p.wait_for_snapshot(snap.seq, timeout=0.3) is None


def test_image_fast_switch_keeps_snapshot(make_pipeline: Callable[..., CastPipeline]) -> None:
    p = make_pipeline(SyntheticSource(), image_fps=200, fast_fps=200, clip_fps=200, clip_frames=2)
    p.start("image")
    snap = p.wait_for_snapshot(0, TIMEOUT)
    assert snap is not None
    p.start("fast")
    kept = p.latest_snapshot()
    assert kept is not None and kept.seq >= snap.seq  # image ↔ fast 共用 snapshot，不回"等待画面"
    assert p.wait_for_snapshot(kept.seq, TIMEOUT) is not None
    p.start("image")
    assert p.latest_snapshot() is not None

    p.stop()
    p.start("fast")
    assert p.latest_snapshot() is None  # 从 off 进入：上一轮的旧截图不再当"最新"
    assert p.wait_for_snapshot(0, TIMEOUT) is not None
    p.start("clip")
    assert p.wait_for_clip(0, TIMEOUT) is not None
    p.start("fast")
    assert p.latest_snapshot() is None  # 从 clip 进入同样丢弃


@pytest.mark.parametrize(("color_mode", "jpeg_mode"), [("gray", "L"), ("rgb", "RGB")])
def test_color_mode_controls_jpeg(
    make_pipeline: Callable[..., CastPipeline], color_mode: str, jpeg_mode: str
) -> None:
    p = make_pipeline(SyntheticSource(), image_fps=200, color_mode=color_mode)
    assert p.color_mode == color_mode
    p.start("image")
    snap = p.wait_for_snapshot(0, TIMEOUT)
    assert snap is not None and _mode_of(snap.data) == jpeg_mode
    assert _mode_of(p.placeholder_snapshot().data) == jpeg_mode  # 占位与真实画面同色彩
    assert p.stats()["color_mode"] == color_mode


def test_gray_output_is_smaller_than_rgb() -> None:
    """灰度 JPEG 明显小于彩色（research.md §9.2 E6 实测约 70%）：同一张合成画面对比。"""
    frame = SyntheticSource(clock=lambda: 1_700_000_000.0).grab()
    sizes: dict[str, int] = {}
    for color_mode in ("gray", "rgb"):
        p = CastPipeline(StaticSource(frame), color_mode=color_mode)  # type: ignore[arg-type]
        try:
            sizes[color_mode] = len(p.make_placeholder_snapshot(frame).data)
        finally:
            p.close()
    assert sizes["gray"] < sizes["rgb"]


def test_gray_placeholders(make_pipeline: Callable[..., CastPipeline]) -> None:
    p = make_pipeline(SyntheticSource(), clip_frames=3)
    assert _mode_of(p.placeholder_snapshot().data) == "L"
    frames = parse_fset(p.placeholder_clip().data)
    assert len(frames) == 3 and all(_mode_of(f) == "L" for f in frames)
    custom = p.make_placeholder_snapshot(Image.new("RGB", (100, 100), (255, 0, 0)))
    assert custom.seq == 0 and (custom.width, custom.height) == CANVAS
    with Image.open(io.BytesIO(custom.data)) as im:
        assert im.size == CANVAS and im.mode == "L"  # 任意尺寸的占位图也会 fit 到画布


def test_gray_clip_over_budget_is_quantized_to_gray(
    make_pipeline: Callable[..., CastPipeline], monkeypatch: pytest.MonkeyPatch
) -> None:
    """灰度 clip 段超预算时同样逐档量化，量化后仍是 8 位灰度 PNG（不变成调色板或 RGBA）。"""
    rnd = random.Random(3)
    gray = Image.frombytes("L", _NOISE_SIZE, bytes(rnd.getrandbits(8) for _ in range(_NOISE_SIZE[0] * _NOISE_SIZE[1])))
    full = len(build_fset([encode_png(gray, 3)] * 3))
    q16_img = gray.quantize(colors=16, method=Image.Quantize.FASTOCTREE).convert("L")
    q16 = len(build_fset([encode_png(q16_img, 3)] * 3))
    assert q16 < full
    monkeypatch.setattr(pipeline_module, "FILE_BUDGET_BYTES", (q16 + full) // 2)

    p = make_pipeline(StaticSource(gray.convert("RGB")), canvas=_NOISE_SIZE, clip_fps=200, clip_frames=3)
    p.start("clip")
    clip = p.wait_for_clip(0, TIMEOUT)
    assert clip is not None and len(clip.data) <= pipeline_module.FILE_BUDGET_BYTES
    for blob in parse_fset(clip.data):
        with Image.open(io.BytesIO(blob)) as im:
            assert im.mode == "L"
            assert len(im.getcolors(maxcolors=256) or []) <= 64
    assert p.stats()["clip_quantized_colors"] in (64, 16)


# --------------------------------------------------------------------------- 运行中修改 fast_fps（不重启调档位）


@pytest.mark.parametrize(
    ("fps", "frame_duration_ms"),
    [(1, 500), (2, 250), (2.5, 200), (3, 167), (4, 125), (6, 83), (8, 62), (10, 50), ("6", 83), (" 2.5 ", 200)],
)
def test_parse_fast_fps_accepts_range(fps: object, frame_duration_ms: int) -> None:
    """合法范围 1～10（含端点、可带小数）；frame_duration_ms = round(1000/fps/2) 都不低于 41（官方文档第 36 节 §3.3）。"""
    value = parse_fast_fps(fps)
    assert isinstance(value, float) and FAST_FPS_MIN <= value <= FAST_FPS_MAX
    assert fast_frame_duration_for(value) == frame_duration_ms >= MIN_FRAME_DURATION_MS


@pytest.mark.parametrize(
    "bad", [0, 0.5, 0.99, 10.01, 11, 20, -3, float("nan"), float("inf"), True, False, None, "", "abc", [6], 10**400]
)
def test_parse_fast_fps_rejects_without_clamping(bad: object) -> None:
    with pytest.raises(FastFpsError) as excinfo:
        parse_fast_fps(bad)
    assert isinstance(excinfo.value, ValueError)  # 调用方可以按 ValueError 统一处理
    assert "1～10" in str(excinfo.value)  # 消息是给人看的中文，带合法范围


def test_parse_fast_fps_checks_frame_duration_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """帧时长下限单独检查：将来放宽了档位上限，round(1000/fps/2) < 41 的值仍会被拒绝。"""
    monkeypatch.setattr(pipeline_module, "FAST_FPS_MAX", 30.0)
    assert parse_fast_fps(12) == 12.0  # round(41.67) = 42
    with pytest.raises(FastFpsError, match="41"):
        parse_fast_fps(15)  # round(33.3) = 33 < 41


def test_fast_fps_warnings() -> None:
    assert fast_fps_warnings(4) == []
    assert fast_fps_warnings(4.5) == [  # 大于 4 提醒：4 档是真机实测稳定上限（research.md §9.20）
        "档位 4.5 偏高：真机可能换图比加载还快，画面卡住（research.md §9.20），建议 4 档或更低（真机约每秒 1.5 帧，是实测稳定上限）"
    ]


def test_fast_fps_setter_validates_and_updates_duration(caplog: pytest.LogCaptureFixture) -> None:
    p = CastPipeline(SyntheticSource())
    try:
        assert p.fast_settings() == (DEFAULT_FAST_FPS, 125)
        p.fast_fps = 6
        assert p.fast_fps == 6.0 and p.fast_frame_duration_ms == 83
        assert p.fast_settings() == (6.0, 83)
        assert p.page_frame_duration_ms("fast") == 83 and p.page_frame_duration_ms("image") == 1000
        stats = p.stats()
        assert (stats["fast_fps"], stats["fast_frame_duration_ms"]) == (6.0, 83)
        for bad in (0.5, 11, "x", None, True):
            with pytest.raises(FastFpsError):
                p.fast_fps = bad  # type: ignore[assignment]
        assert p.fast_settings() == (6.0, 83)  # 不合法：状态不变，也不钳制到边界
        assert p.set_fast_fps("2.5") == (2.5, 200)
        assert p.mode == "off" and not p.running  # 改档位不改变模式、不启动采集
        with caplog.at_level(logging.WARNING, logger="glasscast.pipeline"):
            p.fast_fps = 9
        assert any("档位 9 偏高" in r.getMessage() for r in caplog.records)
    finally:
        p.close()


def _gaps(stamps: list[float]) -> list[float]:
    return [b - a for a, b in zip(stamps, stamps[1:])]


def test_fast_fps_change_retimes_capture_immediately(make_pipeline: Callable[..., CastPipeline]) -> None:
    """运行中改 fast_fps：截帧节拍立即按新档位重排（不等完旧周期），调慢后也立刻变慢。"""
    p = make_pipeline(SyntheticSource(), fast_fps=1.0)
    p.start("fast")
    first = p.wait_for_snapshot(0, TIMEOUT)
    assert first is not None
    # 1 fps 时下一帧要等约 1 s。改成 10 fps 后，下一帧应在约 0.1 s 后到来：旧实现会继续睡完 1 s，这里 0.7 s 内必到
    p.fast_fps = 10
    assert p.fast_frame_duration_ms == 50
    snap = p.wait_for_snapshot(first.seq, timeout=0.7)
    assert snap is not None, "改档位后仍按旧的 1 s 周期等待"
    stamps = [time.monotonic()]
    for _ in range(5):
        snap = p.wait_for_snapshot(snap.seq, TIMEOUT)
        assert snap is not None
        stamps.append(time.monotonic())
    fast_gaps = sorted(_gaps(stamps))
    assert fast_gaps[len(fast_gaps) // 2] < 0.5  # 中位约 0.1 s（远小于 1 fps 的 1 s）
    assert p.stats()["fast_fps"] == 10.0

    # 调回 1 fps：最多再出一帧（改的那一刻可能正在截），之后至少 0.5 s 没有新帧
    p.fast_fps = 1
    snap = p.wait_for_snapshot(snap.seq, TIMEOUT)
    assert snap is not None
    assert p.wait_for_snapshot(snap.seq, timeout=0.5) is None
    assert p.wait_for_snapshot(snap.seq, TIMEOUT) is not None  # 约 1 s 后照常出帧
    assert (p.fast_fps, p.fast_frame_duration_ms) == (1.0, 500)


def test_fast_fps_change_does_not_affect_image_mode(make_pipeline: Callable[..., CastPipeline]) -> None:
    """image 模式按 image_fps 截帧：改 fast_fps 会唤醒线程，但不能多截帧，也不改变模式。"""
    p = make_pipeline(SyntheticSource(), image_fps=0.01, fast_fps=1.0)
    p.start("image")
    first = p.wait_for_snapshot(0, TIMEOUT)
    assert first is not None
    p.fast_fps = 10
    assert p.wait_for_snapshot(first.seq, timeout=0.3) is None
    assert p.mode == "image"
    p.start("fast")  # 切到 fast 后用新档位
    snap = p.wait_for_snapshot(first.seq, TIMEOUT)
    assert snap is not None
    assert p.wait_for_snapshot(snap.seq, timeout=0.7) is not None


def test_fast_fps_change_keeps_error_backoff(make_pipeline: Callable[..., CastPipeline]) -> None:
    """截屏连续失败的退避期间改档位，不会打断退避立即重试（只有 start_cast 的重启才打断）。"""
    src = AlwaysFailing()
    p = make_pipeline(src, fast_fps=10.0, error_backoff_s=5.0, error_backoff_max_s=5.0)
    p.start("fast")
    wait_until(lambda: p.stats()["errors"] >= 1)
    calls = src.calls
    p.fast_fps = 5
    time.sleep(0.3)  # 这里要验证"没有发生"，只能等一小段时间
    assert src.calls == calls


def test_emit_hops_for() -> None:
    """cast-emit 页面跳数 round(1000 / fast_fps / emit_hop_ms)，限制在 1～25（research.md §9.8）。"""
    from glasscast.pipeline import DEFAULT_EMIT_HOP_MS, EMIT_MAX_HOPS, emit_hops_for

    assert DEFAULT_EMIT_HOP_MS == 20.0 and EMIT_MAX_HOPS == 25
    assert emit_hops_for(4) == 12  # 默认：模拟器实测 12 跳约每秒 4.2 帧，真机见 device_fps_for
    assert emit_hops_for(10) == 5
    assert emit_hops_for(2) == 25  # 1000/2/20 = 25，正好是上限
    assert emit_hops_for(1) == 25  # 1000/20 = 50，截到 25
    assert emit_hops_for(10, 200) == 1  # 1000/10/200 = 0.5 → 至少 1
    assert emit_hops_for(4, 40) == 6
    for bad in (0, -1, float("nan")):
        with pytest.raises(ValueError):
            emit_hops_for(4, bad)


@pytest.mark.parametrize(
    ("hops", "device_fps"),
    [(25, 1.0), (20, 1.3), (17, 1.5), (15, 1.5), (12, 1.5), (10, 1.5), (8, 1.5), (1, 1.5)],
)
def test_device_fps_for_hops(hops: int, device_fps: float) -> None:
    """真机一步约 13 ms，每换一张图 hops × 3 步，每张图至少约 650 ms（research.md §9.19、§9.20）。"""
    from glasscast.pipeline import device_fps_for

    assert device_fps_for(hops) == device_fps


@pytest.mark.parametrize(
    ("level", "device_fps"),
    [
        (1, 1.0),
        (1.5, 1.0),
        (2, 1.0),
        (2.5, 1.3),
        (3, 1.5),
        (3.3, 1.5),
        (4, 1.5),
        (5, 1.5),
        (6, 1.5),
        (10, 1.5),
    ],
)
def test_device_fps_for_levels(level: float, device_fps: float) -> None:
    """档位 → hops → 真机帧数：1～2 档约每秒 1 帧，3 档及以上约每秒 1.5 帧。"""
    from glasscast.pipeline import device_fps_for, emit_hops_for

    assert device_fps_for(emit_hops_for(level)) == device_fps
