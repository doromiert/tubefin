from __future__ import annotations

import json
import threading
import urllib.parse
from collections.abc import Callable
from contextlib import suppress
from typing import Any

import websocket

from tubefin.services import JellyfinService, ServiceError


class JellyfinSyncPlayClient:
    """Jellyfin SyncPlay room transport using the current Jellyfin session."""

    DEVICE_ID = "tubefin-desktop"

    def __init__(
        self,
        service: JellyfinService,
        on_message: Callable[[dict[str, Any]], None],
    ) -> None:
        self.service = service
        self.on_message = on_message
        self.socket: websocket.WebSocket | None = None
        self.connected = False
        self.group: dict[str, Any] | None = None
        self.closed = threading.Event()
        self._send_lock = threading.Lock()

    @property
    def group_id(self) -> str:
        return str((self.group or {}).get("GroupId") or "")

    def list_groups(self) -> list[dict[str, Any]]:
        return self.service.syncplay_groups()

    def create(self, name: str) -> dict[str, Any]:
        self._open_socket()
        group = self.service.syncplay_create(name)
        if not group:
            groups = self.service.syncplay_groups()
            group = next(
                (value for value in groups if value.get("GroupName") == name.strip()),
                {},
            )
        if not group:
            raise ServiceError("Jellyfin created the room but did not return its details.")
        self.group = group
        self.on_message({"type": "joined", "group": group})
        return group

    def join(self, group_id: str) -> dict[str, Any]:
        if not group_id:
            raise ValueError("Choose a Jellyfin watch party first.")
        self._open_socket()
        self.service.syncplay_join(group_id)
        group = self.service.syncplay_group(group_id)
        self.group = group or {"GroupId": group_id, "GroupName": "Jellyfin room"}
        self.on_message({"type": "joined", "group": self.group})
        return self.group

    def leave(self) -> None:
        if self.group_id:
            with suppress(ServiceError):
                self.service.syncplay_leave()
        self.close()

    def close(self) -> None:
        self.closed.set()
        self.connected = False
        self.group = None
        if self.socket:
            with suppress(OSError, websocket.WebSocketException):
                self.socket.close()
        self.socket = None

    def set_queue(self, items: list[str], index: int, position: float) -> None:
        self.service.syncplay_set_queue(items, index, position)

    def pause(self) -> None:
        self.service.syncplay_pause()

    def unpause(self) -> None:
        self.service.syncplay_unpause()

    def seek(self, position: float) -> None:
        self.service.syncplay_seek(position)

    def ready(
        self,
        playlist_item_id: str,
        position: float,
        paused: bool,
    ) -> None:
        self.service.syncplay_ready(playlist_item_id, position, paused)

    def _open_socket(self) -> None:
        if self.connected and self.socket:
            return
        session = self.service.session
        if not session:
            raise ServiceError("Connect to Jellyfin before opening a watch party.")
        parsed = urllib.parse.urlsplit(session.server_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        base_path = parsed.path.rstrip("/")
        query = urllib.parse.urlencode(
            {"api_key": session.access_token, "deviceId": self.DEVICE_ID}
        )
        url = urllib.parse.urlunsplit(
            (scheme, parsed.netloc, f"{base_path}/socket", query, "")
        )
        try:
            self.socket = websocket.create_connection(
                url,
                header=[f"Authorization: {self.service.CLIENT_HEADER}"],
                timeout=20,
            )
            self.socket.settimeout(None)
        except (OSError, websocket.WebSocketException) as error:
            self.socket = None
            raise ConnectionError("Could not open Jellyfin's SyncPlay connection.") from error
        self.closed.clear()
        self.connected = True
        threading.Thread(
            target=self._read,
            daemon=True,
            name="jellyfin-syncplay-reader",
        ).start()

    def _read(self) -> None:
        try:
            while not self.closed.is_set() and self.socket:
                raw = self.socket.recv()
                if not raw:
                    break
                message = json.loads(raw)
                if not isinstance(message, dict):
                    continue
                kind = str(message.get("MessageType") or "")
                data = message.get("Data")
                if kind == "ForceKeepAlive":
                    self._send({"MessageType": "KeepAlive"})
                elif kind == "SyncPlayGroupUpdate" and isinstance(data, dict):
                    self._group_update(data)
                elif kind == "SyncPlayCommand" and isinstance(data, dict):
                    self.on_message({"type": "command", "command": data})
        except (OSError, TypeError, ValueError, websocket.WebSocketException):
            pass
        self.connected = False
        if not self.closed.is_set():
            self.on_message(
                {"type": "disconnected", "message": "Jellyfin watch party disconnected."}
            )

    def _group_update(self, update: dict[str, Any]) -> None:
        kind = str(update.get("Type") or "")
        data = update.get("Data")
        if kind in {"GroupJoined", "GroupUpdate"} and isinstance(data, dict):
            self.group = data
            self.on_message({"type": "group", "group": data})
        elif kind == "PlayQueue" and isinstance(data, dict):
            self.on_message({"type": "queue", "queue": data})
        elif kind in {"GroupLeft", "NotInGroup"}:
            self.group = None
            self.on_message({"type": "left"})
        elif kind == "UserJoined":
            self.on_message(
                {"type": "participant-joined", "participant": data}
            )
            self.on_message({"type": "members-changed"})
        elif kind == "UserLeft":
            self.on_message({"type": "members-changed"})
        elif kind.endswith("Denied") or kind in {
            "GroupDoesNotExist",
            "SyncPlayIsDisabled",
        }:
            self.on_message({"type": "error", "message": kind})

    def _send(self, message: dict[str, Any]) -> None:
        if not self.socket:
            return
        with self._send_lock, suppress(OSError, websocket.WebSocketException):
            self.socket.send(json.dumps(message, separators=(",", ":")))
