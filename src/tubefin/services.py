from __future__ import annotations

import hashlib
import http.cookiejar
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from tubefin.models import (
    Availability,
    ChannelDetails,
    Comment,
    CommentPage,
    JellyfinSession,
    MediaItem,
    MediaSection,
    ResolvedStream,
    SponsorSegment,
    StreamVariant,
    SubtitleTrack,
    VideoDetails,
    YouTubeBrowserSession,
)


class ServiceError(RuntimeError):
    pass


class ContentUnavailableError(ServiceError):
    def __init__(self, message: str, availability: Availability) -> None:
        super().__init__(message)
        self.availability = availability


class YouTubeService:
    PERSONAL_FEEDS = {
        "home": "https://www.youtube.com/",
        "subscriptions": "https://www.youtube.com/feed/subscriptions",
        "liked": "https://www.youtube.com/playlist?list=LL",
        "history": "https://www.youtube.com/feed/history",
        "watch_later": "https://www.youtube.com/playlist?list=WL",
    }

    def __init__(self, browser: str = "") -> None:
        self.executable = shutil.which("yt-dlp")
        self.browser = browser
        self.avatar_cache: dict[str, str | None] = {}

    @property
    def available(self) -> bool:
        return self.executable is not None

    @staticmethod
    def video_id_from_url(value: str) -> str | None:
        value = value.strip()
        if not value:
            return None
        if "://" not in value and value.startswith(
            ("youtube.com/", "www.youtube.com/", "m.youtube.com/", "youtu.be/")
        ):
            value = f"https://{value}"
        try:
            parsed = urllib.parse.urlparse(value)
        except ValueError:
            return None
        host = (parsed.hostname or "").casefold().removeprefix("www.")
        video_id = ""
        if host == "youtu.be":
            video_id = parsed.path.strip("/").split("/", 1)[0]
        elif host == "youtube.com" or host.endswith(".youtube.com"):
            video_id = urllib.parse.parse_qs(parsed.query).get("v", [""])[0]
            if not video_id:
                parts = parsed.path.strip("/").split("/")
                if len(parts) >= 2 and parts[0] in {"embed", "live", "shorts"}:
                    video_id = parts[1]
        return video_id if re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id) else None

    @staticmethod
    def playlist_id_from_url(value: str) -> str | None:
        value = value.strip()
        if not value:
            return None
        if "://" not in value and value.startswith(
            ("youtube.com/", "www.youtube.com/", "m.youtube.com/", "youtu.be/")
        ):
            value = f"https://{value}"
        try:
            parsed = urllib.parse.urlparse(value)
        except ValueError:
            return None
        host = (parsed.hostname or "").casefold().removeprefix("www.")
        if host != "youtu.be" and host != "youtube.com" and not host.endswith(
            ".youtube.com"
        ):
            return None
        playlist_id = urllib.parse.parse_qs(parsed.query).get("list", [""])[0]
        return playlist_id or None

    @classmethod
    def playable_playlist_url(cls, value: str) -> str:
        """Keep radio playlists attached to a seed video instead of /playlist."""
        playlist_id = cls.playlist_id_from_url(value)
        if not playlist_id or not playlist_id.startswith("RD"):
            return value
        video_id = cls.video_id_from_url(value)
        if not video_id:
            possible_seed = playlist_id[2:]
            if re.fullmatch(r"[A-Za-z0-9_-]{11}", possible_seed):
                video_id = possible_seed
        if not video_id:
            return value
        return (
            "https://www.youtube.com/watch?"
            + urllib.parse.urlencode(
                {"v": video_id, "list": playlist_id, "start_radio": "1"}
            )
        )

    def _run(self, *arguments: str) -> dict[str, Any]:
        if not self.executable:
            raise ServiceError("yt-dlp is not installed. Run TubeFin through Nix to include it.")
        try:
            browser_arguments = ["--cookies-from-browser", self.browser] if self.browser else []
            result = subprocess.run(
                [self.executable, *browser_arguments, *arguments],
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
            message = message.removeprefix("ERROR: ").strip()
            folded = message.casefold()
            if "members-only" in folded or "members only" in folded:
                raise ContentUnavailableError(
                    "This video is available to channel members only.", Availability.MEMBERS_ONLY
                )
            if "age" in folded and ("restrict" in folded or "confirm" in folded):
                raise ContentUnavailableError(
                    "This age-restricted video requires an authenticated YouTube session.",
                    Availability.AGE_RESTRICTED,
                )
            if "private video" in folded or "video is private" in folded:
                raise ContentUnavailableError("This video is private.", Availability.PRIVATE)
            if any(word in folded for word in ("unavailable", "removed", "terminated")):
                raise ContentUnavailableError(
                    "This video is unavailable or has been removed.", Availability.UNAVAILABLE
                )
            raise ServiceError(message)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise ServiceError("YouTube returned an unexpected response.") from error

    def search(self, query: str, limit: int = 18) -> list[MediaItem]:
        request = urllib.request.Request(
            "https://www.youtube.com/results?"
            + urllib.parse.urlencode({"search_query": query}),
            headers={
                "Accept-Language": "en-US,en;q=0.8",
                "User-Agent": "Mozilla/5.0 TubeFin/0.1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                html = response.read(6 * 1024 * 1024).decode("utf-8", errors="ignore")
            match = re.search(r"var ytInitialData = (\{.*?\});</script>", html, re.DOTALL)
            initial = json.loads(match.group(1)) if match else {}
            renderers: list[dict[str, Any]] = []

            def collect(value: object) -> None:
                if len(renderers) >= limit:
                    return
                if isinstance(value, dict):
                    renderer = value.get("videoRenderer")
                    if isinstance(renderer, dict):
                        renderers.append(renderer)
                        return
                    for child in value.values():
                        collect(child)
                elif isinstance(value, list):
                    for child in value:
                        collect(child)

            collect(initial)
            items = [self._search_renderer_item(value) for value in renderers[:limit]]
            if items:
                return items
        except (OSError, ValueError, urllib.error.URLError, TimeoutError):
            pass
        data = self._run(
            "--dump-single-json",
            "--flat-playlist",
            "--no-warnings",
            "--extractor-args",
            "youtube:player_skip=configs",
            "--playlist-end",
            str(limit),
            f"ytsearch{limit}:{query}",
        )
        return [self._item(entry) for entry in data.get("entries", []) if entry]

    def search_channels(self, query: str, limit: int = 18) -> list[MediaItem]:
        request = urllib.request.Request(
            "https://www.youtube.com/results?"
            + urllib.parse.urlencode(
                {"search_query": query, "sp": "EgIQAg=="}
            ),
            headers={
                "Accept-Language": "en-US,en;q=0.8",
                "User-Agent": "Mozilla/5.0 TubeFin/0.1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                html = response.read(6 * 1024 * 1024).decode("utf-8", errors="ignore")
            match = re.search(r"var ytInitialData = (\{.*?\});</script>", html, re.DOTALL)
            initial = json.loads(match.group(1)) if match else {}
            renderers: list[dict[str, Any]] = []

            def collect(value: object) -> None:
                if len(renderers) >= limit:
                    return
                if isinstance(value, dict):
                    renderer = value.get("channelRenderer")
                    if isinstance(renderer, dict):
                        renderers.append(renderer)
                    for child in value.values():
                        collect(child)
                elif isinstance(value, list):
                    for child in value:
                        collect(child)

            collect(initial)
            return [self._channel_renderer_item(renderer) for renderer in renderers]
        except (OSError, ValueError, urllib.error.URLError, TimeoutError) as error:
            raise ServiceError("Could not search YouTube channels.") from error

    @staticmethod
    def _channel_renderer_item(renderer: dict[str, Any]) -> MediaItem:
        def text(value: object) -> str:
            if not isinstance(value, dict):
                return ""
            if value.get("simpleText"):
                return str(value["simpleText"])
            return "".join(
                str(run.get("text") or "")
                for run in value.get("runs") or []
                if isinstance(run, dict)
            )

        channel_id = str(renderer.get("channelId") or "")
        endpoint = renderer.get("navigationEndpoint") or {}
        browse = endpoint.get("browseEndpoint") or {}
        metadata = endpoint.get("commandMetadata") or {}
        channel_path = str(
            browse.get("canonicalBaseUrl")
            or metadata.get("webCommandMetadata", {}).get("url")
            or f"/channel/{channel_id}"
        )
        thumbnails = (renderer.get("thumbnail") or {}).get("thumbnails") or []
        avatar = next(
            (
                str(candidate.get("url"))
                for candidate in reversed(thumbnails)
                if isinstance(candidate, dict) and candidate.get("url")
            ),
            None,
        )
        detail = " · ".join(
            value
            for value in (
                text(renderer.get("subscriberCountText")),
                text(renderer.get("videoCountText")),
            )
            if value
        )
        channel_url = urllib.parse.urljoin("https://www.youtube.com", channel_path)
        return MediaItem(
            id=channel_id or channel_url,
            title=text(renderer.get("title")) or "YouTube channel",
            subtitle=detail or "YouTube channel",
            source="youtube-channel",
            thumbnail_url=avatar,
            playable=False,
            payload={"channel_url": channel_url, "channel_avatar_url": avatar or ""},
        )

    @staticmethod
    def _search_renderer_item(renderer: dict[str, Any]) -> MediaItem:
        def text(value: object) -> str:
            if not isinstance(value, dict):
                return ""
            if value.get("simpleText"):
                return str(value["simpleText"])
            return "".join(
                str(run.get("text") or "")
                for run in value.get("runs") or []
                if isinstance(run, dict)
            )

        video_id = str(renderer.get("videoId") or "")
        byline = (renderer.get("longBylineText") or {}).get("runs") or []
        channel_run = byline[0] if byline and isinstance(byline[0], dict) else {}
        endpoint = channel_run.get("navigationEndpoint") or {}
        browse = endpoint.get("browseEndpoint") or {}
        channel_id = str(browse.get("browseId") or "")
        channel_path = str(
            browse.get("canonicalBaseUrl")
            or (endpoint.get("commandMetadata") or {}).get("webCommandMetadata", {}).get("url")
            or ""
        )
        thumbnails = (renderer.get("thumbnail") or {}).get("thumbnails") or []
        thumbnail = next(
            (
                str(candidate.get("url"))
                for candidate in reversed(thumbnails)
                if isinstance(candidate, dict) and candidate.get("url")
            ),
            f"https://i.ytimg.com/vi/{urllib.parse.quote(video_id)}/hqdefault.jpg",
        )
        avatar_thumbnails = (
            (renderer.get("channelThumbnailSupportedRenderers") or {})
            .get("channelThumbnailWithLinkRenderer", {})
            .get("thumbnail", {})
            .get("thumbnails", [])
        )
        avatar = next(
            (
                str(candidate.get("url"))
                for candidate in reversed(avatar_thumbnails)
                if isinstance(candidate, dict) and candidate.get("url")
            ),
            "",
        )
        duration_text = text(renderer.get("lengthText"))
        duration = 0
        try:
            for part in duration_text.split(":"):
                duration = duration * 60 + int(part)
        except ValueError:
            duration = 0
        return MediaItem(
            id=video_id,
            title=text(renderer.get("title")) or "Untitled",
            subtitle=str(channel_run.get("text") or "YouTube"),
            source="youtube",
            thumbnail_url=thumbnail,
            duration_seconds=duration or None,
            payload={
                "webpage_url": f"https://www.youtube.com/watch?v={video_id}",
                "channel_id": channel_id,
                "channel_url": urllib.parse.urljoin("https://www.youtube.com", channel_path),
                "channel_avatar_url": avatar,
            },
        )

    def browser_session(self) -> YouTubeBrowserSession:
        if not self.browser:
            raise ServiceError("Choose a browser before connecting YouTube.")
        try:
            data = self._run(
                "--dump-single-json",
                "--flat-playlist",
                "--no-warnings",
                "--playlist-end",
                "1",
                self.PERSONAL_FEEDS["liked"],
            )
        except ServiceError as error:
            raise ServiceError(
                f"YouTube is not signed in through {self.browser.title()}. "
                "Sign in on the website, then try again."
            ) from error
        channel_id = str(data.get("channel_id") or "")
        display_name = str(data.get("channel") or data.get("uploader") or "").strip()
        if data.get("id") != "LL" or not channel_id or not display_name:
            raise ServiceError(
                f"YouTube is not signed in through {self.browser.title()}. "
                "Sign in on the website, then try again."
            )
        return YouTubeBrowserSession(
            browser=self.browser,
            channel_id=channel_id,
            display_name=display_name,
            channel_url=str(data.get("channel_url") or data.get("uploader_url") or ""),
            handle=str(data.get("uploader_id") or ""),
        )

    def wait_for_browser_session(
        self, *, timeout: float = 120, interval: float = 2
    ) -> YouTubeBrowserSession:
        deadline = time.monotonic() + timeout
        last_error: ServiceError | None = None
        while time.monotonic() < deadline:
            try:
                return self.browser_session()
            except ServiceError as error:
                last_error = error
                time.sleep(min(interval, max(0, deadline - time.monotonic())))
        raise last_error or ServiceError("YouTube sign-in was not detected.")

    def personal_feed(self, feed: str, limit: int = 24) -> list[MediaItem]:
        return self.personal_feed_page(feed, 1, limit)

    def subscriptions(self, limit: int = 1000) -> list[MediaItem]:
        data = self._run(
            "--dump-single-json",
            "--flat-playlist",
            "--no-warnings",
            "--playlist-end",
            str(max(1, limit)),
            "https://www.youtube.com/feed/channels",
        )
        channels: list[MediaItem] = []
        for entry in data.get("entries") or []:
            if not entry:
                continue
            channel_id = str(
                entry.get("channel_id")
                or entry.get("uploader_id")
                or entry.get("id")
                or ""
            )
            channel_url = str(
                entry.get("channel_url")
                or entry.get("uploader_url")
                or entry.get("webpage_url")
                or entry.get("url")
                or ""
            )
            if not channel_url.startswith(("http://", "https://")) and channel_id:
                channel_url = f"https://www.youtube.com/channel/{channel_id}"
            if not channel_id or not channel_url:
                continue
            thumbnails = entry.get("thumbnails") or []
            thumbnail = next(
                (
                    candidate.get("url")
                    for candidate in reversed(thumbnails)
                    if candidate.get("url")
                ),
                entry.get("thumbnail"),
            )
            if isinstance(thumbnail, str) and thumbnail.startswith("//"):
                thumbnail = f"https:{thumbnail}"
            title = str(
                entry.get("channel")
                or entry.get("uploader")
                or entry.get("title")
                or "YouTube channel"
            )
            channels.append(
                MediaItem(
                    channel_id,
                    title,
                    subtitle="YouTube subscription",
                    source="youtube-channel",
                    thumbnail_url=str(thumbnail) if thumbnail else None,
                    playable=False,
                    payload={
                        "channel_url": channel_url,
                        "channel_avatar_url": str(thumbnail or ""),
                    },
                )
            )
        return channels

    def personal_feed_page(
        self, feed: str, start: int = 1, limit: int = 24
    ) -> list[MediaItem]:
        try:
            url = self.PERSONAL_FEEDS[feed]
        except KeyError as error:
            raise ValueError(f"Unknown YouTube feed: {feed}") from error
        arguments = ["--dump-single-json", "--no-warnings"]
        if feed == "history":
            # Resolving history entries is slower, but supplies channel names,
            # channel URLs, and avatars that flat history entries omit.
            arguments += ["--skip-download"]
        else:
            arguments += ["--flat-playlist"]
        arguments += [
            "--playlist-start",
            str(max(1, start)),
            "--playlist-end",
            str(max(1, start) + max(1, limit) - 1),
            url,
        ]
        data = self._run(*arguments)
        return [self._item(entry) for entry in data.get("entries") or [] if entry]

    def download_options(self, item: MediaItem) -> tuple[list[str], bool]:
        video_url = item.payload.get("webpage_url") or f"https://www.youtube.com/watch?v={item.id}"
        data = self._run(
            "--dump-single-json",
            "--no-playlist",
            "--no-warnings",
            "--skip-download",
            str(video_url),
        )
        heights = {
            int(candidate.get("height") or 0)
            for candidate in data.get("formats") or []
            if candidate.get("vcodec") not in {None, "none"}
            and int(candidate.get("height") or 0) > 0
        }
        qualities = [f"{height}p" for height in sorted(heights, reverse=True)]
        return qualities, bool(data.get("subtitles"))

    def mark_watched(self, item: MediaItem) -> None:
        video_url = item.payload.get("webpage_url") or f"https://www.youtube.com/watch?v={item.id}"
        self._run(
            "--dump-single-json",
            "--simulate",
            "--mark-watched",
            "--no-warnings",
            str(video_url),
        )

    def dismiss_recommendation(self, video_id: str) -> None:
        if not self.browser:
            raise ServiceError("Connect a browser session before sending recommendation feedback.")
        cookie_path = self._export_browser_cookies()
        try:
            jar = http.cookiejar.MozillaCookieJar(cookie_path)
            jar.load(ignore_discard=True, ignore_expires=True)
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
            homepage = urllib.request.Request(
                self.PERSONAL_FEEDS["home"],
                headers={
                    "Accept-Language": "en-US,en;q=0.8",
                    "User-Agent": "Mozilla/5.0 TubeFin/0.1",
                },
            )
            with opener.open(homepage, timeout=30) as response:
                html = response.read(8 * 1024 * 1024).decode("utf-8", errors="ignore")
            config = self._youtube_page_config(html)
            token = self._recommendation_feedback_token(html, video_id)
            if not token:
                raise ServiceError("YouTube no longer listed that recommendation.")
            api_key = config.get("INNERTUBE_API_KEY")
            context = config.get("INNERTUBE_CONTEXT")
            if not api_key or not isinstance(context, dict):
                raise ServiceError("YouTube did not expose a feedback endpoint.")
            timestamp = int(time.time())
            sapisid = next(
                (
                    cookie.value
                    for cookie in jar
                    if cookie.name in {"SAPISID", "__Secure-3PAPISID"}
                ),
                "",
            )
            if not sapisid:
                raise ServiceError("The browser session is missing YouTube sign-in cookies.")
            origin = "https://www.youtube.com"
            digest = hashlib.sha1(
                f"{timestamp} {sapisid} {origin}".encode(), usedforsecurity=False
            ).hexdigest()
            client = context.get("client") if isinstance(context.get("client"), dict) else {}
            payload = json.dumps(
                {"context": context, "feedbackTokens": [token]}, separators=(",", ":")
            ).encode()
            request = urllib.request.Request(
                f"https://www.youtube.com/youtubei/v1/feedback?key={urllib.parse.quote(str(api_key))}",
                data=payload,
                method="POST",
                headers={
                    "Authorization": f"SAPISIDHASH {timestamp}_{digest}",
                    "Content-Type": "application/json",
                    "Origin": origin,
                    "X-Origin": origin,
                    "X-Youtube-Client-Name": str(
                        config.get("INNERTUBE_CONTEXT_CLIENT_NAME") or "1"
                    ),
                    "X-Youtube-Client-Version": str(client.get("clientVersion") or ""),
                    "User-Agent": "Mozilla/5.0 TubeFin/0.1",
                },
            )
            with opener.open(request, timeout=30) as response:
                if response.status >= 300:
                    raise ServiceError(f"YouTube rejected feedback (HTTP {response.status}).")
        except urllib.error.HTTPError as error:
            raise ServiceError(
                f"YouTube rejected recommendation feedback (HTTP {error.code})."
            ) from error
        except (OSError, ValueError, urllib.error.URLError, TimeoutError) as error:
            raise ServiceError("Could not send recommendation feedback to YouTube.") from error
        finally:
            with suppress(FileNotFoundError):
                os.unlink(cookie_path)

    def _export_browser_cookies(self) -> str:
        if not self.executable:
            raise ServiceError("yt-dlp is not installed.")
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as cookie_file:
            cookie_file.write("# Netscape HTTP Cookie File\n")
            cookie_path = cookie_file.name
        result = subprocess.run(
            [
                self.executable,
                "--cookies-from-browser",
                self.browser,
                "--cookies",
                cookie_path,
                "--simulate",
                "--skip-download",
                "--playlist-end",
                "1",
                self.PERSONAL_FEEDS["home"],
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=90,
        )
        if result.returncode:
            with suppress(FileNotFoundError):
                os.unlink(cookie_path)
            detail = result.stderr.strip().splitlines()
            raise ServiceError(detail[-1] if detail else "Could not read browser cookies.")
        return cookie_path

    @staticmethod
    def _youtube_page_config(html: str) -> dict[str, Any]:
        config: dict[str, Any] = {}
        for match in re.finditer(r"ytcfg\.set\((\{.*?\})\);", html, re.DOTALL):
            try:
                value = json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                config.update(value)
        return config

    @classmethod
    def _recommendation_feedback_token(cls, html: str, video_id: str) -> str:
        match = re.search(r"var ytInitialData = (\{.*?\});</script>", html, re.DOTALL)
        if not match:
            return ""
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            return ""

        def walk(value: object) -> str:
            if isinstance(value, dict):
                title = value.get("title")
                label = title.get("content") if isinstance(title, dict) else ""
                if label == "Not interested":
                    endpoint = (
                        value.get("rendererContext", {})
                        .get("commandContext", {})
                        .get("onTap", {})
                        .get("innertubeCommand", {})
                        .get("feedbackEndpoint", {})
                    )
                    if endpoint.get("contentId") == video_id:
                        return str(endpoint.get("feedbackToken") or "")
                for child in value.values():
                    found = walk(child)
                    if found:
                        return found
            elif isinstance(value, list):
                for child in value:
                    found = walk(child)
                    if found:
                        return found
            return ""

        return walk(data)

    def channel_avatar(self, channel_url: str) -> str | None:
        if channel_url in self.avatar_cache:
            return self.avatar_cache[channel_url]
        request = urllib.request.Request(
            channel_url,
            headers={"User-Agent": "Mozilla/5.0 TubeFin/0.1", "Accept-Language": "en"},
        )
        avatar = None
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                page = response.read(4 * 1024 * 1024).decode("utf-8", errors="ignore")
            match = re.search(r'"avatar":\{"thumbnails":\[\{"url":"([^"]+)', page)
            if match:
                avatar = json.loads(f'"{match.group(1)}"')
        except (OSError, ValueError, urllib.error.URLError, TimeoutError):
            pass
        self.avatar_cache[channel_url] = avatar
        return avatar

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
        variants: list[StreamVariant] = []
        seen_heights: set[int] = set()
        formats = sorted(
            data.get("formats") or [],
            key=lambda candidate: int(candidate.get("height") or 0),
            reverse=True,
        )
        for candidate in formats:
            height = int(candidate.get("height") or 0)
            if (
                not height
                or height in seen_heights
                or candidate.get("vcodec") == "none"
                or candidate.get("acodec") == "none"
                or not candidate.get("url")
            ):
                continue
            seen_heights.add(height)
            variant_headers = {
                str(name): str(value)
                for name, value in (candidate.get("http_headers") or headers).items()
                if value is not None
            }
            variants.append(StreamVariant(f"{height}p", str(candidate["url"]), variant_headers))

        subtitles: list[SubtitleTrack] = []
        authored = data.get("subtitles") or {}
        automatic = data.get("automatic_captions") or {}
        for language, tracks in {**automatic, **authored}.items():
            candidates = [track for track in tracks or [] if track.get("url")]
            if not candidates:
                continue
            if language not in authored:
                # YouTube advertises a large matrix of generated translations,
                # many of which are unavailable when requested. Keep only the
                # source-language automatic track; translated names contain
                # "from <language>" in yt-dlp metadata.
                source_candidates = [
                    candidate
                    for candidate in candidates
                    if " from " not in str(candidate.get("name", "")).casefold()
                ]
                if not source_candidates:
                    continue
                candidates = source_candidates
            track = next(
                (candidate for candidate in candidates if candidate.get("ext") == "vtt"),
                candidates[-1],
            )
            automatic_track = language not in authored
            name = track.get("name") or language
            label = f"{name} (auto)" if automatic_track else str(name)
            subtitles.append(SubtitleTrack(label, str(language), str(track["url"])))
        subtitles.sort(key=lambda track: ("auto" in track.label, track.label.casefold()))

        return ResolvedStream(str(stream_url), headers, variants, subtitles)

    def details(self, item: MediaItem) -> VideoDetails:
        video_url = item.payload.get("webpage_url") or f"https://www.youtube.com/watch?v={item.id}"
        try:
            data = self._run(
                "--dump-single-json",
                "--no-playlist",
                "--no-warnings",
                "--skip-download",
                str(video_url),
            )
        except ContentUnavailableError as error:
            return VideoDetails(
                item,
                availability=error.availability,
                availability_message=str(error),
            )
        detailed_item = self._item(data)
        detailed_item.payload.update(item.payload)
        detailed_item.payload.update(
            {
                "channel_id": data.get("channel_id"),
                "channel_url": data.get("channel_url") or data.get("uploader_url"),
            }
        )
        availability = self._availability(data)
        return VideoDetails(
            item=detailed_item,
            description=str(data.get("description") or ""),
            upload_date=str(data.get("upload_date") or ""),
            view_count=self._integer(data.get("view_count")),
            like_count=self._integer(data.get("like_count")),
            tags=[str(tag) for tag in data.get("tags") or []],
            availability=availability,
            availability_message=self._availability_message(availability),
        )

    def channel(self, channel_url: str, *, page: int = 1, page_size: int = 24) -> ChannelDetails:
        page = max(1, page)
        start = (page - 1) * page_size + 1
        data = self._run(
            "--dump-single-json",
            "--flat-playlist",
            "--no-warnings",
            "--playlist-start",
            str(start),
            "--playlist-end",
            str(start + page_size),
            channel_url.rstrip("/") + "/videos",
        )
        entries = [entry for entry in data.get("entries") or [] if entry]
        channel_id = str(
            data.get("channel_id") or data.get("uploader_id") or data.get("id") or ""
        )
        channel_title = str(
            data.get("channel") or data.get("uploader") or data.get("title") or "Channel"
        )
        thumbnails = data.get("thumbnails") or []
        extracted_avatar = next(
            (
                str(candidate.get("url"))
                for candidate in reversed(thumbnails)
                if isinstance(candidate, dict) and candidate.get("url")
            ),
            None,
        )
        avatar = (
            self.avatar_cache.get(channel_url)
            or extracted_avatar
            or self.channel_avatar(channel_url)
        )
        videos: list[MediaItem] = []
        for entry in entries[:page_size]:
            item = self._item(entry)
            payload = dict(item.payload)
            payload["channel_id"] = payload.get("channel_id") or channel_id
            payload["channel_url"] = payload.get("channel_url") or channel_url
            payload["channel_avatar_url"] = (
                payload.get("channel_avatar_url") or avatar or ""
            )
            if not item.subtitle or item.subtitle.casefold() == "youtube":
                item.subtitle = channel_title
            item.payload = payload
            videos.append(item)
        has_more = len(entries) > page_size
        return ChannelDetails(
            id=channel_id,
            title=channel_title,
            url=channel_url,
            description=str(data.get("description") or ""),
            subscriber_count=self._integer(data.get("channel_follower_count")),
            avatar_url=avatar,
            videos=videos,
            continuation=str(page + 1) if has_more else None,
        )

    def channel_latest(self, channel_url: str) -> MediaItem | None:
        data = self._run(
            "--dump-single-json",
            "--flat-playlist",
            "--no-warnings",
            "--playlist-end",
            "1",
            channel_url.rstrip("/") + "/videos",
        )
        entry = next((value for value in data.get("entries") or [] if value), None)
        return self._item(entry) if entry else None

    def comments(
        self, item: MediaItem, *, cursor: str | None = None, page_size: int = 20
    ) -> CommentPage:
        # yt-dlp exposes nested replies in one extraction. Cursor pagination is
        # deliberately local and stable so a UI never has to load hundreds of
        # widgets at once.
        offset = max(0, int(cursor or 0))
        limit = offset + max(1, min(page_size, 100))
        reply_limit = max(100, limit * 5)
        video_url = item.payload.get("webpage_url") or f"https://www.youtube.com/watch?v={item.id}"
        data = self._run(
            "--dump-single-json",
            "--skip-download",
            "--write-comments",
            "--extractor-args",
            (
                f"youtube:max_comments=all,{limit},{reply_limit},50,all;"
                "comment_sort=top;player_skip=configs"
            ),
            "--no-warnings",
            str(video_url),
        )
        values = [self._comment(value) for value in data.get("comments") or []]
        roots: list[Comment] = []
        indexed = {comment.id: comment for comment in values}
        for comment in values:
            if comment.parent_id and comment.parent_id in indexed:
                indexed[comment.parent_id].replies.append(comment)
            elif not comment.parent_id or comment.parent_id in {"root", "0"}:
                roots.append(comment)
        page = roots[offset:limit]
        return CommentPage(page, str(limit) if len(roots) >= limit else None)

    def playlist(
        self, url: str, *, page: int = 1, page_size: int = 50
    ) -> tuple[str, list[MediaItem], str | None]:
        url = self.playable_playlist_url(url)
        start = (max(1, page) - 1) * page_size + 1
        data = self._run(
            "--dump-single-json",
            "--flat-playlist",
            "--yes-playlist",
            "--no-warnings",
            "--playlist-start",
            str(start),
            "--playlist-end",
            str(start + page_size),
            url,
        )
        entries = [entry for entry in data.get("entries") or [] if entry]
        return (
            str(data.get("title") or "YouTube playlist"),
            [self._item(entry) for entry in entries[:page_size]],
            str(page + 1) if len(entries) > page_size else None,
        )

    @staticmethod
    def _comment(data: dict[str, Any]) -> Comment:
        return Comment(
            id=str(data.get("id") or ""),
            author=str(data.get("author") or "Unknown user"),
            text=str(data.get("text") or ""),
            timestamp=YouTubeService._integer(data.get("timestamp")),
            like_count=YouTubeService._integer(data.get("like_count")) or 0,
            parent_id=str(data["parent"]) if data.get("parent") is not None else None,
        )

    @staticmethod
    def _availability(data: dict[str, Any]) -> Availability:
        value = str(data.get("availability") or "public").replace("-", "_")
        aliases = {
            "needs_auth": Availability.AGE_RESTRICTED,
            "subscriber_only": Availability.MEMBERS_ONLY,
            "premium_only": Availability.MEMBERS_ONLY,
        }
        if value in aliases:
            return aliases[value]
        try:
            return Availability(value)
        except ValueError:
            return Availability.PUBLIC

    @staticmethod
    def _availability_message(availability: Availability) -> str:
        return {
            Availability.PUBLIC: "",
            Availability.UNAVAILABLE: "This video is unavailable or has been removed.",
            Availability.AGE_RESTRICTED: "This video is age-restricted and requires sign-in.",
            Availability.MEMBERS_ONLY: "This video is available to channel members only.",
            Availability.PRIVATE: "This video is private.",
        }[availability]

    @staticmethod
    def _integer(value: object) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def api_playlists(self, access_token: str, mine: bool = True) -> list[dict[str, Any]]:
        data = self._api(
            "playlists",
            access_token,
            {"part": "snippet,contentDetails", "mine": str(mine).lower(), "maxResults": 50},
        )
        return list(data.get("items") or [])

    def api_create_playlist(
        self, access_token: str, title: str, privacy: str = "private"
    ) -> dict[str, Any]:
        return self._api(
            "playlists",
            access_token,
            {"part": "snippet,status"},
            method="POST",
            body={"snippet": {"title": title}, "status": {"privacyStatus": privacy}},
        )

    def api_update_playlist(
        self, access_token: str, playlist_id: str, title: str
    ) -> dict[str, Any]:
        return self._api(
            "playlists",
            access_token,
            {"part": "snippet"},
            method="PUT",
            body={"id": playlist_id, "snippet": {"title": title}},
        )

    def api_delete_playlist(self, access_token: str, playlist_id: str) -> None:
        self._api("playlists", access_token, {"id": playlist_id}, method="DELETE")

    def api_add_playlist_item(
        self, access_token: str, playlist_id: str, video_id: str
    ) -> dict[str, Any]:
        return self._api(
            "playlistItems",
            access_token,
            {"part": "snippet"},
            method="POST",
            body={
                "snippet": {
                    "playlistId": playlist_id,
                    "resourceId": {"kind": "youtube#video", "videoId": video_id},
                }
            },
        )

    def api_feed(
        self, access_token: str, feed: str, page_token: str | None = None
    ) -> tuple[list[dict[str, Any]], str | None]:
        if feed == "subscriptions":
            endpoint = "subscriptions"
            query: dict[str, object] = {
                "part": "snippet",
                "mine": "true",
                "order": "alphabetical",
                "maxResults": 50,
            }
        elif feed == "liked":
            endpoint = "videos"
            query = {"part": "snippet,contentDetails", "myRating": "like", "maxResults": 50}
        else:
            endpoint = "activities"
            query = {"part": "snippet,contentDetails", "mine": "true", "maxResults": 50}
        if page_token:
            query["pageToken"] = page_token
        data = self._api(endpoint, access_token, query)
        return list(data.get("items") or []), data.get("nextPageToken")

    def api_search(
        self, access_token: str, query: str, *, channels: bool = False, limit: int = 18
    ) -> list[MediaItem]:
        data = self._api(
            "search",
            access_token,
            {
                "part": "snippet",
                "q": query,
                "type": "channel" if channels else "video",
                "maxResults": max(1, min(limit, 50)),
            },
        )
        items: list[MediaItem] = []
        for value in data.get("items") or []:
            identity = value.get("id") or {}
            snippet = value.get("snippet") or {}
            thumbnails = list((snippet.get("thumbnails") or {}).values())
            thumbnail = next(
                (
                    candidate.get("url")
                    for candidate in reversed(thumbnails)
                    if candidate.get("url")
                ),
                None,
            )
            if channels:
                channel_id = str(identity.get("channelId") or snippet.get("channelId") or "")
                if not channel_id:
                    continue
                items.append(
                    MediaItem(
                        id=channel_id,
                        title=str(snippet.get("title") or "YouTube channel"),
                        subtitle="YouTube channel",
                        source="youtube-channel",
                        thumbnail_url=str(thumbnail) if thumbnail else None,
                        playable=False,
                        payload={
                            "channel_url": f"https://www.youtube.com/channel/{channel_id}",
                            "channel_avatar_url": str(thumbnail or ""),
                        },
                    )
                )
                continue
            video_id = str(identity.get("videoId") or "")
            if not video_id:
                continue
            items.append(
                self._item(
                    {
                        "id": video_id,
                        "title": snippet.get("title"),
                        "channel": snippet.get("channelTitle"),
                        "channel_id": snippet.get("channelId"),
                        "channel_url": (
                            f"https://www.youtube.com/channel/{snippet.get('channelId')}"
                            if snippet.get("channelId")
                            else ""
                        ),
                        "thumbnails": thumbnails,
                    }
                )
            )
        return items

    @staticmethod
    def _api(
        endpoint: str,
        access_token: str,
        query: dict[str, object],
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"https://www.googleapis.com/youtube/v3/{endpoint}?{urllib.parse.urlencode(query)}"
        payload = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": "application/json", "Authorization": f"Bearer {access_token}"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=payload, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.status == 204:
                    return {}
                return json.load(response)
        except urllib.error.HTTPError as error:
            try:
                detail = json.load(error)
                message = detail.get("error", {}).get("message")
            except (ValueError, AttributeError):
                message = f"HTTP {error.code}"
            raise ServiceError(f"YouTube account request failed: {message}.") from error
        except (OSError, ValueError, urllib.error.URLError, TimeoutError) as error:
            raise ServiceError("Could not reach the YouTube account API.") from error

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
            payload={
                "webpage_url": webpage_url,
                "channel_id": entry.get("channel_id"),
                "channel_url": entry.get("channel_url") or entry.get("uploader_url"),
            },
        )


class SponsorBlockService:
    """Privacy-preserving, read-only SponsorBlock segment lookup."""

    API = "https://sponsor.ajay.app/api/skipSegments"

    def segments(
        self,
        video_id: str,
        categories: tuple[str, ...] = ("sponsor",),
    ) -> list[SponsorSegment]:
        prefix = hashlib.sha256(video_id.encode()).hexdigest()[:4]
        query = urllib.parse.urlencode(
            {
                "categories": json.dumps(categories),
                "actionTypes": json.dumps(["skip"]),
            }
        )
        request = urllib.request.Request(
            f"{self.API}/{prefix}?{query}",
            headers={"Accept": "application/json", "User-Agent": "TubeFin/0.1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                values = json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return []
            raise ServiceError(f"SponsorBlock request failed with HTTP {error.code}.") from error
        except (OSError, ValueError, urllib.error.URLError, TimeoutError) as error:
            raise ServiceError("Could not reach SponsorBlock.") from error
        matching = next(
            (value for value in values if str(value.get("videoID") or "") == video_id),
            {},
        )
        segments: list[SponsorSegment] = []
        for value in matching.get("segments") or []:
            bounds = value.get("segment") or []
            if len(bounds) != 2 or value.get("actionType", "skip") != "skip":
                continue
            try:
                start, end = float(bounds[0]), float(bounds[1])
            except (TypeError, ValueError):
                continue
            if 0 <= start < end:
                segments.append(
                    SponsorSegment(start, end, str(value.get("category") or "sponsor"))
                )
        return sorted(segments, key=lambda segment: segment.start)


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
        return self._session_from_auth(server, data)

    def initiate_quick_connect(self, server: str) -> tuple[str, str, str]:
        server = self.normalize_server(server)
        enabled = self._request(
            server,
            "/QuickConnect/Enabled",
            authenticated=False,
        )
        if not enabled:
            raise ServiceError("Quick Connect is disabled on this Jellyfin server.")
        result = self._request(
            server,
            "/QuickConnect/Initiate",
            method="POST",
            body={},
            authenticated=False,
        )
        secret = str(result.get("Secret") or "")
        code = str(result.get("Code") or "")
        if not secret or not code:
            raise ServiceError("Jellyfin did not return a Quick Connect code.")
        return server, secret, code

    def complete_quick_connect(
        self,
        server: str,
        secret: str,
        *,
        timeout: int = 180,
    ) -> JellyfinSession:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self._request(
                server,
                "/QuickConnect/Connect",
                query={"Secret": secret},
                authenticated=False,
            )
            if state.get("Authenticated"):
                data = self._request(
                    server,
                    "/Users/AuthenticateWithQuickConnect",
                    method="POST",
                    body={"Secret": secret},
                    authenticated=False,
                )
                return self._session_from_auth(server, data)
            time.sleep(2)
        raise ServiceError("Jellyfin Quick Connect timed out. Request a new code and try again.")

    def _session_from_auth(self, server: str, data: Any) -> JellyfinSession:
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

    def browse(self, category: str) -> list[MediaItem]:
        if category == "channels":
            data = self._request_current(
                "/Channels", query={"UserId": self._require_session().user_id}
            )
            return self._items(data.get("Items", []), playable=False)
        item_type = "Movie" if category == "movies" else "Series"
        data = self._request_current(
            "/Users/{user_id}/Items",
            query={
                "Recursive": "true",
                "IncludeItemTypes": item_type,
                "SortBy": "SortName",
                "SortOrder": "Ascending",
                "Fields": "Overview,PrimaryImageAspectRatio,MediaSources",
                "ImageTypeLimit": 1,
                "Limit": 100,
            },
        )
        return self._items(data.get("Items", []))

    def refresh_item_artwork(self, item: MediaItem) -> MediaItem:
        if item.source != "jellyfin" or not item.payload:
            return item
        refreshed = self._item(item.payload, playable=item.playable)
        return MediaItem(
            id=item.id,
            title=item.title,
            subtitle=item.subtitle,
            source=item.source,
            kind=item.kind,
            thumbnail_url=refreshed.thumbnail_url,
            duration_seconds=item.duration_seconds,
            playable=item.playable,
            payload=item.payload,
        )

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
        try:
            playback = self._request_current(
                f"/Items/{urllib.parse.quote(item.id)}/PlaybackInfo",
                query={"UserId": session.user_id},
            )
        except ServiceError:
            # Direct play remains useful on older/restricted servers even when
            # PlaybackInfo is unavailable; item payloads may still carry tracks.
            playback = {}
        sources = playback.get("MediaSources") or item.payload.get("MediaSources") or []
        source = sources[0] if sources else {}
        source_id = str(source.get("Id") or "")
        url = self._url(
            session.server_url,
            f"/{media_path}/{urllib.parse.quote(item.id)}/stream",
            {
                "Static": "true",
                **({"MediaSourceId": source_id} if source_id else {}),
            },
        )
        subtitles: list[SubtitleTrack] = []
        for stream in source.get("MediaStreams") or []:
            if stream.get("Type") != "Subtitle":
                continue
            index = stream.get("Index")
            delivery_url = stream.get("DeliveryUrl")
            codec = str(stream.get("Codec") or "vtt")
            if delivery_url:
                subtitle_url = f"{session.server_url.rstrip('/')}{delivery_url}"
            else:
                if index is None or not source_id:
                    continue
                subtitle_url = self._url(
                    session.server_url,
                    (
                        f"/Videos/{urllib.parse.quote(item.id)}/"
                        f"{urllib.parse.quote(source_id)}/Subtitles/{index}/Stream.{codec}"
                    ),
                )
            label = str(
                stream.get("DisplayTitle")
                or stream.get("Title")
                or stream.get("Language")
                or f"Subtitle {stream.get('Index', '')}"
            )
            subtitles.append(
                SubtitleTrack(label, str(stream.get("Language") or "und"), subtitle_url)
            )
        return ResolvedStream(
            url,
            {
                "Authorization": self.CLIENT_HEADER,
                "X-Emby-Token": session.access_token,
            },
            subtitles=subtitles,
        )

    def report_playback(
        self,
        item: MediaItem,
        position: float,
        paused: bool,
        *,
        event: str = "progress",
    ) -> None:
        paths = {
            "start": "/Sessions/Playing",
            "progress": "/Sessions/Playing/Progress",
            "stop": "/Sessions/Playing/Stopped",
        }
        try:
            path = paths[event]
        except KeyError as error:
            raise ValueError(f"Unknown playback event: {event}") from error
        self._request_current(
            path,
            method="POST",
            body={
                "ItemId": item.id,
                "PositionTicks": max(0, int(position * 10_000_000)),
                "IsPaused": paused,
                "CanSeek": True,
                "PlayMethod": "DirectPlay",
            },
        )

    def mark_watched(self, item: MediaItem) -> None:
        self._request_current(
            f"/Users/{{user_id}}/PlayedItems/{urllib.parse.quote(item.id)}",
            method="POST",
        )

    def get_item(self, item_id: str) -> MediaItem:
        """Return one library item using the active Jellyfin account."""
        data = self._request_current(
            f"/Users/{{user_id}}/Items/{urllib.parse.quote(item_id)}"
        )
        if not isinstance(data, dict) or not data.get("Id"):
            raise ServiceError("Jellyfin did not return the requested item.")
        return self._item(data)

    def syncplay_groups(self) -> list[dict[str, Any]]:
        data = self._request_current("/SyncPlay/List")
        if not isinstance(data, list):
            return []
        return [value for value in data if isinstance(value, dict)]

    def syncplay_group(self, group_id: str) -> dict[str, Any]:
        data = self._request_current(
            f"/SyncPlay/{urllib.parse.quote(group_id)}"
        )
        return data if isinstance(data, dict) else {}

    def syncplay_create(self, name: str) -> dict[str, Any]:
        data = self._request_current(
            "/SyncPlay/New",
            method="POST",
            body={"GroupName": name.strip() or "TubeFin watch party"},
        )
        return data if isinstance(data, dict) else {}

    def syncplay_join(self, group_id: str) -> None:
        self._request_current(
            "/SyncPlay/Join",
            method="POST",
            body={"GroupId": group_id},
        )

    def syncplay_leave(self) -> None:
        self._request_current("/SyncPlay/Leave", method="POST")

    def syncplay_set_queue(
        self,
        item_ids: list[str],
        playing_index: int,
        position: float,
    ) -> None:
        self._request_current(
            "/SyncPlay/SetNewQueue",
            method="POST",
            body={
                "PlayingQueue": item_ids,
                "PlayingItemPosition": playing_index,
                "StartPositionTicks": max(0, int(position * 10_000_000)),
            },
        )

    def syncplay_pause(self) -> None:
        self._request_current("/SyncPlay/Pause", method="POST")

    def syncplay_unpause(self) -> None:
        self._request_current("/SyncPlay/Unpause", method="POST")

    def syncplay_seek(self, position: float) -> None:
        self._request_current(
            "/SyncPlay/Seek",
            method="POST",
            body={"PositionTicks": max(0, int(position * 10_000_000))},
        )

    def syncplay_ready(
        self,
        playlist_item_id: str,
        position: float,
        paused: bool,
    ) -> None:
        self._request_current(
            "/SyncPlay/Ready",
            method="POST",
            body={
                "When": datetime.now(UTC).isoformat(),
                "PositionTicks": max(0, int(position * 10_000_000)),
                "IsPlaying": not paused,
                "PlaylistItemId": playlist_item_id,
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
        image_tags = entry.get("ImageTags") or {}
        image_type = "Thumb" if image_tags.get("Thumb") else "Primary"
        image_tag = image_tags.get(image_type)
        thumbnail = None
        if image_tag:
            thumbnail = self._url(
                session.server_url,
                f"/Items/{urllib.parse.quote(item_id)}/Images/{image_type}",
                {
                    "tag": image_tag,
                    "maxWidth": 640,
                    "quality": 88,
                    "format": "jpg",
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
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
    ) -> Any:
        session = self._require_session()
        return self._request(
            session.server_url,
            path.format(user_id=urllib.parse.quote(session.user_id)),
            query=query,
            method=method,
            body=body,
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
                data = response.read()
                return json.loads(data) if data else {}
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
