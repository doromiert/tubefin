from __future__ import annotations

import base64
import http.cookiejar
import json
import re
import secrets
import socket
import socketserver
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from typing import Any

import websocket

PROTOCOL_VERSION = 1

SYNCTUBE_EVENTS = {
    "room": 0,
    "room.permission": 4,
    "user.add": 10,
    "user.remove": 11,
    "user.name": 12,
    "user.color": 14,
    "user.group": 15,
    "playlist.add": 30,
    "playlist.play": 35,
    "player.play": 40,
    "player.pause": 41,
    "player.seek": 42,
    "player.next": 43,
    "player.time": 44,
    "player.load": 45,
    "player.clear": 46,
    "player.rate": 47,
    "player.previous": 48,
}
SYNCTUBE_EVENT_NAMES = {value: key for key, value in SYNCTUBE_EVENTS.items()}


@dataclass(slots=True)
class RoomState:
    media_source: str = ""
    media_id: str = ""
    position: float = 0.0
    paused: bool = True
    updated_at: float = 0.0
    revision: int = 0

    def projected_position(self, now: float | None = None) -> float:
        now = now or time.time()
        return self.position if self.paused else self.position + max(0, now - self.updated_at)


@dataclass(slots=True)
class Room:
    code: str
    host_id: str
    state: RoomState = field(default_factory=RoomState)
    controllers: set[str] = field(default_factory=set)
    clients: dict[str, socket.socket] = field(default_factory=dict)


class RoomRegistry:
    def __init__(self) -> None:
        self.rooms: dict[str, Room] = {}
        self.lock = threading.RLock()

    def create(self, client_id: str) -> Room:
        with self.lock:
            code = ""
            while not code or code in self.rooms:
                code = secrets.token_hex(3).upper()
            room = Room(code, client_id)
            room.controllers.add(client_id)
            self.rooms[code] = room
            return room

    def join(self, code: str, client_id: str, connection: socket.socket) -> Room | None:
        with self.lock:
            room = self.rooms.get(code.upper())
            if room:
                room.clients[client_id] = connection
            return room

    def leave(self, client_id: str) -> None:
        with self.lock:
            for code, room in list(self.rooms.items()):
                room.clients.pop(client_id, None)
                room.controllers.discard(client_id)
                if room.host_id == client_id:
                    self.broadcast(room, {"type": "room_closed"})
                    self.rooms.pop(code, None)

    def broadcast(self, room: Room, message: dict[str, Any]) -> None:
        payload = (json.dumps(message, separators=(",", ":")) + "\n").encode()
        dead: list[str] = []
        with self.lock:
            clients = list(room.clients.items())
        for client_id, connection in clients:
            try:
                connection.sendall(payload)
            except OSError:
                dead.append(client_id)
        with self.lock:
            for client_id in dead:
                room.clients.pop(client_id, None)


class RoomRequestHandler(socketserver.StreamRequestHandler):
    registry: RoomRegistry

    def setup(self) -> None:
        super().setup()
        self.client_id = secrets.token_urlsafe(12)
        self.room: Room | None = None

    def handle(self) -> None:
        self._send({"type": "hello", "version": PROTOCOL_VERSION, "client_id": self.client_id})
        while line := self.rfile.readline(1 << 20):
            try:
                message = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                self._send({"type": "error", "message": "Invalid JSON message."})
                continue
            self._handle_message(message)

    def finish(self) -> None:
        self.registry.leave(self.client_id)
        super().finish()

    def _handle_message(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "create":
            self.room = self.registry.create(self.client_id)
            self.room.clients[self.client_id] = self.request
            self._send({"type": "created", "room": self.room.code, "role": "host"})
            return
        if kind == "join":
            room = self.registry.join(str(message.get("room", "")), self.client_id, self.request)
            if not room:
                self._send({"type": "error", "message": "Room not found."})
                return
            self.room = room
            self._send(
                {
                    "type": "joined",
                    "room": room.code,
                    "role": "controller" if self.client_id in room.controllers else "viewer",
                    "state": asdict(room.state),
                }
            )
            self.registry.broadcast(
                room,
                {"type": "participant_joined", "client_id": self.client_id},
            )
            return
        if not self.room:
            self._send({"type": "error", "message": "Create or join a room first."})
            return
        if kind == "ping":
            self._send(
                {"type": "pong", "sent_at": message.get("sent_at"), "server_time": time.time()}
            )
        elif kind == "state":
            if self.client_id not in self.room.controllers:
                self._send({"type": "error", "message": "The host has not granted control."})
                return
            self.room.state = RoomState(
                media_source=str(message.get("media_source") or ""),
                media_id=str(message.get("media_id") or ""),
                position=max(0.0, float(message.get("position") or 0)),
                paused=bool(message.get("paused", True)),
                updated_at=time.time(),
                revision=self.room.state.revision + 1,
            )
            self.registry.broadcast(
                self.room,
                {"type": "state", "sender": self.client_id, "state": asdict(self.room.state)},
            )
        elif kind == "permission" and self.client_id == self.room.host_id:
            target = str(message.get("client_id") or "")
            if message.get("can_control"):
                self.room.controllers.add(target)
            else:
                self.room.controllers.discard(target)
            self.registry.broadcast(
                self.room,
                {
                    "type": "permission",
                    "client_id": target,
                    "can_control": target in self.room.controllers,
                },
            )

    def _send(self, message: dict[str, Any]) -> None:
        self.wfile.write((json.dumps(message, separators=(",", ":")) + "\n").encode())
        self.wfile.flush()


class ThreadingRoomServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int]) -> None:
        self.registry = RoomRegistry()

        class Handler(RoomRequestHandler):
            pass

        Handler.registry = self.registry

        super().__init__(address, Handler)


class SyncTubeClient:
    """Adapter for SyncTube's public room API and WebSocket event protocol."""

    BASE_URL = "https://sync-tube.de"

    def __init__(
        self,
        on_message: Callable[[dict[str, Any]], None],
        on_sync: Callable[[RoomState, str, float], None],
        *,
        display_name: str = "TubeFin guest",
        color: str = "#8f5bd7",
    ) -> None:
        self.on_message = on_message
        self.on_sync = on_sync
        self.display_name = display_name
        self.color = color
        self.client_id = ""
        self.room = ""
        self.role = "viewer"
        self.group = 0
        self.permissions: dict[str, list[int]] = {
            "add": [0, 1, 2],
            "play": [1, 2],
            "seek": [1, 2],
        }
        self.offset = 0.0
        self.closed = threading.Event()
        self.connected = False
        self.socket: websocket.WebSocket | None = None
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookies)
        )
        self._send_lock = threading.Lock()
        self._created_room = False
        self._media_source = ""
        self._media_id = ""
        self._position = 0.0
        self._paused = True
        self._updated_at = time.time()
        self._published_position = 0.0
        self._published_at = time.monotonic()
        self._published_paused = True
        self._pending_media_url = ""
        self.members: dict[str, dict[str, Any]] = {}

    def create(self) -> str:
        request = urllib.request.Request(
            f"{self.BASE_URL}/api/create",
            headers={"Accept": "application/json", "User-Agent": "TubeFin/0.1"},
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=20) as response:
                room = str(json.load(response).get("id") or "")
        except (OSError, ValueError, urllib.error.URLError) as error:
            raise ConnectionError("Could not create a SyncTube room.") from error
        if not room:
            raise ConnectionError("SyncTube did not return a room ID.")
        self._created_room = True
        self.join(room)
        return room

    def join(self, room: str) -> str:
        room = self._room_id(room)
        if not room:
            raise ValueError("Enter a SyncTube room ID or URL.")
        payload = json.dumps(
            {
                "id": room,
                "preferences": {
                    "user": {"name": self.display_name, "color": self.color}
                },
            }
        ).encode()
        request = urllib.request.Request(
            f"{self.BASE_URL}/api/join",
            data=payload,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "TubeFin/0.1",
            },
        )
        try:
            with self.opener.open(request, timeout=20) as response:
                response.read()
        except urllib.error.HTTPError as error:
            messages = {403: "Room access was denied.", 404: "SyncTube room not found."}
            raise ConnectionError(messages.get(error.code, "Could not join SyncTube.")) from error
        except (OSError, urllib.error.URLError) as error:
            raise ConnectionError("Could not reach SyncTube.") from error
        self.close()
        self.closed.clear()
        self.room = room
        preference = base64.b64encode(
            json.dumps(
                {
                    "user": {"name": self.display_name, "color": self.color}
                },
                separators=(",", ":"),
            ).encode("utf-16le")
        ).decode().replace("/", "-")
        cookie = "; ".join(f"{value.name}={value.value}" for value in self.cookies)
        try:
            self.socket = websocket.create_connection(
                f"wss://sync-tube.de/ws/{room}/{preference}",
                cookie=cookie or None,
                timeout=20,
            )
            self.socket.settimeout(None)
            self.connected = True
        except (OSError, websocket.WebSocketException) as error:
            self.socket = None
            raise ConnectionError("Could not open the SyncTube room connection.") from error
        threading.Thread(
            target=self._read,
            daemon=True,
            name="synctube-reader",
        ).start()
        return room

    def publish(self, source: str, media_id: str, position: float, paused: bool) -> None:
        if source != "youtube":
            return
        if (source, media_id) != (self._media_source, self._media_id):
            if not self.has_permission("add"):
                return
            media_url = f"https://www.youtube.com/watch?v={media_id}"
            if media_url != self._pending_media_url:
                self._pending_media_url = media_url
                self._send_request("playlist.add", {"src": media_url})
            return
        projected = (
            self._published_position
            if self._published_paused
            else self._published_position + time.monotonic() - self._published_at
        )
        pause_changed = paused != self._published_paused
        if abs(position - projected) > 2 and self.has_permission("seek"):
            self._send("player.seek", {"time": max(0.0, position)})
            self._published_position = max(0.0, position)
            self._published_at = time.monotonic()
        if pause_changed and self.has_permission("play"):
            self._send("player.pause", {"time": position}) if paused else self._send(
                "player.play"
            )
            self._remember_published(position, paused)

    def has_permission(self, permission: str) -> bool:
        return self.group in self.permissions.get(permission, [])

    def grant(self, client_id: str, can_control: bool) -> None:
        if client_id:
            self._send("user.group", {"id": client_id, "group": 1 if can_control else 0})

    def ping(self) -> None:
        return

    def close(self) -> None:
        self.closed.set()
        self.connected = False
        if self.socket:
            with suppress(OSError, websocket.WebSocketException):
                self.socket.close()
        self.socket = None

    def drift_action(self, state: RoomState, local_position: float) -> tuple[str, float]:
        target = state.projected_position()
        drift = target - local_position
        if abs(drift) > 2.0:
            return "seek", target
        if abs(drift) > 0.15 and not state.paused:
            return "speed", 1.03 if drift > 0 else 0.97
        return "speed", 1.0

    def _read(self) -> None:
        try:
            while not self.closed.is_set() and self.socket:
                raw = self.socket.recv()
                if not raw:
                    break
                message = json.loads(raw)
                if not isinstance(message, list) or not message:
                    continue
                if len(message) == 3:
                    continue
                name = SYNCTUBE_EVENT_NAMES.get(int(message[0]), "")
                payload = message[1] if len(message) > 1 else None
                self._handle_event(name, payload)
        except (OSError, TypeError, ValueError, websocket.WebSocketException):
            pass
        self.connected = False
        if not self.closed.is_set():
            self.on_message({"type": "disconnected", "message": "SyncTube disconnected."})

    def _handle_event(self, name: str, payload: Any) -> None:
        if name == "room" and isinstance(payload, dict):
            user = payload.get("user") or {}
            self.client_id = str(user.get("id") or "")
            group = int(user.get("group") or 0)
            self.group = group
            self.role = "host" if group == 2 else "controller" if group == 1 else "viewer"
            permissions = payload.get("permissions") or {}
            if isinstance(permissions, dict):
                self.permissions = {
                    str(permission): [int(value) for value in groups]
                    for permission, groups in permissions.items()
                    if isinstance(groups, list)
                }
            self.members = {}
            users = payload.get("users") or []
            if isinstance(users, dict):
                users = [
                    {**member, "id": member.get("id") or client_id}
                    for client_id, member in users.items()
                    if isinstance(member, dict)
                ]
            if isinstance(users, list):
                for member in users:
                    normalized = self._normalize_member(member)
                    if normalized:
                        self.members[normalized["id"]] = normalized
            normalized_user = self._normalize_member(user)
            if normalized_user:
                self.members[normalized_user["id"]] = normalized_user
            player = payload.get("player") or {}
            self._load_media(player.get("media"), bool(player.get("playing")), player.get("time"))
            self.on_message(
                {
                    "type": "created" if self._created_room else "joined",
                    "room": self.room,
                    "role": self.role,
                    "url": f"{self.BASE_URL}/room/{self.room}",
                    "members": list(self.members.values()),
                }
            )
            self._created_room = False
            return
        if name == "room.permission" and isinstance(payload, dict):
            permission = str(payload.get("pid") or "")
            group = int(payload.get("group") or 0)
            groups = self.permissions.setdefault(permission, [])
            if payload.get("allow"):
                if group not in groups:
                    groups.append(group)
            elif group in groups:
                groups.remove(group)
            self.on_message(
                {
                    "type": "room_permission",
                    "permission": permission,
                    "allowed": self.has_permission(permission),
                }
            )
            return
        if name == "user.add":
            member = self._normalize_member(payload)
            if member:
                self.members[member["id"]] = member
                self._emit_members()
            return
        if name == "user.remove":
            if isinstance(payload, dict):
                nested_user = payload.get("user") or {}
                client_id = str(
                    payload.get("id")
                    or (nested_user.get("id") if isinstance(nested_user, dict) else "")
                )
            else:
                client_id = str(payload or "")
            self.members.pop(client_id, None)
            self._emit_members()
            return
        if name in {"user.name", "user.color", "user.group"} and isinstance(payload, dict):
            client_id = str(payload.get("id") or "")
            member = self.members.setdefault(
                client_id,
                {"id": client_id, "name": "Anonymous", "color": "#8f5bd7", "group": 0},
            )
            if name == "user.name":
                member["name"] = str(payload.get("name") or payload.get("value") or "Anonymous")
            elif name == "user.color":
                member["color"] = str(payload.get("color") or payload.get("value") or "#8f5bd7")
            else:
                member["group"] = int(payload.get("group") or 0)
            self._emit_members()
            if name == "user.group" and client_id == self.client_id:
                group = int(member["group"])
                self.group = group
                self.role = "host" if group == 2 else "controller" if group == 1 else "viewer"
                self.on_message(
                    {
                        "type": "permission",
                        "client_id": self.client_id,
                        "can_control": group > 0,
                    }
                )
            return
        if name == "playlist.add":
            candidates = payload if isinstance(payload, list) else [payload]
            for candidate in candidates:
                if isinstance(candidate, dict) and isinstance(candidate.get("item"), dict):
                    candidate = candidate["item"]
                if not isinstance(candidate, dict):
                    continue
                if str(candidate.get("src") or "") == self._pending_media_url:
                    self._pending_media_url = ""
                    self._send("playlist.play", candidate.get("id"))
                    break
            return
        if name == "player.load":
            self._load_media(payload, True, 0)
        elif name == "player.clear":
            self._media_source = self._media_id = ""
            return
        elif name == "player.play":
            self._paused = False
            self._updated_at = time.time()
        elif name == "player.pause":
            if isinstance(payload, dict):
                self._position = float(payload.get("time") or self._position)
            self._paused = True
            self._updated_at = time.time()
        elif name in {"player.seek", "player.time"}:
            value = payload.get("time") if isinstance(payload, dict) else payload
            self._position = max(0.0, float(value or 0))
            self._updated_at = time.time()
        else:
            return
        self._remember_published(self._position, self._paused)
        self._emit_state(name)

    def _normalize_member(self, value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        if isinstance(value.get("user"), dict):
            value = value["user"]
        client_id = str(value.get("id") or "")
        if not client_id:
            return None
        current_user = client_id == self.client_id
        return {
            "id": client_id,
            "name": str(
                value.get("name") or (self.display_name if current_user else "Anonymous")
            ),
            "color": str(
                value.get("color") or (self.color if current_user else "#8f5bd7")
            ),
            "group": int(value.get("group") or 0),
        }

    def _emit_members(self) -> None:
        self.on_message({"type": "members", "members": list(self.members.values())})

    def _load_media(self, media: Any, playing: bool, position: Any) -> None:
        if not isinstance(media, dict):
            return
        source, media_id = self._media_identity(str(media.get("src") or ""))
        if not media_id:
            return
        self._media_source = source
        self._media_id = media_id
        self._position = max(0.0, float(position or 0))
        self._paused = not playing
        self._updated_at = time.time()
        self._remember_published(self._position, self._paused)
        self._emit_state("player.load")

    def _emit_state(self, action: str) -> None:
        if not self._media_id:
            return
        state = RoomState(
            self._media_source,
            self._media_id,
            self._position,
            self._paused,
            self._updated_at,
        )
        advice, value = self.drift_action(state, self._position)
        self.on_sync(state, action or advice, value)

    def _send(self, event: str, payload: Any = None) -> None:
        if not self.socket:
            return
        message = [SYNCTUBE_EVENTS[event]]
        if payload is not None:
            message.append(payload)
        with self._send_lock:
            self.socket.send(json.dumps(message, separators=(",", ":")))

    def _send_request(self, event: str, payload: Any) -> None:
        if not self.socket:
            return
        message = [SYNCTUBE_EVENTS[event], payload, int(time.time_ns() % (2**53))]
        with self._send_lock:
            self.socket.send(json.dumps(message, separators=(",", ":")))

    def _remember_published(self, position: float, paused: bool) -> None:
        self._published_position = max(0.0, position)
        self._published_paused = paused
        self._published_at = time.monotonic()

    @staticmethod
    def _room_id(value: str) -> str:
        value = value.strip().rstrip("/")
        if "/room/" in value:
            value = value.rsplit("/room/", 1)[-1]
        return re.sub(r"[^A-Za-z0-9_-]", "", value)

    @staticmethod
    def _media_identity(url: str) -> tuple[str, str]:
        parsed = urllib.parse.urlparse(url)
        if parsed.hostname in {"youtu.be", "www.youtu.be"}:
            return "youtube", parsed.path.strip("/").split("/", 1)[0]
        if parsed.hostname and "youtube.com" in parsed.hostname:
            video_id = urllib.parse.parse_qs(parsed.query).get("v", [""])[0]
            if not video_id and "/shorts/" in parsed.path:
                video_id = parsed.path.split("/shorts/", 1)[-1].split("/", 1)[0]
            return "youtube", video_id
        return "", ""


class SyncClient:
    """Room client with NTP-style clock sampling and smooth drift advice."""

    def __init__(
        self,
        on_message: Callable[[dict[str, Any]], None],
        on_sync: Callable[[RoomState, str, float], None],
    ) -> None:
        self.on_message = on_message
        self.on_sync = on_sync
        self.client_id = ""
        self.room = ""
        self.role = "viewer"
        self.socket: socket.socket | None = None
        self.reader: Any = None
        self.offset = 0.0
        self.closed = threading.Event()
        self.endpoint: tuple[str, int] | None = None
        self.desired_room = ""

    def connect(self, host: str, port: int) -> None:
        self.close()
        self.closed.clear()
        self.endpoint = (host, port)
        self._open_socket(host, port)

    def _open_socket(self, host: str, port: int) -> None:
        self.socket = socket.create_connection((host, port), timeout=15)
        self.socket.settimeout(None)
        self.reader = self.socket.makefile("r", encoding="utf-8")
        threading.Thread(target=self._read, daemon=True, name="sync-room-reader").start()

    def create(self) -> None:
        self._send({"type": "create"})

    def join(self, room: str) -> None:
        self.desired_room = room.upper()
        self._send({"type": "join", "room": self.desired_room})

    def publish(self, source: str, media_id: str, position: float, paused: bool) -> None:
        self._send(
            {
                "type": "state",
                "media_source": source,
                "media_id": media_id,
                "position": position,
                "paused": paused,
            }
        )

    def grant(self, client_id: str, can_control: bool) -> None:
        self._send({"type": "permission", "client_id": client_id, "can_control": can_control})

    def ping(self) -> None:
        self._send({"type": "ping", "sent_at": time.time()})

    def close(self) -> None:
        self.closed.set()
        if self.socket:
            with suppress(OSError):
                self.socket.shutdown(socket.SHUT_RDWR)
            self.socket.close()
        if self.reader:
            with suppress(OSError):
                self.reader.close()
        self.socket = None
        self.reader = None

    def drift_action(self, state: RoomState, local_position: float) -> tuple[str, float]:
        target = state.projected_position(time.time() + self.offset)
        drift = target - local_position
        if abs(drift) > 2.0:
            return "seek", target
        if abs(drift) > 0.15 and not state.paused:
            return "speed", 1.03 if drift > 0 else 0.97
        return "speed", 1.0

    def _read(self) -> None:
        try:
            while not self.closed.is_set() and (line := self.reader.readline()):
                message = json.loads(line)
                kind = message.get("type")
                if kind == "hello":
                    self.client_id = str(message.get("client_id") or "")
                elif kind in {"created", "joined"}:
                    self.room = str(message.get("room") or "")
                    self.role = str(message.get("role") or "viewer")
                    if kind == "created":
                        self.desired_room = self.room
                    if state_data := message.get("state"):
                        state = RoomState(**state_data)
                        action, value = self.drift_action(state, state.position)
                        self.on_sync(state, action, value)
                elif kind == "pong":
                    sent = float(message.get("sent_at") or time.time())
                    midpoint = sent + (time.time() - sent) / 2
                    sample = float(message.get("server_time") or midpoint) - midpoint
                    self.offset = self.offset * 0.8 + sample * 0.2
                elif kind == "state":
                    state = RoomState(**message["state"])
                    action, value = self.drift_action(state, state.position)
                    self.on_sync(state, action, value)
                self.on_message(message)
        except (OSError, ValueError, TypeError):
            if not self.closed.is_set():
                self.on_message({"type": "disconnected"})
                threading.Thread(target=self._reconnect, daemon=True).start()

    def _reconnect(self) -> None:
        if not self.endpoint or not self.desired_room:
            return
        for delay in (1, 2, 5, 10):
            if self.closed.wait(delay):
                return
            try:
                self._open_socket(*self.endpoint)
                self.join(self.desired_room)
                self.on_message({"type": "reconnected"})
                return
            except OSError:
                continue

    def _send(self, message: dict[str, Any]) -> None:
        if not self.socket:
            raise ConnectionError("Not connected to a room server.")
        self.socket.sendall((json.dumps(message, separators=(",", ":")) + "\n").encode())
