"""Convert clips as they are imported into a running DaVinci Resolve.

Resolve has no import hook, so this polls the open project's media pool through
the scripting API. A clip that was added after the watcher started and whose
audio Resolve cannot decode on Linux is converted with the normal Resolve Ready
conversion, then refreshed in place with ``MediaPoolItem.ReplaceClip``.

Resolve remembers a failed audio decoder per file path until the project is
closed. A clip that was played (or put on a timeline) before its conversion
finished therefore stays silent under the same path until the project is
reopened; the watcher detects that from Resolve's log and says so.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from resolver.core import BACKUP_DIRECTORY_NAME, Converter, ResolverError, require_tools

# Measured on Resolve Studio 21.2 for Linux by rendering one test clip per codec:
# these decode; AAC, AC-3, E-AC-3 and DTS come in silent.
RESOLVE_DECODABLE_AUDIO = frozenset({"mp3", "flac", "alac", "opus"})
# Resolve imports the picture from these containers but ignores their audio.
CONTAINERS_WITHOUT_AUDIO = frozenset({"matroska", "webm"})

DEFAULT_SCRIPT_MODULES = "/opt/resolve/Developer/Scripting/Modules"
POLL_SECONDS = 2.0
FULL_RESCAN_SECONDS = 30.0
# "Date Added" has one-second resolution and Resolve's clock is ours.
DATE_ADDED_SLACK_SECONDS = 2.0
# A relaunched Resolve starts its watcher while the previous one is still
# noticing that its Resolve has gone, so wait for the lock instead of leaving.
LOCK_WAIT_SECONDS = 60.0
_MONTHS = {
    name: number
    for number, name in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
        start=1,
    )
}


def audio_is_decodable(codec: str) -> bool:
    codec = codec.casefold()
    return codec.startswith("pcm_") or codec in RESOLVE_DECODABLE_AUDIO


def needs_conversion(audio_codecs: Iterable[str], format_name: str) -> bool:
    """Return whether Resolve on Linux would import this file without sound."""
    codecs = list(audio_codecs)
    if not codecs:
        return False
    formats = {name.strip().casefold() for name in format_name.split(",")}
    if formats & CONTAINERS_WITHOUT_AUDIO:
        return True
    return not all(audio_is_decodable(codec) for codec in codecs)


def parse_date_added(value: str) -> float | None:
    """Parse Resolve's 'Wed Nov 11 2026 19:15:57' without relying on the locale."""
    try:
        _weekday, month, day, year, clock = value.split()
        hour, minute, second = (int(part) for part in clock.split(":"))
        return datetime(int(year), _MONTHS[month], int(day), hour, minute, second).timestamp()
    except (ValueError, KeyError, AttributeError):
        return None


def is_backup_path(path: str | os.PathLike[str]) -> bool:
    return BACKUP_DIRECTORY_NAME in Path(path).parts


def replacement_path(clip_path: str, converted: Path) -> str:
    """Return the path to hand back to Resolve for a converted clip.

    The conversion works on the symlink-resolved file. Keep the directory
    exactly as Resolve knows it so the clip's path does not change for
    collaborators who reach the same media through the same mount name.
    """
    return str(Path(clip_path).with_name(converted.name))


def probe_audio(path: str, ffprobe: str) -> tuple[list[str], str, bool]:
    """Return (audio codecs, container format name, has video)."""
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=format_name:stream=codec_type,codec_name",
                "-of",
                "json",
                path,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        payload = json.loads(result.stdout)
    except (
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
    ) as exc:
        raise ResolverError(f"Could not inspect {Path(path).name}") from exc
    streams = payload.get("streams", [])
    codecs = [
        stream.get("codec_name") or "unknown"
        for stream in streams
        if stream.get("codec_type") == "audio"
    ]
    has_video = any(stream.get("codec_type") == "video" for stream in streams)
    return codecs, payload.get("format", {}).get("format_name", ""), has_video


class DecodeFailureLog:
    """Follows Resolve's debug log for audio and video decode failures."""

    MARKER = "Failed to decode clip <"

    def __init__(self, log_path: Path):
        self.log_path = log_path
        self._offset = 0
        self.failed: set[str] = set()
        try:
            self._offset = log_path.stat().st_size
        except OSError:
            pass

    def refresh(self) -> None:
        try:
            size = self.log_path.stat().st_size
            if size < self._offset:  # rotated on a new Resolve launch
                self._offset = 0
            with self.log_path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(self._offset)
                text = handle.read()
                self._offset = handle.tell()
        except OSError:
            return
        for line in text.splitlines():
            start = line.find(self.MARKER)
            if start < 0:
                continue
            end = line.find(">", start)
            if end > 0:
                self.failed.add(line[start + len(self.MARKER) : end])

    def forget(self, path: str) -> None:
        self.failed.discard(path)


@dataclass
class WatchReport:
    converted: list[str] = field(default_factory=list)
    need_reopen: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    def empty(self) -> bool:
        return not (self.converted or self.need_reopen or self.failed)

    def summary(self) -> tuple[str, str]:
        title = "Resolve Ready"
        lines = []
        if self.converted:
            count = len(self.converted)
            title = f"Resolve Ready converted {count} imported clip{'s' if count != 1 else ''}"
            lines.append(", ".join(self.converted[:4]) + ("…" if count > 4 else ""))
        if self.need_reopen:
            lines.append(
                "Reopen the project to fix: "
                + ", ".join(self.need_reopen[:4])
                + ("…" if len(self.need_reopen) > 4 else "")
            )
        if self.failed:
            lines.append("Not converted: " + "; ".join(self.failed[:3]))
        return title, "\n".join(lines)


def desktop_notify(title: str, body: str) -> None:
    commands = []
    notify_send = shutil.which("notify-send")
    if notify_send:
        commands.append(
            [notify_send, "--app-name=Resolve Ready", "--icon=resolve-ready", title, body]
        )
    gdbus = shutil.which("gdbus")
    if gdbus:
        # Fallback for systems whose notify-send is broken or missing.
        commands.append(
            [
                gdbus,
                "call",
                "--session",
                "--dest=org.freedesktop.Notifications",
                "--object-path=/org/freedesktop/Notifications",
                "--method=org.freedesktop.Notifications.Notify",
                "Resolve Ready",
                "0",
                "resolve-ready",
                json.dumps(title, ensure_ascii=False),
                json.dumps(body, ensure_ascii=False),
                "[]",
                "{}",
                "8000",
            ]
        )
    for command in commands:
        try:
            done = subprocess.run(command, check=False, capture_output=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if done.returncode == 0:
            return


class ImportWatcher:
    def __init__(
        self,
        *,
        started: float | None = None,
        converter: Converter | None = None,
        ffprobe: str | None = None,
        failure_log: DecodeFailureLog | None = None,
        notify: Callable[[str, str], None] = desktop_notify,
        log: Callable[[str], None] | None = None,
    ):
        self.started = time.time() if started is None else started
        self.converter = converter or Converter()
        self.ffprobe = ffprobe or require_tools()[1]
        self.failure_log = failure_log
        self.notify = notify
        self.log = log or (lambda message: print(message, file=sys.stderr, flush=True))
        self._seen: set[tuple[str, str]] = set()
        # (project id, bin handle) -> clip handles present at the last look
        self._bins: dict[tuple[str, str], frozenset[str]] = {}
        self._last_full_scan = float("-inf")
        # original clip path -> path handed back to Resolve, for this session
        self._converted: dict[str, str] = {}
        # (clip, path, attempts): clips whose metadata still names the old codec
        self._relabel: list[tuple[object, str, int]] = []
        # Same-name conversions, watched for decode errors Resolve logs later.
        self._swapped: set[str] = set()

    # -- media pool walking -------------------------------------------------

    def _new_clips(self, project) -> list:
        """Return (key, clip) for clips not handled yet.

        Resolve cannot be asked for "clips added since": the media pool has to
        be listed. Listing a bin is one cheap call, while identifying a clip is
        one call per clip, so a bin whose clip count is unchanged is skipped and
        only the clips that were not in a changed bin last time are identified.
        A full pass every FULL_RESCAN_SECONDS catches a bin where one clip was
        removed and another added between two polls.
        """
        project_id = project.GetUniqueId()
        now = time.monotonic()
        full = now - self._last_full_scan >= FULL_RESCAN_SECONDS
        if full:
            self._last_full_scan = now
        pending = [project.GetMediaPool().GetRootFolder()]
        fresh = []
        visited = set()
        while pending:
            folder = pending.pop()
            if folder is None:
                continue
            clips = folder.GetClipList() or []
            bin_key = (project_id, str(folder))
            visited.add(bin_key)
            known = self._bins.get(bin_key)
            if full or known is None or len(known) != len(clips):
                # str() is the remote object's handle; it costs no round trip.
                handles = {str(clip): clip for clip in clips}
                for handle, clip in handles.items():
                    if not full and known is not None and handle in known:
                        continue
                    key = (project_id, clip.GetUniqueId())
                    if key not in self._seen:
                        fresh.append((key, clip))
                self._bins[bin_key] = frozenset(handles)
            pending.extend(folder.GetSubFolderList() or [])
        if full:
            for bin_key in [key for key in self._bins if key[0] == project_id]:
                if bin_key not in visited:
                    del self._bins[bin_key]
        return fresh

    def _wants(self, clip) -> str | None:
        """Return the clip's file path if it is a fresh import needing conversion."""
        properties = clip.GetClipProperty() or {}
        path = properties.get("File Path") or ""
        if not path or is_backup_path(path) or not os.path.isfile(path):
            return None
        added = parse_date_added(properties.get("Date Added") or "")
        if added is None or added < self.started - DATE_ADDED_SLACK_SECONDS:
            return None
        if path in self._converted:
            return path
        try:
            codecs, format_name, has_video = probe_audio(path, self.ffprobe)
        except ResolverError as exc:
            self.log(str(exc))
            return None
        if not has_video or not needs_conversion(codecs, format_name):
            return None
        return path

    # -- conversion ---------------------------------------------------------

    def _convert(self, clip, path: str, report: WatchReport) -> None:
        name = Path(path).name
        new_path = self._converted.get(path)
        if new_path is None:
            self.log(f"Converting imported clip {path}")
            try:
                result = self.converter.convert(path)
            except ResolverError as exc:
                self.log(f"Could not convert {path}: {exc}")
                report.failed.append(f"{name} ({exc})")
                return
            new_path = replacement_path(path, result.path)
            self._converted[path] = new_path
            report.converted.append(name)
        if not clip.ReplaceClip(new_path):
            self.log(f"Resolve refused to refresh {path}")
            report.failed.append(f"{name} (converted, but Resolve did not refresh it)")
            return
        if new_path == path:
            self._relabel.append((clip, new_path, 0))
        if self.failure_log is not None and new_path == path:
            self.failure_log.refresh()
            if path in self.failure_log.failed:
                self.failure_log.forget(path)
                self.log(f"{path} was played before conversion; needs a project reopen")
                report.need_reopen.append(name)
            else:
                self._swapped.add(path)

    def _check_swapped(self, report: WatchReport) -> None:
        """Report files Resolve fails to decode after they were swapped.

        Resolve keeps the readers it made for the old file. The conversion is
        built so that they still work, but when they do not, only reopening the
        project replaces them.
        """
        if self.failure_log is None or not self._swapped:
            return
        self.failure_log.refresh()
        for path in sorted(self._swapped & self.failure_log.failed):
            self._swapped.discard(path)
            self.failure_log.forget(path)
            self.log(f"Resolve reports decode errors for {path} after conversion")
            report.need_reopen.append(Path(path).name)

    def _refresh_labels(self) -> None:
        """Make the clip's metadata name the new codec.

        Resolve plays the converted audio after the first ReplaceClip but keeps
        showing the old codec; a second ReplaceClip a moment later corrects it.
        """
        pending, self._relabel = self._relabel, []
        for clip, path, attempts in pending:
            try:
                if attempts and clip.GetClipProperty("Audio Codec") == "Linear PCM":
                    continue
                if attempts < 3 and clip.ReplaceClip(path):
                    self._relabel.append((clip, path, attempts + 1))
            except Exception:  # the clip's project was closed meanwhile
                continue

    def poll(self, resolve) -> WatchReport:
        report = WatchReport()
        self._refresh_labels()
        self._check_swapped(report)
        project_manager = resolve.GetProjectManager()
        project = project_manager.GetCurrentProject() if project_manager else None
        if project is None:
            return report
        for key, clip in self._new_clips(project):
            path = self._wants(clip)
            if path:
                self._convert(clip, path, report)
            # Marked only once handled, so a failed poll retries the clip.
            self._seen.add(key)
        return report

    def run(self, connect: Callable[[], object], alive: Callable[[], bool]) -> None:
        while alive():
            try:
                resolve = connect()
                if resolve is not None:
                    report = self.poll(resolve)
                    if not report.empty():
                        self.notify(*report.summary())
            except Exception as exc:  # scripting calls fail while Resolve is busy
                self.log(f"Watcher poll failed: {exc!r}")
            time.sleep(POLL_SECONDS)


def _resolve_connector() -> Callable[[], object]:
    modules = os.environ.get("RESOLVE_SCRIPT_API")
    modules = os.path.join(modules, "Modules") if modules else DEFAULT_SCRIPT_MODULES
    if modules not in sys.path:
        sys.path.append(modules)
    try:
        import DaVinciResolveScript as script  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ResolverError(
            f"DaVinci Resolve's scripting module was not found in {modules}."
        ) from exc
    return lambda: script.scriptapp("Resolve")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _acquire_lock(handle, resolve_pid: int | None, wait: float = LOCK_WAIT_SECONDS) -> bool:
    """Take the single-watcher lock, outwaiting a watcher that is shutting down."""
    deadline = time.monotonic() + (wait if resolve_pid is not None else 0.0)
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if time.monotonic() >= deadline or not _pid_alive(resolve_pid):
                return False
            time.sleep(0.5)


def watch_resolve(resolve_pid: int | None = None) -> int:
    """Run until Resolve (``resolve_pid``) exits. Returns a process exit code."""
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp")
    lock_handle = (runtime / "resolve-ready-watch.lock").open("w")
    if not _acquire_lock(lock_handle, resolve_pid):
        return 0  # another watcher already serves this session

    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    log_path = state / "resolve-ready" / "watch.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_lock = threading.Lock()

    def log(message: str) -> None:
        with log_lock, log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}\n")

    try:
        connect = _resolve_connector()
        data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
        watcher = ImportWatcher(
            failure_log=DecodeFailureLog(
                data_home / "DaVinciResolve" / "logs" / "ResolveDebug.txt"
            ),
            log=log,
        )
    except ResolverError as exc:
        log(str(exc))
        print(f"resolve-ready: {exc}", file=sys.stderr)
        return 1

    if resolve_pid is None:
        # Standalone use: stay until Resolve has been seen and then goes away.
        seen = threading.Event()

        def alive() -> bool:
            if connect() is not None:
                seen.set()
                return True
            return not seen.is_set()

    else:
        def alive() -> bool:
            return _pid_alive(resolve_pid)

        # A scripting call can block on a modal dialog; never outlive Resolve.
        def reaper() -> None:
            while _pid_alive(resolve_pid):
                time.sleep(2.0)
            os._exit(0)

        threading.Thread(target=reaper, daemon=True).start()

    log(f"Watching Resolve imports (pid {resolve_pid})")
    watcher.run(connect, alive)
    log("Resolve exited; watcher stopped")
    return 0
