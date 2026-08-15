from __future__ import annotations

import json
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from typing import Any

from tubefin.models import JellyfinSession, MediaItem, MediaSection, ResolvedStream


class ServiceError(RuntimeError):
    pass


class YouTubeService:
    def __init__(self) -> None:
        self.executable = shutil.which("yt-dlp")

    @property
    def available(self) -> bool:
        return self.executable is not None

    def _run(self, *arguments: str) -> dict[str, Any]:
        if not self.executable:
            raise ServiceError("yt-dlp is not installed. Run TubeFin through Nix to include it.")
        try:
            result = subprocess.run(
                [self.executable, *arguments],
                capture_output=True,
                check=False,
                text=True,
                timeout=90,
            )
        except subprocess.TimeoutExpired as error:
            raise ServiceError("YouTube took too long to respond.") from error
        if result.returncode != 0:
            detail = result.stderr.strip().splitlines()
            message = detail[-1] if detail else "yt-dlp could not complete the request."
            raise ServiceError(message.removeprefix("ERROR: ").strip())
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise ServiceError("YouTube returned an unexpected response.") from error

    def search(self, query: str, limit: int = 24) -> list[MediaItem]:
        data = self._run(
            "--dump-single-json",
            "--flat-playlist",
            "--no-warnings",
            "--playlist-end",
            str(limit),
            f"ytsearch{limit}:{query}",
        )
        return [self._item(entry) for entry in data.get("entries", []) if entry]

    def resolve(self, item: MediaItem) -> ResolvedStream:
        video_url = item.payload.get("webpage_url") or f"https://www.youtube.com/watch?v={item.id}"
        data = self._run(
            "--dump-single-json",
            "--no-playlist",
            "--no-warnings",
            "--format",
            "best[height<=1080][vcodec^=avc1][acodec^=mp4a]/best[height<=1080]/best",
            video_url,
        )
        stream_url = data.get("url")
        if not stream_url:
            raise ServiceError("No compatible YouTube stream was found.")
        headers = {
            str(name): str(value)
            for name, value in (data.get("http_headers") or {}).items()
            if value is not None
        }
        return ResolvedStream(str(stream_url), headers)

    @staticmethod
    def _item(entry: dict[str, Any]) -> MediaItem:
        thumbnails = entry.get("thumbnails") or []
        video_id = str(entry.get("id", ""))
        thumbnail = (
            f"https://i.ytimg.com/vi/{urllib.parse.quote(video_id)}/hqdefault.jpg"
            if video_id
            else next(
                (
                    candidate.get("url")
                    for candidate in reversed(thumbnails)
                    if candidate.get("url")
                ),
                entry.get("thumbnail"),
            )
        )
        channel = entry.get("channel") or entry.get("uploader") or "YouTube"
        webpage_url = entry.get("webpage_url") or entry.get("url") or ""
        if not str(webpage_url).startswith(("http://", "https://")):
            webpage_url = f"https://www.youtube.com/watch?v={entry.get('id', '')}"
        return MediaItem(
            id=video_id,
            title=entry.get("title") or "Untitled video",
            subtitle=channel,
            source="youtube",
            kind="video",
            thumbnail_url=thumbnail,
            duration_seconds=entry.get("duration"),
            payload={"webpage_url": webpage_url},
        )


class JellyfinService:
    CLIENT_HEADER = (
        'MediaBrowser Client="TubeFin", Device="Linux Desktop", '
        'DeviceId="tubefin-desktop", Version="0.1.0"'
    )

    def __init__(self, session: JellyfinSession | None = None) -> None:
        self.session = session

    @staticmethod
    def normalize_server(server: str) -> str:
        server = server.strip().rstrip("/")
        if "://" not in server:
            server = f"http://{server}"
        return server

    def authenticate(self, server: str, username: str, password: str) -> JellyfinSession:
        server = self.normalize_server(server)
        data = self._request(
            server,
            "/Users/AuthenticateByName",
            method="POST",
            body={"Username": username, "Pw": password},
            authenticated=False,
        )
        try:
            session = JellyfinSession(
                server_url=server,
                user_id=data["User"]["Id"],
                username=data["User"]["Name"],
                access_token=data["AccessToken"],
            )
        except (KeyError, TypeError) as error:
            raise ServiceError("The server did not return a valid Jellyfin session.") from error
        self.session = session
        return session

    def get_home(self) -> list[MediaSection]:
        self._require_session()
        resume = self._request_current(
            "/Users/{user_id}/Items/Resume",
            query={"Limit": 12, "MediaTypes": "Video", "Recursive": "true"},
        )
        latest = self._request_current(
            "/Users/{user_id}/Items/Latest",
            query={"Limit": 12, "GroupItems": "true", "EnableImages": "true"},
        )
        return [
            MediaSection("Continue watching", self._items(resume.get("Items", []))),
            MediaSection("Recently added", self._items(latest)),
        ]

    def get_libraries(self) -> list[MediaItem]:
        data = self._request_current("/Users/{user_id}/Views")
        return self._items(data.get("Items", []), playable=False)

    def get_items(self, parent_id: str, search: str = "") -> list[MediaItem]:
        query: dict[str, str | int] = {
            "Recursive": "false",
            "SortBy": "SortName",
            "SortOrder": "Ascending",
            "Fields": "Overview,PrimaryImageAspectRatio,MediaSources",
            "ImageTypeLimit": 1,
            "Limit": 100,
        }
        if parent_id:
            query["ParentId"] = parent_id
        if search:
            query.update({"SearchTerm": search, "Recursive": "true"})
        data = self._request_current("/Users/{user_id}/Items", query=query)
        return self._items(data.get("Items", []))

    def stream_url(self, item: MediaItem) -> str:
        session = self._require_session()
        media_path = "Audio" if item.kind == "Audio" else "Videos"
        return self._url(
            session.server_url,
            f"/{media_path}/{urllib.parse.quote(item.id)}/stream",
            {"Static": "true", "api_key": session.access_token},
        )

    def resolve(self, item: MediaItem) -> ResolvedStream:
        """Resolve Jellyfin media without exposing the access token in a player URI."""
        session = self._require_session()
        media_path = "Audio" if item.kind == "Audio" else "Videos"
        url = self._url(
            session.server_url,
            f"/{media_path}/{urllib.parse.quote(item.id)}/stream",
            {"Static": "true"},
        )
        return ResolvedStream(
            url,
            {
                "Authorization": self.CLIENT_HEADER,
                "X-Emby-Token": session.access_token,
            },
        )

    def _items(
        self,
        entries: Iterable[dict[str, Any]],
        playable: bool | None = None,
    ) -> list[MediaItem]:
        return [self._item(entry, playable) for entry in entries]

    def _item(self, entry: dict[str, Any], playable: bool | None = None) -> MediaItem:
        session = self._require_session()
        item_id = str(entry.get("Id", ""))
        item_type = entry.get("Type", "Video")
        non_playable = {
            "BoxSet",
            "CollectionFolder",
            "Folder",
            "Genre",
            "MusicGenre",
            "Series",
            "Season",
            "MusicAlbum",
            "MusicArtist",
            "Person",
            "PhotoAlbum",
            "Playlist",
            "Studio",
            "UserView",
        }
        image_tag = (entry.get("ImageTags") or {}).get("Primary")
        thumbnail = None
        if image_tag:
            thumbnail = self._url(
                session.server_url,
                f"/Items/{urllib.parse.quote(item_id)}/Images/Primary",
                {
                    "tag": image_tag,
                    "maxWidth": 640,
                    "quality": 88,
                    "api_key": session.access_token,
                },
            )
        ticks = entry.get("RunTimeTicks") or 0
        subtitle = entry.get("SeriesName") or entry.get("ProductionYear") or item_type
        return MediaItem(
            id=item_id,
            title=entry.get("Name") or "Untitled",
            subtitle=str(subtitle),
            source="jellyfin",
            kind=item_type,
            thumbnail_url=thumbnail,
            duration_seconds=int(ticks / 10_000_000) if ticks else None,
            playable=(item_type not in non_playable) if playable is None else playable,
            payload=entry,
        )

    def _request_current(
        self,
        path: str,
        query: dict[str, str | int] | None = None,
    ) -> Any:
        session = self._require_session()
        return self._request(
            session.server_url,
            path.format(user_id=urllib.parse.quote(session.user_id)),
            query=query,
        )

    def _request(
        self,
        server: str,
        path: str,
        *,
        method: str = "GET",
        query: dict[str, str | int] | None = None,
        body: dict[str, Any] | None = None,
        authenticated: bool = True,
    ) -> Any:
        url = self._url(server, path, query)
        headers = {"Accept": "application/json", "Authorization": self.CLIENT_HEADER}
        if authenticated:
            session = self._require_session()
            headers["X-Emby-Token"] = session.access_token
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=payload, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 401:
                message = "The username or password is incorrect."
            else:
                message = f"Jellyfin returned HTTP {error.code}."
            raise ServiceError(message) from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise ServiceError(f"Could not reach Jellyfin at {server}.") from error
        except json.JSONDecodeError as error:
            raise ServiceError("Jellyfin returned an unexpected response.") from error

    @staticmethod
    def _url(server: str, path: str, query: dict[str, Any] | None = None) -> str:
        url = f"{server.rstrip('/')}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        return url

    def _require_session(self) -> JellyfinSession:
        if not self.session:
            raise ServiceError("Connect to a Jellyfin server first.")
        return self.session
