from __future__ import annotations

import re
from typing import Any

from gi.repository import Gio, GLib

OBJECT_PATH = "/org/mpris/MediaPlayer2"
BUS_NAME = "org.mpris.MediaPlayer2.tubefin"

INTROSPECTION_XML = """
<node>
  <interface name="org.mpris.MediaPlayer2">
    <method name="Raise"/>
    <method name="Quit"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="DesktopEntry" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
    <property name="Fullscreen" type="b" access="readwrite"/>
    <property name="CanSetFullscreen" type="b" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Next"/>
    <method name="Previous"/>
    <method name="Pause"/>
    <method name="PlayPause"/>
    <method name="Stop"/>
    <method name="Play"/>
    <method name="Seek"><arg direction="in" type="x" name="Offset"/></method>
    <method name="SetPosition">
      <arg direction="in" type="o" name="TrackId"/>
      <arg direction="in" type="x" name="Position"/>
    </method>
    <method name="OpenUri"><arg direction="in" type="s" name="Uri"/></method>
    <signal name="Seeked"><arg type="x" name="Position"/></signal>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="LoopStatus" type="s" access="readwrite"/>
    <property name="Rate" type="d" access="readwrite"/>
    <property name="Shuffle" type="b" access="readwrite"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Volume" type="d" access="readwrite"/>
    <property name="Position" type="x" access="read"/>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
  </interface>
</node>
"""


class MprisService:
    def __init__(self, window: Any) -> None:
        self.window = window
        self.connection: Gio.DBusConnection | None = None
        self.registrations: list[int] = []
        self.node = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION_XML)
        self.owner_id = Gio.bus_own_name(
            Gio.BusType.SESSION,
            BUS_NAME,
            Gio.BusNameOwnerFlags.NONE,
            self._bus_acquired,
            None,
            None,
        )
        self.last_fingerprint: tuple[object, ...] | None = None
        self.last_root_fingerprint: tuple[object, ...] | None = None
        self.update_source = GLib.timeout_add_seconds(1, self.update)

    def _bus_acquired(self, connection: Gio.DBusConnection, _name: str) -> None:
        self.connection = connection
        for interface in self.node.interfaces:
            registration = connection.register_object(
                OBJECT_PATH,
                interface,
                self._method_called,
                self._get_property,
                self._set_property,
            )
            self.registrations.append(registration)
        self.update(force=True)

    def close(self) -> None:
        if self.update_source:
            GLib.source_remove(self.update_source)
            self.update_source = 0
        if self.connection:
            for registration in self.registrations:
                self.connection.unregister_object(registration)
        self.registrations.clear()
        self.connection = None
        if self.owner_id:
            Gio.bus_unown_name(self.owner_id)
            self.owner_id = 0

    def _method_called(
        self,
        _connection: Gio.DBusConnection,
        _sender: str,
        _path: str,
        interface: str,
        method: str,
        parameters: GLib.Variant,
        invocation: Gio.DBusMethodInvocation,
    ) -> None:
        player = self.window.mpv_player
        if interface == "org.mpris.MediaPlayer2":
            if method == "Raise":
                GLib.idle_add(self.window.present)
            elif method == "Quit":
                GLib.idle_add(self.window.close)
        else:
            actions = {
                "Next": self.window._skip_queued,
                "Previous": self.window._play_previous_queued,
                "Pause": lambda: player and player.set_paused(True),
                "PlayPause": lambda: player and player.toggle_pause(),
                "Stop": self.window._stop_playback,
                "Play": lambda: player and player.set_paused(False),
            }
            if action := actions.get(method):
                GLib.idle_add(action)
            elif method == "Seek" and player:
                offset = parameters.unpack()[0] / 1_000_000
                GLib.idle_add(player.seek_relative, offset)
            elif method == "SetPosition" and player:
                _track_id, position = parameters.unpack()
                GLib.idle_add(player.seek_absolute, position / 1_000_000)
            elif method == "OpenUri":
                uri = str(parameters.unpack()[0])
                GLib.idle_add(self._open_uri, uri)
        invocation.return_value(None)

    def _open_uri(self, uri: str) -> bool:
        self.window.global_search.set_text(uri)
        self.window._global_search_requested(self.window.global_search)
        return GLib.SOURCE_REMOVE

    def _get_property(
        self,
        _connection: Gio.DBusConnection,
        _sender: str,
        _path: str,
        interface: str,
        name: str,
    ) -> GLib.Variant:
        properties = (
            self._root_properties()
            if interface == "org.mpris.MediaPlayer2"
            else self._player_properties()
        )
        return properties[name]

    def _set_property(
        self,
        _connection: Gio.DBusConnection,
        _sender: str,
        _path: str,
        interface: str,
        name: str,
        value: GLib.Variant,
    ) -> bool:
        player = self.window.mpv_player
        unpacked = value.unpack()
        if interface == "org.mpris.MediaPlayer2" and name == "Fullscreen":
            if bool(unpacked) != self.window._player_is_fullscreen():
                GLib.idle_add(self.window._toggle_fullscreen)
            return True
        if not player:
            return False
        if name == "Rate":
            GLib.idle_add(player.set_playback_speed, float(unpacked))
            return True
        if name == "Volume":
            GLib.idle_add(player.set_volume, float(unpacked) * 100)
            return True
        if name == "LoopStatus":
            self.window.queue_loop = str(unpacked) == "Playlist"
            GLib.idle_add(self.window._update_transport_buttons)
            return True
        if name == "Shuffle":
            return not bool(unpacked)
        return False

    def _root_properties(self) -> dict[str, GLib.Variant]:
        return {
            "CanQuit": GLib.Variant("b", True),
            "CanRaise": GLib.Variant("b", True),
            "HasTrackList": GLib.Variant("b", False),
            "Identity": GLib.Variant("s", "TubeFin"),
            "DesktopEntry": GLib.Variant("s", "io.github.doromiert.TubeFin"),
            "SupportedUriSchemes": GLib.Variant("as", ["http", "https"]),
            "SupportedMimeTypes": GLib.Variant("as", ["video/mp4", "video/webm"]),
            "Fullscreen": GLib.Variant("b", self.window._player_is_fullscreen()),
            "CanSetFullscreen": GLib.Variant("b", True),
        }

    def _player_properties(self) -> dict[str, GLib.Variant]:
        player = self.window.mpv_player
        has_media = bool(player and self.window.current_item)
        multiple = len(self.window.queue) > 1
        has_previous = bool(
            self.window.queue
            and (self.window.queue_index > 0 or self.window.queue_loop and multiple)
        )
        has_next = bool(
            self.window.queue
            and (
                self.window.queue_index + 1 < len(self.window.queue)
                or self.window.queue_loop and multiple
            )
        )
        status = "Stopped"
        if has_media and player:
            status = "Paused" if player.paused else "Playing"
        return {
            "PlaybackStatus": GLib.Variant("s", status),
            "LoopStatus": GLib.Variant(
                "s", "Playlist" if self.window.queue_loop else "None"
            ),
            "Rate": GLib.Variant("d", player.playback_speed if player else 1.0),
            "Shuffle": GLib.Variant("b", False),
            "Metadata": GLib.Variant("a{sv}", self._metadata()),
            "Volume": GLib.Variant(
                "d", player.volume.get_value() / 100 if player else 1.0
            ),
            "Position": GLib.Variant(
                "x", round(player.position * 1_000_000) if player else 0
            ),
            "MinimumRate": GLib.Variant("d", 0.5),
            "MaximumRate": GLib.Variant("d", 2.0),
            "CanGoNext": GLib.Variant("b", has_next),
            "CanGoPrevious": GLib.Variant("b", has_previous),
            "CanPlay": GLib.Variant("b", has_media),
            "CanPause": GLib.Variant("b", has_media),
            "CanSeek": GLib.Variant("b", has_media),
            "CanControl": GLib.Variant("b", True),
        }

    def _metadata(self) -> dict[str, GLib.Variant]:
        item = self.window.current_item
        player = self.window.mpv_player
        if not item:
            return {"mpris:trackid": GLib.Variant("o", "/org/mpris/MediaPlayer2/Track/None")}
        safe_id = re.sub(r"[^A-Za-z0-9_]", "_", f"{item.source}_{item.id}")
        metadata = {
            "mpris:trackid": GLib.Variant(
                "o", f"/org/mpris/MediaPlayer2/Track/{safe_id}"
            ),
            "xesam:title": GLib.Variant("s", item.title),
            "xesam:artist": GLib.Variant("as", [item.subtitle] if item.subtitle else []),
            "mpris:length": GLib.Variant(
                "x", round(player.media_duration * 1_000_000) if player else 0
            ),
        }
        if item.thumbnail_url:
            metadata["mpris:artUrl"] = GLib.Variant("s", item.thumbnail_url)
        return metadata

    def update(self, force: bool = False) -> bool:
        root_properties = self._root_properties()
        player_properties = self._player_properties()
        root_fingerprint = tuple(
            (name, value.print_(False)) for name, value in root_properties.items()
        )
        fingerprint = tuple(
            (name, value.print_(False))
            for name, value in player_properties.items()
            if name != "Position"
        )
        if self.connection and (force or root_fingerprint != self.last_root_fingerprint):
            self.connection.emit_signal(
                None,
                OBJECT_PATH,
                "org.freedesktop.DBus.Properties",
                "PropertiesChanged",
                GLib.Variant(
                    "(sa{sv}as)",
                    ("org.mpris.MediaPlayer2", root_properties, []),
                ),
            )
            self.last_root_fingerprint = root_fingerprint
        if self.connection and (force or fingerprint != self.last_fingerprint):
            self.connection.emit_signal(
                None,
                OBJECT_PATH,
                "org.freedesktop.DBus.Properties",
                "PropertiesChanged",
                GLib.Variant(
                    "(sa{sv}as)",
                    ("org.mpris.MediaPlayer2.Player", player_properties, []),
                ),
            )
            self.last_fingerprint = fingerprint
        return GLib.SOURCE_CONTINUE
