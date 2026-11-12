from __future__ import annotations

import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

from resolver import graft
from resolver.core import BACKUP_DIRECTORY_NAME, Converter, build_ffmpeg_command

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


def run(*command: str) -> str:
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout


def make_clip(path: Path, *options: str, audio_tracks: int = 1) -> Path:
    """Write a two-second clip with long-GOP video and AAC audio."""
    inputs = ["-f", "lavfi", "-i", "testsrc=duration=2:size=160x120:rate=25"]
    maps = ["-map", "0:v"]
    for track in range(audio_tracks):
        inputs += ["-f", "lavfi", "-i", f"sine=frequency={440 * (track + 1)}:duration=2"]
        maps += ["-map", f"{track + 1}:a"]
    run(
        FFMPEG, "-v", "error", "-y", *inputs, *maps,
        "-c:v", "mpeg4", "-g", "25", "-bf", "2", "-c:a", "aac", *options, str(path),
    )
    return path


def video_packets(path: Path) -> str:
    return run(
        FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
        "packet=pts,dts,size,flags", "-show_data_hash", "md5", "-of", "csv", str(path),
    )


def audio_codecs(path: Path) -> list[str]:
    return run(
        FFPROBE, "-v", "error", "-select_streams", "a", "-show_entries",
        "stream=codec_name", "-of", "csv=p=0", str(path),
    ).split()


def audio_md5(path: Path) -> str:
    return run(FFMPEG, "-v", "error", "-i", str(path), "-map", "0:a", "-f", "md5", "-")


def top_level(path: Path) -> list[bytes]:
    with path.open("rb") as handle:
        return [kind for kind, *_ in graft._top_level(handle, path.stat().st_size)]


def tracks(path: Path) -> list[tuple[bytes, int, tuple[int, ...]]]:
    """Each track's handler, ID and chunk positions."""
    found = []
    for box in graft._Index(path, deep=True).boxes:
        if box.kind != b"trak":
            continue
        table = next(
            child
            for child in box.find(b"mdia", b"minf", b"stbl").children
            if child.kind in (b"stco", b"co64")
        )
        (count,) = struct.unpack_from(">I", table.payload, 4)
        code = "I" if table.kind == b"stco" else "Q"
        offsets = struct.unpack_from(f">{count}{code}", table.payload, 8)
        track_id = graft._read_tkhd(graft._track_header(box).payload)[3]
        found.append((graft._handler(box), track_id, offsets))
    return found


@unittest.skipUnless(FFMPEG and FFPROBE, "FFmpeg is not installed")
class GraftedConversionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def convert(self, source: Path) -> Path:
        result = Converter().convert(source)
        self.assertEqual(result.path, source)
        self.assertEqual(
            sorted(path.name for path in self.directory.iterdir()),
            sorted([source.name, BACKUP_DIRECTORY_NAME]),
            "temporary files were left behind",
        )
        return result.backup_path

    def assert_video_untouched(self, converted: Path, original: Path) -> None:
        self.assertEqual(video_packets(converted), video_packets(original))
        before = [track for track in tracks(original) if track[0] != b"soun"]
        after = [track for track in tracks(converted) if track[0] != b"soun"]
        self.assertEqual(after, before, "video is no longer where it was in the file")

    def test_mp4_keeps_video_in_place_and_gains_pcm(self):
        source = make_clip(self.directory / "clip.mp4")
        backup = self.convert(source)
        self.assert_video_untouched(source, backup)
        self.assertEqual(audio_codecs(source), ["pcm_s24le"])
        self.assertEqual(top_level(source)[-2:], [b"mdat", b"moov"])
        self.assertEqual(top_level(source).count(b"moov"), 1)
        # Everything the original file held is still at the start of the new one.
        size = backup.stat().st_size
        original = bytearray(backup.read_bytes())
        with source.open("rb") as handle:
            copied = bytearray(handle.read(size))
        retired = original.find(b"moov") - 4
        self.assertEqual(copied[retired + 4 : retired + 8], b"free")
        copied[retired + 4 : retired + 8] = b"moov"
        self.assertEqual(copied, original)

    def test_index_at_the_front_is_retired_in_place(self):
        source = make_clip(self.directory / "clip.mp4", "-movflags", "+faststart")
        self.assertLess(top_level(source).index(b"moov"), top_level(source).index(b"mdat"))
        backup = self.convert(source)
        self.assert_video_untouched(source, backup)
        self.assertEqual(audio_codecs(source), ["pcm_s24le"])

    def test_mov_keeps_video_in_place(self):
        source = make_clip(self.directory / "clip.mov")
        backup = self.convert(source)
        self.assert_video_untouched(source, backup)
        self.assertEqual(audio_codecs(source), ["pcm_s24le"])

    def test_every_audio_track_is_replaced_and_keeps_its_id(self):
        source = make_clip(self.directory / "clip.mp4", audio_tracks=2)
        ids_before = [track[1] for track in tracks(source)]
        backup = self.convert(source)
        self.assert_video_untouched(source, backup)
        self.assertEqual(audio_codecs(source), ["pcm_s24le", "pcm_s24le"])
        self.assertEqual([track[1] for track in tracks(source)], ids_before)

    def test_audio_matches_a_plain_remux(self):
        source = make_clip(self.directory / "clip.mp4")
        remuxed = self.directory.parent / f"{self.directory.name}-remux.mp4"
        self.addCleanup(remuxed.unlink, missing_ok=True)
        run(*[
            argument
            for argument in build_ffmpeg_command(source, remuxed, FFMPEG, container="mp4")
            if argument not in ("-progress", "pipe:1", "-nostats")
        ])
        self.convert(source)
        self.assertEqual(audio_md5(source), audio_md5(remuxed))

    def test_fragmented_file_is_remuxed_instead(self):
        source = make_clip(
            self.directory / "clip.mp4", "-movflags", "+frag_keyframe+empty_moov"
        )
        with self.assertRaises(graft.GraftUnsupported):
            graft.check_source(source)
        self.convert(source)
        self.assertEqual(audio_codecs(source), ["pcm_s24le"])
        self.assertNotIn(b"moof", top_level(source))

    def test_cancelling_stops_the_copy(self):
        source = make_clip(self.directory / "clip.mp4")
        audio = self.directory / "audio.mp4"
        run(FFMPEG, "-v", "error", "-i", str(source), "-map", "0:a", "-c:a", "pcm_s24le", str(audio))
        with self.assertRaises(graft.GraftCancelled):
            graft.graft_audio(source, audio, self.directory / "out.mp4", cancelled=lambda: True)

    def test_mismatched_audio_is_refused(self):
        source = make_clip(self.directory / "clip.mp4", audio_tracks=2)
        audio = self.directory / "audio.mp4"
        run(FFMPEG, "-v", "error", "-i", str(source), "-map", "0:a:0", "-c:a", "pcm_s24le", str(audio))
        with self.assertRaises(graft.GraftUnsupported):
            graft.graft_audio(source, audio, self.directory / "out.mp4")
        self.assertFalse((self.directory / "out.mp4").exists())


class BoxTests(unittest.TestCase):
    def test_chunk_positions_widen_past_four_gigabytes(self):
        table = graft.Box(b"stco", struct.pack(">II2I", 0, 2, 100, 200))
        stbl = graft.Box(b"stbl", children=[table])
        graft._shift_chunk_offsets(stbl, 5_000_000_000)
        self.assertEqual(table.kind, b"co64")
        self.assertEqual(
            struct.unpack_from(">2Q", table.payload, 8), (5_000_000_100, 5_000_000_200)
        )

    def test_chunk_positions_stay_narrow_when_they_fit(self):
        table = graft.Box(b"co64", struct.pack(">II2Q", 0, 2, 100, 200))
        graft._shift_chunk_offsets(graft.Box(b"stbl", children=[table]), 50)
        self.assertEqual(table.kind, b"stco")
        self.assertEqual(struct.unpack_from(">2I", table.payload, 8), (150, 250))

    def test_edit_list_is_rescaled_and_widened(self):
        short = struct.pack(">II", 0, 1) + struct.pack(">Ii4s", 2000, 0, b"\x00\x01\x00\x00")
        self.assertEqual(
            struct.unpack_from(">Ii", graft._rescale_edit_list(short, 1000, 600), 8), (1200, 0)
        )
        widened = graft._rescale_edit_list(short, 1000, 3_528_000_000)
        self.assertEqual(widened[0], 1)
        self.assertEqual(struct.unpack_from(">Qq", widened, 8), (7_056_000_000, 0))

    def test_track_header_round_trips_and_widens(self):
        rest = bytes(60)
        narrow = graft._write_tkhd(b"\x00\x00\x03", 1, 2, 7, 90_000, rest)
        self.assertEqual(graft._read_tkhd(narrow), (b"\x00\x00\x03", 1, 2, 7, 90_000, rest))
        wide = graft._write_tkhd(b"\x00\x00\x03", 1, 2, 7, 2**33, rest)
        self.assertEqual(wide[0], 1)
        self.assertEqual(graft._read_tkhd(wide), (b"\x00\x00\x03", 1, 2, 7, 2**33, rest))

    def test_open_ended_box_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clip.mov"
            path.write_bytes(struct.pack(">I4s", 8, b"wide") + struct.pack(">I4s", 0, b"mdat") + b"x" * 32)
            with self.assertRaises(graft.GraftUnsupported):
                graft.check_source(path)


if __name__ == "__main__":
    unittest.main()
