from __future__ import annotations

import argparse
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from resolver import __version__
from resolver.core import ConversionCancelled, Converter, ResolverError


@dataclass
class QueueItem:
    path: Path
    row: object
    status_label: object
    progress_bar: object
    state: str = "ready"


def _load_gtk():
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Gdk", "4.0")
        from gi.repository import Gdk, Gio, GLib, Gtk
    except (ImportError, ValueError) as exc:
        raise ResolverError(
            "GTK 4 Python bindings are required for the graphical app. "
            "Install python3-gobject and GTK 4 from your Linux distribution."
        ) from exc
    return Gdk, Gio, GLib, Gtk


class ResolveReadyApplication:
    def __init__(self):
        self.Gdk, self.Gio, self.GLib, self.Gtk = _load_gtk()
        flags = self.Gio.ApplicationFlags.HANDLES_OPEN
        self.application = self.Gtk.Application(
            application_id="io.github.resolve_ready",
            flags=flags,
        )
        self.application.connect("activate", self._on_activate)
        self.application.connect("open", self._on_open)
        self.application.connect("shutdown", self._on_shutdown)
        self.window = None
        self.queue_box = None
        self.empty_state = None
        self.convert_button = None
        self.cancel_button = None
        self.summary_label = None
        self.items: list[QueueItem] = []
        self.known_paths: set[Path] = set()
        self.converter: Converter | None = None
        self.converting = False
        self.autostart_pending = False
    def run(self, argv: list[str]) -> int:
        return self.application.run(argv)

    def _on_activate(self, _application):
        self._ensure_window()
        self.window.present()

    def _on_open(self, _application, files, _file_count, _hint):
        self._ensure_window()
        paths = [Path(item.get_path()) for item in files if item.get_path()]
        self.add_paths(paths)
        self.window.present()
        self.autostart_pending = bool(paths)
        if paths and not self.converting:
            self.GLib.idle_add(self.start_conversion)

    def _on_shutdown(self, _application):
        if self.converter:
            self.converter.cancel()

    def _ensure_window(self):
        if self.window is not None:
            return

        Gtk = self.Gtk
        self._load_css()
        self.window = Gtk.ApplicationWindow(application=self.application)
        self.window.set_title("Resolve Ready")
        self.window.set_default_size(760, 620)
        self.window.set_size_request(560, 440)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.window.set_child(root)

        header = Gtk.HeaderBar()
        header.set_show_title_buttons(True)
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        title = Gtk.Label(label="Resolve Ready")
        title.add_css_class("title")
        subtitle = Gtk.Label(label="Resolve-friendly video and audio.")
        subtitle.add_css_class("caption")
        title_box.append(title)
        title_box.append(subtitle)
        header.set_title_widget(title_box)
        root.append(header)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        content.set_margin_top(24)
        content.set_margin_bottom(20)
        content.set_margin_start(24)
        content.set_margin_end(24)
        root.append(content)

        drop_zone = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        drop_zone.add_css_class("drop-zone")
        drop_zone.set_size_request(-1, 148)
        drop_zone.set_valign(Gtk.Align.CENTER)
        drop_zone.set_vexpand(False)

        drop_icon = Gtk.Image.new_from_icon_name("video-x-generic-symbolic")
        drop_icon.set_pixel_size(38)
        drop_icon.add_css_class("accent")
        drop_zone.append(drop_icon)

        drop_title = Gtk.Label(label="Drop videos here")
        drop_title.add_css_class("drop-title")
        drop_zone.append(drop_title)
        drop_hint = Gtk.Label(label="or choose one or more files")
        drop_hint.add_css_class("dim-label")
        drop_zone.append(drop_hint)
        add_button = Gtk.Button(label="Choose videos")
        add_button.set_halign(Gtk.Align.CENTER)
        add_button.add_css_class("pill")
        add_button.connect("clicked", self._choose_files)
        drop_zone.append(add_button)
        content.append(drop_zone)

        if hasattr(self.Gdk, "FileList"):
            drop_target = Gtk.DropTarget.new(self.Gdk.FileList, self.Gdk.DragAction.COPY)
            drop_target.connect("drop", self._on_drop)
        else:
            # Gdk.FileList arrived after early GTK 4 releases. URI text keeps
            # drag-and-drop working on distributions that still ship GTK 4.6.
            drop_target = Gtk.DropTarget.new(str, self.Gdk.DragAction.COPY)
            drop_target.connect("drop", self._on_uri_drop)
        drop_zone.add_controller(drop_target)

        format_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        format_box.set_halign(Gtk.Align.CENTER)
        for label in (
            "Video · copied or DNxHR",
            "Audio · PCM 24-bit / 48 kHz",
            "Output · MP4 stays MP4, otherwise MOV",
        ):
            chip = Gtk.Label(label=label)
            chip.add_css_class("format-chip")
            format_box.append(chip)
        content.append(format_box)

        queue_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        queue_title = Gtk.Label(label="Queue")
        queue_title.add_css_class("heading")
        queue_title.set_halign(Gtk.Align.START)
        queue_header.append(queue_title)
        self.summary_label = Gtk.Label(label="0 videos")
        self.summary_label.add_css_class("dim-label")
        self.summary_label.set_hexpand(True)
        self.summary_label.set_halign(Gtk.Align.END)
        queue_header.append(self.summary_label)
        content.append(queue_header)

        queue_frame = Gtk.Frame()
        queue_frame.add_css_class("queue-frame")
        queue_frame.set_vexpand(True)
        overlay = Gtk.Overlay()
        queue_frame.set_child(overlay)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.queue_box = Gtk.ListBox()
        self.queue_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self.queue_box.add_css_class("queue")
        scroller.set_child(self.queue_box)
        overlay.set_child(scroller)
        self.empty_state = Gtk.Label(label="Your repaired videos will appear here")
        self.empty_state.add_css_class("dim-label")
        overlay.add_overlay(self.empty_state)
        content.append(queue_frame)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        footer.set_halign(Gtk.Align.END)
        self.cancel_button = Gtk.Button(label="Cancel")
        self.cancel_button.set_visible(False)
        self.cancel_button.connect("clicked", self._cancel_conversion)
        footer.append(self.cancel_button)
        self.convert_button = Gtk.Button(label="Repair files for Resolve")
        self.convert_button.add_css_class("suggested-action")
        self.convert_button.add_css_class("pill")
        self.convert_button.set_sensitive(False)
        self.convert_button.connect("clicked", lambda _button: self.start_conversion())
        footer.append(self.convert_button)
        content.append(footer)

    def _load_css(self):
        css = b"""
        window { background: #101318; }
        headerbar { background: #151922; box-shadow: none; }
        .title { font-size: 17px; font-weight: 700; }
        .caption { font-size: 11px; opacity: .62; }
        .drop-zone {
            background: #171c25;
            border: 1px dashed #59677d;
            border-radius: 14px;
            padding: 16px;
        }
        .drop-zone:hover { border-color: #72a7ff; background: #192231; }
        .drop-title { font-size: 19px; font-weight: 700; }
        .accent { color: #73a8ff; }
        .format-chip {
            background: #1b2230;
            border-radius: 999px;
            color: #b9c8dc;
            font-size: 11px;
            padding: 6px 10px;
        }
        .heading { font-size: 15px; font-weight: 700; }
        .queue-frame { border-radius: 12px; background: #151922; }
        .queue row { padding: 12px; }
        .queue row:not(:last-child) { border-bottom: 1px solid alpha(#ffffff, .07); }
        .file-name { font-weight: 650; }
        .file-meta { font-size: 11px; opacity: .62; }
        progressbar trough { min-height: 5px; border-radius: 5px; }
        progressbar progress { min-height: 5px; border-radius: 5px; }
        .pill { border-radius: 999px; padding-left: 14px; padding-right: 14px; }
        """
        provider = self.Gtk.CssProvider()
        provider.load_from_data(css)
        self.Gtk.StyleContext.add_provider_for_display(
            self.Gdk.Display.get_default(),
            provider,
            self.Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    def _choose_files(self, _button):
        dialog = self.Gtk.FileChooserNative(
            title="Choose videos",
            transient_for=self.window,
            action=self.Gtk.FileChooserAction.OPEN,
            accept_label="Add to queue",
            cancel_label="Cancel",
        )
        dialog.set_select_multiple(True)
        file_filter = self.Gtk.FileFilter()
        file_filter.set_name("Video files")
        file_filter.add_mime_type("video/*")
        dialog.add_filter(file_filter)
        dialog.connect("response", self._files_chosen)
        dialog.show()

    def _files_chosen(self, dialog, response):
        if response == self.Gtk.ResponseType.ACCEPT:
            model = dialog.get_files()
            paths = []
            for index in range(model.get_n_items()):
                path = model.get_item(index).get_path()
                if path:
                    paths.append(Path(path))
            self.add_paths(paths)
        dialog.destroy()

    def _on_drop(self, _target, value, _x, _y):
        paths = []
        for item in value.get_files():
            path = item.get_path()
            if path:
                paths.append(Path(path))
        self.add_paths(paths)
        return bool(paths)

    def _on_uri_drop(self, _target, value, _x, _y):
        from urllib.parse import unquote, urlparse

        paths = []
        for uri in str(value).splitlines():
            parsed = urlparse(uri.strip())
            if parsed.scheme == "file":
                paths.append(Path(unquote(parsed.path)))
        self.add_paths(paths)
        return bool(paths)

    def add_paths(self, paths: list[Path]):
        for raw_path in paths:
            path = raw_path.expanduser().resolve()
            if path in self.known_paths or not path.is_file():
                continue
            self._append_row(path)
            self.known_paths.add(path)
        self._refresh_summary()

    def _append_row(self, path: Path):
        Gtk = self.Gtk
        row = Gtk.ListBoxRow()
        main = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        icon = Gtk.Image.new_from_icon_name("video-x-generic-symbolic")
        icon.set_pixel_size(30)
        main.append(icon)

        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        labels.set_hexpand(True)
        name = Gtk.Label(label=path.name, ellipsize=3)
        name.set_halign(Gtk.Align.START)
        name.add_css_class("file-name")
        labels.append(name)
        status = Gtk.Label(label="Ready")
        status.set_halign(Gtk.Align.START)
        status.add_css_class("file-meta")
        labels.append(status)
        progress = Gtk.ProgressBar()
        progress.set_visible(False)
        labels.append(progress)
        main.append(labels)

        remove = Gtk.Button.new_from_icon_name("window-close-symbolic")
        remove.set_tooltip_text("Remove from queue")
        remove.add_css_class("flat")
        main.append(remove)
        row.set_child(main)
        self.queue_box.append(row)
        item = QueueItem(path, row, status, progress)
        remove.connect("clicked", lambda _button, entry=item: self._remove_item(entry))
        self.items.append(item)

    def _remove_item(self, item: QueueItem):
        if self.converting:
            return
        self.queue_box.remove(item.row)
        self.items.remove(item)
        self.known_paths.discard(item.path)
        self._refresh_summary()

    def _refresh_summary(self):
        count = len(self.items)
        completed = sum(item.state == "done" for item in self.items)
        summary = f"{count} video{'s' if count != 1 else ''}"
        if completed:
            summary += f" · {completed} complete"
        self.summary_label.set_label(summary)
        self.empty_state.set_visible(count == 0)
        ready = any(item.state != "done" for item in self.items)
        self.convert_button.set_sensitive(ready and not self.converting)

    def start_conversion(self):
        if self.converting or not any(item.state != "done" for item in self.items):
            return False
        self.converting = True
        # A context-menu request has now been consumed. If more files arrive
        # during this batch, _on_open will set this flag again for the next batch.
        self.autostart_pending = False
        self.convert_button.set_sensitive(False)
        self.cancel_button.set_visible(True)
        thread = threading.Thread(target=self._conversion_worker, daemon=True)
        thread.start()
        return False

    def _conversion_worker(self):
        try:
            self.converter = Converter()
            pending_items = [item for item in self.items if item.state != "done"]
            for index, item in enumerate(pending_items):
                self.GLib.idle_add(self._mark_started, item, index, len(pending_items))
                try:
                    result = self.converter.convert(
                        item.path,
                        lambda update, entry=item: self.GLib.idle_add(
                            self._update_progress, entry, update.fraction
                        ),
                    )
                except ConversionCancelled:
                    self.GLib.idle_add(self._mark_cancelled, item)
                    break
                except ResolverError as exc:
                    self.GLib.idle_add(self._mark_failed, item, str(exc))
                    continue
                self.GLib.idle_add(self._mark_complete, item, result)
        except ResolverError as exc:
            self.GLib.idle_add(self._show_error, str(exc))
        finally:
            self.GLib.idle_add(self._conversion_finished)

    def _mark_started(self, item: QueueItem, index: int, total: int):
        item.state = "working"
        item.status_label.set_label(f"Converting media · item {index + 1} of {total}")
        item.progress_bar.set_fraction(0.0)
        item.progress_bar.set_visible(True)

    def _update_progress(self, item: QueueItem, fraction: float):
        item.progress_bar.set_fraction(fraction)

    def _mark_complete(self, item: QueueItem, result):
        item.state = "done"
        backup_label = f"{result.backup_path.parent.name}/{result.backup_path.name}"
        item.status_label.set_label(f"Repaired · backup: {backup_label}")
        item.progress_bar.set_fraction(1.0)
        item.row.add_css_class("success")
        self._refresh_summary()
        self._notify("Resolve-ready file complete", result.path.name)

    def _mark_failed(self, item: QueueItem, message: str):
        item.state = "failed"
        item.status_label.set_label(message)
        item.progress_bar.set_visible(False)
        item.row.add_css_class("error")

    def _mark_cancelled(self, item: QueueItem):
        item.state = "ready"
        item.status_label.set_label("Cancelled")
        item.progress_bar.set_visible(False)

    def _conversion_finished(self):
        self.converting = False
        self.converter = None
        self.cancel_button.set_visible(False)
        self.cancel_button.set_sensitive(True)
        has_pending = any(item.state != "done" for item in self.items)
        self.convert_button.set_sensitive(has_pending)
        if self.autostart_pending and has_pending:
            self.autostart_pending = False
            self.GLib.idle_add(self.start_conversion)
        elif not has_pending:
            self.autostart_pending = False

    def _cancel_conversion(self, _button):
        self.cancel_button.set_sensitive(False)
        if self.converter:
            self.converter.cancel()

    def _show_error(self, message: str):
        dialog = self.Gtk.MessageDialog(
            transient_for=self.window,
            modal=True,
            buttons=self.Gtk.ButtonsType.CLOSE,
            message_type=self.Gtk.MessageType.ERROR,
            text="Resolve Ready could not start",
            secondary_text=message,
        )
        dialog.connect("response", lambda window, _response: window.destroy())
        dialog.present()

    def _notify(self, title: str, body: str):
        notification = self.Gio.Notification.new(title)
        notification.set_body(body)
        notification.set_icon(self.Gio.ThemedIcon.new("resolve-ready"))
        self.application.send_notification(None, notification)


def _headless_convert(paths: list[Path]) -> int:
    try:
        converter = Converter()
        for path in paths:
            print(f"Converting {path}…", file=sys.stderr)
            result = converter.convert(
                path,
                lambda update: print(
                    f"\r{update.fraction * 100:5.1f}%",
                    end="",
                    file=sys.stderr,
                    flush=True,
                ),
            )
            print(
                f"\rRepaired {result.path} (backup: {result.backup_path})",
                file=sys.stderr,
            )
        return 0
    except (ResolverError, KeyboardInterrupt) as exc:
        print(f"resolve-ready: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="resolve-ready",
        description=(
            "Create a Resolve-compatible file with PCM audio (MP4 and MOV keep "
            "their name, other containers become MOV), transcoding video to DNxHR "
            "only when needed, and keeping the source in a hidden backup folder."
        ),
    )
    parser.add_argument("videos", nargs="*", type=Path)
    parser.add_argument(
        "--no-gui",
        action="store_true",
        help="convert in the terminal instead of opening the app",
    )
    parser.add_argument(
        "--watch-resolve",
        action="store_true",
        help=(
            "convert clips automatically as they are imported into the running "
            "DaVinci Resolve"
        ),
    )
    parser.add_argument(
        "--resolve-pid",
        type=int,
        help="with --watch-resolve: stop when this Resolve process exits",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    if args.watch_resolve:
        from resolver.watch import watch_resolve

        return watch_resolve(args.resolve_pid)

    if args.no_gui:
        if not args.videos:
            parser.error("--no-gui requires at least one video")
        return _headless_convert(args.videos)

    try:
        app = ResolveReadyApplication()
        # Passing paths through GApplication also forwards them correctly when an
        # already-running instance receives a file-manager context-menu action.
        gtk_argv = [sys.argv[0], *(str(path.expanduser().resolve()) for path in args.videos)]
        return app.run(gtk_argv)
    except ResolverError as exc:
        print(f"resolve-ready: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
