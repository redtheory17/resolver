# Resolve Ready

**Fix silent clips in DaVinci Resolve on Linux — without re-encoding the picture,
without renaming the file, and optionally without lifting a finger.**

Phone footage, screen recordings, drone clips and most camera MP4s carry AAC
audio. DaVinci Resolve on Linux cannot decode AAC, so those clips import with a
picture and a flat, silent audio track. Resolve Ready rewrites just the audio to
uncompressed PCM, which Resolve plays everywhere, and leaves the video stream
bit-for-bit untouched. It can do that from a small app, from the file manager's
right-click menu, or automatically the moment a clip is imported into Resolve.

## The problem

On Linux, Resolve imports these clips with no usable sound. Measured on Resolve
Studio 21.2 by rendering one test clip per codec and checking the output level:

| Audio in the file                  | In Resolve on Linux                      |
| ---------------------------------- | ---------------------------------------- |
| PCM, MP3, FLAC, ALAC, Opus         | plays                                    |
| AAC (MP4, MOV)                     | silent                                   |
| AC-3, E-AC-3                       | silent                                   |
| DTS                                | silent (and mislabelled as Linear PCM)   |
| anything inside MKV                | audio track is not imported at all       |

There is no setting, plugin or system library that changes this. Resolve ships
its own FFmpeg build with the AAC decoder compiled out
(`--disable-decoder='aac*'`), and only hands a short, fixed list of codecs to it
in the first place, so installing codecs on the system has no effect. Resolve
also gives no warning: the clip's metadata happily reports "AAC, 2 channels".

## How Resolve Ready solves it

It fixes the file instead of the application:

- **Audio becomes 24-bit / 48 kHz PCM.** Every audio track is kept, and gaps in
  the audio timestamps are filled with silence so sync is preserved.
- **Video is copied, not re-encoded.** A conversion takes about as long as
  copying the file, and the picture is identical to the source. (AV1, VP8 and
  VP9 are the exception — see [Conversion details](#conversion-details).)
- **The file keeps its name.** `clip.mp4` stays `clip.mp4`: the PCM audio is
  stored in the MP4 as a standard `ipcm` track. A project that already
  references the file stays linked, on your machine and for collaborators who
  open the same media.
- **The original is never lost.** It moves into a hidden
  `.resolve-ready-backups/` folder beside the file.
- **It can happen on import.** A watcher notices clips as they land in Resolve's
  media pool, converts the ones that would be silent, and refreshes them in
  place — typically within a few seconds for short clips.

```text
footage/
├── interview.mp4                         (Resolve-ready media, same name)
└── .resolve-ready-backups/
    └── interview_backup.mp4              (untouched source)
```

## Install

Requirements:

- Python 3
- FFmpeg 6.1 or newer (`ffmpeg` and `ffprobe`)
- GTK 4 with Python GObject bindings (only for the graphical app)
- DaVinci Resolve Studio (only for automatic conversion on import, which uses
  Resolve's scripting API)

```bash
# Fedora
sudo dnf install ffmpeg python3-gobject gtk4

# Ubuntu / Debian
sudo apt install ffmpeg python3-gi gir1.2-gtk-4.0

# Arch Linux
sudo pacman -S ffmpeg python-gobject gtk4
```

Install for the current user:

```bash
git clone https://github.com/redtheory17/resolve-ready.git
cd resolve-ready
./scripts/install.sh
```

The installer adds the app to your launcher and a **Resolving** action to the
right-click menu of Dolphin, Nemo and Nautilus (Nautilus also needs its
distribution's `nautilus-python` package). Restart the file manager once if the
action does not appear immediately.

## Use

**From the file manager** — right-click one or more videos and choose
**Resolving**. Conversion starts immediately.

**From the app** — open **Resolve Ready**, drag videos in, and start the queue.

**From a terminal**

```bash
resolve-ready --no-gui recording.mp4 another.mkv
```

**Without installing**

```bash
./bin/resolve-ready
./bin/resolve-ready --no-gui recording.mkv
```

## Convert automatically on import

```bash
resolve-ready --watch-resolve --resolve-pid <pid of Resolve>
```

Resolve has no import hook, so the watcher polls the open project's media pool
through the scripting API. When a clip appears that Resolve would import without
sound, it is converted and then refreshed in the media pool with
`MediaPoolItem.ReplaceClip`. A desktop notification lists what was converted.

The easiest way to run it is from the script that launches Resolve, so it starts
and stops with Resolve:

```bash
#!/usr/bin/env bash
"$HOME/.local/bin/resolve-ready" --watch-resolve --resolve-pid $$ >/dev/null 2>&1 &
exec /opt/resolve/bin/resolve "$@"
```

`$$` becomes Resolve's process ID through `exec`, and the watcher exits when
that process does. In Resolve, set *Preferences → System → General → External
scripting using* to **Local**.

What it does and does not touch:

- Only clips added **after the watcher started** are considered. Clips that were
  already in a project are never converted behind your back.
- A clip is converted when it has audio in MKV or WebM, or any audio codec other
  than PCM, MP3, FLAC, ALAC or Opus.
- Nothing inside `.resolve-ready-backups/` is ever converted.
- Files on read-only media cannot be converted; you get a notification instead.

It is cheap to leave running. Bins whose clip count has not changed are skipped,
and only clips it has not seen before are inspected, with a full pass every 30
seconds as a safety net. On a 600-clip bin a check takes about 7 ms every two
seconds — roughly 0.3 % of one CPU core — and about 32 MB of memory.

Activity is logged to `~/.local/state/resolve-ready/watch.log`.

### One thing to know: play it too early and it stays silent until you reopen

Resolve remembers a failed audio decoder per file path until the project is
closed. If you play a clip, or put it on a timeline, *before* its conversion has
finished, the converted file under the same name stays silent for the rest of
that session. Reopening the project fixes it. The watcher detects this from
Resolve's log and names those clips in its notification. For long files, give
the conversion a moment before you start cutting.

## Conversion details

- The first video stream is copied untouched when the container supports it.
- MP4, M4V and MOV sources keep their exact name and container. Other
  containers become a MOV with the source's base name.
- AV1, VP8 and VP9 video is transcoded to DNxHR in a MOV: 10-bit DNxHR HQX for
  HDR and other high-bit-depth sources (colour tags are kept), DNxHR HQ
  otherwise. These files get much larger.
- Every audio stream is converted to 24-bit linear PCM at 48 kHz.
- Global metadata, video-stream metadata, timecode, chapters and rotation are
  retained where the container supports them.
- Subtitle and attachment streams are omitted.
- Conversion happens in a hidden file beside the source. Only after FFmpeg
  succeeds is the converted file published and the source moved under
  `.resolve-ready-backups/`. If anything fails, the original stays where it was.
- Existing backups are never overwritten; a later conversion uses
  `interview_backup_2.mp4`, then `_3`, and so on. If a source that becomes a
  MOV would overwrite an existing MOV, conversion stops without changing either
  file.

## Limitations

- **Disk space.** PCM audio adds roughly 1 GB per hour of stereo, and the
  original is kept as a backup, so converted footage takes about twice the
  space until you delete the backups.
- **Audio-only files** (for example `.m4a`) are not handled yet.
- **PCM-in-MP4 outside Resolve.** Resolve on Linux reads it, and so does
  anything built on a recent FFmpeg. It has not been verified in Resolve on
  macOS or Windows, or in players such as QuickTime; check one clip before
  relying on it for a shared project.
- **Tested with** DaVinci Resolve Studio 21.2 on Linux. Which codecs Resolve
  decodes can change between versions.

## Uninstall

```bash
./scripts/uninstall.sh
```

## Test

```bash
python3 -m unittest discover -v
```

---

Resolve Ready is an independent project. It is not affiliated with or endorsed
by Blackmagic Design. DaVinci Resolve is a trademark of Blackmagic Design Pty.
Ltd.
