from __future__ import annotations

import fcntl
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from resolver.core import BACKUP_DIRECTORY_NAME, ConversionResult, ResolverError
from resolver.watch import (
    _acquire_lock,
    DecodeFailureLog,
    ImportWatcher,
    WatchReport,
    is_backup_path,
    needs_conversion,
    parse_date_added,
    replacement_path,
)


class RuleTests(unittest.TestCase):
    def test_codecs_resolve_decodes_are_left_alone(self):
        for codec in ("pcm_s24le", "pcm_s16be", "mp3", "flac", "alac", "opus"):
            self.assertFalse(needs_conversion([codec], "mov,mp4,m4a,3gp,3g2,mj2"), codec)

    def test_silent_codecs_need_conversion(self):
        for codec in ("aac", "ac3", "eac3", "dts", "vorbis"):
            self.assertTrue(needs_conversion([codec], "mov,mp4,m4a,3gp,3g2,mj2"), codec)

    def test_one_bad_track_is_enough(self):
        self.assertTrue(needs_conversion(["pcm_s24le", "aac"], "mov,mp4"))

    def test_matroska_audio_is_always_converted(self):
        self.assertTrue(needs_conversion(["flac"], "matroska,webm"))

    def test_clip_without_audio_is_left_alone(self):
        self.assertFalse(needs_conversion([], "matroska,webm"))

    def test_date_added_is_parsed_without_locale(self):
        parsed = parse_date_added("Wed Nov 11 2026 19:15:57")
        self.assertEqual(time.localtime(parsed)[:6], (2026, 11, 11, 19, 15, 57))
        self.assertIsNone(parse_date_added(""))
        self.assertIsNone(parse_date_added("mer nov 11 2026"))

    def test_backup_folder_is_never_touched(self):
        self.assertTrue(is_backup_path(f"/media/{BACKUP_DIRECTORY_NAME}/a_backup.mp4"))
        self.assertFalse(is_backup_path("/media/a.mp4"))

    def test_replacement_keeps_the_directory_resolve_knows(self):
        self.assertEqual(
            replacement_path("/Volumes/Shared/a.mkv", Path("/mnt/nas/shared/a.mov")),
            "/Volumes/Shared/a.mov",
        )


class DecodeFailureLogTests(unittest.TestCase):
    def test_only_new_failures_are_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "ResolveDebug.txt"
            log.write_text("x | Failed to decode clip </old.mp4>, track: 0\n")
            follower = DecodeFailureLog(log)
            with log.open("a") as handle:
                handle.write("x | Failed to decode clip </new clip.mp4>, track: 0, position: 0\n")
            follower.refresh()
            self.assertEqual(follower.failed, {"/new clip.mp4"})


class FakeClip:
    def __init__(self, uid, path, added, replace_ok=True):
        self.uid, self.path, self.added, self.replace_ok = uid, path, added, replace_ok
        self.replaced_with = None
        self.replace_count = 0
        self.identified = 0

    def GetUniqueId(self):
        self.identified += 1
        return self.uid

    def GetClipProperty(self, name=None):
        properties = {
            "File Path": self.path,
            "Date Added": self.added,
            # Resolve only corrects the label on a later, second refresh.
            "Audio Codec": "Linear PCM" if self.replace_count >= 2 else "AAC",
        }
        return properties if name is None else properties[name]

    def ReplaceClip(self, path):
        self.replaced_with = path
        self.replace_count += 1
        return self.replace_ok


class FakeFolder:
    def __init__(self, clips, subfolders=()):
        self.clips, self.subfolders = clips, list(subfolders)

    def GetClipList(self):
        return self.clips

    def GetSubFolderList(self):
        return self.subfolders


class FakeResolve:
    def __init__(self, root):
        self.root = root

    def GetProjectManager(self):
        return self

    def GetCurrentProject(self):
        return self

    def GetUniqueId(self):
        return "project"

    def GetMediaPool(self):
        return self

    def GetRootFolder(self):
        return self.root


class FakeConverter:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def convert(self, path):
        self.calls.append(path)
        if self.fail:
            raise ResolverError("read-only")
        return ConversionResult(Path(path), Path(path).with_name("backup"))


NOW = "Wed Nov 11 2026 19:15:57"
STARTED = parse_date_added("Wed Nov 11 2026 19:00:00")


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.media = Path(self.directory.name) / "clip.mp4"
        self.media.touch()

    def watcher(self, converter, failure_log=None):
        return ImportWatcher(
            started=STARTED,
            converter=converter,
            ffprobe="ffprobe",
            failure_log=failure_log,
            notify=lambda *_: None,
            log=lambda _message: None,
        )

    def poll(self, watcher, clips, probe=(["aac"], "mov,mp4", True)):
        with patch("resolver.watch.probe_audio", return_value=probe):
            return watcher.poll(FakeResolve(FakeFolder([], [FakeFolder(clips)])))

    def test_fresh_aac_import_is_converted_and_refreshed_once(self):
        converter = FakeConverter()
        watcher = self.watcher(converter)
        clip = FakeClip("a", str(self.media), NOW)
        report = self.poll(watcher, [clip])
        self.assertEqual(converter.calls, [str(self.media)])
        self.assertEqual(clip.replaced_with, str(self.media))
        self.assertEqual(report.converted, ["clip.mp4"])
        self.assertTrue(self.poll(watcher, [clip]).empty())
        self.assertEqual(len(converter.calls), 1)

    def test_codec_label_is_refreshed_on_a_later_poll_then_left_alone(self):
        watcher = self.watcher(FakeConverter())
        clip = FakeClip("a", str(self.media), NOW)
        self.poll(watcher, [clip])
        self.assertEqual(clip.replace_count, 1)
        for _ in range(4):
            self.poll(watcher, [clip])
        self.assertEqual(clip.replace_count, 2)

    def test_unchanged_bin_is_not_identified_again(self):
        watcher = self.watcher(FakeConverter())
        old = [FakeClip(str(i), str(self.media), "Wed Nov 11 2026 18:00:00") for i in range(5)]
        folder = FakeFolder(old)
        resolve = FakeResolve(FakeFolder([], [folder]))
        with patch("resolver.watch.probe_audio", return_value=(["aac"], "mov,mp4", True)):
            watcher.poll(resolve)
            watcher.poll(resolve)
            self.assertEqual([clip.identified for clip in old], [1] * 5)
            new = FakeClip("new", str(self.media), NOW)
            folder.clips = old + [new]
            report = watcher.poll(resolve)
        self.assertEqual(report.converted, ["clip.mp4"])
        self.assertEqual([clip.identified for clip in old], [1] * 5)
        self.assertEqual(new.identified, 1)

    def test_swap_within_one_poll_is_caught_by_the_full_rescan(self):
        converter = FakeConverter()
        watcher = self.watcher(converter)
        folder = FakeFolder([FakeClip("a", str(self.media), "Wed Nov 11 2026 18:00:00")])
        resolve = FakeResolve(FakeFolder([], [folder]))
        with patch("resolver.watch.probe_audio", return_value=(["aac"], "mov,mp4", True)):
            watcher.poll(resolve)
            folder.clips = [FakeClip("b", str(self.media), NOW)]  # same count
            self.assertTrue(watcher.poll(resolve).empty())
            watcher._last_full_scan = float("-inf")
            self.assertEqual(watcher.poll(resolve).converted, ["clip.mp4"])

    def test_clips_imported_before_the_watcher_started_are_left_alone(self):
        converter = FakeConverter()
        clip = FakeClip("a", str(self.media), "Wed Nov 11 2026 18:00:00")
        self.assertTrue(self.poll(self.watcher(converter), [clip]).empty())
        self.assertEqual(converter.calls, [])

    def test_decodable_audio_is_left_alone(self):
        converter = FakeConverter()
        clip = FakeClip("a", str(self.media), NOW)
        self.poll(self.watcher(converter), [clip], probe=(["pcm_s24le"], "mov,mp4", True))
        self.assertEqual(converter.calls, [])

    def test_failed_conversion_is_reported_and_not_retried(self):
        converter = FakeConverter(fail=True)
        watcher = self.watcher(converter)
        clip = FakeClip("a", str(self.media), NOW)
        report = self.poll(watcher, [clip])
        self.assertEqual(len(report.failed), 1)
        self.assertIsNone(clip.replaced_with)
        self.poll(watcher, [clip])
        self.assertEqual(len(converter.calls), 1)

    def test_second_clip_of_a_converted_file_is_only_refreshed(self):
        converter = FakeConverter()
        watcher = self.watcher(converter)
        self.poll(watcher, [FakeClip("a", str(self.media), NOW)])
        twin = FakeClip("b", str(self.media), NOW)
        self.poll(watcher, [twin], probe=(["pcm_s24le"], "mov,mp4", True))
        self.assertEqual(len(converter.calls), 1)
        self.assertEqual(twin.replaced_with, str(self.media))

    def test_clip_played_before_conversion_is_flagged_for_reopen(self):
        log = Path(self.directory.name) / "ResolveDebug.txt"
        log.touch()
        follower = DecodeFailureLog(log)
        log.write_text(f"x | Failed to decode clip <{self.media}>, track: 0\n")
        report = self.poll(
            self.watcher(FakeConverter(), follower), [FakeClip("a", str(self.media), NOW)]
        )
        self.assertEqual(report.need_reopen, ["clip.mp4"])

    def test_decode_errors_after_a_swap_are_flagged_for_reopen(self):
        log = Path(self.directory.name) / "ResolveDebug.txt"
        log.touch()
        watcher = self.watcher(FakeConverter(), DecodeFailureLog(log))
        clip = FakeClip("a", str(self.media), NOW)
        self.assertEqual(self.poll(watcher, [clip]).need_reopen, [])
        with log.open("a") as handle:
            handle.write(f"x | Failed to decode clip <{self.media}>, track 0, frame 12: no\n")
        self.assertEqual(self.poll(watcher, [clip]).need_reopen, ["clip.mp4"])
        with log.open("a") as handle:
            handle.write(f"x | Failed to decode clip <{self.media}>, track 0, frame 13: no\n")
        self.assertEqual(self.poll(watcher, [clip]).need_reopen, [], "reported twice")

    def test_report_summary_mentions_reopen(self):
        report = WatchReport(converted=["a.mp4"], need_reopen=["a.mp4"])
        title, body = report.summary()
        self.assertIn("1 imported clip", title)
        self.assertIn("Reopen the project", body)


if __name__ == "__main__":
    unittest.main()


class LockTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "watch.lock"
        self.holder = self.path.open("w")
        self.addCleanup(self.holder.close)
        fcntl.flock(self.holder, fcntl.LOCK_EX)

    def test_waits_for_a_watcher_that_is_shutting_down(self):
        threading.Timer(0.3, lambda: fcntl.flock(self.holder, fcntl.LOCK_UN)).start()
        with self.path.open("w") as handle:
            self.assertTrue(_acquire_lock(handle, os.getpid(), wait=10.0))

    def test_gives_up_when_the_other_watcher_stays(self):
        with self.path.open("w") as handle:
            self.assertFalse(_acquire_lock(handle, os.getpid(), wait=0.2))
            self.assertFalse(_acquire_lock(handle, None))
