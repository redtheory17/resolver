from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from resolver import graft


class ResolverError(RuntimeError):
    """A user-facing conversion error."""


class ConversionCancelled(ResolverError):
    """Raised when the user cancels an active conversion."""


@dataclass(frozen=True)
class MediaInfo:
    path: Path
    duration_seconds: float
    video_codec: str
    audio_streams: int
    pixel_format: str = ""
    color_range: str = ""
    color_space: str = ""
    color_transfer: str = ""
    color_primaries: str = ""


@dataclass(frozen=True)
class Progress:
    fraction: float
    seconds: float


@dataclass(frozen=True)
class ConversionResult:
    path: Path
    backup_path: Path


ProgressCallback = Callable[[Progress], None]
BACKUP_DIRECTORY_NAME = ".resolver-backups"
# The folder name used while the app was called Resolve Ready. A folder that
# already has one keeps using it, so backups never end up in two places.
LEGACY_BACKUP_DIRECTORY_NAME = ".resolve-ready-backups"
VIDEO_CODECS_REQUIRING_TRANSCODE = frozenset({"av1", "vp8", "vp9"})
# Sources that keep their own name and container, so a project that already
# references them (on this or a collaborator's machine) stays linked. MP4 holds
# PCM as an ISO 23003-5 "ipcm" track, which needs FFmpeg 6.1 or newer.
SAME_NAME_CONTAINERS = {".mov": "mov", ".mp4": "mp4", ".m4v": "mp4"}


def require_tools() -> tuple[str, str]:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        missing = " and ".join(
            name
            for name, value in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe))
            if not value
        )
        raise ResolverError(
            f"{missing} is required. Install the FFmpeg package from your Linux "
            "distribution and try again."
        )
    return ffmpeg, ffprobe


def probe_media(path: str | os.PathLike[str], ffprobe: str | None = None) -> MediaInfo:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise ResolverError(f"File not found: {source}")

    if ffprobe is None:
        _, ffprobe = require_tools()

    command = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        (
            "format=duration:stream=codec_type,codec_name,pix_fmt,color_range,"
            "color_space,color_transfer,color_primaries"
        ),
        "-of",
        "json",
        str(source),
    ]
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        payload = json.loads(result.stdout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        detail = getattr(exc, "stderr", "") or ""
        detail = detail.strip().splitlines()[-1] if detail.strip() else "Unsupported media file"
        raise ResolverError(f"Could not inspect {source.name}: {detail}") from exc

    streams = payload.get("streams", [])
    video_streams = [stream for stream in streams if stream.get("codec_type") == "video"]
    audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if not video_streams:
        raise ResolverError(f"{source.name} does not contain a video stream.")
    if not audio_streams:
        raise ResolverError(f"{source.name} does not contain an audio stream.")

    try:
        duration = max(0.0, float(payload.get("format", {}).get("duration", 0.0)))
    except (TypeError, ValueError):
        duration = 0.0

    video_stream = video_streams[0]
    return MediaInfo(
        path=source,
        duration_seconds=duration,
        video_codec=video_stream.get("codec_name") or "unknown",
        audio_streams=len(audio_streams),
        pixel_format=video_stream.get("pix_fmt") or "",
        color_range=video_stream.get("color_range") or "",
        color_space=video_stream.get("color_space") or "",
        color_transfer=video_stream.get("color_transfer") or "",
        color_primaries=video_stream.get("color_primaries") or "",
    )


def backup_directory_for(source: str | os.PathLike[str]) -> Path:
    """Return the backup folder for ``source``: the existing one, else the default."""
    parent = Path(source).parent
    current = parent / BACKUP_DIRECTORY_NAME
    legacy = parent / LEGACY_BACKUP_DIRECTORY_NAME
    if legacy.is_dir() and not current.exists():
        return legacy
    return current


def next_backup_path(source: str | os.PathLike[str]) -> Path:
    source_path = Path(source)
    backup_directory = backup_directory_for(source_path)
    first = backup_directory / (
        f"{source_path.stem}_backup{source_path.suffix}"
    )
    if not first.exists():
        return first

    index = 2
    while True:
        candidate = backup_directory / (
            f"{source_path.stem}_backup_{index}{source_path.suffix}"
        )
        if not candidate.exists():
            return candidate
        index += 1


def mov_output_path(source: str | os.PathLike[str]) -> Path:
    source_path = Path(source)
    if source_path.suffix.lower() == ".mov":
        return source_path
    return source_path.with_suffix(".mov")


def output_container(
    source: str | os.PathLike[str], transcode_video: bool = False
) -> str:
    """Return the FFmpeg muxer for a source: its own container when possible."""
    if transcode_video:
        # DNxHR is only stored in QuickTime.
        return "mov"
    return SAME_NAME_CONTAINERS.get(Path(source).suffix.lower(), "mov")


def output_path(
    source: str | os.PathLike[str], transcode_video: bool = False
) -> Path:
    """Return where the converted file goes, keeping the name when possible."""
    source_path = Path(source)
    if output_container(source_path, transcode_video) == "mp4":
        return source_path
    return mov_output_path(source_path)


def build_ffmpeg_command(
    source: Path,
    temporary_output: Path,
    ffmpeg: str = "ffmpeg",
    *,
    container: str = "mov",
    transcode_video: bool = False,
    high_bit_depth: bool = False,
    color_range: str = "",
    color_space: str = "",
    color_transfer: str = "",
    color_primaries: str = "",
) -> list[str]:
    """Build a Resolve-friendly MOV or MP4 conversion command."""
    if transcode_video:
        profile = "dnxhr_hqx" if high_bit_depth else "dnxhr_hq"
        pixel_format = "yuv422p10le" if high_bit_depth else "yuv422p"
        video_options = [
            "-c:v",
            "dnxhd",
            "-profile:v",
            profile,
            "-pix_fmt",
            pixel_format,
        ]
        for option, value in (
            ("-color_range", color_range),
            ("-colorspace", color_space),
            ("-color_trc", color_transfer),
            ("-color_primaries", color_primaries),
        ):
            if value and value != "unknown":
                video_options.extend((option, value))
    else:
        video_options = ["-c:v", "copy"]

    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-n",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-map_metadata",
        "0",
        "-map_metadata:s:v:0",
        "0:s:v:0",
        "-map_chapters",
        "0",
        *video_options,
        "-c:a",
        "pcm_s24le",
        "-filter:a",
        "aresample=48000:async=1:first_pts=0",
        "-movflags",
        "+use_metadata_tags",
        "-f",
        container,
        "-progress",
        "pipe:1",
        "-nostats",
        str(temporary_output),
    ]


def build_audio_command(
    source: Path,
    temporary_output: Path,
    ffmpeg: str = "ffmpeg",
    *,
    container: str = "mov",
) -> list[str]:
    """Build the command for the PCM audio that is grafted onto a source."""
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-n",
        "-i",
        str(source),
        "-map",
        "0:a",
        "-map_chapters",
        "-1",
        "-c:a",
        "pcm_s24le",
        "-filter:a",
        "aresample=48000:async=1:first_pts=0",
        "-f",
        container,
        "-progress",
        "pipe:1",
        "-nostats",
        str(temporary_output),
    ]


def video_requires_transcode(codec: str) -> bool:
    """Return whether a source codec cannot be stream-copied into MOV."""
    return codec.casefold() in VIDEO_CODECS_REQUIRING_TRANSCODE


def video_requires_high_bit_depth(info: MediaInfo) -> bool:
    """Return whether DNxHR HQX is needed to retain source precision or HDR."""
    pixel_format = info.pixel_format.casefold()
    is_high_bit_depth = any(
        marker in pixel_format
        for marker in ("p9", "p10", "p12", "p14", "p16", "p010", "p012")
    )
    is_hdr = (
        info.color_transfer.casefold() in {"smpte2084", "arib-std-b67"}
        or info.color_primaries.casefold() == "bt2020"
    )
    return is_high_bit_depth or is_hdr


class Converter:
    def __init__(self, ffmpeg: str | None = None, ffprobe: str | None = None):
        if ffmpeg is None or ffprobe is None:
            detected_ffmpeg, detected_ffprobe = require_tools()
            ffmpeg = ffmpeg or detected_ffmpeg
            ffprobe = ffprobe or detected_ffprobe
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._cancel_requested = threading.Event()

    def cancel(self) -> None:
        self._cancel_requested.set()
        with self._lock:
            if self._process and self._process.poll() is None:
                self._process.terminate()

    def convert(
        self,
        source: str | os.PathLike[str],
        progress_callback: ProgressCallback | None = None,
    ) -> ConversionResult:
        if self._cancel_requested.is_set():
            raise ConversionCancelled("Conversion cancelled.")
        info = probe_media(source, self.ffprobe)
        if self._cancel_requested.is_set():
            raise ConversionCancelled("Conversion cancelled.")
        transcode_video = video_requires_transcode(info.video_codec)
        container = output_container(info.path, transcode_video)
        output = output_path(info.path, transcode_video)
        if output != info.path and output.exists():
            raise ResolverError(
                f"Cannot create {output.name} because that file already exists. "
                "Move or rename it and try again."
            )
        source_identity = _source_identity(info.path)
        token = uuid.uuid4().hex
        temporary = info.path.with_name(f".{info.path.stem}.{token}.partial.{container}")
        audio_temporary = info.path.with_name(
            f".{info.path.stem}.{token}.partial.audio.{container}"
        )

        try:
            # A file that keeps its name may already be open in Resolve, so its
            # video must stay where it is in the file (see resolver.graft).
            grafted = output == info.path and self._graft(
                info, container, audio_temporary, temporary, progress_callback
            )
            if not grafted:
                command = build_ffmpeg_command(
                    info.path,
                    temporary,
                    self.ffmpeg,
                    container=container,
                    transcode_video=transcode_video,
                    high_bit_depth=video_requires_high_bit_depth(info),
                    color_range=info.color_range,
                    color_space=info.color_space,
                    color_transfer=info.color_transfer,
                    color_primaries=info.color_primaries,
                )
                self._run_ffmpeg(command, info, progress_callback)
            if not temporary.is_file():
                raise ResolverError("FFmpeg finished without creating an output file.")
            if self._cancel_requested.is_set():
                raise ConversionCancelled("Conversion cancelled.")

            backup = _publish_mov(temporary, info.path, output, source_identity)
            if progress_callback:
                progress_callback(Progress(1.0, info.duration_seconds))
            return ConversionResult(output, backup)
        except OSError as exc:
            raise ResolverError(f"Could not write the converted file: {exc}") from exc
        finally:
            with self._lock:
                self._process = None
            for leftover in (temporary, audio_temporary):
                try:
                    leftover.unlink(missing_ok=True)
                except OSError:
                    pass

    def _run_ffmpeg(
        self,
        command: list[str],
        info: MediaInfo,
        progress_callback: ProgressCallback | None,
        share: float = 1.0,
    ) -> None:
        """Run FFmpeg, reporting progress as ``share`` of the whole conversion."""
        recent_output: list[str] = []
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        with self._lock:
            self._process = process
            if self._cancel_requested.is_set():
                process.terminate()

        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.strip()
            if line:
                recent_output.append(line)
                recent_output = recent_output[-100:]
            if line.startswith("out_time_us="):
                try:
                    seconds = int(line.partition("=")[2]) / 1_000_000
                except ValueError:
                    continue
                fraction = (
                    min(1.0, seconds / info.duration_seconds)
                    if info.duration_seconds
                    else 0.0
                )
                if progress_callback:
                    progress_callback(Progress(fraction * share, seconds * share))

        process.stdout.close()
        return_code = process.wait()
        with self._lock:
            self._process = None

        if return_code < 0:
            raise ConversionCancelled("Conversion cancelled.")
        if return_code != 0:
            message = _extract_ffmpeg_error(recent_output)
            raise ResolverError(f"FFmpeg could not convert {info.path.name}: {message}")

    def _graft(
        self,
        info: MediaInfo,
        container: str,
        audio_temporary: Path,
        temporary: Path,
        progress_callback: ProgressCallback | None,
    ) -> bool:
        """Write ``temporary`` as the source plus PCM audio, video left in place.

        Returns False when the file's structure rules that out, in which case
        nothing is left behind and the caller remuxes the file instead.
        """
        try:
            graft.check_source(info.path)
        except graft.GraftUnsupported:
            return False
        self._run_ffmpeg(
            build_audio_command(info.path, audio_temporary, self.ffmpeg, container=container),
            info,
            progress_callback,
            share=0.5,
        )
        if self._cancel_requested.is_set():
            raise ConversionCancelled("Conversion cancelled.")

        def report(fraction: float) -> None:
            if progress_callback:
                done = 0.5 + 0.5 * fraction
                progress_callback(Progress(done, done * info.duration_seconds))

        try:
            graft.graft_audio(
                info.path,
                audio_temporary,
                temporary,
                cancelled=self._cancel_requested.is_set,
                progress=report,
            )
            usable = self._graft_is_sound(temporary, info)
        except graft.GraftCancelled as exc:
            raise ConversionCancelled("Conversion cancelled.") from exc
        except graft.GraftUnsupported:
            usable = False
        finally:
            audio_temporary.unlink(missing_ok=True)
        if not usable:
            temporary.unlink(missing_ok=True)
        return usable

    def _graft_is_sound(self, candidate: Path, info: MediaInfo) -> bool:
        """Check that FFmpeg reads the grafted file as the source with PCM audio."""
        try:
            result = subprocess.run(
                [
                    self.ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "stream=codec_type,codec_name",
                    "-of",
                    "json",
                    str(candidate),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
            streams = json.loads(result.stdout).get("streams", [])
        except (subprocess.SubprocessError, json.JSONDecodeError):
            return False
        video = [s for s in streams if s.get("codec_type") == "video"]
        audio = [s for s in streams if s.get("codec_type") == "audio"]
        return (
            bool(video)
            and video[0].get("codec_name") == info.video_codec
            and len(audio) == info.audio_streams
            and all(str(s.get("codec_name", "")).startswith("pcm_") for s in audio)
        )


def _source_identity(source: Path) -> tuple[int, int, int, int]:
    try:
        stat = source.stat()
    except OSError as exc:
        raise ResolverError(f"Could not inspect {source.name}: {exc}") from exc
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def _publish_mov(
    temporary: Path,
    source: Path,
    output: Path,
    expected_identity: tuple[int, int, int, int] | None = None,
) -> Path:
    if expected_identity is not None and _source_identity(source) != expected_identity:
        raise ResolverError(
            f"{source.name} changed while it was being converted; the original was "
            "left untouched."
        )

    if output != source:
        _reserve_output(output)
        try:
            os.replace(temporary, output)
        except BaseException:
            output.unlink(missing_ok=True)
            raise

        try:
            if expected_identity is not None and _source_identity(source) != expected_identity:
                raise ResolverError(
                    f"{source.name} changed while it was being converted; the original "
                    "was left untouched."
                )
            backup = _move_source_to_backup(source)
        except BaseException:
            output.unlink(missing_ok=True)
            raise

        _fsync_directory(source.parent)
        return backup

    # Preferred: give the original a second name in the backup folder, then put
    # the converted file over the first in one step. The name never goes
    # missing, so a program that has the file open (Resolve) cannot hit a gap.
    backup = _link_source_to_backup(source)
    if backup is not None:
        try:
            os.replace(temporary, source)
        except BaseException:
            backup.unlink(missing_ok=True)
            raise
        _fsync_directory(source.parent)
        return backup

    backup = _move_source_to_backup(source)
    try:
        os.replace(temporary, source)
        _fsync_directory(source.parent)
        return backup
    except BaseException as publish_error:
        try:
            os.replace(backup, source)
        except OSError as restore_error:
            raise ResolverError(
                f"Could not publish {source.name}; its original remains at {backup}. "
                f"Restore also failed: {restore_error}"
            ) from publish_error
        raise


def _reserve_output(output: Path) -> None:
    try:
        with output.open("xb"):
            pass
    except FileExistsError as exc:
        raise ResolverError(
            f"Cannot create {output.name} because that file already exists. "
            "Move or rename it and try again."
        ) from exc


def _link_source_to_backup(source: Path) -> Path | None:
    """Hard-link ``source`` into the backup folder, or return None if unsupported."""
    _ensure_backup_directory(backup_directory_for(source))
    while True:
        candidate = next_backup_path(source)
        try:
            os.link(source, candidate)
            return candidate
        except FileExistsError:
            continue
        except OSError:
            return None  # the filesystem has no hard links; move the file instead


def _move_source_to_backup(source: Path) -> Path:
    _ensure_backup_directory(backup_directory_for(source))

    while True:
        candidate = next_backup_path(source)
        try:
            with candidate.open("xb"):
                pass
        except FileExistsError:
            continue

        try:
            # The backup folder is beside the source, so this is a fast atomic
            # rename even on removable filesystems without hard-link support.
            os.replace(source, candidate)
            _fsync_directory(source.parent)
            return candidate
        except BaseException:
            try:
                candidate.unlink()
            except OSError:
                pass
            raise


def _ensure_backup_directory(backup_directory: Path) -> None:
    try:
        backup_directory.mkdir(mode=0o700)
    except FileExistsError:
        if backup_directory.is_symlink() or not backup_directory.is_dir():
            raise ResolverError(
                f"Cannot create backups because {backup_directory.name} is not a "
                "regular directory."
            )
    except OSError as exc:
        raise ResolverError(
            f"Could not create backup directory {backup_directory}: {exc}"
        ) from exc


def _fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _extract_ffmpeg_error(lines: list[str]) -> str:
    ignored_prefixes = (
        "frame=",
        "fps=",
        "stream_",
        "bitrate=",
        "total_size=",
        "out_time=",
        "out_time_",
        "dup_frames=",
        "drop_frames=",
        "speed=",
        "progress=",
    )
    errors = [line for line in lines if not line.startswith(ignored_prefixes)]
    for marker in (
        " only supported in ",
        "unknown encoder",
        "no space left on device",
        "permission denied",
    ):
        for line in errors:
            if marker in line.casefold():
                return line
    return errors[-1] if errors else "Unknown FFmpeg error"
