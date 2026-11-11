from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from resolver.core import (
    BACKUP_DIRECTORY_NAME,
    MediaInfo,
    ResolverError,
    _extract_ffmpeg_error,
    _publish_mov,
    build_ffmpeg_command,
    mov_output_path,
    next_backup_path,
    output_container,
    output_path,
    probe_media,
    video_requires_high_bit_depth,
    video_requires_transcode,
)


class ProbeMediaTests(unittest.TestCase):
    def test_probe_extracts_duration_video_and_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "clip.mkv"
            source.touch()
            payload = {
                "format": {"duration": "12.5"},
                "streams": [
                    {"codec_type": "video", "codec_name": "h264"},
                    {"codec_type": "audio", "codec_name": "opus"},
                    {"codec_type": "audio", "codec_name": "aac"},
                ],
            }
            with patch("resolver.core.subprocess.run") as run:
                run.return_value.stdout = json.dumps(payload)
                info = probe_media(source, "ffprobe")

            self.assertEqual(
                info,
                MediaInfo(source.resolve(), 12.5, "h264", 2),
            )

    def test_probe_rejects_audio_only_files(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "song.wav"
            source.touch()
            payload = {"format": {"duration": "1"}, "streams": [{"codec_type": "audio"}]}
            with patch("resolver.core.subprocess.run") as run:
                run.return_value.stdout = json.dumps(payload)
                with self.assertRaisesRegex(ResolverError, "does not contain a video"):
                    probe_media(source, "ffprobe")

    def test_probe_extracts_hdr_video_properties(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "hdr.mkv"
            source.touch()
            payload = {
                "format": {"duration": "5"},
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "vp9",
                        "pix_fmt": "yuv420p10le",
                        "color_range": "tv",
                        "color_space": "bt2020nc",
                        "color_transfer": "smpte2084",
                        "color_primaries": "bt2020",
                    },
                    {"codec_type": "audio", "codec_name": "aac"},
                ],
            }
            with patch("resolver.core.subprocess.run") as run:
                run.return_value.stdout = json.dumps(payload)
                info = probe_media(source, "ffprobe")

            self.assertEqual(info.video_codec, "vp9")
            self.assertEqual(info.pixel_format, "yuv420p10le")
            self.assertEqual(info.color_transfer, "smpte2084")
            self.assertEqual(info.color_primaries, "bt2020")


class OutputNamingTests(unittest.TestCase):
    def test_uses_backup_suffix_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "take.one.mkv"
            source.touch()
            self.assertEqual(
                next_backup_path(source).name,
                "take.one_backup.mkv",
            )
            backup_directory = Path(directory) / BACKUP_DIRECTORY_NAME
            backup_directory.mkdir()
            (backup_directory / "take.one_backup.mkv").touch()
            self.assertEqual(
                next_backup_path(source).name,
                "take.one_backup_2.mkv",
            )

    def test_publish_creates_mov_and_moves_original_to_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "clip.mp4"
            output = Path(directory) / "clip.mov"
            temporary = Path(directory) / ".clip.partial.mov"
            source.write_bytes(b"original")
            temporary.write_bytes(b"converted")

            backup = _publish_mov(temporary, source, output)

            self.assertFalse(source.exists())
            self.assertEqual(output.read_bytes(), b"converted")
            self.assertEqual(backup.name, "clip_backup.mp4")
            self.assertEqual(backup.parent.name, BACKUP_DIRECTORY_NAME)
            self.assertEqual(backup.read_bytes(), b"original")
            self.assertFalse(temporary.exists())

    def test_publish_does_not_overwrite_existing_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "clip.mp4"
            output = Path(directory) / "clip.mov"
            temporary = Path(directory) / ".clip.partial.mov"
            backup_directory = Path(directory) / BACKUP_DIRECTORY_NAME
            backup_directory.mkdir()
            existing = backup_directory / "clip_backup.mp4"
            source.write_bytes(b"original")
            temporary.write_bytes(b"converted")
            existing.write_bytes(b"older backup")

            backup = _publish_mov(temporary, source, output)

            self.assertEqual(existing.read_bytes(), b"older backup")
            self.assertEqual(backup.name, "clip_backup_2.mp4")
            self.assertEqual(backup.read_bytes(), b"original")

    def test_publish_refuses_to_replace_a_source_that_changed(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "clip.mp4"
            output = Path(directory) / "clip.mov"
            temporary = Path(directory) / ".clip.partial.mov"
            source.write_bytes(b"original")
            identity = (source.stat().st_dev, source.stat().st_ino, 999, 999)
            temporary.write_bytes(b"converted")

            with self.assertRaisesRegex(ResolverError, "changed while"):
                _publish_mov(temporary, source, output, identity)

            self.assertEqual(source.read_bytes(), b"original")
            self.assertTrue(temporary.exists())
            self.assertFalse(output.exists())
            self.assertFalse(
                (Path(directory) / BACKUP_DIRECTORY_NAME / "clip_backup.mp4").exists()
            )

    def test_failed_atomic_replace_leaves_original_and_no_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "clip.mp4"
            output = Path(directory) / "clip.mov"
            temporary = Path(directory) / ".clip.partial.mov"
            source.write_bytes(b"original")
            temporary.write_bytes(b"converted")

            with patch("resolver.core.os.replace", side_effect=OSError("no space")):
                with self.assertRaisesRegex(OSError, "no space"):
                    _publish_mov(temporary, source, output)

            self.assertEqual(source.read_bytes(), b"original")
            self.assertTrue(temporary.exists())
            self.assertFalse(output.exists())
            self.assertFalse(
                (Path(directory) / BACKUP_DIRECTORY_NAME / "clip_backup.mp4").exists()
            )

    def test_existing_mov_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "clip.mp4"
            output = Path(directory) / "clip.mov"
            temporary = Path(directory) / ".clip.partial.mov"
            source.write_bytes(b"original")
            output.write_bytes(b"existing mov")
            temporary.write_bytes(b"converted")

            with self.assertRaisesRegex(ResolverError, "already exists"):
                _publish_mov(temporary, source, output)

            self.assertEqual(source.read_bytes(), b"original")
            self.assertEqual(output.read_bytes(), b"existing mov")
            self.assertTrue(temporary.exists())


class CommandTests(unittest.TestCase):
    def test_video_is_copied_and_audio_is_pcm_in_mov(self):
        command = build_ffmpeg_command(
            Path("/video/input.mp4"),
            Path("/video/.output.partial.mov"),
            "/usr/bin/ffmpeg",
        )
        self.assertIn("copy", command)
        self.assertEqual(command[command.index("-c:v") + 1], "copy")
        self.assertEqual(command[command.index("-c:a") + 1], "pcm_s24le")
        self.assertEqual(
            command[command.index("-filter:a") + 1],
            "aresample=48000:async=1:first_pts=0",
        )
        self.assertEqual(command[command.index("-f") + 1], "mov")
        self.assertIn("0:v:0", command)
        self.assertIn("0:a?", command)
        self.assertNotIn("-filter:v", command)

    def test_av1_is_transcoded_to_dnxhr_hq(self):
        command = build_ffmpeg_command(
            Path("/video/input.mp4"),
            Path("/video/.output.partial.mov"),
            "/usr/bin/ffmpeg",
            transcode_video=video_requires_transcode("av1"),
        )
        self.assertEqual(command[command.index("-c:v") + 1], "dnxhd")
        self.assertEqual(command[command.index("-profile:v") + 1], "dnxhr_hq")
        self.assertEqual(command[command.index("-pix_fmt") + 1], "yuv422p")

    def test_compatible_video_does_not_require_transcoding(self):
        self.assertFalse(video_requires_transcode("h264"))
        self.assertFalse(video_requires_transcode("hevc"))
        self.assertTrue(video_requires_transcode("AV1"))
        self.assertTrue(video_requires_transcode("vp9"))

    def test_hdr_vp9_is_transcoded_to_10_bit_dnxhr_hqx(self):
        info = MediaInfo(
            Path("/video/hdr.mkv"),
            10.0,
            "vp9",
            1,
            "yuv420p10le",
            "tv",
            "bt2020nc",
            "smpte2084",
            "bt2020",
        )
        command = build_ffmpeg_command(
            info.path,
            Path("/video/.output.partial.mov"),
            "/usr/bin/ffmpeg",
            transcode_video=video_requires_transcode(info.video_codec),
            high_bit_depth=video_requires_high_bit_depth(info),
            color_range=info.color_range,
            color_space=info.color_space,
            color_transfer=info.color_transfer,
            color_primaries=info.color_primaries,
        )
        self.assertEqual(command[command.index("-profile:v") + 1], "dnxhr_hqx")
        self.assertEqual(command[command.index("-pix_fmt") + 1], "yuv422p10le")
        self.assertEqual(command[command.index("-colorspace") + 1], "bt2020nc")
        self.assertEqual(command[command.index("-color_trc") + 1], "smpte2084")
        self.assertEqual(command[command.index("-color_primaries") + 1], "bt2020")

    def test_error_extraction_ignores_all_progress_timestamps(self):
        lines = [
            "[mov] av1 only supported in MP4 and AVIF.",
            "out_time_us=83333",
            "out_time_ms=83333",
            "out_time=00:00:00.083333",
            "progress=end",
        ]
        self.assertEqual(
            _extract_ffmpeg_error(lines),
            "[mov] av1 only supported in MP4 and AVIF.",
        )

    def test_error_extraction_prefers_specific_codec_error(self):
        lines = [
            "[mov] vp9 only supported in MP4.",
            "[out] Could not write header: Invalid argument",
            "[out] Nothing was written into output file.",
        ]
        self.assertEqual(
            _extract_ffmpeg_error(lines),
            "[mov] vp9 only supported in MP4.",
        )

    def test_output_uses_mov_extension_without_renaming_existing_mov(self):
        self.assertEqual(mov_output_path("clip.mp4"), Path("clip.mov"))
        self.assertEqual(mov_output_path("clip.MP4"), Path("clip.mov"))
        self.assertEqual(mov_output_path("clip.MOV"), Path("clip.MOV"))

    def test_mp4_and_mov_keep_their_name_and_container(self):
        for name, container in (
            ("clip.mp4", "mp4"),
            ("clip.MP4", "mp4"),
            ("clip.m4v", "mp4"),
            ("clip.MOV", "mov"),
        ):
            self.assertEqual(output_path(name), Path(name))
            self.assertEqual(output_container(name), container)

    def test_other_containers_become_mov(self):
        self.assertEqual(output_path("clip.mkv"), Path("clip.mov"))
        self.assertEqual(output_container("clip.mkv"), "mov")

    def test_transcoded_video_always_becomes_mov(self):
        self.assertEqual(
            output_path("clip.mp4", transcode_video=True), Path("clip.mov")
        )
        self.assertEqual(output_container("clip.mp4", transcode_video=True), "mov")

    def test_mp4_command_uses_mp4_muxer(self):
        command = build_ffmpeg_command(
            Path("/video/input.mp4"),
            Path("/video/.output.partial.mp4"),
            "/usr/bin/ffmpeg",
            container="mp4",
        )
        self.assertEqual(command[command.index("-f") + 1], "mp4")
        self.assertEqual(command[command.index("-c:v") + 1], "copy")
        self.assertEqual(command[command.index("-c:a") + 1], "pcm_s24le")


if __name__ == "__main__":
    unittest.main()
