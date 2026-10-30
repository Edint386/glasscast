"""glasscast.frames 的测试。不真截屏：ScreenSource 通过注入假的 mss 工厂验证。"""

from __future__ import annotations

import io
import threading
from dataclasses import dataclass, field
from typing import Any

import pytest
from PIL import Image, ImageChops

from glasscast import frames
from glasscast.fset import inspect_fset, parse_fset, png_dimensions
from glasscast.frames import (
    CANVAS,
    COLOR_MODES,
    FILE_BUDGET_BYTES,
    PIXEL_BUDGET_BYTES,
    TIMER_FRAMES,
    TIMER_SIZE,
    FrameSource,
    ScreenSource,
    StaticSource,
    SyntheticSource,
    apply_color_mode,
    build_timer_fset,
    check_budget,
    encode_gif,
    encode_jpeg,
    encode_jpeg_target,
    encode_png,
    find_mozjpeg,
    fit_to_canvas,
    frame_digest,
    list_monitors,
    load_font,
    placeholder_image,
)


def _solid(size: tuple[int, int], color: tuple[int, int, int] = (255, 255, 255), mode: str = "RGB") -> Image.Image:
    return Image.new("RGB", size, color).convert(mode)


def _is_blank(im: Image.Image) -> bool:
    return im.convert("L").getextrema()[1] == 0


# ----------------------------------------------------------------- fit_to_canvas

SIZES = [
    (640, 290),
    (1280, 580),
    (2880, 1800),  # 典型 Retina 物理像素
    (1800, 2880),
    (100, 1000),
    (10_000, 1),
    (1, 10_000),
    (1, 1),
    (3000, 10),
    (639, 291),
]


@pytest.mark.parametrize("mode", ["fit", "crop"])
@pytest.mark.parametrize("size", SIZES)
def test_output_is_always_canvas(size: tuple[int, int], mode: str) -> None:
    out = fit_to_canvas(_solid(size), mode=mode)  # type: ignore[arg-type]
    assert out.size == CANVAS
    assert out.mode == "RGB"


@pytest.mark.parametrize("canvas", [(320, 145), (64, 64), (1, 1), (800, 600)])
def test_custom_canvas(canvas: tuple[int, int]) -> None:
    for mode in ("fit", "crop"):
        assert fit_to_canvas(_solid((1920, 1080)), canvas, mode).size == canvas  # type: ignore[arg-type]


def test_fit_letterboxes_tall_image() -> None:
    out = fit_to_canvas(_solid((100, 1000)), mode="fit")
    w, h = out.size
    assert out.getpixel((0, 0)) == (0, 0, 0)  # 左右是黑边
    assert out.getpixel((w - 1, h - 1)) == (0, 0, 0)
    assert out.getpixel((w // 2, h // 2)) == (255, 255, 255)  # 中间是内容
    # 内容宽度约 290/1000*100 = 29 像素
    bbox = out.convert("L").getbbox()
    assert bbox is not None
    assert bbox[1] == 0 and bbox[3] == h
    assert 27 <= bbox[2] - bbox[0] <= 31


def test_fit_letterboxes_wide_image_with_custom_bg() -> None:
    out = fit_to_canvas(_solid((6400, 290), (10, 200, 10)), mode="fit", bg=(255, 0, 0))
    assert out.getpixel((0, 0)) == (255, 0, 0)
    center = out.getpixel((320, 145))
    assert isinstance(center, tuple) and abs(center[1] - 200) <= 2


def test_crop_fills_canvas_without_bars() -> None:
    for size in [(100, 1000), (6400, 290), (2880, 1800)]:
        out = fit_to_canvas(_solid(size), mode="crop")
        assert out.convert("L").getextrema() == (255, 255)


def test_crop_takes_center() -> None:
    # 左黑右白的宽图，按 640:290 居中裁剪后应左右各占一半
    im = Image.new("RGB", (2000, 290), (0, 0, 0))
    im.paste((255, 255, 255), (1000, 0, 2000, 290))
    out = fit_to_canvas(im, mode="crop")
    assert out.getpixel((10, 145)) == (0, 0, 0)
    assert out.getpixel((630, 145)) == (255, 255, 255)


def test_same_size_returns_independent_copy() -> None:
    src = _solid(CANVAS, (1, 2, 3))
    out = fit_to_canvas(src)
    assert out is not src
    out.putpixel((0, 0), (9, 9, 9))
    assert src.getpixel((0, 0)) == (1, 2, 3)


@pytest.mark.parametrize("mode", ["RGBA", "L", "P", "LA", "1", "CMYK"])
def test_non_rgb_inputs_converted(mode: str) -> None:
    out = fit_to_canvas(_solid((1000, 700), (200, 100, 50), mode))
    assert out.mode == "RGB" and out.size == CANVAS


def test_fit_invalid_arguments() -> None:
    im = _solid((10, 10))
    with pytest.raises(ValueError):
        fit_to_canvas(im, mode="stretch")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        fit_to_canvas(im, (0, 290))
    with pytest.raises(ValueError):
        fit_to_canvas(im, (640, -1))


# ----------------------------------------------------------------- 编码


def test_encode_jpeg_reopens_baseline() -> None:
    data = encode_jpeg(SyntheticSource().grab())
    assert data[:3] == b"\xff\xd8\xff"
    with Image.open(io.BytesIO(data)) as im:
        assert im.format == "JPEG"
        assert im.size == CANVAS
        assert not im.info.get("progressive") and not im.info.get("progression")


@pytest.mark.parametrize("mode", ["L", "RGB"])
def test_encode_jpeg_optimized_not_larger(mode: str) -> None:
    im = SyntheticSource().grab().convert(mode)
    plain = io.BytesIO()
    im.save(plain, format="JPEG", quality=30, progressive=False, optimize=False)
    data = encode_jpeg(im, 30)
    assert len(data) <= len(plain.getvalue())
    with Image.open(io.BytesIO(data)) as reopened:
        reopened.load()
        assert reopened.mode == mode
        assert not reopened.info.get("progressive") and not reopened.info.get("progression")


def _is_baseline(data: bytes) -> bool:
    """第一个 SOF 段是 SOF0（基线），不是 SOF2（渐进）。"""
    i = 2
    while i < len(data) - 4:
        marker, seg = data[i + 1], int.from_bytes(data[i + 2 : i + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2):
            return marker == 0xC0
        i += 2 + seg
    return False


@pytest.mark.skipif(find_mozjpeg() is None, reason="没装 mozjpeg（brew install mozjpeg）")
@pytest.mark.parametrize(("mode", "subsampling"), [("L", 2), ("RGB", 2), ("RGB", 0)])
def test_encode_jpeg_mozjpeg_baseline_and_smaller(mode: str, subsampling: int) -> None:
    im = SyntheticSource().grab().convert(mode)
    moz = encode_jpeg(im, 30, subsampling=subsampling, encoder="mozjpeg")
    pil = encode_jpeg(im, 30, subsampling=subsampling, encoder="pillow")
    assert _is_baseline(moz) and _is_baseline(pil)
    assert len(moz) < len(pil)
    with Image.open(io.BytesIO(moz)) as reopened:
        reopened.load()
        assert (reopened.mode, reopened.size) == (mode, CANVAS)
    assert encode_jpeg(im, 30, subsampling=subsampling) == moz  # auto 选 mozjpeg


def test_encode_jpeg_encoder_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    im = SyntheticSource().grab().convert("L")
    with pytest.raises(ValueError):
        encode_jpeg(im, 30, encoder="cjpegli")  # type: ignore[arg-type]
    monkeypatch.setattr(frames, "find_mozjpeg", lambda: None)
    assert encode_jpeg(im, 30) == encode_jpeg(im, 30, encoder="pillow")  # 没装时 auto 退回 Pillow
    with pytest.raises(RuntimeError):
        encode_jpeg(im, 30, encoder="mozjpeg")


def test_encode_jpeg_mozjpeg_failure_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    im = SyntheticSource().grab().convert("L")
    monkeypatch.setattr(frames, "find_mozjpeg", lambda: "/nonexistent/cjpeg")
    monkeypatch.setattr(frames, "_mozjpeg_failed", False)
    assert encode_jpeg(im, 30) == encode_jpeg(im, 30, encoder="pillow")
    assert frames._mozjpeg_failed is True
    with pytest.raises(OSError):
        encode_jpeg(im, 30, encoder="mozjpeg")


@pytest.mark.parametrize("encoder", ["pillow", "auto"])
def test_encode_jpeg_target_picks_highest_quality_under_budget(encoder: str) -> None:
    im = SyntheticSource().grab().convert("L")
    budget = len(encode_jpeg(im, 50, encoder=encoder)) + 1  # type: ignore[arg-type]
    data, q = encode_jpeg_target(im, budget, quality_max=90, encoder=encoder)  # type: ignore[arg-type]
    assert len(data) <= budget and q >= 50
    assert data == encode_jpeg(im, q, encoder=encoder)  # type: ignore[arg-type]
    assert len(encode_jpeg(im, q + 1, encoder=encoder)) > budget  # type: ignore[arg-type]
    # 上限压住质量；预算太小时退到最低质量
    assert encode_jpeg_target(im, 10**7, quality_max=40, encoder=encoder)[1] == 40  # type: ignore[arg-type]
    assert encode_jpeg_target(im, 1, quality_min=12, encoder=encoder)[1] == 12  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        encode_jpeg_target(im, 0)
    with pytest.raises(ValueError):
        encode_jpeg_target(im, 1000, quality_min=50, quality_max=40)


@pytest.mark.parametrize("levels", [2, 4, 8, 16])
def test_encode_gif_levels_delay_not_interlaced(levels: int) -> None:
    data = encode_gif(SyntheticSource().grab(), levels, 100)
    assert data[:6] == b"GIF89a"
    with Image.open(io.BytesIO(data)) as im:
        assert (im.format, im.size, im.info.get("duration")) == ("GIF", CANVAS, 100)
        assert not im.info.get("interlace")
        assert len(set(im.convert("L").get_flattened_data())) <= levels
    assert encode_gif(SyntheticSource().grab().convert("L"), levels, 0)[:3] == b"GIF"  # 0 = 不写帧时长


@pytest.mark.parametrize(("levels", "delay"), [(3, 100), (0, 100), (4, 105), (4, -10), (4, 100.0)])
def test_encode_gif_rejects_bad_arguments(levels: int, delay: Any) -> None:
    with pytest.raises(ValueError):
        encode_gif(SyntheticSource().grab(), levels, delay)


def test_encode_jpeg_quality_and_subsampling() -> None:
    im = SyntheticSource().grab()
    assert len(encode_jpeg(im, 20)) < len(encode_jpeg(im, 95))
    for ss in (0, 1, 2):
        with Image.open(io.BytesIO(encode_jpeg(im, 70, subsampling=ss))) as reopened:
            reopened.load()
            assert reopened.size == CANVAS
    with pytest.raises(ValueError):
        encode_jpeg(im, 0)
    with pytest.raises(ValueError):
        encode_jpeg(im, 101)
    with pytest.raises(ValueError):
        encode_jpeg(im, 70, subsampling=3)


def test_encode_jpeg_accepts_rgba() -> None:
    with Image.open(io.BytesIO(encode_jpeg(_solid((20, 10), mode="RGBA")))) as im:
        assert im.mode == "RGB"


def test_encode_png_modes_and_lossless() -> None:
    src = SyntheticSource().grab()
    rgb = encode_png(src)
    with Image.open(io.BytesIO(rgb)) as im:
        assert im.format == "PNG" and im.mode == "RGB" and im.size == CANVAS
        assert ImageChops.difference(im.convert("RGB"), src).getbbox() is None
    rgba = encode_png(src, alpha=True)
    with Image.open(io.BytesIO(rgba)) as im:
        assert im.mode == "RGBA"
        assert im.getchannel("A").getextrema() == (255, 255)
    assert png_dimensions(rgba) == CANVAS
    for level in (0, 9):
        assert encode_png(src, level)[:8] == b"\x89PNG\r\n\x1a\n"
    with pytest.raises(ValueError):
        encode_png(src, 10)


# ----------------------------------------------------------------- 预算


def test_budget_constants_match_research() -> None:
    assert PIXEL_BUDGET_BYTES == 640 * 290 * 4 == 742_400
    assert FILE_BUDGET_BYTES == 1024 * 1024


def test_check_budget_boundaries() -> None:
    assert check_budget(640, 290, FILE_BUDGET_BYTES) == []  # 恰好等于上限不告警
    assert len(check_budget(641, 290, 0)) == 1
    assert len(check_budget(640, 291, 0)) == 1
    assert len(check_budget(640, 290, FILE_BUDGET_BYTES + 1)) == 1
    both = check_budget(1920, 1080, 5_000_000)
    assert len(both) == 2
    assert any("742400" in w for w in both) and any("1048576" in w for w in both)


def test_check_budget_jpeg_bytes_per_pixel() -> None:
    # 文档折算：JPEG 最大约 640×386（按 3 字节/像素）
    assert check_budget(640, 386, 10_000, bytes_per_pixel=3) == []
    assert check_budget(640, 387, 10_000, bytes_per_pixel=3) != []
    assert check_budget(640, 386, 10_000) != []  # 按默认 4 字节则超


# ----------------------------------------------------------------- 摘要


def test_frame_digest_stable_and_distinct() -> None:
    a = SyntheticSource(clock=lambda: 1_700_000_000.0).grab()
    a_copy = a.copy()
    b = SyntheticSource(clock=lambda: 1_700_000_000.0, start=41).grab()
    for size in [(64, 32), None]:
        assert frame_digest(a, size=size) == frame_digest(a_copy, size=size)
        assert frame_digest(a, size=size) != frame_digest(b, size=size)
    assert frame_digest(a) == frame_digest(a.convert("RGBA"))  # 默认摘要只看灰度


def test_exact_digest_sees_single_pixel_change() -> None:
    im = _solid(CANVAS, (128, 128, 128))
    changed = im.copy()
    changed.putpixel((300, 100), (129, 128, 128))
    assert frame_digest(im, size=None) != frame_digest(changed, size=None)


def test_digest_includes_size() -> None:
    assert frame_digest(_solid((10, 20)), size=None) != frame_digest(_solid((20, 10)), size=None)


def test_digest_invalid_size() -> None:
    with pytest.raises(ValueError):
        frame_digest(_solid((4, 4)), size=(0, 4))


# ----------------------------------------------------------------- 帧源


def test_sources_satisfy_protocol() -> None:
    assert isinstance(StaticSource(_solid((2, 2))), FrameSource)
    assert isinstance(SyntheticSource(), FrameSource)
    assert isinstance(ScreenSource(mss_factory=lambda: None), FrameSource)


def test_static_source_single_image_returns_copies() -> None:
    src = StaticSource(_solid((8, 8), (5, 5, 5), "RGBA"))
    first = src.grab()
    assert first.mode == "RGB"
    first.putpixel((0, 0), (255, 0, 0))
    assert src.grab().getpixel((0, 0)) == (5, 5, 5)
    assert src.grabs == 2


def test_static_source_sequence_loop_and_hold() -> None:
    colors = [(1, 0, 0), (2, 0, 0), (3, 0, 0)]
    images = [_solid((2, 2), c) for c in colors]
    looping = StaticSource(images)
    assert [looping.grab().getpixel((0, 0)) for _ in range(7)] == [colors[i % 3] for i in range(7)]
    holding = StaticSource(images, loop=False)
    assert [holding.grab().getpixel((0, 0)) for _ in range(5)] == colors + [colors[2]] * 2


def test_static_source_requires_images() -> None:
    with pytest.raises(ValueError):
        StaticSource([])


def test_synthetic_source_counts_and_changes() -> None:
    src = SyntheticSource()
    a, b = src.grab(), src.grab()
    assert src.count == 2
    assert a.size == CANVAS and a.mode == "RGB"
    assert frame_digest(a, size=None) != frame_digest(b, size=None)
    assert not _is_blank(a)


def test_synthetic_source_is_deterministic_with_fixed_clock() -> None:
    def clock() -> float:
        return 1_700_000_000.25

    a = SyntheticSource(clock=clock, start=9).grab()
    b = SyntheticSource(clock=clock, start=9).grab()
    assert ImageChops.difference(a, b).getbbox() is None


def test_synthetic_source_custom_size_and_cjk_title() -> None:
    im = SyntheticSource((320, 145), title="电脑投屏测试").grab()
    assert im.size == (320, 145)
    with pytest.raises(ValueError):
        SyntheticSource((0, 10))


def test_synthetic_source_is_thread_safe() -> None:
    src = SyntheticSource((64, 32))
    threads = [threading.Thread(target=lambda: [src.grab() for _ in range(20)]) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert src.count == 80


def test_placeholder_image() -> None:
    im = placeholder_image()
    assert im.size == CANVAS and im.mode == "RGB"
    assert not _is_blank(im)
    assert placeholder_image("x", (100, 50), detail=None).size == (100, 50)


# ----------------------------------------------------------------- 字体回退


def test_load_font_returns_usable_font() -> None:
    for cjk in (False, True):
        font = load_font(24, cjk=cjk)
        assert font.getbbox("08:00:00")[2] > 0


def test_load_font_falls_back_when_nothing_available(monkeypatch: pytest.MonkeyPatch) -> None:
    load_font.cache_clear()
    monkeypatch.setattr(frames, "_CJK_FONT_CANDIDATES", ("/nonexistent/a.ttc",))
    monkeypatch.setattr(frames, "_LATIN_FONT_CANDIDATES", ("/nonexistent/b.ttf",))

    def no_freetype(size: float | None = None) -> Any:
        raise OSError("no freetype")

    bitmap = frames.ImageFont.load_default_imagefont
    monkeypatch.setattr(
        frames.ImageFont,
        "load_default",
        lambda size=None: no_freetype() if size is not None else bitmap(),
    )
    try:
        font = load_font(30, cjk=True)
        assert not isinstance(font, frames.ImageFont.FreeTypeFont)  # 最后一级：位图字体
        assert font.getbbox("123")[2] > 0
        # 回退字体下合成源和占位图仍能画出来
        assert SyntheticSource((100, 50)).grab().size == (100, 50)
        assert placeholder_image(canvas=(100, 50)).size == (100, 50)
    finally:
        load_font.cache_clear()


# ----------------------------------------------------------------- ScreenSource（假 mss）


@dataclass
class FakeShot:
    size: tuple[int, int]
    bgra: bytes


@dataclass
class FakeMss:
    """模拟 mss：monitors[0] 为并集，1/2 为两块屏；grab 返回 2 倍物理像素（模拟 Retina）。"""

    monitors: list[dict[str, int]] = field(
        default_factory=lambda: [
            {"left": 0, "top": 0, "width": 2160, "height": 900},
            {"left": 0, "top": 0, "width": 1440, "height": 900},
            {"left": 1440, "top": 0, "width": 720, "height": 900},
        ]
    )
    grabbed: list[dict[str, int]] = field(default_factory=list)
    closed: int = 0
    thread: int = field(default_factory=threading.get_ident)

    def grab(self, monitor: dict[str, int]) -> FakeShot:
        self.grabbed.append(dict(monitor))
        w, h = monitor["width"] * 2, monitor["height"] * 2
        # 像素：B=10, G=20, R=30, X=255 → RGB 应为 (30, 20, 10)
        return FakeShot((w, h), bytes([10, 20, 30, 255]) * (w * h))

    def close(self) -> None:
        self.closed += 1


class FakeFactory:
    def __init__(self) -> None:
        self.instances: list[FakeMss] = []

    def __call__(self) -> FakeMss:
        inst = FakeMss()
        self.instances.append(inst)
        return inst


def test_screen_source_is_lazy_and_converts_bgra() -> None:
    factory = FakeFactory()
    src = ScreenSource(mss_factory=factory)
    assert factory.instances == []  # 构造时不触碰 mss
    im = src.grab()
    assert im.mode == "RGB"
    assert im.size == (2880, 1800)  # 物理像素
    assert im.getpixel((0, 0)) == (30, 20, 10)
    assert factory.instances[0].grabbed == [{"left": 0, "top": 0, "width": 1440, "height": 900}]
    src.grab()
    assert len(factory.instances) == 1  # 同一线程复用实例


def test_screen_source_region_is_relative_to_monitor_and_clipped() -> None:
    factory = FakeFactory()
    src = ScreenSource(monitor=2, region=(100, 50, 400, 300), mss_factory=factory)
    assert src.capture_rect() == {"left": 1540, "top": 50, "width": 400, "height": 300}
    clipped = ScreenSource(monitor=2, region=(600, 800, 400, 300), mss_factory=factory)
    assert clipped.capture_rect() == {"left": 2040, "top": 800, "width": 120, "height": 100}
    outside = ScreenSource(monitor=1, region=(5000, 0, 10, 10), mss_factory=factory)
    with pytest.raises(ValueError, match="之外"):
        outside.grab()


def test_screen_source_validates_monitor() -> None:
    with pytest.raises(ValueError):
        ScreenSource(monitor=-1)
    with pytest.raises(ValueError):
        ScreenSource(region=(0, 0, 0, 10))
    with pytest.raises(ValueError):
        ScreenSource(region=(0, 0, 10))  # type: ignore[arg-type]
    src = ScreenSource(monitor=3, mss_factory=FakeFactory())
    with pytest.raises(ValueError, match="不存在"):
        src.grab()
    whole = ScreenSource(monitor=0, mss_factory=FakeFactory())
    assert whole.grab().size == (4320, 1800)


def test_screen_source_one_instance_per_thread_and_close() -> None:
    factory = FakeFactory()
    src = ScreenSource(mss_factory=factory)
    src.grab()
    worker = threading.Thread(target=src.grab)
    worker.start()
    worker.join(10)
    assert len(factory.instances) == 2
    assert factory.instances[0].thread != factory.instances[1].thread

    src.close()
    src.close()  # 幂等
    assert [inst.closed for inst in factory.instances] == [1, 1]
    src.grab()  # 关闭后再用会重建
    assert len(factory.instances) == 3


def test_list_monitors_uses_factory() -> None:
    factory = FakeFactory()
    monitors = list_monitors(factory)
    assert len(monitors) == 3 and monitors[1]["width"] == 1440
    assert factory.instances[0].closed == 1


def test_default_mss_factory_prefers_new_api(monkeypatch: pytest.MonkeyPatch) -> None:
    """mss 10.2+ 有 mss.MSS（旧工厂 mss.mss 已弃用、将来移除）；9.x 只有 mss.mss。用假模块验证，不碰真 mss。"""
    import sys
    import types

    calls: list[str] = []

    def legacy() -> str:
        calls.append("mss.mss")
        return "legacy-instance"

    class NewMSS:
        def __init__(self) -> None:
            calls.append("mss.MSS")

    new_style = types.ModuleType("mss")
    new_style.MSS = NewMSS  # type: ignore[attr-defined]
    new_style.mss = legacy  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mss", new_style)
    assert isinstance(frames._default_mss_factory(), NewMSS)
    assert calls == ["mss.MSS"]

    old_style = types.ModuleType("mss")
    old_style.mss = legacy  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mss", old_style)
    assert frames._default_mss_factory() == "legacy-instance"
    assert calls == ["mss.MSS", "mss.mss"]


def test_installed_mss_exposes_a_factory() -> None:
    """已安装的 mss 至少提供 MSS 或 mss 其中之一（只检查属性，不创建实例、不截屏）。"""
    import mss

    assert callable(getattr(mss, "MSS", None) or getattr(mss, "mss", None))


# ----------------------------------------------------------------- 灰度输出（research.md §9.2 E6）


def test_color_modes() -> None:
    assert COLOR_MODES == ("gray", "rgb")


@pytest.mark.parametrize("src_mode", ["RGB", "RGBA", "L", "P"])
def test_apply_color_mode(src_mode: str) -> None:
    src = _solid((20, 10), (200, 100, 50), mode=src_mode)
    gray = apply_color_mode(src, "gray")
    assert gray.mode == "L" and gray.size == (20, 10)
    rgb = apply_color_mode(src, "rgb")
    assert rgb.mode == "RGB" and rgb.size == (20, 10)
    # 已经是目标模式：原样返回，不复制
    already_gray = _solid((4, 4), mode="L")
    assert apply_color_mode(already_gray, "gray") is already_gray
    already_rgb = _solid((4, 4))
    assert apply_color_mode(already_rgb, "rgb") is already_rgb
    assert apply_color_mode(already_rgb).mode == "L"  # 默认 gray


def test_apply_color_mode_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        apply_color_mode(_solid((4, 4)), "cmyk")  # type: ignore[arg-type]


def test_apply_color_mode_uses_luma() -> None:
    # 绿屏只显示亮度：转灰度用 ITU-R 601 亮度，纯绿比纯红、纯蓝亮
    levels = {c: apply_color_mode(_solid((2, 2), c), "gray").getpixel((0, 0)) for c in [(255, 0, 0), (0, 255, 0), (0, 0, 255)]}
    assert levels[(0, 255, 0)] > levels[(255, 0, 0)] > levels[(0, 0, 255)]


def test_encode_gray_jpeg_is_single_channel_and_smaller() -> None:
    src = SyntheticSource(clock=lambda: 1_700_000_000.0).grab()
    gray = apply_color_mode(src, "gray")
    data = encode_jpeg(gray)
    with Image.open(io.BytesIO(data)) as im:
        assert (im.format, im.mode, im.size) == ("JPEG", "L", CANVAS)
        assert not im.info.get("progressive") and not im.info.get("progression")
    assert len(data) < len(encode_jpeg(src))  # 灰度 JPEG ≈ 彩色的 70%
    # subsampling 对灰度无意义，但合法值不报错、非法值仍报错
    assert encode_jpeg(gray, 70, subsampling=0)[:3] == b"\xff\xd8\xff"
    with pytest.raises(ValueError):
        encode_jpeg(gray, 70, subsampling=3)


def test_encode_gray_png() -> None:
    gray = apply_color_mode(SyntheticSource().grab(), "gray")
    data = encode_png(gray)
    with Image.open(io.BytesIO(data)) as im:
        assert (im.format, im.mode, im.size) == ("PNG", "L", CANVAS)
        assert ImageChops.difference(im, gray).getbbox() is None  # 无损
    assert data[25] == 0  # IHDR 色彩类型 0 = 灰度
    assert len(data) < len(encode_png(gray, alpha=True))  # 灰度 PNG 明显小于 RGBA
    with Image.open(io.BytesIO(encode_png(gray, alpha=True))) as im:
        assert im.mode == "RGBA"  # alpha=True 仍然输出官方样本格式


def test_digest_on_gray_frames() -> None:
    a = apply_color_mode(_solid((64, 32), (10, 20, 30)), "gray")
    b = a.copy()
    b.putpixel((3, 3), 255)
    assert frame_digest(a, size=None) == frame_digest(a.copy(), size=None)
    assert frame_digest(a, size=None) != frame_digest(b, size=None)
    # 灰度与彩色的同一画面摘要不同（含模式），不会互相复用编码结果
    assert frame_digest(a, size=None) != frame_digest(a.convert("RGB"), size=None)


# ----------------------------------------------------------------- 定时器 .fset（research.md §9.2 E4）


def test_timer_fset_layout() -> None:
    assert (TIMER_FRAMES, TIMER_SIZE) == (2, (8, 8))
    data = build_timer_fset()
    info = inspect_fset(data)
    assert info["frames"] == 2
    assert info["header_bytes"] == 4 + 12 * 2
    assert info["formats"] == ["png", "png"]
    assert info["png_dims"] == [(8, 8), (8, 8)]
    assert info["total_bytes"] == len(data) < 512  # 几十字节一帧
    for blob in parse_fset(data):
        with Image.open(io.BytesIO(blob)) as im:
            assert (im.mode, im.size) == ("L", (8, 8))  # 8 位灰度 PNG（不能用 JPEG 帧，research.md §9.2 E5）


def test_timer_fset_is_fixed_and_cached() -> None:
    first = build_timer_fset()
    assert build_timer_fset() is first  # lru_cache：同一对象
    assert build_timer_fset(2, (8, 8)) == first
    build_timer_fset.cache_clear()
    assert build_timer_fset() == first  # 重新生成也逐字节相同（内容固定）


def test_timer_fset_custom_and_invalid() -> None:
    info = inspect_fset(build_timer_fset(4, (16, 4)))
    assert info["frames"] == 4 and info["png_dims"] == [(16, 4)] * 4
    for frames_count in (0, -1, True):
        with pytest.raises(ValueError):
            build_timer_fset(frames_count)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        build_timer_fset(2, (0, 8))
