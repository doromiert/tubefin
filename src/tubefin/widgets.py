from __future__ import annotations

import hashlib
import os
import threading
import urllib.parse
import urllib.request
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk, Pango  # noqa: E402

from tubefin.models import MediaItem, MediaSection  # noqa: E402


def icon_label(label: str, icon_name: str) -> Gtk.Widget:
    content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    content.append(Gtk.Image.new_from_icon_name(icon_name))
    content.append(Gtk.Label(label=label, xalign=0))
    return content


def labeled_button(label: str, icon_name: str) -> Gtk.Button:
    button = Gtk.Button(child=icon_label(label, icon_name))
    button.add_css_class("labeled-action")
    button.set_hexpand(False)
    return button


class ThumbnailLoader:
    def __init__(self) -> None:
        cache_root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        self.directory = cache_root / "tubefin" / "thumbnails"
        self._callbacks: dict[str, list[Callable[[Path | None], None]]] = {}
        self._lock = threading.Lock()
        self._shutdown = False

    def load(self, url: str, callback: Callable[[Path | None], None]) -> None:
        if self._shutdown:
            GLib.idle_add(callback, None)
            return
        if url.startswith("//"):
            url = f"https:{url}"
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme == "file":
            path = Path(urllib.request.url2pathname(parsed.path))
            GLib.idle_add(callback, path if path.is_file() else None)
            return
        local_path = Path(url)
        if not parsed.scheme and local_path.is_file():
            GLib.idle_add(callback, local_path)
            return
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
            if self._shutdown:
                return
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            request = urllib.request.Request(
                url,
                headers={
                    "Accept": "image/jpeg,image/png,*/*",
                    "User-Agent": "Mozilla/5.0 TubeFin/0.1",
                },
            )
            with urllib.request.urlopen(request, timeout=20) as response:
                contents = response.read(8 * 1024 * 1024)
            if self._shutdown:
                return
            temp_path.write_bytes(contents)
            temp_path.replace(cache_path)
            result = cache_path
        except (OSError, urllib.error.URLError, TimeoutError):
            with suppress(FileNotFoundError):
                temp_path.unlink()

        with self._lock:
            callbacks = self._callbacks.pop(url, [])
        for callback in callbacks:
            GLib.idle_add(callback, result)

    def shutdown(self) -> None:
        self._shutdown = True
        with self._lock:
            self._callbacks.clear()


class MediaCard(Gtk.Box):
    CONTENT_WIDTH = 251
    CONTENT_HEIGHT = 214

    def __init__(
        self,
        item: MediaItem,
        thumbnail_loader: ThumbnailLoader,
        on_activate: Callable[[MediaItem], None],
        on_queue: Callable[[MediaItem], None] | None = None,
        on_queue_next: Callable[[MediaItem], None] | None = None,
        on_save: Callable[[MediaItem], None] | None = None,
        on_watch_later: Callable[[MediaItem], None] | None = None,
        avatar_resolver: Callable[[str], str | None] | None = None,
        on_download: Callable[[MediaItem], None] | None = None,
        on_remove: Callable[[MediaItem], None] | None = None,
        on_dismiss: Callable[[MediaItem], None] | None = None,
        show_channel: bool = True,
        expand: bool = False,
        on_mark_watched: Callable[[MediaItem], None] | None = None,
        on_share: Callable[[MediaItem], None] | None = None,
        on_preview: Callable[[MediaItem], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.item = item
        self.add_css_class("media-card")
        self.set_hexpand(expand)
        self.set_valign(Gtk.Align.START)
        # Together with the 10px card padding this is an exact 271x235 tile.
        self.set_size_request(self.CONTENT_WIDTH, self.CONTENT_HEIGHT)
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.set_tooltip_text(item.title)
        preview_source = 0

        def preview() -> bool:
            nonlocal preview_source
            preview_source = 0
            if on_preview:
                on_preview(item)
            return GLib.SOURCE_REMOVE

        def schedule_preview(*_args: object) -> None:
            nonlocal preview_source
            if not preview_source:
                preview_source = GLib.timeout_add(600, preview)

        def cancel_preview(*_args: object) -> None:
            nonlocal preview_source
            if preview_source:
                GLib.source_remove(preview_source)
                preview_source = 0

        def activate(*_args: object) -> None:
            cancel_preview()
            on_activate(item)

        if item.playable and on_preview:
            motion = Gtk.EventControllerMotion()
            motion.connect("enter", schedule_preview)
            motion.connect("leave", cancel_preview)
            self.add_controller(motion)
            focus = Gtk.EventControllerFocus()
            focus.connect("enter", schedule_preview)
            focus.connect("leave", cancel_preview)
            self.add_controller(focus)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        content.set_size_request(self.CONTENT_WIDTH, self.CONTENT_HEIGHT)
        content.set_overflow(Gtk.Overflow.HIDDEN)

        media_overlay = Gtk.Overlay()
        media_overlay.add_css_class("thumbnail")
        media_overlay.set_size_request(251, 141)
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

        labels_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=9)
        labels_row.set_size_request(251, 63)
        labels_row.set_overflow(Gtk.Overflow.HIDDEN)
        avatar_frame = Gtk.Overlay(width_request=36, height_request=36)
        avatar_frame.set_size_request(36, 36)
        avatar_frame.set_halign(Gtk.Align.START)
        avatar_frame.set_valign(Gtk.Align.CENTER)
        avatar_frame.set_overflow(Gtk.Overflow.HIDDEN)
        avatar_frame.add_css_class("channel-avatar")
        avatar_fallback = Gtk.Image.new_from_icon_name("avatar-default-symbolic")
        avatar_fallback.set_pixel_size(20)
        avatar_fallback.add_css_class("dim-label")
        avatar_frame.set_child(avatar_fallback)
        avatar = Gtk.Picture(width_request=36, height_request=36)
        avatar.set_content_fit(Gtk.ContentFit.COVER)
        avatar.set_hexpand(True)
        avatar.set_vexpand(True)
        avatar.set_visible(False)
        avatar_frame.add_overlay(avatar)
        if show_channel and item.source == "youtube":
            channel_url = str(item.payload.get("channel_url") or "")
            if channel_url:
                channel_button = Gtk.Button(child=avatar_frame)
                channel_button.add_css_class("flat")
                channel_button.add_css_class("channel-avatar-button")
                channel_button.set_valign(Gtk.Align.CENTER)
                channel_button.set_tooltip_text(f"Open {item.subtitle or 'channel'}")
                channel_button.connect(
                    "clicked",
                    lambda *_: on_activate(
                        MediaItem(
                            id=str(item.payload.get("channel_id") or channel_url),
                            title=item.subtitle or "Channel",
                            source="youtube-channel",
                            playable=False,
                            payload={"channel_url": channel_url},
                        )
                    ),
                )
                labels_row.append(channel_button)
            else:
                labels_row.append(avatar_frame)
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True)
        labels.set_margin_start(2)
        labels.set_margin_end(2)
        labels.set_valign(Gtk.Align.CENTER)

        title = Gtk.Label(label=item.title, xalign=0)
        title.add_css_class("media-title")
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_lines(2)
        title.set_wrap(True)
        title.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        title.set_max_width_chars(15)
        labels.append(title)

        if show_channel and item.subtitle:
            subtitle = Gtk.Label(label=item.subtitle, xalign=0)
            subtitle.add_css_class("caption")
            subtitle.add_css_class("dim-label")
            subtitle.set_ellipsize(Pango.EllipsizeMode.END)
            subtitle.set_max_width_chars(15)
            labels.append(subtitle)

        thumbnail_button = Gtk.Button(child=aspect_frame)
        thumbnail_button.add_css_class("flat")
        thumbnail_button.add_css_class("media-card-target")
        thumbnail_button.connect("clicked", activate)
        content.append(thumbnail_button)
        title_button = Gtk.Button(child=labels, hexpand=True)
        title_button.add_css_class("flat")
        title_button.add_css_class("media-card-target")
        title_button.connect("clicked", activate)
        labels_row.append(title_button)
        if any(
            (
                on_queue,
                on_queue_next,
                on_save,
                on_watch_later,
                on_download,
                on_remove,
                on_dismiss,
                on_mark_watched,
                on_share,
            )
        ):
            actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            actions.set_margin_top(8)
            actions.set_margin_bottom(8)
            actions.set_margin_start(8)
            actions.set_margin_end(8)
            menu_stack = Gtk.Stack()
            menu_stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
            menu_stack.set_transition_duration(180)
            menu_stack.add_named(actions, "actions")
            queue_actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            queue_actions.set_margin_top(8)
            queue_actions.set_margin_bottom(8)
            queue_actions.set_margin_start(8)
            queue_actions.set_margin_end(8)
            queue_back = labeled_button("Back", "go-previous-symbolic")
            queue_back.add_css_class("flat")
            queue_back.connect(
                "clicked", lambda *_: menu_stack.set_visible_child_name("actions")
            )
            queue_actions.append(queue_back)
            popover = Gtk.Popover(child=menu_stack)

            def choose(callback: Callable[[MediaItem], None]) -> None:
                popover.popdown()
                callback(item)

            if item.playable and on_queue_next:
                play_next = labeled_button("Play next", "go-next-symbolic")
                play_next.add_css_class("flat")
                play_next.connect("clicked", lambda *_: choose(on_queue_next))
                queue_actions.append(play_next)
            if item.playable and on_queue:
                queue_end = labeled_button("Add to the end", "list-add-symbolic")
                queue_end.add_css_class("flat")
                queue_end.connect("clicked", lambda *_: choose(on_queue))
                queue_actions.append(queue_end)
            menu_stack.add_named(queue_actions, "queue")
            if item.playable and (on_queue or on_queue_next):
                add_to_queue = labeled_button("Add to queue", "list-add-symbolic")
                add_to_queue.add_css_class("flat")
                add_to_queue.connect(
                    "clicked", lambda *_: menu_stack.set_visible_child_name("queue")
                )
                actions.append(add_to_queue)
            if item.playable and on_save:
                save = labeled_button("Save", "list-add-symbolic")
                save.add_css_class("flat")
                save.connect("clicked", lambda *_: choose(on_save))
                actions.append(save)
            if item.playable and on_watch_later:
                later = labeled_button("Watch later", "alarm-symbolic")
                later.add_css_class("flat")
                later.connect("clicked", lambda *_: choose(on_watch_later))
                actions.append(later)
            if item.playable and on_mark_watched:
                watched = labeled_button("Mark as watched", "object-select-symbolic")
                watched.add_css_class("flat")
                watched.connect("clicked", lambda *_: choose(on_mark_watched))
                actions.append(watched)
            if item.playable and on_share:
                share = labeled_button("Copy share link", "send-to-symbolic")
                share.add_css_class("flat")
                share.connect("clicked", lambda *_: choose(on_share))
                actions.append(share)
            if item.playable and on_download and item.source in {"youtube", "jellyfin"}:
                download = labeled_button("Download", "folder-download-symbolic")
                download.add_css_class("flat")
                download.connect("clicked", lambda *_: choose(on_download))
                actions.append(download)
            if on_dismiss and item.payload.get("recommendation"):
                dismiss = labeled_button("Not interested", "edit-delete-symbolic")
                dismiss.add_css_class("flat")
                dismiss.connect("clicked", lambda *_: choose(on_dismiss))
                actions.append(dismiss)
            if on_remove:
                remove = labeled_button("Remove download", "user-trash-symbolic")
                remove.add_css_class("flat")
                remove.connect("clicked", lambda *_: choose(on_remove))
                actions.append(remove)
            action_button = Gtk.MenuButton(
                icon_name="view-more-symbolic",
                tooltip_text="More actions",
                popover=popover,
            )
            popover.connect(
                "closed", lambda *_: menu_stack.set_visible_child_name("actions")
            )
            action_button.add_css_class("flat")
            action_button.set_valign(Gtk.Align.CENTER)
            labels_row.append(action_button)
        content.append(labels_row)
        self.append(content)

        if item.thumbnail_url:
            thumbnail_loader.load(
                item.thumbnail_url,
                lambda path: self._set_picture(picture, path),
            )
        avatar_url = item.payload.get("channel_avatar_url")
        if show_channel and avatar_url:
            thumbnail_loader.load(
                str(avatar_url),
                lambda path: self._set_picture(avatar, path),
            )
        elif show_channel and avatar_resolver and item.payload.get("channel_url"):
            threading.Thread(
                target=self._resolve_avatar,
                args=(avatar_resolver, str(item.payload["channel_url"]), thumbnail_loader, avatar),
                daemon=True,
                name="channel-avatar-resolver",
            ).start()

    def do_measure(
        self, orientation: Gtk.Orientation, _for_size: int
    ) -> tuple[int, int, int, int]:
        size = (
            self.CONTENT_WIDTH
            if orientation == Gtk.Orientation.HORIZONTAL
            else self.CONTENT_HEIGHT
        )
        return size, size, -1, -1

    @classmethod
    def _resolve_avatar(
        cls,
        resolver: Callable[[str], str | None],
        channel_url: str,
        thumbnail_loader: ThumbnailLoader,
        picture: Gtk.Picture,
    ) -> None:
        try:
            avatar_url = resolver(channel_url)
        except Exception:
            return
        if avatar_url:
            thumbnail_loader.load(avatar_url, lambda path: cls._set_picture(picture, path))

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
        on_queue: Callable[[MediaItem], None] | None = None,
        on_preview: Callable[[MediaItem], None] | None = None,
        on_queue_next: Callable[[MediaItem], None] | None = None,
        on_save: Callable[[MediaItem], None] | None = None,
        on_watch_later: Callable[[MediaItem], None] | None = None,
        avatar_resolver: Callable[[str], str | None] | None = None,
        on_download: Callable[[MediaItem], None] | None = None,
        on_remove: Callable[[MediaItem], None] | None = None,
        on_dismiss: Callable[[MediaItem], None] | None = None,
        show_channel: bool = True,
        on_mark_watched: Callable[[MediaItem], None] | None = None,
        on_share: Callable[[MediaItem], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.thumbnail_loader = thumbnail_loader
        self.on_activate = on_activate
        self.on_queue = on_queue
        self.on_preview = on_preview
        self.on_queue_next = on_queue_next
        self.on_save = on_save
        self.on_watch_later = on_watch_later
        self.avatar_resolver = avatar_resolver
        self.on_download = on_download
        self.on_remove = on_remove
        self.on_dismiss = on_dismiss
        self.show_channel = show_channel
        self.on_mark_watched = on_mark_watched
        self.on_share = on_share

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

        clamp = Adw.Clamp(maximum_size=2400, tightening_threshold=1600)
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
        self.flow.set_homogeneous(True)
        self.flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self.flow.set_min_children_per_line(1)
        self.flow.set_max_children_per_line(20)
        self.flow.set_column_spacing(10)
        self.flow.set_row_spacing(14)
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
        for index, item in enumerate(items):
            self.flow.append(
                MediaCard(
                    item,
                    self.thumbnail_loader,
                    self.on_activate,
                    self.on_queue,
                    self.on_queue_next,
                    self.on_save,
                    self.on_watch_later,
                    self.avatar_resolver,
                    self.on_download,
                    self.on_remove,
                    self.on_dismiss,
                    self.show_channel,
                    True,
                    self.on_mark_watched,
                    self.on_share,
                    self.on_preview,
                )
            )
            if index < 4 and item.playable and self.on_preview:
                self.on_preview(item)
        self.status.set_visible(False)
        self.scroller.set_visible(True)

    def append_items(self, items: list[MediaItem]) -> None:
        for item in items:
            self.flow.append(
                MediaCard(
                    item,
                    self.thumbnail_loader,
                    self.on_activate,
                    self.on_queue,
                    self.on_queue_next,
                    self.on_save,
                    self.on_watch_later,
                    self.avatar_resolver,
                    self.on_download,
                    self.on_remove,
                    self.on_dismiss,
                    self.show_channel,
                    True,
                    self.on_mark_watched,
                    self.on_share,
                    self.on_preview,
                )
            )

    def scroll_to_item(self, index: int) -> None:
        def scroll() -> bool:
            child = self.flow.get_child_at_index(index)
            if not child:
                return GLib.SOURCE_REMOVE
            adjustment = self.scroller.get_vadjustment()
            allocation = child.get_allocation()
            target = max(
                adjustment.get_lower(),
                min(
                    float(allocation.y),
                    adjustment.get_upper() - adjustment.get_page_size(),
                ),
            )
            adjustment.set_value(target)
            child.grab_focus()
            return GLib.SOURCE_REMOVE

        GLib.idle_add(scroll)


class SectionShelf(Gtk.Box):
    def __init__(
        self,
        section: MediaSection,
        thumbnail_loader: ThumbnailLoader,
        on_activate: Callable[[MediaItem], None],
        on_queue: Callable[[MediaItem], None] | None = None,
        on_queue_next: Callable[[MediaItem], None] | None = None,
        on_save: Callable[[MediaItem], None] | None = None,
        on_watch_later: Callable[[MediaItem], None] | None = None,
        *,
        horizontal: bool = False,
        avatar_resolver: Callable[[str], str | None] | None = None,
        on_download: Callable[[MediaItem], None] | None = None,
        on_remove: Callable[[MediaItem], None] | None = None,
        on_dismiss: Callable[[MediaItem], None] | None = None,
        expand_cards: bool = False,
        on_mark_watched: Callable[[MediaItem], None] | None = None,
        on_share: Callable[[MediaItem], None] | None = None,
        on_preview: Callable[[MediaItem], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.thumbnail_loader = thumbnail_loader
        self.on_activate = on_activate
        self.on_queue = on_queue
        self.on_queue_next = on_queue_next
        self.on_save = on_save
        self.on_watch_later = on_watch_later
        self.avatar_resolver = avatar_resolver
        self.on_download = on_download
        self.on_remove = on_remove
        self.on_dismiss = on_dismiss
        self.expand_cards = expand_cards
        self.on_mark_watched = on_mark_watched
        self.on_share = on_share
        self.on_preview = on_preview
        self.flow: Gtk.FlowBox | None = None
        self.row: Gtk.Box | None = None
        self.content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.revealer = Gtk.Revealer()
        self.revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.revealer.set_transition_duration(180)
        self.revealer.set_reveal_child(True)
        self.revealer.set_child(self.content_box)
        if section.title:
            heading = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            title = Gtk.Label(label=section.title, xalign=0, hexpand=True)
            title.add_css_class("title-2")
            heading.append(title)
            self.collapse_icon = Gtk.Image.new_from_icon_name("pan-down-symbolic")
            heading.append(self.collapse_icon)
            toggle = Gtk.Button(child=heading)
            toggle.add_css_class("flat")
            toggle.add_css_class("section-heading")
            toggle.set_tooltip_text("Collapse section")
            toggle.connect("clicked", self._toggle_collapsed)
            self.append(toggle)
        self.append(self.revealer)

        if horizontal:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18)
            row.set_halign(Gtk.Align.CENTER)
            self.row = row
            for item in section.items:
                row.append(
                    MediaCard(
                        item,
                        thumbnail_loader,
                        on_activate,
                        on_queue,
                        on_queue_next,
                        on_save,
                        on_watch_later,
                        avatar_resolver,
                        on_download,
                        on_remove,
                        on_dismiss,
                        show_channel=True,
                        expand=expand_cards,
                        on_mark_watched=on_mark_watched,
                        on_share=on_share,
                        on_preview=on_preview,
                    )
                )
            scroller = Gtk.ScrolledWindow()
            scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
            scroller.set_propagate_natural_height(True)
            scroller.set_child(row)
            self.content_box.append(scroller)
            return

        flow = Gtk.FlowBox()
        self.flow = flow
        flow.add_css_class("media-grid")
        flow.set_halign(Gtk.Align.FILL if expand_cards else Gtk.Align.START)
        flow.set_hexpand(True)
        flow.set_homogeneous(True)
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_min_children_per_line(1)
        flow.set_max_children_per_line(20)
        flow.set_column_spacing(10)
        flow.set_row_spacing(14)
        self.append_items(section.items)
        self.content_box.append(flow)

    def _toggle_collapsed(self, button: Gtk.Button) -> None:
        expanded = not self.revealer.get_reveal_child()
        self.revealer.set_reveal_child(expanded)
        self.collapse_icon.set_from_icon_name(
            "pan-down-symbolic" if expanded else "pan-end-symbolic"
        )
        button.set_tooltip_text("Collapse section" if expanded else "Expand section")

    def _card(self, item: MediaItem) -> MediaCard:
        return MediaCard(
            item,
            self.thumbnail_loader,
            self.on_activate,
            self.on_queue,
            self.on_queue_next,
            self.on_save,
            self.on_watch_later,
            self.avatar_resolver,
            self.on_download,
            self.on_remove,
            self.on_dismiss,
            True,
            self.expand_cards,
            self.on_mark_watched,
            self.on_share,
            self.on_preview,
        )

    def append_items(self, items: list[MediaItem]) -> None:
        target = self.flow or self.row
        if target is None:
            return
        for item in items:
            target.append(self._card(item))

    def remove_item(self, item_id: str) -> None:
        target = self.flow or self.row
        if target is None:
            return
        child = target.get_first_child()
        while child:
            following = child.get_next_sibling()
            card = child.get_child() if isinstance(child, Gtk.FlowBoxChild) else child
            if isinstance(card, MediaCard) and card.item.id == item_id:
                target.remove(child)
                return
            child = following

    def cards(self) -> list[MediaCard]:
        target = self.flow or self.row
        if target is None:
            return []
        cards: list[MediaCard] = []
        child = target.get_first_child()
        while child:
            card = (
                child.get_child()
                if isinstance(child, Gtk.FlowBoxChild)
                else child
            )
            if isinstance(card, MediaCard):
                cards.append(card)
            child = child.get_next_sibling()
        return cards
