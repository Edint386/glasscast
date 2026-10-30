"""`.fset` 帧合集文件的读写。

格式来自对模拟器资源包里 3 个官方样本的逐字节验证（research.md §4）::

    offset 0      uint32 LE  frame_count (N)
    offset 4      N × { uint32 LE offset, uint32 LE size, uint32 LE reserved(=0) }   每项 12 字节
    offset 4+12N  帧数据首尾相接；第 i 帧 = file[offset_i : offset_i + size_i]

- 帧尺寸不在文件头里，由页面 XML 的 ``width``/``height``/``total-frames`` 指定（research.md §4）。
- 官方样本的每一帧都是完整的 8-bit RGBA PNG；JPEG 帧能否被眼镜端解码【待验证】（research.md §4、§8.3）。
- 文件末尾正好等于最后一帧末尾，reserved 全为 0。本模块写出的文件严格满足这些性质，
  解析时默认也按同样的严格程度校验（``strict=False`` 可放宽尾部多余字节和 reserved 非零两项）。
"""

from __future__ import annotations

import struct
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, TypedDict

__all__ = [
    "COUNT_STRUCT",
    "ENTRY_SIZE",
    "ENTRY_STRUCT",
    "FrameFormat",
    "FsetEntry",
    "FsetError",
    "FsetInfo",
    "build_fset",
    "detect_frame_format",
    "header_size",
    "inspect_fset",
    "parse_fset",
    "parse_fset_entries",
    "png_dimensions",
]

#: 文件头里的帧数：uint32 LE（research.md §4）
COUNT_STRUCT = struct.Struct("<I")
#: 每个索引项：offset、size、reserved，各 uint32 LE，共 12 字节（research.md §4）
ENTRY_STRUCT = struct.Struct("<III")
ENTRY_SIZE = ENTRY_STRUCT.size  # 12
_UINT32_MAX = 0xFFFFFFFF

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SOI = b"\xff\xd8\xff"

FrameFormat = Literal["png", "jpeg", "unknown"]
BytesLike = bytes | bytearray | memoryview


class FsetError(ValueError):
    """`.fset` 数据不合法（头部损坏、索引越界、帧不连续等）。"""


@dataclass(frozen=True)
class FsetEntry:
    """文件头里的一条帧索引。"""

    offset: int
    size: int
    reserved: int


class FsetInfo(TypedDict):
    """:func:`inspect_fset` 的返回值。"""

    frames: int
    header_bytes: int
    total_bytes: int
    frame_sizes: list[int]
    formats: list[FrameFormat]
    #: 与帧一一对应；PNG 帧为 (宽, 高)，非 PNG 帧为 None
    png_dims: list[tuple[int, int] | None]


def header_size(frame_count: int) -> int:
    """N 帧文件的头部字节数（= 第一帧的 offset）：4 + 12N。"""
    if frame_count < 0:
        raise ValueError(f"帧数不能为负: {frame_count}")
    return COUNT_STRUCT.size + ENTRY_SIZE * frame_count


def build_fset(frames: Sequence[BytesLike]) -> bytes:
    """把若干帧（通常是 PNG 字节串）打包成 `.fset`。

    - ``frames`` 非空，且每帧非空；
    - 输出布局见模块文档，reserved 一律写 0，帧数据首尾相接，无尾部填充。
    """
    if isinstance(frames, (bytes, bytearray, memoryview)):
        # 防止误把单个 bytes 当成"字节序列"逐字节打包
        raise TypeError("frames 应为帧的序列（list[bytes]），而不是单个 bytes")
    n = len(frames)
    if n == 0:
        raise FsetError("至少需要 1 帧")
    if n > _UINT32_MAX:
        raise FsetError(f"帧数过多: {n}")

    blobs: list[bytes] = []
    for i, frame in enumerate(frames):
        if not isinstance(frame, (bytes, bytearray, memoryview)):
            raise TypeError(f"第 {i} 帧不是字节串: {type(frame).__name__}")
        blob = bytes(frame)
        if not blob:
            raise FsetError(f"第 {i} 帧为空")
        blobs.append(blob)

    head = header_size(n)
    total = head + sum(len(b) for b in blobs)
    if total > _UINT32_MAX:
        # offset/size 都是 uint32，超过 4 GiB 无法表示
        raise FsetError(f"文件过大（{total} 字节），超出 uint32 偏移范围")

    out = bytearray(total)
    COUNT_STRUCT.pack_into(out, 0, n)
    offset = head
    for i, blob in enumerate(blobs):
        ENTRY_STRUCT.pack_into(out, COUNT_STRUCT.size + ENTRY_SIZE * i, offset, len(blob), 0)
        out[offset : offset + len(blob)] = blob
        offset += len(blob)
    return bytes(out)


def parse_fset_entries(data: BytesLike, *, strict: bool = True) -> list[FsetEntry]:
    """解析并校验文件头，返回索引项列表（不复制帧数据）。

    校验项（任一不满足抛 :class:`FsetError`）：

    - 文件至少 4 字节，N > 0，且头部 4+12N 字节完整；
    - 首项 offset == 4+12N；
    - 每项 size > 0，offset+size 不超出文件；
    - 各帧连续：offset_i + size_i == offset_{i+1}；
    - ``strict=True`` 时额外要求：最后一帧末尾 == 文件末尾、reserved 全为 0
      （官方样本均满足，research.md §4）。
    """
    view = memoryview(data).cast("B") if not isinstance(data, bytes) else data
    total = len(view)
    if total < COUNT_STRUCT.size:
        raise FsetError(f"文件过短（{total} 字节），连帧数都读不到")
    (n,) = COUNT_STRUCT.unpack_from(view, 0)
    if n == 0:
        raise FsetError("帧数 N=0")
    head = header_size(n)
    if head > total:
        raise FsetError(f"头部不完整：N={n} 需要 {head} 字节头部，文件只有 {total} 字节")

    entries: list[FsetEntry] = []
    expected_offset = head
    for i in range(n):
        offset, size, reserved = ENTRY_STRUCT.unpack_from(view, COUNT_STRUCT.size + ENTRY_SIZE * i)
        if size == 0:
            raise FsetError(f"第 {i} 帧 size=0")
        if offset + size > total:
            raise FsetError(f"第 {i} 帧越界：offset={offset} size={size}，文件只有 {total} 字节")
        if offset != expected_offset:
            if i == 0:
                raise FsetError(f"首帧 offset={offset}，应等于头部大小 4+12N={head}")
            raise FsetError(f"第 {i} 帧不连续：offset={offset}，上一帧结束于 {expected_offset}")
        if strict and reserved != 0:
            raise FsetError(f"第 {i} 帧 reserved={reserved}，应为 0")
        entries.append(FsetEntry(offset=offset, size=size, reserved=reserved))
        expected_offset = offset + size

    if strict and expected_offset != total:
        raise FsetError(f"文件尾部有 {total - expected_offset} 字节多余数据")
    return entries


def parse_fset(data: BytesLike, *, strict: bool = True) -> list[bytes]:
    """解析 `.fset`，返回每一帧的字节串。校验规则见 :func:`parse_fset_entries`。"""
    raw = bytes(data)
    return [raw[e.offset : e.offset + e.size] for e in parse_fset_entries(raw, strict=strict)]


def detect_frame_format(blob: BytesLike) -> FrameFormat:
    """按魔数识别帧格式：PNG 签名 / JPEG SOI，其余为 unknown。"""
    head = bytes(blob[:8])
    if head.startswith(_PNG_SIGNATURE):
        return "png"
    if head.startswith(_JPEG_SOI):
        return "jpeg"
    return "unknown"


def png_dimensions(blob: BytesLike) -> tuple[int, int] | None:
    """从 PNG 的 IHDR 块读取 (宽, 高)；不是合法 PNG 头则返回 None。

    PNG 结构：8 字节签名，随后第一个块必须是 IHDR（长度 13），宽高为其前 8 字节（大端）。
    """
    head = bytes(blob[:24])
    if len(head) < 24 or not head.startswith(_PNG_SIGNATURE):
        return None
    length, chunk_type = struct.unpack_from(">I4s", head, 8)
    if chunk_type != b"IHDR" or length != 13:
        return None
    width, height = struct.unpack_from(">II", head, 16)
    return int(width), int(height)


def inspect_fset(data: BytesLike, *, strict: bool = True) -> FsetInfo:
    """返回 `.fset` 的结构摘要（不合法时抛 :class:`FsetError`）。

    ``{"frames": N, "header_bytes": 4+12N, "total_bytes": len, "frame_sizes": [...],
    "formats": ["png"|"jpeg"|"unknown", ...], "png_dims": [(w, h) | None, ...]}``

    JPEG 帧只标 "jpeg"，不解析尺寸。
    """
    raw = bytes(data)
    entries = parse_fset_entries(raw, strict=strict)
    frames = [raw[e.offset : e.offset + e.size] for e in entries]
    return FsetInfo(
        frames=len(entries),
        header_bytes=header_size(len(entries)),
        total_bytes=len(raw),
        frame_sizes=[e.size for e in entries],
        formats=[detect_frame_format(f) for f in frames],
        png_dims=[png_dimensions(f) for f in frames],
    )
