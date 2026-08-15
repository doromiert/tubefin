from __future__ import annotations

import hashlib
import os
import threading
import urllib.request
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk, Pango  # noqa: E402

from tubefin.models import MediaItem, MediaSection  # noqa: E402


class ThumbnailLoader:
    def __init__(self) -> None:
        cache_root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        self.directory = cache_root / "tubefin" / "thumbnails"
        self._callbacks: dict[str, list[Callable[[Path | None], None]]] = {}
        self._lock = threading.Lock()

    def load(self, url: str, callback: Callable[[Path | None], None]) -> None:
        cache_path = self.directory / hashlib.sha256(url.encode()).hexdigest()
        if cache_path.exists():
            GLib.idle_add(callback, cache_path)
            return

        with self._lock:
            callbacks = self._callbacks.setdefault(url, [])
            callbacks.append(callback)
            if len(callbacks) > 1:
                return

        threading.Thread(
            target=self._download,
            args=(url, cache_path),
            daemon=True,
            name="thumbnail-loader",
        ).start()

    def _download(self, url: str, cache_path: Path) -> None:
        result: Path | None = None
        temp_path = cache_path.with_suffix(".part")
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            request = urllib.request.Request(
                url,
                headers={
                    "Accept": "image/avif,image/webp,image/png,image/jpeg,*/*",
                    "User-Agent": "Mozilla/5.0 TubeFin/0.1",
                },
            )
            with urllib.request.urlopen(request, timeout=20) as response:
                temp_path.write_bytes(response.read(8 * 1024 * 1024))
            temp_path.replace(cache_path)
            result = cache_path
        except (OSError, urllib.error.URLError, TimeoutError):
            with suppress(FileNotFoundError):
                temp_path.unlink()

        with self._lock:
            callbacks = self._callbacks.pop(url, [])
        for callback in callbacks:
            GLib.idle_add(callback, result)


class MediaCard(Gtk.Button):
    def __init__(
        self,
        item: MediaItem,
        thumbnail_loader: ThumbnailLoader,
        on_activate: Callable[[MediaItem], None],
    ) -> None:
        super().__init__()
        self.item = item
        self.add_css_class("media-card")
        self.set_hexpand(False)
        self.set_valign(Gtk.Align.START)
        self.set_size_request(280, -1)
        self.set_tooltip_text(item.title)
        self.connect("clicked", lambda *_: on_activate(item))

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        content.set_size_request(280, -1)

        media_overlay = Gtk.Overlay()
        media_overlay.add_css_class("thumbnail")
        media_overlay.set_size_request(280, 158)
        media_overlay.set_overflow(Gtk.Overflow.HIDDEN)

        aspect_frame = Gtk.AspectFrame(ratio=16 / 9, obey_child=False)
        aspect_frame.set_hexpand(True)
        aspect_frame.set_child(media_overlay)

        fallback = Gtk.Image.new_from_icon_name(self._fallback_icon(item))
        fallback.set_pixel_size(42)
        fallback.add_css_class("dim-label")
        media_overlay.set_child(fallback)

        picture = Gtk.Picture()
        picture.set_content_fit(Gtk.ContentFit.COVER)
        picture.set_hexpand(True)
        picture.set_vexpand(True)
        picture.set_can_shrink(True)
        picture.set_visible(False)
        media_overlay.add_overlay(picture)

        if item.duration_label:
            duration = Gtk.Label(label=item.duration_label)
            duration.add_css_class("duration-badge")
            duration.set_halign(Gtk.Align.END)
            duration.set_valign(Gtk.Align.END)
            duration.set_margin_end(8)
            duration.set_margin_bottom(8)
            media_overlay.add_overlay(duration)

        if not item.playable:
            affordance = Gtk.Image.new_from_icon_name("go-next-symbolic")
            affordance.add_css_class("round-badge")
            affordance.set_halign(Gtk.Align.END)
            affordance.set_valign(Gtk.Align.END)
            affordance.set_margin_end(8)
            affordance.set_margin_bottom(8)
            media_overlay.add_overlay(affordance)

        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        labels.set_margin_start(2)
        labels.set_margin_end(2)

        title = Gtk.Label(label=item.title, xalign=0)
        title.add_css_class("media-title")
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_lines(2)
        title.set_wrap(True)
        title.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        title.set_max_width_chars(36)
        labels.append(title)

        if item.subtitle:
            subtitle = Gtk.Label(label=item.subtitle, xalign=0)
            subtitle.add_css_class("caption")
            subtitle.add_css_class("dim-label")
            subtitle.set_ellipsize(Pango.EllipsizeMode.END)
            subtitle.set_max_width_chars(36)
            labels.append(subtitle)

        content.append(aspect_frame)
        content.append(labels)
        self.set_child(content)

        if item.thumbnail_url:
            thumbnail_loader.load(
                item.thumbnail_url,
                lambda path: self._set_picture(picture, path),
            )

    @staticmethod
    def _set_picture(picture: Gtk.Picture, path: Path | None) -> bool:
        if path:
            picture.set_filename(str(path))
            picture.set_visible(True)
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _fallback_icon(item: MediaItem) -> str:
        if not item.playable:
            return "folder-symbolic"
        if item.kind == "Audio":
            return "audio-x-generic-symbolic"
        return "video-x-generic-symbolic"


class MediaGrid(Gtk.Box):
    def __init__(
        self,
        thumbnail_loader: ThumbnailLoader,
        on_activate: Callable[[MediaItem], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.thumbnail_loader = thumbnail_loader
        self.on_activate = on_activate

        self.status = Adw.StatusPage()
        self.status.set_vexpand(True)
        self.append(self.status)

        self.spinner = Gtk.Spinner(spinning=True)
        self.status.set_child(self.spinner)

        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroller.set_vexpand(True)
        self.scroller.set_visible(False)
        self.append(self.scroller)

        clamp = Adw.Clamp(maximum_size=1650, tightening_threshold=1200)
        clamp.set_margin_top(22)
        clamp.set_margin_bottom(32)
        clamp.set_margin_start(22)
        clamp.set_margin_end(22)
        self.scroller.set_child(clamp)

        self.flow = Gtk.FlowBox()
        self.flow.add_css_class("media-grid")
        self.flow.set_halign(Gtk.Align.FILL)
        self.flow.set_valign(Gtk.Align.START)
        self.flow.set_hexpand(True)
        self.flow.set_homogeneous(False)
        self.flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self.flow.set_min_children_per_line(1)
        self.flow.set_max_children_per_line(5)
        self.flow.set_column_spacing(16)
        self.flow.set_row_spacing(24)
        clamp.set_child(self.flow)

    def set_loading(self, title: str = "Loading…") -> None:
        self.status.set_icon_name("content-loading-symbolic")
        self.status.set_title(title)
        self.status.set_description("")
        self.status.set_child(self.spinner)
        self.spinner.start()
        self.status.set_visible(True)
        self.scroller.set_visible(False)

    def set_status(
        self,
        icon: str,
        title: str,
        description: str,
        action: Gtk.Widget | None = None,
    ) -> None:
        self.spinner.stop()
        self.status.set_icon_name(icon)
        self.status.set_title(title)
        self.status.set_description(description)
        self.status.set_child(action)
        self.status.set_visible(True)
        self.scroller.set_visible(False)

    def set_items(self, items: list[MediaItem]) -> None:
        child = self.flow.get_first_child()
        while child:
            following = child.get_next_sibling()
            self.flow.remove(child)
            child = following
        for item in items:
            self.flow.append(MediaCard(item, self.thumbnail_loader, self.on_activate))
        self.status.set_visible(False)
        self.scroller.set_visible(True)


class SectionShelf(Gtk.Box):
    def __init__(
        self,
        section: MediaSection,
        thumbnail_loader: ThumbnailLoader,
        on_activate: Callable[[MediaItem], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        title = Gtk.Label(label=section.title, xalign=0)
        title.add_css_class("title-2")
        self.append(title)

        flow = Gtk.FlowBox()
        flow.set_homogeneous(True)
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_min_children_per_line(1)
        flow.set_max_children_per_line(6)
        flow.set_column_spacing(18)
        flow.set_row_spacing(24)
        for item in section.items:
            flow.append(MediaCard(item, thumbnail_loader, on_activate))
        self.append(flow)
