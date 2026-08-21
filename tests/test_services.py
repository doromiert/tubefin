from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tubefin.config import SPONSORBLOCK_DEFAULTS, ConfigStore
from tubefin.models import Availability, JellyfinSession, MediaItem
from tubefin.services import (
    ContentUnavailableError,
    JellyfinService,
    ServiceError,
    SponsorBlockService,
    YouTubeService,
)


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


class YouTubeServiceTests(unittest.TestCase):
    def test_video_id_from_watch_url_ignores_radio_and_playlist_parameters(self) -> None:
        url = (
            "https://www.youtube.com/watch?v=YpPxGsELTgU&list=RDYpPxGsELTgU"
            "&start_radio=1&pp=oAcB"
        )

        self.assertEqual(YouTubeService.video_id_from_url(url), "YpPxGsELTgU")

    def test_video_id_from_url_supports_common_youtube_links(self) -> None:
        links = (
            "https://youtu.be/YpPxGsELTgU?t=20",
            "https://www.youtube.com/shorts/YpPxGsELTgU",
            "https://music.youtube.com/watch?v=YpPxGsELTgU",
            "youtube.com/watch?v=YpPxGsELTgU",
        )

        for link in links:
            with self.subTest(link=link):
                self.assertEqual(
                    YouTubeService.video_id_from_url(link), "YpPxGsELTgU"
                )

    def test_video_id_from_url_rejects_searches_and_non_youtube_links(self) -> None:
        values = (
            "lofi hip hop",
            "https://www.youtube.com/results?search_query=lofi",
            "https://example.com/watch?v=YpPxGsELTgU",
            "https://www.youtube.com/watch?v=too-short",
        )

        for value in values:
            with self.subTest(value=value):
                self.assertIsNone(YouTubeService.video_id_from_url(value))

    @patch("subprocess.run")
    def test_browser_session_is_passed_to_ytdlp(self, run: object) -> None:
        run.return_value = SimpleNamespace(  # type: ignore[attr-defined]
            returncode=0,
            stdout="{}",
            stderr="",
        )
        service = YouTubeService("firefox")
        service.executable = "yt-dlp"

        service._run("--dump-single-json", "https://www.youtube.com/")

        command = run.call_args.args[0]  # type: ignore[attr-defined]
        self.assertEqual(command[0], "yt-dlp")
        self.assertIn("--ignore-config", command)
        browser_option = command.index("--cookies-from-browser")
        self.assertEqual(
            command[browser_option : browser_option + 2],
            ["--cookies-from-browser", "firefox"],
        )

    def test_browser_session_verifies_and_returns_identity(self) -> None:
        service = YouTubeService("firefox")
        service._run = lambda *_args: {  # type: ignore[method-assign]
            "id": "LL",
            "channel_id": "channel-1",
            "channel": "Ada",
            "channel_url": "https://www.youtube.com/channel/channel-1",
            "uploader_id": "@ada",
            "entries": [],
        }

        session = service.browser_session()

        self.assertEqual(session.display_name, "Ada")
        self.assertEqual(session.channel_id, "channel-1")
        self.assertEqual(session.browser, "firefox")

    def test_browser_session_rejects_anonymous_response(self) -> None:
        service = YouTubeService("firefox")
        service._run = lambda *_args: {"id": "LL", "entries": []}  # type: ignore[method-assign]

        with self.assertRaisesRegex(ServiceError, "not signed in"):
            service.browser_session()

    def test_personal_feed_maps_cookie_backed_entries(self) -> None:
        service = YouTubeService("firefox")
        service._run = lambda *_args: {  # type: ignore[method-assign]
            "entries": [{"id": "abc123", "title": "For you", "channel": "Creator"}]
        }

        items = service.personal_feed("subscriptions", limit=12)

        self.assertEqual([item.id for item in items], ["abc123"])
        self.assertEqual(items[0].subtitle, "Creator")

    def test_subscriptions_uses_the_complete_authenticated_channels_feed(self) -> None:
        service = YouTubeService("firefox")
        service._run = Mock(  # type: ignore[method-assign]
            return_value={
                "entries": [
                    {
                        "id": "channel-1",
                        "title": "Creator",
                        "url": "https://www.youtube.com/channel/channel-1",
                        "thumbnails": [{"url": "//example/avatar.jpg"}],
                    }
                ]
            }
        )

        channels = service.subscriptions()

        self.assertEqual([channel.id for channel in channels], ["channel-1"])
        self.assertEqual(channels[0].title, "Creator")
        self.assertEqual(channels[0].thumbnail_url, "https://example/avatar.jpg")
        arguments = service._run.call_args.args  # type: ignore[union-attr]
        self.assertIn("https://www.youtube.com/feed/channels", arguments)
        self.assertIn("--flat-playlist", arguments)

    def test_personal_feed_page_requests_the_requested_window(self) -> None:
        service = YouTubeService("firefox")
        service._run = Mock(return_value={"entries": []})  # type: ignore[method-assign]

        service.personal_feed_page("home", 13, 12)

        arguments = service._run.call_args.args  # type: ignore[union-attr]
        self.assertEqual(arguments[arguments.index("--playlist-start") + 1], "13")
        self.assertEqual(arguments[arguments.index("--playlist-end") + 1], "24")

    def test_history_feed_resolves_channel_metadata_instead_of_staying_flat(self) -> None:
        service = YouTubeService("firefox")
        service._run = Mock(return_value={"entries": []})  # type: ignore[method-assign]

        service.personal_feed("history", limit=50)

        arguments = service._run.call_args.args  # type: ignore[union-attr]
        self.assertIn("--skip-download", arguments)
        self.assertNotIn("--flat-playlist", arguments)

    def test_download_options_excludes_generated_captions(self) -> None:
        service = YouTubeService("firefox")
        service._run = Mock(  # type: ignore[method-assign]
            return_value={
                "formats": [
                    {"height": 1080, "vcodec": "avc1"},
                    {"height": 720, "vcodec": "avc1"},
                    {"height": 720, "vcodec": "vp9"},
                    {
                        "format_id": "140",
                        "vcodec": "none",
                        "acodec": "mp4a.40.2",
                        "language": "en",
                        "format_note": "English original (default), medium",
                        "abr": 129,
                        "url": "https://example/original.m4a",
                    },
                    {
                        "format_id": "140-1",
                        "vcodec": "none",
                        "acodec": "mp4a.40.2",
                        "language": "ru",
                        "format_note": "Russian, medium",
                        "abr": 129,
                        "url": "https://example/russian.m4a",
                    },
                ],
                "language": "en",
                "subtitles": {},
                "automatic_captions": {"en": [{"url": "https://example/auto.vtt"}]},
            }
        )

        qualities, subtitles, audio_tracks = service.download_options(
            MediaItem("video", "Video", source="youtube")
        )

        self.assertEqual(qualities, ["1080p", "720p"])
        self.assertFalse(subtitles)
        self.assertEqual(
            [(track.language, track.original) for track in audio_tracks],
            [("en", True), ("ru", False)],
        )

    def test_channel_latest_maps_first_video_without_loading_channel_artwork(self) -> None:
        service = YouTubeService("firefox")
        service._run = Mock(  # type: ignore[method-assign]
            return_value={"entries": [{"id": "new", "title": "Newest", "channel": "Creator"}]}
        )

        item = service.channel_latest("https://www.youtube.com/channel/channel")

        self.assertIsNotNone(item)
        self.assertEqual(item.id, "new")  # type: ignore[union-attr]
        self.assertTrue(
            service._run.call_args.args[-1].endswith("/videos")  # type: ignore[union-attr]
        )

    def test_personal_feed_rejects_unknown_feed(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown YouTube feed"):
            YouTubeService("firefox").personal_feed("recommendations")

    def test_mark_watched_uses_authenticated_ytdlp_reporting(self) -> None:
        service = YouTubeService("firefox")
        service._run = Mock(return_value={})  # type: ignore[method-assign]
        item = MediaItem(
            "video",
            "Video",
            source="youtube",
            payload={"webpage_url": "https://www.youtube.com/watch?v=video"},
        )

        service.mark_watched(item)

        self.assertIn("--mark-watched", service._run.call_args.args)  # type: ignore[union-attr]
        self.assertIn("--simulate", service._run.call_args.args)  # type: ignore[union-attr]

    def test_recommendation_feedback_finds_matching_not_interested_token(self) -> None:
        initial = {
            "menu": {
                "listItemViewModel": {
                    "title": {"content": "Not interested"},
                    "rendererContext": {
                        "commandContext": {
                            "onTap": {
                                "innertubeCommand": {
                                    "feedbackEndpoint": {
                                        "contentId": "video-id",
                                        "feedbackToken": "feedback-token",
                                    }
                                }
                            }
                        }
                    },
                }
            }
        }
        html = f"<script>var ytInitialData = {json.dumps(initial)};</script>"

        token = YouTubeService._recommendation_feedback_token(html, "video-id")

        self.assertEqual(token, "feedback-token")

    def test_comments_limit_parent_threads_instead_of_replies(self) -> None:
        service = YouTubeService("firefox")
        service._run = Mock(  # type: ignore[method-assign]
            return_value={
                "comments": [
                    {"id": "one", "parent": "root", "author": "One", "text": "First"},
                    {"id": "two", "parent": "root", "author": "Two", "text": "Second"},
                ]
            }
        )

        page = service.comments(MediaItem("video", "Video", source="youtube"), page_size=20)

        extractor_arguments = service._run.call_args.args  # type: ignore[union-attr]
        self.assertIn(
            "youtube:max_comments=all,20,100,50,all;comment_sort=top;player_skip=configs",
            extractor_arguments,
        )
        self.assertEqual([comment.id for comment in page.comments], ["one", "two"])

    def test_comments_attach_replies_to_their_clickable_parent_thread(self) -> None:
        service = YouTubeService("firefox")
        service._run = Mock(  # type: ignore[method-assign]
            return_value={
                "comments": [
                    {"id": "parent", "parent": "root", "author": "One", "text": "First"},
                    {
                        "id": "reply",
                        "parent": "parent",
                        "author": "Two",
                        "text": "A reply",
                    },
                ]
            }
        )

        page = service.comments(MediaItem("video", "Video", source="youtube"))

        self.assertEqual([reply.id for reply in page.comments[0].replies], ["reply"])

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

    def test_search_renderer_maps_channel_navigation_and_avatar(self) -> None:
        item = YouTubeService._search_renderer_item(
            {
                "videoId": "abc123",
                "title": {"runs": [{"text": "A result"}]},
                "lengthText": {"simpleText": "1:02"},
                "longBylineText": {
                    "runs": [
                        {
                            "text": "Creator",
                            "navigationEndpoint": {
                                "browseEndpoint": {
                                    "browseId": "channel-id",
                                    "canonicalBaseUrl": "/@creator",
                                }
                            },
                        }
                    ]
                },
                "thumbnail": {"thumbnails": [{"url": "https://example/thumb.jpg"}]},
                "channelThumbnailSupportedRenderers": {
                    "channelThumbnailWithLinkRenderer": {
                        "thumbnail": {"thumbnails": [{"url": "https://example/avatar.jpg"}]}
                    }
                },
            }
        )

        self.assertEqual(item.duration_seconds, 62)
        self.assertEqual(item.payload["channel_url"], "https://www.youtube.com/@creator")
        self.assertEqual(item.payload["channel_avatar_url"], "https://example/avatar.jpg")

    def test_channel_renderer_maps_to_youtube_channel(self) -> None:
        item = YouTubeService._channel_renderer_item(
            {
                "channelId": "channel-id",
                "title": {"simpleText": "Creator"},
                "subscriberCountText": {"simpleText": "12K subscribers"},
                "videoCountText": {"runs": [{"text": "42 videos"}]},
                "navigationEndpoint": {
                    "browseEndpoint": {"canonicalBaseUrl": "/@creator"}
                },
                "thumbnail": {"thumbnails": [{"url": "https://example/avatar.jpg"}]},
            }
        )

        self.assertEqual(item.source, "youtube-channel")
        self.assertFalse(item.playable)
        self.assertEqual(item.title, "Creator")
        self.assertEqual(item.subtitle, "12K subscribers · 42 videos")
        self.assertEqual(item.payload["channel_url"], "https://www.youtube.com/@creator")

    def test_resolve_exposes_quality_and_caption_choices(self) -> None:
        service = YouTubeService()
        item = service._item({"id": "abc123", "title": "Video"})
        service._run = lambda *_args: {  # type: ignore[method-assign]
            "url": "https://video.example/auto.mp4",
            "http_headers": {"User-Agent": "TubeFin"},
            "formats": [
                {
                    "height": 720,
                    "vcodec": "avc1",
                    "acodec": "mp4a",
                    "url": "https://video.example/720.mp4",
                },
                {
                    "height": 1080,
                    "vcodec": "avc1",
                    "acodec": "none",
                    "url": "https://video.example/video-only.mp4",
                },
            ],
            "subtitles": {"en": [{"name": "English", "ext": "vtt", "url": "https://subs/en.vtt"}]},
            "automatic_captions": {
                "pl": [{"name": "Polish", "ext": "vtt", "url": "https://subs/pl.vtt"}]
            },
        }
        service._youtube_stream_responds = staticmethod(  # type: ignore[method-assign]
            lambda _url, _headers: True
        )

        stream = service.resolve(item)

        self.assertEqual([variant.label for variant in stream.variants], ["720p"])
        self.assertEqual([track.label for track in stream.subtitles], ["English", "Polish (auto)"])

    def test_details_turns_restrictions_into_clear_state(self) -> None:
        service = YouTubeService()
        item = MediaItem("video", "Restricted", source="youtube")

        def unavailable(*_args: str) -> dict[str, object]:
            raise ContentUnavailableError("Members only", Availability.MEMBERS_ONLY)

        service._run = unavailable  # type: ignore[method-assign]

        details = service.details(item)

        self.assertEqual(details.availability, Availability.MEMBERS_ONLY)
        self.assertEqual(details.availability_message, "Members only")


class SponsorBlockServiceTests(unittest.TestCase):
    @patch("urllib.request.urlopen")
    def test_lookup_hashes_the_video_id_and_maps_matching_skip_segments(
        self, urlopen: Mock
    ) -> None:
        video_id = "private-video-id"
        urlopen.return_value = FakeResponse(
            [
                {
                    "videoID": video_id,
                    "segments": [
                        {
                            "segment": [12.5, 42],
                            "category": "sponsor",
                            "actionType": "skip",
                        },
                        {
                            "segment": [50, 60],
                            "category": "interaction",
                            "actionType": "mute",
                        },
                    ],
                }
            ]
        )

        segments = SponsorBlockService().segments(video_id)

        self.assertEqual([(segment.start, segment.end) for segment in segments], [(12.5, 42)])
        request = urlopen.call_args.args[0]
        expected_prefix = hashlib.sha256(video_id.encode()).hexdigest()[:4]
        self.assertIn(f"/{expected_prefix}?", request.full_url)
        self.assertNotIn(video_id, request.full_url)


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

    @patch.object(JellyfinService, "_request")
    def test_quick_connect_initiates_and_builds_session(self, request: object) -> None:
        request.side_effect = [  # type: ignore[attr-defined]
            True,
            {"Secret": "secret", "Code": "ABC123"},
            {"Authenticated": True},
            {
                "User": {"Id": "user-id", "Name": "Ada"},
                "AccessToken": "token",
            },
        ]
        service = JellyfinService()

        server, secret, code = service.initiate_quick_connect("jellyfin.local:8096")
        session = service.complete_quick_connect(server, secret)

        self.assertEqual((server, secret, code), ("http://jellyfin.local:8096", "secret", "ABC123"))
        self.assertEqual(session.username, "Ada")
        self.assertEqual(session.access_token, "token")

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

    def test_thumbnail_prefers_landscape_jpeg(self) -> None:
        service = JellyfinService(JellyfinSession("https://media.example", "user", "Ada", "token"))

        item = service._item(
            {
                "Id": "movie-id",
                "Name": "Movie",
                "Type": "Movie",
                "ImageTags": {"Primary": "poster", "Thumb": "landscape"},
            }
        )

        self.assertIn("/Images/Thumb", item.thumbnail_url or "")
        self.assertIn("format=jpg", item.thumbnail_url or "")

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
    def test_playback_progress_is_reported_in_jellyfin_ticks(self, request: object) -> None:
        service = JellyfinService(
            JellyfinSession("https://media.example", "user", "Ada", "secret-token")
        )
        item = MediaItem("movie-id", "Movie", source="jellyfin")

        service.report_playback(item, 12.5, True, event="progress")

        request.assert_called_once_with(  # type: ignore[union-attr]
            "/Sessions/Playing/Progress",
            method="POST",
            body={
                "ItemId": "movie-id",
                "PositionTicks": 125_000_000,
                "IsPaused": True,
                "CanSeek": True,
                "PlayMethod": "DirectPlay",
            },
        )

    @patch.object(JellyfinService, "_request_current")
    def test_resolve_lists_external_and_embedded_subtitles(self, request: object) -> None:
        request.return_value = {  # type: ignore[attr-defined]
            "MediaSources": [
                {
                    "Id": "source-id",
                    "MediaStreams": [
                        {
                            "Type": "Subtitle",
                            "Index": 2,
                            "Language": "eng",
                            "DisplayTitle": "English",
                            "IsExternal": False,
                        },
                        {
                            "Type": "Subtitle",
                            "Index": 3,
                            "Language": "pol",
                            "DisplayTitle": "Polish",
                            "IsExternal": True,
                            "DeliveryUrl": "/subs/polish.vtt",
                        },
                    ],
                }
            ]
        }
        service = JellyfinService(
            JellyfinSession("https://media.example", "user", "Ada", "secret-token")
        )
        item = service._item({"Id": "movie-id", "Name": "Movie", "Type": "Movie"})

        stream = service.resolve(item)

        self.assertEqual([track.label for track in stream.subtitles], ["English", "Polish"])
        self.assertEqual(
            stream.subtitles[0].url,
            "https://media.example/Videos/movie-id/source-id/Subtitles/2/Stream.vtt",
        )
        self.assertEqual(stream.subtitles[1].url, "https://media.example/subs/polish.vtt")

    @patch.object(JellyfinService, "_request_current")
    def test_global_search_does_not_send_an_empty_parent_id(self, request: object) -> None:
        request.return_value = {"Items": []}  # type: ignore[attr-defined]
        service = JellyfinService(JellyfinSession("https://media.example", "user", "Ada", "token"))

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

    def test_player_settings_survive_session_changes(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": directory}),
        ):
            store = ConfigStore()
            store.save_player_settings(buffer_seconds=60)
            store.save_session(JellyfinSession("https://media.example", "user", "Ada", "token"))
            store.clear_session()

            self.assertEqual(
                store.load_player_settings(),
                {
                    "buffer_seconds": 60,
                    "default_caption_language": "",
                    "preferred_audio_language": "",
                    "sponsorblock_enabled": True,
                    "sponsorblock_categories": SPONSORBLOCK_DEFAULTS,
                },
            )
            self.assertIsNone(store.load_session())

    def test_player_settings_preserve_sponsorblock_preference(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": directory}),
        ):
            store = ConfigStore()
            store.save_player_settings(sponsorblock_enabled=False)
            store.save_player_settings(buffer_seconds=45)

            self.assertEqual(
                store.load_player_settings(),
                {
                    "buffer_seconds": 45,
                    "default_caption_language": "",
                    "preferred_audio_language": "",
                    "sponsorblock_enabled": False,
                    "sponsorblock_categories": SPONSORBLOCK_DEFAULTS,
                },
            )

    def test_player_settings_preserve_default_caption_language(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": directory}),
        ):
            store = ConfigStore()
            store.save_player_settings(default_caption_language=" English ")

            self.assertEqual(
                store.load_player_settings()["default_caption_language"],
                "English",
            )

    def test_player_settings_preserve_preferred_audio_language(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": directory}),
        ):
            store = ConfigStore()
            store.save_player_settings(preferred_audio_language=" Russian ")

            self.assertEqual(
                store.load_player_settings()["preferred_audio_language"],
                "Russian",
            )

    def test_player_settings_preserve_sponsorblock_category_behaviors(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": directory}),
        ):
            store = ConfigStore()
            store.save_player_settings(
                sponsorblock_categories={
                    "sponsor": "button",
                    "intro": "auto",
                    "filler": "ignore",
                    "not-a-category": "auto",
                }
            )

            settings = store.load_player_settings()

            self.assertEqual(
                settings["sponsorblock_categories"],
                {
                    **SPONSORBLOCK_DEFAULTS,
                    "sponsor": "button",
                    "intro": "auto",
                    "filler": "ignore",
                },
            )

    def test_synctube_identity_round_trip(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": directory}),
        ):
            store = ConfigStore()
            store.save_sync_settings("Ada", "#12ABef")

            self.assertEqual(
                store.load_sync_settings(),
                {"username": "Ada", "color": "#12abef"},
            )

    def test_youtube_sync_settings_default_and_round_trip(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": directory}),
        ):
            store = ConfigStore()

            self.assertEqual(
                store.load_youtube_sync_settings(),
                {
                    "subscriptions_enabled": True,
                    "history_enabled": True,
                    "history_limit": 100,
                },
            )

            store.save_youtube_sync_settings(
                subscriptions_enabled=False,
                history_enabled=False,
                history_limit=250,
            )
            store.save_youtube_browser("firefox")

            self.assertEqual(
                store.load_youtube_sync_settings(),
                {
                    "subscriptions_enabled": False,
                    "history_enabled": False,
                    "history_limit": 250,
                },
            )

    def test_youtube_history_sync_limit_is_clamped(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": directory}),
        ):
            store = ConfigStore()

            store.save_youtube_sync_settings(history_limit=5000)

            self.assertEqual(store.load_youtube_sync_settings()["history_limit"], 1000)

    def test_clear_all_removes_the_config_file(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": directory}),
        ):
            store = ConfigStore()
            store.save_youtube_browser("firefox")

            store.clear_all()

            self.assertFalse(store.path.exists())

    def test_oauth_config_contains_metadata_but_no_tokens(self) -> None:
        from tubefin.models import OAuthAccount

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": directory}),
        ):
            store = ConfigStore()
            account = OAuthAccount("account", "ada@example.com", "Ada", ["readonly"])
            store.save_oauth_client_id("client-id")
            store.save_oauth_account(account)

            raw = Path(store.path).read_text(encoding="utf-8")
            settings = store.load_oauth_settings()

            self.assertEqual(settings["accounts"], [account])
            self.assertNotIn("refresh_token", raw)
            self.assertNotIn("access_token", raw)

    def test_youtube_browser_session_is_persisted(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": directory}),
        ):
            store = ConfigStore()
            store.save_youtube_browser("firefox")

            self.assertEqual(store.load_oauth_settings()["browser"], "firefox")


if __name__ == "__main__":
    unittest.main()
