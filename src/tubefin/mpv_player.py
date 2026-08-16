"""GTK4/libmpv player adapted from Cine's GPL-3.0-or-later implementation.

Cine copyright 2025-2026 Diego Povliuk:
https://github.com/diegopvlk/Cine/blob/main/src/mpv_gl_area.py
"""

from __future__ import annotations

import ctypes
import difflib
import logging
import re
import threading
import time
from collections.abc import Callable
from contextlib import suppress

import gi
import mpv

gi.require_version("Gdk", "4.0")
gi.require_version("GdkWayland", "4.0")
gi.require_version("GdkX11", "4.0")
gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, GdkWayland, GdkX11, GLib, Gtk, Pango  # noqa: E402

from tubefin.models import (  # noqa: E402
    AudioTrack,
    StreamVariant,
    SubtitleTrack,
    VideoChapter,
)

logger = logging.getLogger(__name__)

_LANGUAGE_CODES = {
    "arabic": "ar",
    "chinese": "zh",
    "czech": "cs",
    "danish": "da",
    "dutch": "nl",
    "english": "en",
    "finnish": "fi",
    "french": "fr",
    "german": "de",
    "hindi": "hi",
    "indonesian": "id",
    "italian": "it",
    "japanese": "ja",
    "korean": "ko",
    "norwegian": "no",
    "polish": "pl",
    "portuguese": "pt",
    "russian": "ru",
    "slovak": "sk",
    "spanish": "es",
    "swedish": "sv",
    "turkish": "tr",
    "ukrainian": "uk",
}

_libegl = ctypes.CDLL("libEGL.so.1")
_egl_get_proc_address = _libegl.eglGetProcAddress
_egl_get_proc_address.restype = ctypes.c_void_p
_egl_get_proc_address.argtypes = [ctypes.c_char_p]

_libgl = ctypes.CDLL("libGL.so.1")
_gl_get_integer = _libgl.glGetIntegerv
_gl_get_integer.argtypes = [ctypes.c_uint, ctypes.POINTER(ctypes.c_int)]
_gl_framebuffer_binding = 0x8CA6


def _display_parameters() -> dict[str, int]:
    display = Gdk.Display.get_default()
    gtk = ctypes.CDLL("libgtk-4.so.1")

    def pointer(value: object) -> int:
        ctypes.pythonapi.PyCapsule_GetPointer.restype = ctypes.c_void_p
        ctypes.pythonapi.PyCapsule_GetPointer.argtypes = (ctypes.py_object,)
        return ctypes.pythonapi.PyCapsule_GetPointer(value.__gpointer__, None)  # type: ignore[attr-defined]

    if isinstance(display, GdkWayland.WaylandDisplay):
        gtk.gdk_wayland_display_get_wl_display.restype = ctypes.c_void_p
        gtk.gdk_wayland_display_get_wl_display.argtypes = [ctypes.c_void_p]
        return {"wl_display": gtk.gdk_wayland_display_get_wl_display(pointer(display))}
    if isinstance(display, GdkX11.X11Display):
        gtk.gdk_x11_display_get_xdisplay.restype = ctypes.c_void_p
        gtk.gdk_x11_display_get_xdisplay.argtypes = [ctypes.c_void_p]
        return {"x11_display": gtk.gdk_x11_display_get_xdisplay(pointer(display))}
    return {}


class MpvGLArea(Gtk.GLArea):
    def __init__(self, player: mpv.MPV, on_error: Callable[[str], None]) -> None:
        super().__init__(auto_render=False, hexpand=True, vexpand=True)
        self.player = player
        self.on_error = on_error
        self.context: mpv.MpvRenderContext | None = None
        self.render_generation = 0
        self.render_error_reported = False
        self.framebuffer = ctypes.c_int()
        self.proc_address = mpv.MpvGlGetProcAddressFn(
            lambda _instance, name: _egl_get_proc_address(name)
        )
        self.connect("realize", self._realize)
        self.connect("unrealize", self._unrealize)
        self.connect("render", self._render)

    def _realize(self, _area: Gtk.GLArea) -> None:
        self.make_current()
        self.render_generation += 1
        generation = self.render_generation
        if error := self.get_error():
            self._report_render_error(error)
            return
        try:
            self.context = mpv.MpvRenderContext(
                self.player,
                "opengl",
                opengl_init_params={"get_proc_address": self.proc_address},
                **_display_parameters(),
            )
        except Exception as error:
            logger.exception("Could not create the libmpv OpenGL render context")
            self._report_render_error(error)
            return
        self.context.update_cb = lambda: GLib.idle_add(
            self._queue_render,
            generation,
            priority=GLib.PRIORITY_HIGH_IDLE,
        )

    def _report_render_error(self, error: object) -> None:
        if self.render_error_reported:
            return
        self.render_error_reported = True
        GLib.idle_add(
            self.on_error,
            "OpenGL video output is unavailable. Check the graphics driver "
            f"or try an X11 session. ({error})",
        )

    def _unrealize(self, _area: Gtk.GLArea) -> None:
        self.release_context()

    def release_context(self) -> None:
        self.render_generation += 1
        if self.context:
            if self.get_realized():
                self.make_current()
            self.context.update_cb = None
            self.context.free()
            self.context = None

    def _queue_render(self, generation: int) -> bool:
        if generation == self.render_generation and self.context and self.get_mapped():
            self.queue_render()
        return GLib.SOURCE_REMOVE

    def _render(self, _area: Gtk.GLArea, _context: object) -> bool:
        if not self.context:
            return False
        width = self.get_width() * self.get_scale_factor()
        height = self.get_height() * self.get_scale_factor()
        if width <= 0 or height <= 0 or self.get_error():
            return False
        _gl_get_integer(_gl_framebuffer_binding, self.framebuffer)
        self.context.render(
            flip_y=True,
            opengl_fbo={
                "w": width,
                "h": height,
                "fbo": self.framebuffer.value,
            },
        )
        return True


class ChapterSeekBar(Gtk.DrawingArea):
    """One continuous drag target rendered as chapter-sized track pieces."""

    def __init__(self, on_seek: Callable[[float], None]) -> None:
        super().__init__()
        self.on_seek = on_seek
        self.duration = 1.0
        self.position = 0.0
        self.chapters: list[VideoChapter] = []
        self.drag_origin = 0.0
        self.set_hexpand(True)
        self.set_content_height(28)
        self.set_draw_func(self._draw)
        self.set_has_tooltip(True)
        self.connect("query-tooltip", self._query_tooltip)

        click = Gtk.GestureClick(button=1)
        click.connect("pressed", self._pressed)
        self.add_controller(click)
        drag = Gtk.GestureDrag(button=1)
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.group(click)
        self.add_controller(drag)

    def set_value(self, value: float) -> None:
        value = max(0.0, min(float(value), self.duration))
        if abs(value - self.position) >= 0.01:
            self.position = value
            self.queue_draw()

    def get_value(self) -> float:
        return self.position

    def set_range(self, _minimum: float, maximum: float) -> None:
        self.duration = max(1.0, float(maximum))
        self.position = min(self.position, self.duration)
        self.queue_draw()

    def set_chapters(self, chapters: list[VideoChapter]) -> None:
        self.chapters = sorted(chapters, key=lambda chapter: chapter.start)
        self.queue_draw()

    def chapter_target(self, direction: int) -> float | None:
        starts = [chapter.start for chapter in self._sections()]
        if len(starts) <= 1:
            return None
        if direction > 0:
            return next(
                (start for start in starts if start > self.position + 0.75),
                None,
            )
        current = max(
            (index for index, start in enumerate(starts) if start <= self.position),
            default=0,
        )
        if self.position - starts[current] > 3:
            return starts[current]
        return starts[current - 1] if current else starts[0]

    def chapter_at(self, value: float) -> VideoChapter | None:
        if not self.chapters:
            return None
        sections = self._sections()
        for index, section in enumerate(sections):
            if section.start <= value < section.end:
                return section
            if index == len(sections) - 1 and value == section.end:
                return section
        return None

    def _sections(self) -> list[VideoChapter]:
        valid = [
            chapter
            for chapter in self.chapters
            if 0 <= chapter.start < self.duration
        ]
        if not valid:
            return [VideoChapter("Video", 0.0, self.duration)]
        sections: list[VideoChapter] = []
        if valid[0].start > 0:
            sections.append(VideoChapter("Video", 0.0, valid[0].start))
        for index, chapter in enumerate(valid):
            end = (
                valid[index + 1].start
                if index + 1 < len(valid)
                else self.duration
            )
            if end > chapter.start:
                sections.append(VideoChapter(chapter.title, chapter.start, end))
        return sections or [VideoChapter("Video", 0.0, self.duration)]

    def _layout(self, width: float) -> list[tuple[VideoChapter, float, float]]:
        sections = self._sections()
        gap = 4.0 if len(sections) > 1 else 0.0
        available = max(1.0, width - 16 - gap * (len(sections) - 1))
        result: list[tuple[VideoChapter, float, float]] = []
        cursor = 8.0
        for section in sections:
            section_width = available * (section.end - section.start) / self.duration
            result.append((section, cursor, cursor + section_width))
            cursor += section_width + gap
        return result

    def _time_at(self, x: float) -> float:
        layout = self._layout(max(1, self.get_width()))
        for index, (section, left, right) in enumerate(layout):
            if x <= right:
                fraction = max(0.0, min(1.0, (x - left) / max(1.0, right - left)))
                return section.start + fraction * (section.end - section.start)
            if index + 1 < len(layout) and x < layout[index + 1][1]:
                return section.end
        return self.duration

    def _x_at(self, value: float, width: float) -> float:
        for section, left, right in self._layout(width):
            if value <= section.end:
                fraction = (value - section.start) / max(0.001, section.end - section.start)
                return left + max(0.0, min(1.0, fraction)) * (right - left)
        return width

    def _seek_at(self, x: float) -> None:
        value = self._time_at(x)
        self.set_value(value)
        self.on_seek(value)

    def _pressed(
        self, _gesture: Gtk.GestureClick, _presses: int, x: float, _y: float
    ) -> None:
        self._seek_at(x)

    def _drag_begin(self, _gesture: Gtk.GestureDrag, x: float, _y: float) -> None:
        self.drag_origin = x

    def _drag_update(
        self, _gesture: Gtk.GestureDrag, offset_x: float, _offset_y: float
    ) -> None:
        self._seek_at(self.drag_origin + offset_x)

    def _draw(
        self,
        _area: Gtk.DrawingArea,
        context: object,
        width: int,
        height: int,
    ) -> None:
        center = height / 2
        context.set_line_width(6)
        context.set_line_cap(1)
        for section, left, right in self._layout(width):
            context.set_source_rgba(0.75, 0.75, 0.78, 0.3)
            context.move_to(left + 3, center)
            context.line_to(max(left + 3, right - 3), center)
            context.stroke()
            if self.position <= section.start:
                continue
            progress = min(self.position, section.end)
            fraction = (progress - section.start) / max(0.001, section.end - section.start)
            filled = left + fraction * (right - left)
            context.set_source_rgba(0.57, 0.25, 0.7, 1)
            context.move_to(left + 3, center)
            context.line_to(max(left + 3, filled - 3), center)
            context.stroke()
        thumb = self._x_at(self.position, width)
        context.set_source_rgba(0.92, 0.92, 0.94, 1)
        context.arc(thumb, center, 8, 0, 6.283185307)
        context.fill()

    def _query_tooltip(
        self,
        _widget: Gtk.Widget,
        x: int,
        _y: int,
        keyboard_mode: bool,
        tooltip: Gtk.Tooltip,
    ) -> bool:
        value = self.position if keyboard_mode else self._time_at(x)
        chapter = next(
            (
                section
                for section in self._sections()
                if section.start <= value <= section.end
            ),
            None,
        )
        total = max(0, int(value))
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        timestamp = (
            f"{hours}:{minutes:02d}:{seconds:02d}"
            if hours
            else f"{minutes}:{seconds:02d}"
        )
        tooltip.set_text(f"{chapter.title} · {timestamp}" if chapter else timestamp)
        return True


class MpvPlayer(Gtk.Box):
    def __init__(
        self,
        on_ready: Callable[[], None],
        on_error: Callable[[str], None],
        on_ended: Callable[[], None] | None = None,
        on_previous: Callable[[], None] | None = None,
        on_next: Callable[[], None] | None = None,
        on_fullscreen: Callable[[], None] | None = None,
        on_collapse: Callable[[], None] | None = None,
        on_fullscreen_swipe: Callable[[float], None] | None = None,
        on_seek_feedback: Callable[[int], None] | None = None,
        on_controls_visibility: Callable[[bool], None] | None = None,
        buffer_seconds: int = 20,
        on_buffer_changed: Callable[[int], None] | None = None,
        on_state_changed: Callable[[float, float, bool], None] | None = None,
        default_caption_language: str = "",
        default_audio_language: str = "",
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.set_focusable(True)
        self.on_ready = on_ready
        self.on_error = on_error
        self.on_ended = on_ended
        self.on_previous = on_previous
        self.on_next = on_next
        self.on_fullscreen = on_fullscreen
        self.on_collapse = on_collapse
        self.on_fullscreen_swipe = on_fullscreen_swipe
        self.on_seek_feedback = on_seek_feedback
        self.on_controls_visibility = on_controls_visibility
        self.on_buffer_changed = on_buffer_changed
        self.on_state_changed = on_state_changed
        self.default_caption_language = default_caption_language.strip()
        self.default_audio_language = default_audio_language.strip()
        self.buffer_seconds = buffer_seconds
        self.seeking = False
        self.should_play = False
        self.shutting_down = False
        self.variants: list[StreamVariant] = []
        self.subtitles: list[SubtitleTrack] = []
        self.audio_tracks: list[AudioTrack] = []
        self.chapters: list[VideoChapter] = []
        self.original_audio_id = ""
        self.external_audio_ids: dict[str, str] = {}
        self.default_url = ""
        self.default_headers: dict[str, str] = {}
        self.default_quality_label = "Auto"
        self.pending_seek: float | None = None
        self.syncing_options = False
        self.hide_controls_source = 0
        self.single_click_source = 0
        self.fullscreen_mode = False
        self.collapse_swipe_from_top = False
        self.touch_swipe_direction = ""
        self.swipe_progress = 0.0
        self.swipe_reset_source = 0
        self.time_has_hours = False
        self.bound_picker_popovers: set[int] = set()
        self.playback_speed = 1.0
        self.subtitle_offset = 0.0
        self.sync_speed = 1.0
        self.last_audible_volume = 100.0
        self.last_motion: tuple[float, float] | None = None
        ytdl_raw_options = ["ignore-config=", "js-runtimes=deno"]
        self.player = mpv.MPV(
            vo="libmpv",
            osc=False,
            config=False,
            terminal=False,
            input_default_bindings=False,
            input_vo_keyboard=False,
            keep_open=True,
            hwdec="no",
            gpu_hwdec_interop="no",
            ytdl=True,
            ytdl_raw_options=",".join(ytdl_raw_options),
            cache_secs=buffer_seconds,
            demuxer_max_bytes="32MiB",
            audio_client_name="TubeFin",
        )
        self.video = MpvGLArea(self.player, self.on_error)
        click = Gtk.GestureClick(button=1)
        click.connect("released", self._video_clicked)
        self.video.add_controller(click)
        touch_drag = Gtk.GestureDrag()
        touch_drag.set_touch_only(True)
        touch_drag.connect("drag-begin", self._touch_drag_begin)
        touch_drag.connect("drag-update", self._touch_drag_update)
        touch_drag.connect("drag-end", self._touch_drag_end)
        self.video.add_controller(touch_drag)
        self.video_overlay = Gtk.Overlay()
        self.video_overlay.set_hexpand(True)
        self.video_overlay.set_vexpand(True)
        self.video_overlay.set_child(self.video)
        self.fullscreen_transport = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=18
        )
        self.fullscreen_transport.add_css_class("fullscreen-transport")
        self.fullscreen_transport.set_halign(Gtk.Align.CENTER)
        self.fullscreen_transport.set_valign(Gtk.Align.CENTER)
        self.fullscreen_transport.set_visible(False)
        self.video_overlay.add_overlay(self.fullscreen_transport)
        self.fullscreen_swipe_indicator = Gtk.Image.new_from_icon_name(
            "go-down-symbolic"
        )
        self.fullscreen_swipe_indicator.add_css_class(
            "fullscreen-swipe-indicator"
        )
        self.fullscreen_swipe_indicator.set_halign(Gtk.Align.CENTER)
        self.fullscreen_swipe_indicator.set_valign(Gtk.Align.START)
        self.fullscreen_swipe_indicator.set_can_target(False)
        self.fullscreen_swipe_indicator.set_size_request(46, 46)
        self.fullscreen_swipe_indicator.set_visible(False)
        self.fullscreen_swipe_layer = Gtk.Fixed()
        self.fullscreen_swipe_layer.set_hexpand(True)
        self.fullscreen_swipe_layer.set_vexpand(True)
        self.fullscreen_swipe_layer.set_can_target(False)
        self.fullscreen_swipe_layer.put(self.fullscreen_swipe_indicator, 0, 18)
        self.video_overlay.add_overlay(self.fullscreen_swipe_layer)
        self.playback_overlay = Gtk.Overlay()
        self.playback_overlay.set_hexpand(True)
        self.playback_overlay.set_vexpand(True)
        self.playback_overlay.set_child(self.video_overlay)
        self.append(self.playback_overlay)
        self.chapter_label = Gtk.Label(xalign=0)
        self.chapter_label.add_css_class("chapter-label")
        self.chapter_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.chapter_label.set_halign(Gtk.Align.FILL)
        self.chapter_label.set_valign(Gtk.Align.END)
        self.chapter_label.set_can_target(False)
        self.chapter_label.set_visible(False)
        self.playback_overlay.add_overlay(self.chapter_label)
        self.controls = self._build_controls()
        self.controls.set_halign(Gtk.Align.FILL)
        self.controls.set_valign(Gtk.Align.END)
        self.playback_overlay.add_overlay(self.controls)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._motion)
        self.video.add_controller(motion)
        self._observe()

    def _build_controls(self) -> Gtk.Widget:
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        controls.add_css_class("mpv-controls")
        self.previous_button = Gtk.Button(
            icon_name="media-skip-backward-symbolic", tooltip_text="Previous"
        )
        self.previous_button.connect(
            "clicked", lambda *_: self.on_previous and self.on_previous()
        )
        controls.append(self.previous_button)
        self.play_button = Gtk.Button(icon_name="media-playback-pause-symbolic")
        self.play_button.set_tooltip_text("Play or pause")
        self.play_button.connect("clicked", lambda *_: self.player.command_async("cycle", "pause"))
        controls.append(self.play_button)
        self.next_button = Gtk.Button(
            icon_name="media-skip-forward-symbolic", tooltip_text="Next"
        )
        self.next_button.connect("clicked", lambda *_: self.on_next and self.on_next())
        controls.append(self.next_button)
        self.elapsed = Gtk.Label(label="00:00")
        self.elapsed.add_css_class("player-timestamp")
        controls.append(self.elapsed)
        self.progress = ChapterSeekBar(self.seek_absolute)
        controls.append(self.progress)
        self.duration = Gtk.Label(label="00:00")
        self.duration.add_css_class("player-timestamp")
        controls.append(self.duration)
        self.quality = Gtk.DropDown()
        self.quality.set_halign(Gtk.Align.END)
        self.quality.set_tooltip_text("Video quality")
        self.quality.connect("notify::selected", self._quality_changed)
        self.captions = Gtk.DropDown()
        self.captions.set_halign(Gtk.Align.END)
        self.captions.set_tooltip_text("Closed captions")
        self.captions.connect("notify::selected", self._captions_changed)
        self.audio = Gtk.DropDown()
        self.audio.set_halign(Gtk.Align.END)
        self.audio.set_tooltip_text("Audio track")
        self.audio.connect("notify::selected", self._audio_changed)
        self.buffer = Gtk.DropDown.new_from_strings(
            ["10s buffer", "20s buffer", "30s buffer", "60s buffer", "120s buffer"]
        )
        buffer_values = [10, 20, 30, 60, 120]
        closest = min(
            range(len(buffer_values)),
            key=lambda index: abs(buffer_values[index] - self.buffer_seconds),
        )
        self.buffer.set_selected(closest)
        self.buffer.set_tooltip_text("Network buffer")
        self.buffer.connect("notify::selected", self._buffer_changed)
        self.buffer.set_halign(Gtk.Align.END)
        self.speed_values = (0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0)
        self.speed_control = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=6
        )
        self.speed_control.set_halign(Gtk.Align.FILL)
        self.speed_control.set_hexpand(True)
        speed_down = Gtk.Button(label="−", tooltip_text="Decrease playback speed")
        speed_down.add_css_class("speed-step-button")
        speed_down.connect("clicked", lambda *_: self._step_playback_speed(-1))
        self.speed_value = Gtk.Button(
            label="1×", tooltip_text="Reset playback speed to 1×"
        )
        self.speed_value.add_css_class("speed-value-button")
        self.speed_value.set_hexpand(True)
        self.speed_value.connect("clicked", lambda *_: self.set_playback_speed(1.0))
        speed_up = Gtk.Button(label="+", tooltip_text="Increase playback speed")
        speed_up.add_css_class("speed-step-button")
        speed_up.connect("clicked", lambda *_: self._step_playback_speed(1))
        self.speed_control.append(speed_down)
        self.speed_control.append(self.speed_value)
        self.speed_control.append(speed_up)
        self.subtitle_offset_control = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=6
        )
        self.subtitle_offset_control.set_halign(Gtk.Align.FILL)
        self.subtitle_offset_control.set_hexpand(True)
        subtitle_earlier = Gtk.Button(
            label="−", tooltip_text="Show subtitles 0.1 seconds earlier"
        )
        subtitle_earlier.add_css_class("speed-step-button")
        subtitle_earlier.connect(
            "clicked", lambda *_: self._step_subtitle_offset(-1)
        )
        self.subtitle_offset_value = Gtk.Button(
            label="0.0s", tooltip_text="Reset subtitle offset"
        )
        self.subtitle_offset_value.add_css_class("speed-value-button")
        self.subtitle_offset_value.set_hexpand(True)
        self.subtitle_offset_value.connect(
            "clicked", lambda *_: self.set_subtitle_offset(0.0)
        )
        subtitle_later = Gtk.Button(
            label="+", tooltip_text="Show subtitles 0.1 seconds later"
        )
        subtitle_later.add_css_class("speed-step-button")
        subtitle_later.connect(
            "clicked", lambda *_: self._step_subtitle_offset(1)
        )
        self.subtitle_offset_control.append(subtitle_earlier)
        self.subtitle_offset_control.append(self.subtitle_offset_value)
        self.subtitle_offset_control.append(subtitle_later)
        self.volume = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 200, 1)
        self.volume.set_draw_value(False)
        self.volume.set_value(100)
        self.volume.set_size_request(150, -1)
        self.volume.set_tooltip_text("Volume")
        self.volume.connect("change-value", self._volume_change_value)
        self.volume.connect("value-changed", self._volume_changed)
        self.mute_button = Gtk.Button(
            icon_name="audio-volume-high-symbolic", tooltip_text="Mute"
        )
        self.mute_button.add_css_class("square-button")
        self.mute_button.set_size_request(36, 36)
        self.mute_button.connect("clicked", lambda *_: self._toggle_mute())
        self.volume_control = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8
        )
        self.volume_control.append(self.mute_button)
        self.volume_control.append(self.volume)
        settings_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        settings_box.set_margin_top(12)
        settings_box.set_margin_bottom(12)
        settings_box.set_margin_start(12)
        settings_box.set_margin_end(12)
        self.option_rows: dict[str, Gtk.Box] = {}
        self.option_control_size_group = Gtk.SizeGroup(
            mode=Gtk.SizeGroupMode.HORIZONTAL
        )
        for label_text, control in (
            ("Quality", self.quality),
            ("Closed captions", self.captions),
            ("Audio track", self.audio),
            ("Network buffer", self.buffer),
            ("Playback speed", self.speed_control),
            ("Subtitle offset", self.subtitle_offset_control),
            ("Volume", self.volume_control),
        ):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
            row.append(Gtk.Label(label=label_text, xalign=0, hexpand=True))
            self.option_control_size_group.add_widget(control)
            row.append(control)
            settings_box.append(row)
            self.option_rows[label_text] = row
        self.settings_popover = Gtk.Popover(child=settings_box)
        self.settings_popover.set_autohide(True)
        self.settings_popover.set_cascade_popdown(True)
        self.settings_popover.connect(
            "notify::visible", self._settings_visibility_changed
        )
        for picker in (self.quality, self.captions, self.audio, self.buffer):
            picker.connect("realize", self._bind_picker_popovers)
        settings = Gtk.MenuButton(
            icon_name="emblem-system-symbolic",
            tooltip_text="Playback settings",
            popover=self.settings_popover,
        )
        self.settings_button = settings
        controls.append(settings)
        self.fullscreen_button = Gtk.Button(
            icon_name="view-fullscreen-symbolic", tooltip_text="Fullscreen"
        )
        self.fullscreen_button.connect(
            "clicked", lambda *_: self.on_fullscreen and self.on_fullscreen()
        )
        controls.append(self.fullscreen_button)
        return controls

    def _observe(self) -> None:
        @self.player.event_callback("file-loaded")
        def file_loaded(_event: object) -> None:
            GLib.idle_add(self._file_loaded)

        @self.player.event_callback("end-file")
        def end_file(event: object) -> None:
            data = event.data  # type: ignore[attr-defined]
            if data.reason == mpv.MpvEventEndFile.ERROR:
                message = mpv.ErrorCode.exception_for_ec(data.error)
                message = str(message or "unknown playback error")
                GLib.idle_add(self.on_error, message)
            elif data.reason == mpv.MpvEventEndFile.EOF and self.on_ended:
                GLib.idle_add(self.on_ended)

        @self.player.property_observer("pause")
        def pause_changed(_name: str, paused: bool) -> None:
            GLib.idle_add(self._sync_pause, paused)

        @self.player.property_observer("time-pos")
        def position_changed(_name: str, position: float | None) -> None:
            GLib.idle_add(self._sync_position, float(position or 0))

        @self.player.property_observer("duration")
        def duration_changed(_name: str, duration: float | None) -> None:
            GLib.idle_add(self._sync_duration, float(duration or 0))

    def load(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        variants: list[StreamVariant] | None = None,
        subtitles: list[SubtitleTrack] | None = None,
        audio_tracks: list[AudioTrack] | None = None,
        chapters: list[VideoChapter] | None = None,
        default_label: str = "Auto",
    ) -> None:
        self.should_play = True
        self.default_url = url
        self.default_headers = headers or {}
        self.default_quality_label = default_label
        self.variants = variants or []
        self.subtitles = subtitles or []
        self.audio_tracks = audio_tracks or []
        self.chapters = chapters or []
        self.progress.set_range(0, 1)
        self.progress.set_value(0)
        self.progress.set_chapters(self.chapters)
        self._sync_chapter_label()
        self.set_subtitle_offset(0.0)
        self.original_audio_id = ""
        self.external_audio_ids.clear()
        self._set_options()
        self._load_url(url, self.default_headers)

    def set_variants(self, variants: list[StreamVariant]) -> None:
        """Publish qualities resolved after playback has already started."""
        self.variants = variants
        self._set_quality_options()

    def set_audio_tracks(self, tracks: list[AudioTrack]) -> None:
        """Publish alternate audio while preserving embedded local tracks."""
        combined = list(self.audio_tracks)
        languages = {track.language.casefold() for track in combined}
        for track in tracks:
            if track.language.casefold() not in languages:
                combined.append(track)
                languages.add(track.language.casefold())
        self.audio_tracks = combined
        self._set_audio_options()
        self._apply_audio_selection()

    def set_subtitles(self, subtitles: list[SubtitleTrack]) -> None:
        self.subtitles = list(subtitles)
        was_syncing = self.syncing_options
        self.syncing_options = True
        captions = ["Captions off", *(track.label for track in self.subtitles)]
        self.captions.set_model(Gtk.StringList.new(captions))
        self.captions.set_selected(self._preferred_caption_index())
        self.option_rows["Closed captions"].set_visible(bool(self.subtitles))
        self.option_rows["Subtitle offset"].set_visible(bool(self.subtitles))
        self.syncing_options = was_syncing
        GLib.idle_add(self._normalize_option_widths)
        self._apply_caption_selection()

    def set_chapters(self, chapters: list[VideoChapter]) -> None:
        self.chapters = list(chapters)
        self.progress.set_chapters(self.chapters)
        self._sync_chapter_label()

    def set_default_audio_language(self, language: str) -> None:
        self.default_audio_language = language.strip()
        self._set_audio_options()
        self._apply_audio_selection()

    def stop(self) -> None:
        if self.shutting_down:
            return
        self.should_play = False
        with suppress(mpv.ShutdownError):
            self.player.stop()

    def shutdown(self) -> None:
        if self.shutting_down:
            return
        self.shutting_down = True
        self.should_play = False
        self.video.release_context()
        player = self.player

        def terminate() -> None:
            with suppress(mpv.ShutdownError):
                player.terminate()

        threading.Thread(
            target=terminate, daemon=True, name="libmpv-shutdown"
        ).start()

    def toggle_pause(self) -> None:
        if not self.shutting_down:
            with suppress(mpv.ShutdownError):
                self.player.command_async("cycle", "pause")

    def seek_relative(self, seconds: int) -> None:
        if not self.shutting_down:
            with suppress(mpv.ShutdownError):
                self.player.command_async("seek", seconds, "relative")

    def seek_absolute(self, seconds: float) -> None:
        if not self.shutting_down:
            self.progress.set_value(seconds)
            self._sync_chapter_label()
            with suppress(mpv.ShutdownError):
                self.player.command_async("seek", max(0, seconds), "absolute+exact")

    def seek_chapter(self, direction: int) -> bool:
        target = self.progress.chapter_target(direction)
        if target is None:
            return False
        self.seek_absolute(target)
        return True

    def resume_at(self, seconds: float) -> None:
        if float(self.player.duration or 0) > 0:
            self.seek_absolute(seconds)
        else:
            self.pending_seek = max(0, seconds)

    def set_paused(self, paused: bool) -> None:
        if not self.shutting_down:
            with suppress(mpv.ShutdownError):
                self.player.pause = paused

    def set_speed(self, speed: float) -> None:
        self.sync_speed = max(0.9, min(speed, 1.1))
        self._apply_speed()

    def _apply_speed(self) -> None:
        if self.shutting_down:
            return
        with suppress(mpv.ShutdownError):
            self.player.speed = self.playback_speed * self.sync_speed
            self.player.cache_secs = max(
                1, round(self.buffer_seconds * self.playback_speed)
            )

    def set_playback_speed(self, speed: float) -> None:
        speed = max(0.5, min(speed, 2.0))
        self.playback_speed = speed
        self.speed_value.set_label(self._speed_label(speed))
        self._apply_speed()

    @staticmethod
    def _speed_label(speed: float) -> str:
        return f"{speed:g}×"

    def _step_playback_speed(self, direction: int) -> None:
        index = min(
            range(len(self.speed_values)),
            key=lambda value: abs(self.speed_values[value] - self.playback_speed),
        )
        index = max(0, min(index + direction, len(self.speed_values) - 1))
        self.set_playback_speed(self.speed_values[index])

    def set_subtitle_offset(self, seconds: float) -> None:
        self.subtitle_offset = max(-10.0, min(10.0, round(seconds, 1)))
        label = (
            "0.0s"
            if abs(self.subtitle_offset) < 0.05
            else f"{self.subtitle_offset:+.1f}s"
        )
        self.subtitle_offset_value.set_label(label)
        if not self.shutting_down:
            with suppress(mpv.ShutdownError):
                self.player.sub_delay = self.subtitle_offset

    def _step_subtitle_offset(self, direction: int) -> None:
        self.set_subtitle_offset(self.subtitle_offset + direction * 0.1)

    def set_volume(self, volume: float) -> None:
        self.volume.set_value(max(0, min(volume, 200)))

    @property
    def position(self) -> float:
        if self.shutting_down:
            return 0.0
        try:
            return float(self.player.time_pos or 0)
        except mpv.ShutdownError:
            return 0.0

    @property
    def media_duration(self) -> float:
        if self.shutting_down:
            return 0.0
        try:
            return float(self.player.duration or 0)
        except mpv.ShutdownError:
            return 0.0

    @property
    def paused(self) -> bool:
        if self.shutting_down:
            return True
        try:
            return bool(self.player.pause)
        except mpv.ShutdownError:
            return True

    def reveal_controls(self) -> None:
        self.controls.set_visible(True)
        self.fullscreen_transport.set_visible(self.fullscreen_mode)
        self._sync_chapter_label()
        if self.on_controls_visibility:
            self.on_controls_visibility(True)
        if self.hide_controls_source:
            GLib.source_remove(self.hide_controls_source)
            self.hide_controls_source = 0
        if self.fullscreen_mode and not self.settings_popover.get_visible():
            self.hide_controls_source = GLib.timeout_add_seconds(3, self._hide_controls)

    def set_fullscreen_mode(self, fullscreen: bool) -> None:
        self.fullscreen_mode = fullscreen
        self._move_transport_controls(fullscreen)
        controls_height = self.controls.measure(Gtk.Orientation.VERTICAL, -1)[1]
        self.chapter_label.set_margin_bottom(controls_height if fullscreen else 0)
        self.fullscreen_button.set_icon_name(
            "view-restore-symbolic" if fullscreen else "view-fullscreen-symbolic"
        )
        self.fullscreen_button.set_tooltip_text("Exit fullscreen" if fullscreen else "Fullscreen")
        self.reveal_controls()

    def _move_transport_controls(self, fullscreen: bool) -> None:
        buttons = (self.previous_button, self.play_button, self.next_button)
        if fullscreen:
            for button in buttons:
                if button.get_parent() == self.controls:
                    self.controls.remove(button)
                if button.get_parent() != self.fullscreen_transport:
                    self.fullscreen_transport.append(button)
                button.add_css_class("fullscreen-transport-button")
                size = 76 if button == self.play_button else 64
                button.set_size_request(size, size)
                button.set_valign(Gtk.Align.CENTER)
                if button != self.play_button:
                    button.add_css_class("fullscreen-skip-button")
                icon = button.get_child()
                if isinstance(icon, Gtk.Image):
                    icon.set_pixel_size(36 if button == self.play_button else 30)
            self.play_button.add_css_class("fullscreen-play-button")
            return
        for button in buttons:
            if button.get_parent() == self.fullscreen_transport:
                self.fullscreen_transport.remove(button)
            button.remove_css_class("fullscreen-transport-button")
            button.remove_css_class("fullscreen-skip-button")
            button.set_size_request(-1, -1)
            button.set_valign(Gtk.Align.FILL)
            icon = button.get_child()
            if isinstance(icon, Gtk.Image):
                icon.set_pixel_size(-1)
        self.play_button.remove_css_class("fullscreen-play-button")
        for button in reversed(buttons):
            if button.get_parent() != self.controls:
                self.controls.prepend(button)

    def _video_clicked(
        self,
        gesture: Gtk.GestureClick,
        presses: int,
        x: float,
        _y: float,
    ) -> None:
        self.grab_focus()
        if self.settings_popover.get_visible():
            self.settings_popover.popdown()
            self.reveal_controls()
            return
        device = gesture.get_current_event_device()
        touch = bool(device and device.get_source() == Gdk.InputSource.TOUCHSCREEN)
        if presses == 2:
            if self.single_click_source:
                GLib.source_remove(self.single_click_source)
                self.single_click_source = 0
            if touch:
                width = max(1, self.video.get_allocated_width())
                if x < width * 0.4:
                    self.seek_relative(-10)
                    if self.on_seek_feedback:
                        self.on_seek_feedback(-10)
                elif x > width * 0.6:
                    self.seek_relative(10)
                    if self.on_seek_feedback:
                        self.on_seek_feedback(10)
            elif self.on_fullscreen:
                self.on_fullscreen()
            self.reveal_controls()
        else:
            self.single_click_source = GLib.timeout_add(
                250, self._single_video_clicked, touch
            )
            if not touch:
                self.reveal_controls()

    def _single_video_clicked(self, touch: bool = False) -> bool:
        self.single_click_source = 0
        if touch:
            if self.controls.get_visible():
                self.hide_controls()
            else:
                self.reveal_controls()
        else:
            self.toggle_pause()
        return GLib.SOURCE_REMOVE

    def _touch_drag_begin(
        self, _gesture: Gtk.GestureDrag, _x: float, y: float
    ) -> None:
        height = max(1, self.video.get_allocated_height())
        edge_zone = max(64, min(160, height * 0.18))
        self.collapse_swipe_from_top = y <= edge_zone
        if self.collapse_swipe_from_top:
            self.touch_swipe_direction = "down"
        elif not self.fullscreen_mode and y >= height - edge_zone:
            self.touch_swipe_direction = "up"
        else:
            self.touch_swipe_direction = ""
        if self.touch_swipe_direction:
            if self.swipe_reset_source:
                GLib.source_remove(self.swipe_reset_source)
                self.swipe_reset_source = 0
            self.reveal_controls()

    def _touch_drag_update(
        self, _gesture: Gtk.GestureDrag, offset_x: float, offset_y: float
    ) -> None:
        if self.touch_swipe_direction != "down" or not self.fullscreen_mode:
            return
        if offset_y <= 0 or offset_y <= abs(offset_x) * 0.75:
            self._set_swipe_progress(0)
            return
        height = max(1, self.video.get_allocated_height())
        self._set_swipe_progress(min(1.0, offset_y / (height * 0.42)))

    def _touch_drag_end(
        self, _gesture: Gtk.GestureDrag, offset_x: float, offset_y: float
    ) -> None:
        direction = self.touch_swipe_direction
        self.collapse_swipe_from_top = False
        self.touch_swipe_direction = ""
        distance = max(80, self.video.get_allocated_height() * 0.12)
        downward = (
            direction == "down"
            and offset_y >= distance
            and offset_y > abs(offset_x) * 1.25
        )
        upward = (
            direction == "up"
            and offset_y <= -distance
            and abs(offset_y) > abs(offset_x) * 1.25
        )
        if downward:
            if self.fullscreen_mode and self.on_fullscreen:
                self.on_fullscreen()
            elif self.on_collapse:
                self.on_collapse()
        elif upward and self.on_fullscreen:
            self.on_fullscreen()
        if downward or upward:
            self._clear_swipe_transform()
        else:
            self._animate_swipe_reset()

    def _set_swipe_progress(self, progress: float) -> None:
        self.swipe_progress = max(0.0, min(progress, 1.0))
        if self.fullscreen_mode:
            self.fullscreen_swipe_indicator.set_visible(self.swipe_progress > 0)
            self.fullscreen_swipe_layer.move(
                self.fullscreen_swipe_indicator,
                max(0, (self.video_overlay.get_allocated_width() - 46) / 2),
                18 + round(54 * self.swipe_progress),
            )
            self.fullscreen_swipe_indicator.set_opacity(
                min(1.0, self.swipe_progress * 2.5)
            )
            if self.on_fullscreen_swipe:
                self.on_fullscreen_swipe(self.swipe_progress)
            return
        width = max(1, self.get_allocated_width())
        height = max(1, self.get_allocated_height())
        horizontal = round(width * 0.08 * self.swipe_progress)
        vertical = round(height * 0.055 * self.swipe_progress)
        self.video.set_margin_start(horizontal)
        self.video.set_margin_end(horizontal)
        self.video.set_margin_top(vertical)
        self.video.set_margin_bottom(vertical)
        self.video.set_opacity(1.0 - 0.12 * self.swipe_progress)

    def _animate_swipe_reset(self) -> None:
        if self.swipe_reset_source:
            GLib.source_remove(self.swipe_reset_source)
        started = time.monotonic()
        initial = self.swipe_progress

        def tick() -> bool:
            elapsed = (time.monotonic() - started) / 0.18
            if elapsed >= 1:
                self._set_swipe_progress(0)
                self.swipe_reset_source = 0
                return GLib.SOURCE_REMOVE
            eased = 1 - (1 - elapsed) ** 3
            self._set_swipe_progress(initial * (1 - eased))
            return GLib.SOURCE_CONTINUE

        self.swipe_reset_source = GLib.timeout_add(16, tick)

    def _clear_swipe_transform(self) -> None:
        self.swipe_progress = 0
        self.fullscreen_swipe_indicator.set_visible(False)
        self.fullscreen_swipe_indicator.set_opacity(0)
        self.video.set_margin_start(0)
        self.video.set_margin_end(0)
        self.video.set_margin_top(0)
        self.video.set_margin_bottom(0)
        self.video.set_opacity(1)

    def _settings_visibility_changed(
        self, _popover: Gtk.Popover, _property: object
    ) -> None:
        self.reveal_controls()

    def _normalize_option_widths(self) -> bool:
        controls = (
            self.quality,
            self.captions,
            self.audio,
            self.buffer,
            self.speed_control,
            self.subtitle_offset_control,
            self.volume_control,
        )
        width = max(
            control.measure(Gtk.Orientation.HORIZONTAL, -1)[1]
            for control in controls
        )
        for control in controls:
            control.set_size_request(width, -1)
        return GLib.SOURCE_REMOVE

    def _motion(self, _controller: Gtk.EventControllerMotion, _x: float, _y: float) -> None:
        position = (_x, _y)
        if (
            self.last_motion
            and abs(position[0] - self.last_motion[0]) < 2
            and abs(position[1] - self.last_motion[1]) < 2
        ):
            return
        self.last_motion = position
        self.reveal_controls()

    def _hide_controls(self) -> bool:
        if self.settings_popover.get_visible():
            self.hide_controls_source = 0
            return GLib.SOURCE_REMOVE
        self.controls.set_visible(False)
        self.fullscreen_transport.set_visible(False)
        self._sync_chapter_label()
        if self.on_controls_visibility:
            self.on_controls_visibility(False)
        self.hide_controls_source = 0
        return GLib.SOURCE_REMOVE

    def hide_controls(self) -> None:
        if self.settings_popover.get_visible():
            return
        if self.hide_controls_source:
            GLib.source_remove(self.hide_controls_source)
            self.hide_controls_source = 0
        self._hide_controls()

    def _ready(self) -> bool:
        if not self.shutting_down:
            self.on_ready()
        return GLib.SOURCE_REMOVE

    def _file_loaded(self) -> bool:
        if self.shutting_down:
            return GLib.SOURCE_REMOVE
        if self.should_play:
            with suppress(mpv.ShutdownError):
                self.player.pause = False
        if self.pending_seek is not None:
            with suppress(mpv.ShutdownError):
                self.player.command_async("seek", self.pending_seek, "absolute+exact")
            self.pending_seek = None
        self._discover_embedded_audio_tracks()
        self._apply_audio_selection()
        self._apply_caption_selection()
        return self._ready()

    def _load_url(self, url: str, headers: dict[str, str]) -> None:
        self.original_audio_id = ""
        self.external_audio_ids.clear()
        self.player.http_header_fields = [f"{name}: {value}" for name, value in headers.items()]
        self.player.loadfile(url, "replace")
        self.player.pause = False

    def _set_options(self) -> None:
        self._set_quality_options()
        self.syncing_options = True
        captions = ["Captions off", *(track.label for track in self.subtitles)]
        self.captions.set_model(Gtk.StringList.new(captions))
        self.captions.set_selected(self._preferred_caption_index())
        self.option_rows["Closed captions"].set_visible(bool(self.subtitles))
        self.option_rows["Subtitle offset"].set_visible(bool(self.subtitles))
        self._set_audio_options()
        self.syncing_options = False
        GLib.idle_add(self._normalize_option_widths)

    def _set_audio_options(self) -> None:
        was_syncing = self.syncing_options
        self.syncing_options = True
        labels = ["Original", *(track.label for track in self.audio_tracks)]
        self.audio.set_model(Gtk.StringList.new(labels))
        self.audio.set_selected(self._preferred_audio_index())
        self.option_rows["Audio track"].set_visible(True)
        self.syncing_options = was_syncing

    def _discover_embedded_audio_tracks(self) -> None:
        with suppress(mpv.ShutdownError):
            tracks = [
                track
                for track in (self.player.track_list or [])
                if track.get("type") == "audio"
            ]
            if not tracks:
                return
            original = next(
                (
                    track
                    for track in tracks
                    if track.get("default") or track.get("selected")
                ),
                tracks[0],
            )
            self.original_audio_id = str(original.get("id") or "")
            if self.audio_tracks:
                return
            embedded: list[AudioTrack] = []
            for track in tracks:
                track_id = str(track.get("id") or "")
                if not track_id or track_id == self.original_audio_id:
                    continue
                language = str(track.get("lang") or "und")
                label = str(track.get("title") or language.upper() or "Audio")
                embedded.append(
                    AudioTrack(label, language, f"mpv://aid/{track_id}")
                )
            if embedded:
                self.audio_tracks = embedded
                self._set_audio_options()

    def _preferred_audio_index(self) -> int:
        preference = self.default_audio_language.strip()
        if not preference or not self.audio_tracks:
            return 0
        scores = [
            self.language_match_score(preference, track.label, track.language)
            for track in self.audio_tracks
        ]
        best = max(range(len(scores)), key=scores.__getitem__)
        return best + 1 if scores[best] >= 0.55 else 0

    def _audio_changed(self, _dropdown: Gtk.DropDown, _property: object) -> None:
        if not self.syncing_options:
            self._apply_audio_selection()

    def _apply_audio_selection(self) -> None:
        if self.shutting_down:
            return
        selected = self.audio.get_selected()
        if selected == 0:
            with suppress(mpv.ShutdownError):
                self.player.command("set", "aid", self.original_audio_id or "auto")
            return
        if selected > len(self.audio_tracks):
            return
        track = self.audio_tracks[selected - 1]
        if track.url.startswith("mpv://aid/"):
            with suppress(mpv.ShutdownError):
                self.player.command("set", "aid", track.url.rpartition("/")[2])
            return
        known_id = self.external_audio_ids.get(track.url)
        if known_id:
            with suppress(mpv.ShutdownError):
                self.player.command("set", "aid", known_id)
            return
        if track.headers:
            self.player.http_header_fields = [
                f"{name}: {value}" for name, value in track.headers.items()
            ]
        with suppress(mpv.ShutdownError):
            before = {
                str(value.get("id"))
                for value in (self.player.track_list or [])
                if value.get("type") == "audio" and value.get("id") is not None
            }
            self.player.command(
                "audio-add", track.url, "select", track.label, track.language
            )
            audio_tracks = [
                value
                for value in (self.player.track_list or [])
                if value.get("type") == "audio" and value.get("id") is not None
            ]
            added = next(
                (value for value in reversed(audio_tracks) if str(value["id"]) not in before),
                next((value for value in audio_tracks if value.get("selected")), None),
            )
            if added:
                track_id = str(added["id"])
                self.external_audio_ids[track.url] = track_id
                self.player.command("set", "aid", track_id)

    def _set_quality_options(self) -> None:
        self.syncing_options = True
        qualities = [
            self.default_quality_label,
            *(variant.label for variant in self.variants),
        ]
        self.quality.set_model(Gtk.StringList.new(qualities))
        self.quality.set_selected(0)
        self.option_rows["Quality"].set_visible(True)
        self.syncing_options = False
        GLib.idle_add(self._normalize_option_widths)

    def _quality_changed(self, dropdown: Gtk.DropDown, _property: object) -> None:
        selected = dropdown.get_selected()
        if self.syncing_options or selected > len(self.variants):
            return
        self.pending_seek = float(self.player.time_pos or 0)
        if selected == 0:
            self._load_url(self.default_url, self.default_headers)
            return
        variant = self.variants[selected - 1]
        self._load_url(variant.url, variant.headers)

    def _captions_changed(self, dropdown: Gtk.DropDown, _property: object) -> None:
        if self.syncing_options:
            return
        self._apply_caption_selection()

    def _bind_picker_popovers(self, picker: Gtk.Widget) -> None:
        pending = [picker]
        while pending:
            widget = pending.pop()
            child = widget.get_first_child()
            while child:
                if (
                    isinstance(child, Gtk.Popover)
                    and id(child) not in self.bound_picker_popovers
                ):
                    self.bound_picker_popovers.add(id(child))
                    child.connect("closed", self._picker_popover_closed)
                pending.append(child)
                child = child.get_next_sibling()

    def _picker_popover_closed(self, _popover: Gtk.Popover) -> None:
        GLib.idle_add(self.settings_popover.popdown)

    def _apply_caption_selection(self) -> None:
        selected = self.captions.get_selected()
        if selected == 0:
            self.player.sub_visibility = False
            self.player.command_async("set", "sid", "no")
            return
        if selected <= len(self.subtitles):
            self.player.sub_visibility = True
            url = self.subtitles[selected - 1].url
            if url.startswith("mpv://sid/"):
                self.player.command_async("set", "sid", url.rpartition("/")[2])
            else:
                self.player.command_async("sub-add", url, "select")

    def _preferred_caption_index(self) -> int:
        preference = self.default_caption_language.strip()
        if not preference or not self.subtitles:
            return 0
        scores = [self._caption_match_score(preference, track) for track in self.subtitles]
        best = max(range(len(scores)), key=scores.__getitem__)
        return best + 1 if scores[best] >= 0.55 else 0

    @staticmethod
    def _caption_match_score(preference: str, track: SubtitleTrack) -> float:
        return MpvPlayer.language_match_score(preference, track.label, track.language)

    @staticmethod
    def language_match_score(
        preference: str, label_value: str, language_value: str
    ) -> float:
        preferred = " ".join(re.findall(r"[\w]+", preference.casefold()))
        preferred_code = _LANGUAGE_CODES.get(preferred)
        if not preferred_code and len(preferred) in {2, 3}:
            preferred_code = preferred
        language = language_value.casefold().replace("_", "-")
        label = " ".join(re.findall(r"[\w]+", label_value.casefold()))
        label_words = set(label.split()) - {
            "auto",
            "automatic",
            "captions",
            "generated",
            "subtitles",
        }
        preferred_words = set(preferred.split())
        if preferred == label:
            score = 1.0
        elif preferred_code and (
            language == preferred_code or language.startswith(f"{preferred_code}-")
        ):
            score = 0.98
        elif preferred_words and preferred_words <= label_words:
            score = 0.92
        else:
            candidates = [label, language.replace("-", " ")]
            score = max(
                difflib.SequenceMatcher(None, preferred, candidate).ratio()
                for candidate in candidates
            )
        if "auto" in label_value.casefold():
            score -= 0.01
        return score

    def _buffer_changed(self, dropdown: Gtk.DropDown, _property: object) -> None:
        values = [10, 20, 30, 60, 120]
        selected = dropdown.get_selected()
        if selected >= len(values):
            return
        self.buffer_seconds = values[selected]
        self._apply_speed()
        if self.on_buffer_changed:
            self.on_buffer_changed(self.buffer_seconds)

    def _volume_changed(self, scale: Gtk.Scale) -> None:
        if self.shutting_down:
            return
        value = scale.get_value()
        if value > 0:
            self.last_audible_volume = value
        if value <= 0:
            icon = "audio-volume-muted-symbolic"
            tooltip = "Unmute"
        elif value < 50:
            icon = "audio-volume-low-symbolic"
            tooltip = "Mute"
        elif value < 100:
            icon = "audio-volume-medium-symbolic"
            tooltip = "Mute"
        else:
            icon = "audio-volume-high-symbolic"
            tooltip = "Mute"
        self.mute_button.set_icon_name(icon)
        self.mute_button.set_tooltip_text(tooltip)
        with suppress(mpv.ShutdownError):
            self.player.volume = value

    def _volume_change_value(
        self, scale: Gtk.Scale, _scroll: Gtk.ScrollType, value: float
    ) -> bool:
        if abs(value - 100) <= 4 and value != 100:
            scale.set_value(100)
            return True
        return False

    def _toggle_mute(self) -> None:
        if self.volume.get_value() > 0:
            self.volume.set_value(0)
        else:
            self.volume.set_value(max(1, self.last_audible_volume))

    def _sync_pause(self, paused: bool) -> bool:
        if self.shutting_down:
            return GLib.SOURCE_REMOVE
        self.play_button.set_icon_name(
            "media-playback-start-symbolic" if paused else "media-playback-pause-symbolic"
        )
        self._state_changed()
        return GLib.SOURCE_REMOVE

    def _sync_position(self, position: float) -> bool:
        if self.shutting_down:
            return GLib.SOURCE_REMOVE
        self.progress.set_value(position)
        self._sync_chapter_label()
        self.elapsed.set_label(self._time_label(position, self.time_has_hours))
        self._state_changed()
        return GLib.SOURCE_REMOVE

    def _sync_duration(self, duration: float) -> bool:
        if self.shutting_down:
            return GLib.SOURCE_REMOVE
        self.progress.set_range(0, max(duration, 1))
        self.time_has_hours = duration >= 3600
        self.elapsed.set_label(
            self._time_label(self.progress.get_value(), self.time_has_hours)
        )
        self.duration.set_label(self._time_label(duration, self.time_has_hours))
        self._sync_chapter_label()
        return GLib.SOURCE_REMOVE

    def _sync_chapter_label(self) -> None:
        chapter = self.progress.chapter_at(self.progress.get_value())
        self.chapter_label.set_label(chapter.title if chapter else "")
        self.chapter_label.set_visible(
            chapter is not None
            and (not self.fullscreen_mode or self.controls.get_visible())
        )

    def _state_changed(self) -> None:
        if self.shutting_down or not self.on_state_changed:
            return
        try:
            self.on_state_changed(self.position, self.media_duration, self.paused)
        except mpv.ShutdownError:
            self.shutting_down = True

    @staticmethod
    def _time_label(seconds: float, show_hours: bool = False) -> str:
        total = max(0, int(seconds))
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        if show_hours:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"
