from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

from tubefin.sync import RoomState, SyncClient, SyncTubeClient, ThreadingRoomServer


class SyncTests(unittest.TestCase):
    def test_synctube_urls_and_media_ids_are_normalized(self) -> None:
        self.assertEqual(
            SyncTubeClient._room_id("https://sync-tube.de/room/Room_123/"),
            "Room_123",
        )
        self.assertEqual(
            SyncTubeClient._media_identity("https://www.youtube.com/watch?v=video-id"),
            ("youtube", "video-id"),
        )

    def test_synctube_room_event_is_adapted_to_tubefin_state(self) -> None:
        messages: list[dict[str, object]] = []
        states: list[RoomState] = []
        client = SyncTubeClient(messages.append, lambda state, *_args: states.append(state))
        client.room = "room-id"
        client._created_room = True

        client._handle_event(
            "room",
            {
                "user": {
                    "id": "me",
                    "name": "Ada",
                    "color": "#112233",
                    "group": 2,
                },
                "users": [
                    {"id": "friend", "name": "Lin", "color": "#abcdef", "group": 0}
                ],
                "player": {
                    "media": {"src": "https://youtu.be/video-id"},
                    "playing": True,
                    "time": 42,
                },
            },
        )

        self.assertEqual(messages[-1]["type"], "created")
        self.assertEqual(messages[-1]["role"], "host")
        self.assertEqual(
            {member["name"] for member in messages[-1]["members"]},
            {"Ada", "Lin"},
        )
        self.assertEqual((states[-1].media_source, states[-1].media_id), ("youtube", "video-id"))
        self.assertEqual(states[-1].position, 42)

        client._handle_event(
            "user.group", {"id": "friend", "group": 1}
        )
        self.assertEqual(messages[-1]["type"], "members")
        friend = next(member for member in messages[-1]["members"] if member["id"] == "friend")
        self.assertEqual(friend["group"], 1)

    def test_drift_uses_smooth_speed_then_exact_seek(self) -> None:
        client = SyncClient(lambda _message: None, lambda *_args: None)
        now = time.time()

        action, speed = client.drift_action(
            RoomState(position=10.4, paused=False, updated_at=now), 10
        )
        self.assertEqual((action, speed), ("speed", 1.03))

        action, position = client.drift_action(
            RoomState(position=20, paused=True, updated_at=now), 10
        )
        self.assertEqual(action, "seek")
        self.assertAlmostEqual(position, 20, places=1)

    def test_synctube_viewer_can_pause_when_room_permissions_allow_it(self) -> None:
        client = SyncTubeClient(lambda _message: None, lambda *_args: None)
        client.room = "room-id"
        client._handle_event(
            "room",
            {
                "user": {"id": "me", "group": 0},
                "permissions": {"play": [0, 1, 2], "seek": [1, 2]},
                "player": {
                    "media": {"src": "https://youtu.be/video-id"},
                    "playing": True,
                    "time": 42,
                },
            },
        )

        with patch.object(client, "_send") as send:
            client.publish("youtube", "video-id", 42, True)

        send.assert_called_once_with("player.pause", {"time": 42})

    def test_synctube_remote_time_updates_do_not_echo_back(self) -> None:
        client = SyncTubeClient(lambda _message: None, lambda *_args: None)
        client.room = "room-id"
        client._handle_event(
            "room",
            {
                "user": {"id": "me", "group": 1},
                "permissions": {"play": [1, 2], "seek": [1, 2]},
                "player": {
                    "media": {"src": "https://youtu.be/video-id"},
                    "playing": True,
                    "time": 42,
                },
            },
        )
        client._handle_event("player.time", 50)

        with patch.object(client, "_send") as send:
            client.publish("youtube", "video-id", 50, False)

        send.assert_not_called()

    def test_host_state_is_delivered_to_participant(self) -> None:
        try:
            server = ThreadingRoomServer(("127.0.0.1", 0))
        except PermissionError:
            self.skipTest("Local sockets are disabled by this test sandbox")
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        host_created = threading.Event()
        participant_joined = threading.Event()
        state_received = threading.Event()
        room: list[str] = []
        states: list[RoomState] = []

        def receive_state(state: RoomState, _action: str, _value: float) -> None:
            states.append(state)
            if state.media_id == "video":
                state_received.set()

        host = SyncClient(
            lambda message: (
                (room.append(str(message["room"])), host_created.set())
                if message.get("type") == "created"
                else None
            ),
            lambda *_args: None,
        )
        participant = SyncClient(
            lambda message: participant_joined.set() if message.get("type") == "joined" else None,
            receive_state,
        )
        try:
            port = server.server_address[1]
            host.connect("127.0.0.1", port)
            host.create()
            self.assertTrue(host_created.wait(2))
            participant.connect("127.0.0.1", port)
            participant.join(room[0])
            self.assertTrue(participant_joined.wait(2))

            host.publish("youtube", "video", 42, False)

            self.assertTrue(state_received.wait(2))
            self.assertEqual((states[-1].media_source, states[-1].media_id), ("youtube", "video"))
        finally:
            participant.close()
            host.close()
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
