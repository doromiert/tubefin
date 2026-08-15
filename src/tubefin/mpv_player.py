"""GTK4/libmpv player adapted from Cine's GPL-3.0-or-later implementation.

Cine copyright 2025-2026 Diego Povliuk:
https://github.com/diegopvlk/Cine/blob/main/src/mpv_gl_area.py
"""

from __future__ import annotations

import ctypes
import logging
from collections.abc import Callable

import gi
import mpv

gi.require_version("Gdk", "4.0")
gi.require_version("GdkWayland", "4.0")
gi.require_version("GdkX11", "4.0")
gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, GdkWayland, GdkX11, GLib, Gtk  # noqa: E402

logger = logging.getLogger(__name__)

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
    def __init__(self, player: mpv.MPV) -> None:
        super().__init__(auto_render=False, hexpand=True, vexpand=True)
        self.player = player
        self.context: mpv.MpvRenderContext | None = None
        self.framebuffer = ctypes.c_int()
        self.proc_address = mpv.MpvGlGetProcAddressFn(
            lambda _instance, name: _egl_get_proc_address(name)
        )
        self.connect("realize", self._realize)
        self.connect("unrealize", self._unrealize)
        self.connect("render", self._render)

    def _realize(self, _area: Gtk.GLArea) -> None:
        self.make_current()
        self.context = mpv.MpvRenderContext(
            self.player,
            "opengl",
            opengl_init_params={"get_proc_address": self.proc_address},
            **_display_parameters(),
        )
        self.context.update_cb = lambda: GLib.idle_add(
            self.queue_render, priority=GLib.PRIORITY_HIGH_IDLE
        )

    def _unrealize(self, _area: Gtk.GLArea) -> None:
        self.make_current()
        if self.context:
            self.context.free()
            self.context = None

    def _render(self, _area: Gtk.GLArea, _context: object) -> bool:
        if not self.context:
            return False
        _gl_get_integer(_gl_framebuffer_binding, self.framebuffer)
        self.context.render(
            flip_y=True,
            opengl_fbo={
                "w": self.get_width() * self.get_scale_factor(),
                "h": self.get_height() * self.get_scale_factor(),
                "fbo": self.framebuffer.value,
            },
        )
        return True


class MpvPlayer(Gtk.Box):
    def __init__(
        self,
        on_ready: Callable[[], None],
        on_error: Callable[[str], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.on_ready = on_ready
        self.on_error = on_error
        self.seeking = False
        self.should_play = False
        self.player = mpv.MPV(
            vo="libmpv",
            osc=False,
            config=False,
            terminal=False,
            input_default_bindings=False,
            input_vo_keyboard=False,
            keep_open=True,
            hwdec="auto-safe",
            ytdl=False,
            cache_secs=20,
            demuxer_max_bytes="32MiB",
            audio_client_name="TubeFin",
        )
        self.video = MpvGLArea(self.player)
        self.append(self.video)
        self.append(self._build_controls())
        self._observe()

    def _build_controls(self) -> Gtk.Widget:
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        controls.add_css_class("mpv-controls")
        self.play_button = Gtk.Button(icon_name="media-playback-pause-symbolic")
        self.play_button.set_tooltip_text("Play or pause")
        self.play_button.connect("clicked", lambda *_: self.player.command_async("cycle", "pause"))
        controls.append(self.play_button)
        self.elapsed = Gtk.Label(label="0:00")
        controls.append(self.elapsed)
        self.progress = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 1)
        self.progress.set_draw_value(False)
        self.progress.set_hexpand(True)
        self.progress.connect("change-value", self._seek)
        controls.append(self.progress)
        self.duration = Gtk.Label(label="0:00")
        controls.append(self.duration)
        return controls

    def _observe(self) -> None:
        @self.player.event_callback("file-loaded")
        def file_loaded(_event: object) -> None:
            GLib.idle_add(self._file_loaded)

        @self.player.event_callback("end-file")
        def end_file(event: object) -> None:
            info = event.as_dict()  # type: ignore[attr-defined]
            if info.get("reason") == b"error":
                error = info.get("file_error", b"unknown playback error")
                message = error.decode() if isinstance(error, bytes) else str(error)
                GLib.idle_add(self.on_error, message)

        @self.player.property_observer("pause")
        def pause_changed(_name: str, paused: bool) -> None:
            GLib.idle_add(self._sync_pause, paused)

        @self.player.property_observer("time-pos")
        def position_changed(_name: str, position: float | None) -> None:
            GLib.idle_add(self._sync_position, float(position or 0))

        @self.player.property_observer("duration")
        def duration_changed(_name: str, duration: float | None) -> None:
            GLib.idle_add(self._sync_duration, float(duration or 0))

    def load(self, url: str, headers: dict[str, str] | None = None) -> None:
        self.should_play = True
        self.player.http_header_fields = [
            f"{name}: {value}" for name, value in (headers or {}).items()
        ]
        self.player.loadfile(url, "replace")
        self.player.pause = False

    def stop(self) -> None:
        self.should_play = False
        self.player.stop()

    def shutdown(self) -> None:
        self.player.terminate()

    def _seek(self, _scale: Gtk.Scale, _scroll: Gtk.ScrollType, value: float) -> bool:
        self.player.command_async("seek", value, "absolute")
        return True

    def _ready(self) -> bool:
        self.on_ready()
        return GLib.SOURCE_REMOVE

    def _file_loaded(self) -> bool:
        if self.should_play:
            self.player.pause = False
        return self._ready()

    def _sync_pause(self, paused: bool) -> bool:
        self.play_button.set_icon_name(
            "media-playback-start-symbolic" if paused else "media-playback-pause-symbolic"
        )
        return GLib.SOURCE_REMOVE

    def _sync_position(self, position: float) -> bool:
        self.progress.set_value(position)
        self.elapsed.set_label(self._time_label(position))
        return GLib.SOURCE_REMOVE

    def _sync_duration(self, duration: float) -> bool:
        self.progress.set_range(0, max(duration, 1))
        self.duration.set_label(self._time_label(duration))
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _time_label(seconds: float) -> str:
        total = max(0, int(seconds))
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"
