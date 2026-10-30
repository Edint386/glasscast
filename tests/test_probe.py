"""glasscast.probe 的测试：探针文件内容固定、体积大致按名字、格式正确。"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from glasscast.fset import inspect_fset
from glasscast.probe import PROBE_FRAMES, PROBE_SIZE, build_probe_file


@pytest.mark.parametrize(("name", "kb"), [("f40", 40), ("f80", 80), ("i12", 12)])
def test_probe_size_close_to_name(name: str, kb: int) -> None:
    assert abs(len(build_probe_file(name)) - kb * 1024) <= kb * 1024 * 0.1


def test_probe_formats() -> None:
    info = inspect_fset(build_probe_file("f16"))
    assert info["frames"] == PROBE_FRAMES and info["formats"] == ["png"] * PROBE_FRAMES
    assert info["png_dims"] == [PROBE_SIZE] * PROBE_FRAMES
    assert build_probe_file("fj20")[:4] == build_probe_file("f20")[:4]  # .fset 字节，只是换了地址后缀
    with Image.open(io.BytesIO(build_probe_file("g8"))) as im:
        assert (im.format, im.size, im.n_frames, im.info.get("duration")) == ("GIF", PROBE_SIZE, PROBE_FRAMES, 100)
    with Image.open(io.BytesIO(build_probe_file("i4"))) as im:
        assert (im.format, im.size) == ("JPEG", PROBE_SIZE)


def test_probe_is_deterministic_and_rejects_bad_names() -> None:
    build_probe_file.cache_clear()
    first = build_probe_file("i4")
    build_probe_file.cache_clear()
    assert build_probe_file("i4") == first
    for bad in ("x4", "f0", "f513", "f", "F16", "f16.fset"):
        with pytest.raises(ValueError):
            build_probe_file(bad)
