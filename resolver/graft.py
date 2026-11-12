"""Give an MP4 or MOV new audio tracks without moving its video.

A converted file that keeps its name replaces a file DaVinci Resolve may
already have open. Resolve keeps the video readers it created for the old file,
together with the sample positions they read from it, until the project is
closed. If the new file stores the video anywhere else, those readers fetch the
wrong bytes and Resolve draws "Media Offline" on the frames they serve.

So the video is not rewritten at all. The source is copied byte for byte, the
new audio data is appended after it, and a new index (``moov``) is written at
the end. The new index is the old one with its audio tracks swapped for the new
ones; every other track still points at the same positions as before. The old
index is turned into a ``free`` box and the old audio data stays behind as
unreferenced bytes.
"""

from __future__ import annotations

import os
import struct
from pathlib import Path
from typing import Callable, Iterable

COPY_CHUNK = 8 * 1024 * 1024
MAX_INDEX_BYTES = 1024 * 1024 * 1024
# Boxes whose children are read. Everything else is carried as opaque bytes.
CONTAINERS = frozenset({b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts"})
UINT32_MAX = 0xFFFFFFFF
INT32_MIN, INT32_MAX = -(2**31), 2**31 - 1


class GraftUnsupported(Exception):
    """The file's structure rules out grafting; convert it another way."""


class GraftCancelled(Exception):
    """The caller asked to stop while the file was being written."""


class Box:
    __slots__ = ("kind", "payload", "children")

    def __init__(self, kind: bytes, payload: bytes = b"", children: list[Box] | None = None):
        self.kind = kind
        self.payload = payload
        self.children = children

    def find(self, *kinds: bytes) -> Box | None:
        """Return the first descendant reached by following ``kinds`` in order."""
        box: Box | None = self
        for kind in kinds:
            if box is None or box.children is None:
                return None
            box = next((child for child in box.children if child.kind == kind), None)
        return box

    def serialize(self) -> bytes:
        body = (
            b"".join(child.serialize() for child in self.children)
            if self.children is not None
            else self.payload
        )
        size = len(body) + 8
        if size > UINT32_MAX:
            return struct.pack(">I4sQ", 1, self.kind, size + 8) + body
        return struct.pack(">I4s", size, self.kind) + body


def _parse(data: bytes, containers: Iterable[bytes] = CONTAINERS) -> list[Box]:
    boxes: list[Box] = []
    position, end = 0, len(data)
    while position < end:
        if end - position < 8:
            raise GraftUnsupported("damaged index")
        size, kind = struct.unpack_from(">I4s", data, position)
        header = 8
        if size == 1:
            if end - position < 16:
                raise GraftUnsupported("damaged index")
            size = struct.unpack_from(">Q", data, position + 8)[0]
            header = 16
        elif size == 0:
            size = end - position
        if size < header or position + size > end:
            raise GraftUnsupported("damaged index")
        body = data[position + header : position + size]
        if kind in containers:
            boxes.append(Box(kind, children=_parse(body, containers)))
        else:
            boxes.append(Box(kind, payload=body))
        position += size
    return boxes


def _top_level(handle, size: int) -> list[tuple[bytes, int, int, int]]:
    """List the file's top-level boxes as (kind, offset, size, header size)."""
    boxes = []
    position = 0
    while position < size:
        handle.seek(position)
        header = handle.read(16)
        if len(header) < 8:
            raise GraftUnsupported("damaged file structure")
        length, kind = struct.unpack_from(">I4s", header)
        header_size = 8
        if length == 1:
            if len(header) < 16:
                raise GraftUnsupported("damaged file structure")
            length = struct.unpack_from(">Q", header, 8)[0]
            header_size = 16
        elif length == 0:
            # Runs to the end of the file, so nothing can be appended after it.
            raise GraftUnsupported("open-ended box")
        if length < header_size or position + length > size:
            raise GraftUnsupported("damaged file structure")
        boxes.append((kind, position, length, header_size))
        position += length
    return boxes


class _Index:
    """The parts of a file's structure needed to graft."""

    def __init__(self, path: Path, *, deep: bool):
        self.size = path.stat().st_size
        with path.open("rb") as handle:
            self.top = _top_level(handle, self.size)
            kinds = [kind for kind, *_ in self.top]
            if kinds.count(b"moov") != 1 or b"mdat" not in kinds:
                raise GraftUnsupported("unexpected file structure")
            if b"moof" in kinds:
                raise GraftUnsupported("fragmented file")
            _, self.moov_offset, moov_size, moov_header = next(
                box for box in self.top if box[0] == b"moov"
            )
            if moov_size > MAX_INDEX_BYTES:
                raise GraftUnsupported("index too large")
            handle.seek(self.moov_offset + moov_header)
            moov = handle.read(moov_size - moov_header)
        if len(moov) != moov_size - moov_header:
            raise GraftUnsupported("damaged file structure")
        # A source's tracks are kept verbatim, so only the new audio is parsed
        # all the way down.
        self.boxes = _parse(moov, CONTAINERS if deep else ())
        kinds = [box.kind for box in self.boxes]
        if b"mvex" in kinds or b"cmov" in kinds or kinds.count(b"mvhd") != 1:
            raise GraftUnsupported("unsupported index")
        self.mvhd = next(box for box in self.boxes if box.kind == b"mvhd")
        self.timescale = _read_mvhd(self.mvhd.payload)[3]
        if not self.timescale:
            raise GraftUnsupported("unsupported index")

    def tracks(self, handler: bytes) -> list[int]:
        """Positions in ``boxes`` of the tracks with this handler type."""
        return [
            position
            for position, box in enumerate(self.boxes)
            if box.kind == b"trak" and _handler(box) == handler
        ]


def _handler(trak: Box) -> bytes:
    children = trak.children
    if children is None:
        children = _parse(trak.payload, {b"mdia"})
    hdlr = Box(b"trak", children=children).find(b"mdia", b"hdlr")
    if hdlr is None or len(hdlr.payload) < 12:
        raise GraftUnsupported("track without a handler")
    return hdlr.payload[8:12]


def _track_header(trak: Box) -> Box:
    children = trak.children if trak.children is not None else _parse(trak.payload, ())
    tkhd = next((child for child in children if child.kind == b"tkhd"), None)
    if tkhd is None:
        raise GraftUnsupported("track without a header")
    return tkhd


# -- fixed-layout boxes -------------------------------------------------------


def _read_mvhd(payload: bytes) -> tuple[bytes, int, int, int, int, bytes]:
    try:
        if payload[0] == 1:
            created, modified, timescale, duration = struct.unpack_from(">QQIQ", payload, 4)
            return payload[1:4], created, modified, timescale, duration, payload[32:]
        created, modified, timescale, duration = struct.unpack_from(">IIII", payload, 4)
        return payload[1:4], created, modified, timescale, duration, payload[20:]
    except (struct.error, IndexError) as exc:
        raise GraftUnsupported("damaged movie header") from exc


def _write_mvhd(flags, created, modified, timescale, duration, rest) -> bytes:
    if max(created, modified, duration) > UINT32_MAX:
        return b"\x01" + flags + struct.pack(">QQIQ", created, modified, timescale, duration) + rest
    return b"\x00" + flags + struct.pack(">IIII", created, modified, timescale, duration) + rest


def _read_tkhd(payload: bytes) -> tuple[bytes, int, int, int, int, bytes]:
    try:
        if payload[0] == 1:
            created, modified, track_id, _, duration = struct.unpack_from(">QQIIQ", payload, 4)
            return payload[1:4], created, modified, track_id, duration, payload[36:]
        created, modified, track_id, _, duration = struct.unpack_from(">IIIII", payload, 4)
        return payload[1:4], created, modified, track_id, duration, payload[24:]
    except (struct.error, IndexError) as exc:
        raise GraftUnsupported("damaged track header") from exc


def _write_tkhd(flags, created, modified, track_id, duration, rest) -> bytes:
    if max(created, modified, duration) > UINT32_MAX:
        return b"\x01" + flags + struct.pack(">QQIIQ", created, modified, track_id, 0, duration) + rest
    return b"\x00" + flags + struct.pack(">IIIII", created, modified, track_id, 0, duration) + rest


def _rescale(value: int, source: int, target: int) -> int:
    return (value * target + source // 2) // source


def _rescale_edit_list(payload: bytes, source: int, target: int) -> bytes:
    """Re-express an edit list's segment durations in another movie timescale."""
    try:
        version, flags = payload[0], payload[1:4]
        (count,) = struct.unpack_from(">I", payload, 4)
        layout, width = (">Qq4s", 20) if version == 1 else (">Ii4s", 12)
        entries = [struct.unpack_from(layout, payload, 8 + width * n) for n in range(count)]
    except (struct.error, IndexError) as exc:
        raise GraftUnsupported("damaged edit list") from exc
    entries = [(_rescale(length, source, target), start, rate) for length, start, rate in entries]
    wide = any(
        length > UINT32_MAX or not INT32_MIN <= start <= INT32_MAX
        for length, start, _ in entries
    )
    layout = ">Qq4s" if wide else ">Ii4s"
    return (
        (b"\x01" if wide else b"\x00")
        + flags
        + struct.pack(">I", count)
        + b"".join(struct.pack(layout, *entry) for entry in entries)
    )


def _shift_chunk_offsets(stbl: Box, delta: int) -> None:
    """Move a track's chunk positions by ``delta`` bytes, widening if needed."""
    assert stbl.children is not None
    for box in stbl.children:
        if box.kind not in (b"stco", b"co64"):
            continue
        code = "I" if box.kind == b"stco" else "Q"
        try:
            (count,) = struct.unpack_from(">I", box.payload, 4)
            offsets = struct.unpack_from(f">{count}{code}", box.payload, 8)
        except struct.error as exc:
            raise GraftUnsupported("damaged chunk table") from exc
        offsets = [offset + delta for offset in offsets]
        wide = bool(offsets) and max(offsets) > UINT32_MAX
        box.kind = b"co64" if wide else b"stco"
        box.payload = box.payload[:8] + struct.pack(f">{count}{'Q' if wide else 'I'}", *offsets)
        return
    raise GraftUnsupported("track without a chunk table")


# -- public API ---------------------------------------------------------------


def check_source(source: Path) -> int:
    """Return how many audio tracks ``source`` has, or raise GraftUnsupported."""
    try:
        index = _Index(source, deep=False)
        if not index.tracks(b"vide"):
            raise GraftUnsupported("no video track")
        audio = index.tracks(b"soun")
    except OSError as exc:
        raise GraftUnsupported(str(exc)) from exc
    if not audio:
        raise GraftUnsupported("no audio track")
    return len(audio)


def graft_audio(
    source: Path,
    audio: Path,
    output: Path,
    *,
    cancelled: Callable[[], bool] | None = None,
    progress: Callable[[float], None] | None = None,
) -> None:
    """Write ``output``: ``source`` with its audio tracks replaced by ``audio``'s.

    ``audio`` is an audio-only file of the same container family whose tracks
    correspond, in order, to the audio tracks of ``source``.
    """
    old = _Index(source, deep=False)
    new = _Index(audio, deep=True)
    old_audio = old.tracks(b"soun")
    new_audio = new.tracks(b"soun")
    if not old.tracks(b"vide") or not old_audio or len(old_audio) != len(new_audio):
        raise GraftUnsupported("audio tracks do not line up")
    data_boxes = [box for box in new.top if box[0] == b"mdat"]
    if len(data_boxes) != 1:
        raise GraftUnsupported("unexpected audio file structure")
    _, data_offset, data_size, _ = data_boxes[0]

    # The audio data lands right after the copied source.
    delta = old.size - data_offset
    longest = 0
    for old_position, new_position in zip(old_audio, new_audio):
        trak = new.boxes[new_position]
        replaced = old.boxes[old_position]
        # Keep the track ID, so references from other tracks still resolve.
        track_id = _read_tkhd(_track_header(replaced).payload)[3]
        header = _track_header(trak)
        flags, created, modified, _, duration, rest = _read_tkhd(header.payload)
        duration = _rescale(duration, new.timescale, old.timescale)
        longest = max(longest, duration)
        header.payload = _write_tkhd(flags, created, modified, track_id, duration, rest)
        edits = trak.find(b"edts", b"elst")
        if edits is not None:
            edits.payload = _rescale_edit_list(edits.payload, new.timescale, old.timescale)
        stbl = trak.find(b"mdia", b"minf", b"stbl")
        if stbl is None:
            raise GraftUnsupported("unexpected audio file structure")
        _shift_chunk_offsets(stbl, delta)
        old.boxes[old_position] = trak

    flags, created, modified, timescale, duration, rest = _read_mvhd(old.mvhd.payload)
    old.mvhd.payload = _write_mvhd(
        flags, created, modified, timescale, max(duration, longest), rest
    )
    moov = Box(b"moov", children=old.boxes).serialize()

    total = old.size + data_size
    written = 0

    def copy(reader, length: int, writer) -> None:
        nonlocal written
        while length:
            if cancelled is not None and cancelled():
                raise GraftCancelled()
            chunk = reader.read(min(COPY_CHUNK, length))
            if not chunk:
                raise OSError("file ended while it was being copied")
            writer.write(chunk)
            length -= len(chunk)
            written += len(chunk)
            if progress is not None:
                progress(written / total)

    with output.open("xb") as writer:
        with source.open("rb") as reader:
            copy(reader, old.size, writer)
        with audio.open("rb") as reader:
            reader.seek(data_offset)
            copy(reader, data_size, writer)
        writer.write(moov)
        # Retire the old index only now that its replacement is in the file.
        writer.seek(old.moov_offset + 4)
        writer.write(b"free")
        writer.flush()
        os.fsync(writer.fileno())
