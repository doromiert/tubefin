from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tubefin.config import ConfigStore
from tubefin.models import JellyfinSession
from tubefin.services import JellyfinService, YouTubeService


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


class YouTubeServiceTests(unittest.TestCase):
    def test_flat_video_is_mapped_to_playable_url(self) -> None:
        item = YouTubeService._item(
            {
                "id": "abc123",
                "url": "abc123",
                "title": "A video",
                "channel": "A creator",
                "duration": 125,
                "thumbnails": [{"url": "https://images.example/thumb.jpg"}],
            }
        )

        self.assertEqual(item.title, "A video")
        self.assertEqual(item.subtitle, "A creator")
        self.assertEqual(item.duration_label, "2:05")
        self.assertEqual(item.payload["webpage_url"], "https://www.youtube.com/watch?v=abc123")


class JellyfinServiceTests(unittest.TestCase):
    @patch("urllib.request.urlopen")
    def test_authentication_normalizes_server_and_builds_session(self, urlopen: object) -> None:
        urlopen.return_value = FakeResponse(  # type: ignore[attr-defined]
            {
                "User": {"Id": "user-id", "Name": "Ada"},
                "AccessToken": "secret-token",
            }
        )
        service = JellyfinService()

        session = service.authenticate("jellyfin.local:8096/", "Ada", "password")

        self.assertEqual(session.server_url, "http://jellyfin.local:8096")
        self.assertEqual(session.user_id, "user-id")
        self.assertEqual(session.access_token, "secret-token")

    def test_stream_url_is_static_and_authenticated(self) -> None:
        service = JellyfinService(
            JellyfinSession("https://media.example", "user", "Ada", "token value")
        )
        item = service._item({"Id": "movie-id", "Name": "Movie", "Type": "Movie"})

        url = service.stream_url(item)

        self.assertEqual(
            url,
            "https://media.example/Videos/movie-id/stream?Static=true&api_key=token+value",
        )

    def test_resolve_keeps_token_out_of_the_media_url(self) -> None:
        service = JellyfinService(
            JellyfinSession("https://media.example", "user", "Ada", "secret-token")
        )
        item = service._item({"Id": "movie-id", "Name": "Movie", "Type": "Movie"})

        stream = service.resolve(item)

        self.assertEqual(stream.url, "https://media.example/Videos/movie-id/stream?Static=true")
        self.assertEqual(stream.headers["X-Emby-Token"], "secret-token")
        self.assertNotIn("secret-token", stream.url)

    @patch.object(JellyfinService, "_request_current")
    def test_global_search_does_not_send_an_empty_parent_id(self, request: object) -> None:
        request.return_value = {"Items": []}  # type: ignore[attr-defined]
        service = JellyfinService(
            JellyfinSession("https://media.example", "user", "Ada", "token")
        )

        service.get_items("", "Movie")

        query = request.call_args.kwargs["query"]  # type: ignore[attr-defined]
        self.assertNotIn("ParentId", query)


class ConfigStoreTests(unittest.TestCase):
    def test_session_round_trip_uses_private_file_permissions(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": directory}),
        ):
            store = ConfigStore()
            session = JellyfinSession("https://media.example", "user", "Ada", "token")
            store.save_session(session)

            self.assertEqual(store.load_session(), session)
            permissions = Path(store.path).stat().st_mode & 0o777
            self.assertEqual(permissions, 0o600)


if __name__ == "__main__":
    unittest.main()
