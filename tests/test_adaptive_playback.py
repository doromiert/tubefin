from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from tubefin.models import MediaItem
from tubefin.services import YouTubeService
from tubefin.youtube_login import save_cookies


class AdaptivePlaybackTests(unittest.TestCase):
    def test_signed_in_playback_keeps_public_4k_hdr_with_working_audio(self):
        with tempfile.TemporaryDirectory(prefix="tubefin-account-formats-") as root:
            cookie = Path(root) / "cookies.txt"
            cookie.write_text("# Netscape HTTP Cookie File\n")
            service = YouTubeService(cookie_file=cookie)
            service.max_height = 2160
            service.resume_position = Mock(return_value=2891)
            account = {
                "description": "Account metadata",
                "formats": [
                    {"url": "account-audio", "vcodec": "none", "acodec": "aac"},
                    {"url": "account-1080", "height": 1080, "vcodec": "avc1", "acodec": "aac"},
                ],
            }
            public = {
                "formats": [
                    {"url": "public-audio", "vcodec": "none", "acodec": "opus"},
                    {"url": "public-4k", "height": 2160, "vcodec": "vp9",
                     "acodec": "none", "dynamic_range": "HDR10"},
                ],
            }
            service._run = Mock(side_effect=lambda *_args, **kwargs:
                                public if kwargs.get("authenticated") is False else account)
            service._youtube_stream_responds = lambda url, _headers: url != "account-audio"
            stream = service.resolve(MediaItem("test", "Test", source="youtube"))
            self.assertEqual(stream.default_label, "Auto · 2160p HDR")
            self.assertEqual(stream.audio_url, "public-audio")
            self.assertEqual(stream.description, "Account metadata")
            self.assertEqual(stream.resume_position, 2891)
            self.assertIn("1080p", [variant.label for variant in stream.variants])

    def test_resume_reads_only_the_current_players_saved_position(self):
        import json
        player = {
            "videoDetails": {"videoId": "test", "lengthSeconds": "5000"},
            "playerConfig": {"playbackStartConfig": {"startSeconds": 2891}},
        }
        html = ('var ytInitialData = {"chapter": {"startTimeSeconds": 900}}; '
                'var ytInitialPlayerResponse = ' + json.dumps(player) + ';')
        self.assertEqual(YouTubeService._resume_from_watch_page(html, "test"), 2891)
        self.assertEqual(YouTubeService._resume_from_watch_page(html, "other"), 0)
        for value in (None, -1, "NaN", 5000, "invalid"):
            player["playerConfig"]["playbackStartConfig"]["startSeconds"] = value
            html = 'ytInitialPlayerResponse = ' + json.dumps(player)
            self.assertEqual(YouTubeService._resume_from_watch_page(html, "test"), 0)
        player["playerConfig"]["playbackStartConfig"] = {
            "startPosition": {"streamTimeMillis": "2891000"},
        }
        self.assertEqual(YouTubeService._resume_from_watch_page(
            'ytInitialPlayerResponse = ' + json.dumps(player), "test"), 2891)

    def resolve(self, height: int):
        service = YouTubeService()
        service.max_height = height
        service._run = Mock(
            return_value={
                "formats": [
                    {"url": "dead-mux", "height": 720, "vcodec": "avc1", "acodec": "aac"},
                    *(
                        {
                            "url": f"video-{h}",
                            "height": h,
                            "vcodec": "vp9",
                            "acodec": "none",
                            "dynamic_range": "HDR10" if h >= 2160 else "SDR",
                        }
                        for h in (360, 720, 1080, 1440, 2160, 4320)
                    ),
                    {"url": "audio", "vcodec": "none", "acodec": "opus", "abr": 160},
                ],
            }
        )
        service._youtube_stream_responds = lambda url, _headers: url != "dead-mux"
        return service.resolve(MediaItem("test", "Test", source="youtube"))

    def test_screen_default_preserves_all_manual_qualities(self):
        for screen_height, expected in (
            (1080, 1080),
            (2160, 2160),
            (2880, 2160),
            (4320, 4320),
            (240, 360),
        ):
            with self.subTest(screen_height=screen_height):
                stream = self.resolve(screen_height)
                self.assertEqual(stream.url, f"video-{expected}")
                self.assertEqual(stream.audio_url, "audio")
                self.assertEqual(len(stream.variants), 6)
                self.assertEqual(stream.variants[0].label, "4320p HDR")
                self.assertTrue(all(variant.audio_url == "audio" for variant in stream.variants))

    def test_login_exports_only_service_cookies_with_private_permissions(self):
        def cookie(domain, name):
            value = Mock()
            value.get_domain.return_value = domain
            value.get_name.return_value = name
            value.get_value.return_value = "test-value"
            value.get_path.return_value = "/"
            value.get_secure.return_value = True
            value.get_http_only.return_value = True
            value.get_expires.return_value = None
            return value

        with tempfile.TemporaryDirectory(prefix="tubefin-login-test-") as root:
            destination = Path(root) / "cookies.txt"
            self.assertFalse(save_cookies([cookie(".example.com", "SAPISID")], destination))
            self.assertFalse(destination.exists())
            self.assertTrue(
                save_cookies(
                    [
                        cookie(".youtube.com", "SAPISID"),
                        cookie(".google.com", "SID"),
                        cookie(".youtube.com.attacker.test", "secret"),
                    ],
                    destination,
                )
            )
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("attacker", destination.read_text())
            import http.cookiejar

            jar = http.cookiejar.MozillaCookieJar(str(destination))
            jar.load(ignore_discard=True)
            self.assertEqual({item.name for item in jar}, {"SAPISID", "SID"})


if __name__ == "__main__":
    unittest.main()
