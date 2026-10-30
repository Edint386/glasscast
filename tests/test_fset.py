"""glasscast.fset 的测试。

``.fset`` 格式是对照官方模拟器资源包里的 3 个样本（``genie_ball.fset`` 等）逐字节验证出来的（research.md §4）。
官方样本不随仓库分发；这里用自制夹具 ``tests/fixtures/sample.fset`` 代替：形状与 ``genie_ball.fset`` 相同
（48 帧、32×30、8-bit RGBA PNG、帧首尾相接、reserved 全 0、无尾部填充），由
``tests/fixtures/make_sample_fset.py`` 生成。
"""

from __future__ import annotations

import importlib.util
import io
import struct
from pathlib import Path
from types import ModuleType

import pytest
from PIL import Image

from glasscast.fset import (
    ENTRY_SIZE,
    FsetEntry,
    FsetError,
    build_fset,
    detect_frame_format,
    header_size,
    inspect_fset,
    parse_fset,
    parse_fset_entries,
    png_dimensions,
)

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE = FIXTURES / "sample.fset"
#: 已提交的 sample.fset 的总字节数（PNG 压缩字节取决于生成时的 zlib；像素由下面的逐像素测试保证）
FIXTURE_BYTES = 9_673


@pytest.fixture(scope="module")
def sample() -> bytes:
    return FIXTURE.read_bytes()


@pytest.fixture(scope="module")
def generator() -> ModuleType:
    """按路径导入 tests/fixtures/make_sample_fset.py（tests/ 不是包）。"""
    path = FIXTURES / "make_sample_fset.py"
    spec = importlib.util.spec_from_file_location("make_sample_fset", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _png(size: tuple[int, int], color: tuple[int, int, int, int] = (255, 0, 0, 255)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _jpeg(size: tuple[int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (0, 128, 255)).save(buf, format="JPEG")
    return buf.getvalue()


def _raw(n: int, entries: list[tuple[int, int, int]], payload: bytes) -> bytes:
    """手工拼一个（可能损坏的）.fset。"""
    head = struct.pack("<I", n) + b"".join(struct.pack("<III", *e) for e in entries)
    return head + payload


# ----------------------------------------------------------------- 夹具（与官方样本同形）


def test_fixture_matches_research_notes(sample: bytes) -> None:
    """与 research.md §4 记录的官方样本性质一致：48 帧、全 PNG、32×30、头部 580 字节、连续、无尾部填充。"""
    frames = parse_fset(sample)
    assert len(frames) == 48
    assert len(sample) == FIXTURE_BYTES
    assert header_size(48) == 580

    entries = parse_fset_entries(sample)
    assert entries[0].offset == 580
    for prev, nxt in zip(entries, entries[1:]):
        assert prev.offset + prev.size == nxt.offset
    assert entries[-1].offset + entries[-1].size == len(sample)
    assert all(e.reserved == 0 for e in entries)

    for blob in frames:
        assert detect_frame_format(blob) == "png"
        assert png_dimensions(blob) == (32, 30)
        assert blob.endswith(b"IEND\xaeB`\x82")
        with Image.open(io.BytesIO(blob)) as im:
            assert im.format == "PNG"
            assert im.size == (32, 30)
            assert im.mode == "RGBA"  # 官方样本是 8-bit RGBA，夹具照此生成


def test_fixture_pixels_match_generator(sample: bytes, generator: ModuleType) -> None:
    """已提交的 sample.fset 与生成脚本的画法逐像素一致（换 zlib 版本重新生成也只会变压缩字节）。"""
    assert generator.FRAMES == 48 and generator.SIZE == (32, 30)
    assert generator.frames_match(sample)
    # 帧与帧确实不同（是动画，不是 48 张同一帧）
    assert len({generator.draw_frame(i).tobytes() for i in range(48)}) > 24
    # 当前环境重新生成：布局与像素都对，字节数不强求相同
    rebuilt = generator.build_sample()
    assert generator.frames_match(rebuilt)
    assert inspect_fset(rebuilt)["header_bytes"] == 580


def test_fixture_inspect(sample: bytes) -> None:
    info = inspect_fset(sample)
    assert info["frames"] == 48
    assert info["header_bytes"] == 580
    assert info["total_bytes"] == len(sample)
    assert len(info["frame_sizes"]) == 48
    assert sum(info["frame_sizes"]) + 580 == len(sample)
    assert info["formats"] == ["png"] * 48
    assert info["png_dims"] == [(32, 30)] * 48


def test_fixture_byte_exact_roundtrip(sample: bytes) -> None:
    assert build_fset(parse_fset(sample)) == sample


def test_accepts_bytearray_and_memoryview(sample: bytes) -> None:
    expected = parse_fset(sample)
    assert parse_fset(bytearray(sample)) == expected
    assert parse_fset(memoryview(sample)) == expected
    assert inspect_fset(bytearray(sample))["frames"] == 48


# ----------------------------------------------------------------- 构造与回放


@pytest.mark.parametrize("count", [1, 3])
def test_build_layout_and_roundtrip(count: int) -> None:
    frames = [_png((4 + i, 2 + i), (i * 40, 0, 0, 255)) for i in range(count)]
    data = build_fset(frames)

    (n,) = struct.unpack_from("<I", data, 0)
    assert n == count
    offset = 4 + 12 * count
    for i, frame in enumerate(frames):
        assert struct.unpack_from("<III", data, 4 + ENTRY_SIZE * i) == (offset, len(frame), 0)
        assert data[offset : offset + len(frame)] == frame
        offset += len(frame)
    assert offset == len(data)

    assert parse_fset(data) == frames
    assert build_fset(parse_fset(data)) == data
    assert inspect_fset(data)["png_dims"] == [(4 + i, 2 + i) for i in range(count)]


def test_build_accepts_any_bytes_like() -> None:
    data = build_fset([b"abc", bytearray(b"de"), memoryview(b"f")])
    assert parse_fset(data) == [b"abc", b"de", b"f"]


def test_mixed_formats_inspect() -> None:
    data = build_fset([_png((8, 6)), _jpeg((10, 10)), b"not an image"])
    info = inspect_fset(data)
    assert info["formats"] == ["png", "jpeg", "unknown"]
    assert info["png_dims"] == [(8, 6), None, None]


def test_build_rejects_bad_input() -> None:
    with pytest.raises(FsetError):
        build_fset([])
    with pytest.raises(FsetError):
        build_fset([b"ok", b""])
    with pytest.raises(TypeError):
        build_fset(b"single bytes object")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        build_fset(["str is not bytes"])  # type: ignore[list-item]


def test_fset_error_is_value_error() -> None:
    assert issubclass(FsetError, ValueError)
    assert FsetEntry(1, 2, 0) == FsetEntry(1, 2, 0)


# ----------------------------------------------------------------- 损坏的头部


def test_rejects_too_short() -> None:
    with pytest.raises(FsetError, match="过短"):
        parse_fset(b"\x01\x00")


def test_rejects_zero_frames() -> None:
    with pytest.raises(FsetError, match="N=0"):
        parse_fset(struct.pack("<I", 0))
    with pytest.raises(FsetError):
        parse_fset(struct.pack("<I", 0) + b"garbage")


def test_rejects_truncated_header() -> None:
    # N=2 需要 28 字节头部，只给 1 项
    with pytest.raises(FsetError, match="头部不完整"):
        parse_fset(_raw(2, [(28, 1, 0)], b""))


def test_huge_frame_count_fails_fast() -> None:
    with pytest.raises(FsetError, match="头部不完整"):
        parse_fset(struct.pack("<I", 0xFFFFFFFF) + b"\x00" * 64)


def test_rejects_offset_out_of_range() -> None:
    with pytest.raises(FsetError, match="越界"):
        parse_fset(_raw(1, [(16, 100, 0)], b"x" * 10))
    with pytest.raises(FsetError, match="越界"):
        parse_fset(_raw(1, [(0xFFFFFF00, 4, 0)], b"x" * 10))


def test_rejects_wrong_first_offset() -> None:
    with pytest.raises(FsetError, match="首帧"):
        parse_fset(_raw(1, [(17, 3, 0)], b"xxxx"))


def test_rejects_gap_between_frames() -> None:
    # 头部 28 字节；第 0 帧 [28, 30)，第 1 帧从 31 开始（中间空 1 字节）
    with pytest.raises(FsetError, match="不连续"):
        parse_fset(_raw(2, [(28, 2, 0), (31, 2, 0)], b"aa_bb"))


def test_rejects_overlapping_frames() -> None:
    with pytest.raises(FsetError, match="不连续"):
        parse_fset(_raw(2, [(28, 3, 0), (29, 2, 0)], b"aaa"))


def test_rejects_zero_size_frame() -> None:
    with pytest.raises(FsetError, match="size=0"):
        parse_fset(_raw(2, [(28, 0, 0), (28, 2, 0)], b"bb"))


def test_strict_trailing_bytes_and_reserved(sample: bytes) -> None:
    with pytest.raises(FsetError, match="多余"):
        parse_fset(sample + b"\x00")
    assert len(parse_fset(sample + b"\x00", strict=False)) == 48

    tampered = bytearray(sample)
    struct.pack_into("<I", tampered, 4 + 8, 7)  # 第 0 项的 reserved 改成 7
    with pytest.raises(FsetError, match="reserved"):
        parse_fset(bytes(tampered))
    assert parse_fset_entries(bytes(tampered), strict=False)[0].reserved == 7


def test_corrupted_fixture_entry_detected(sample: bytes) -> None:
    tampered = bytearray(sample)
    offset, size, _ = struct.unpack_from("<III", tampered, 4 + 12 * 5)
    struct.pack_into("<I", tampered, 4 + 12 * 5 + 4, size + 1)  # 第 5 帧多报 1 字节
    with pytest.raises(FsetError, match="不连续"):
        parse_fset(bytes(tampered))


# ----------------------------------------------------------------- 辅助函数


def test_png_dimensions_rejects_non_png() -> None:
    assert png_dimensions(b"") is None
    assert png_dimensions(b"\x89PNG\r\n\x1a\n") is None  # 只有签名
    assert png_dimensions(_jpeg((5, 5))) is None
    bad_ihdr = bytearray(_png((3, 3)))
    bad_ihdr[12:16] = b"XXXX"
    assert png_dimensions(bytes(bad_ihdr)) is None


def test_detect_frame_format() -> None:
    assert detect_frame_format(_png((2, 2))) == "png"
    assert detect_frame_format(_jpeg((2, 2))) == "jpeg"
    assert detect_frame_format(b"GIF89a") == "unknown"
    assert detect_frame_format(b"") == "unknown"


def test_header_size() -> None:
    assert header_size(1) == 16
    assert header_size(10) == 124
    with pytest.raises(ValueError):
        header_size(-1)
