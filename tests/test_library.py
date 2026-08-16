from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tubefin.library import (
    ChannelFeedCache,
    HistoryStore,
    OfflineLibrary,
    PlaylistStore,
    SubscriptionStore,
)
from tubefin.models import (
    ChannelDetails,
    ChannelSubscription,
    DownloadRecord,
    DownloadStatus,
    MediaItem,
)


class LocalLibraryTests(unittest.TestCase):
    def test_channel_feed_cache_round_trip_preserves_video_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = ChannelFeedCache(Path(directory))
            channel = ChannelDetails(
                "channel-id",
                "Channel",
                "https://youtube.example/channel",
                avatar_url="https://img.example/avatar.jpg",
                videos=[
                    MediaItem(
                        "video-id",
                        "Video",
                        subtitle="Channel",
                        source="youtube",
                        payload={"channel_id": "channel-id"},
                    )
                ],
            )

            cache.put(channel)

            self.assertEqual(cache.get(channel.url), channel)

    def test_mixed_source_playlist_can_be_reordered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = PlaylistStore(Path(directory))
            playlist = store.create("Mixed")
            store.add(playlist.id, MediaItem("yt", "YouTube", source="youtube"))
            store.add(playlist.id, MediaItem("jf", "Jellyfin", source="jellyfin"))

            store.reorder(playlist.id, 1, 0)

            self.assertEqual(
                [item.source for item in store.list()[0].items], ["jellyfin", "youtube"]
            )

    def test_playlists_can_be_exported_and_imported_without_overwriting(self) -> None:
        with (
            tempfile.TemporaryDirectory() as source_directory,
            tempfile.TemporaryDirectory() as destination_directory,
        ):
            source = PlaylistStore(Path(source_directory))
            playlist = source.create("Favorites")
            source.add(playlist.id, MediaItem("video", "Video", source="youtube"))
            export_path = Path(source_directory) / "playlists.json"

            self.assertEqual(source.export_file(export_path), 1)

            destination = PlaylistStore(Path(destination_directory))
            destination.create("Existing")
            self.assertEqual(destination.import_file(export_path), 1)
            loaded = destination.list()
            self.assertEqual({value.name for value in loaded}, {"Existing", "Favorites"})
            imported = next(value for value in loaded if value.name == "Favorites")
            self.assertEqual([item.id for item in imported.items], ["video"])

    def test_missing_download_is_reconciled_without_losing_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            library = OfflineLibrary(Path(directory))
            record = DownloadRecord(
                "download",
                MediaItem("video", "Video", source="youtube"),
                directory,
                media_path=str(Path(directory) / "missing.mp4"),
                status=DownloadStatus.COMPLETE,
            )
            library.upsert(record)

            loaded = library.get(record.id)

            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.status, DownloadStatus.MISSING)  # type: ignore[union-attr]
            self.assertEqual(loaded.item.title, "Video")  # type: ignore[union-attr]

    def test_history_drives_continue_watching_and_local_channels(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            history = HistoryStore(Path(directory))
            item = MediaItem(
                "video",
                "Video",
                source="youtube",
                payload={"channel_url": "https://youtube.example/channel"},
            )
            history.record(item, 60, 600)

            self.assertEqual(history.continue_watching(), [item])
            self.assertEqual(history.recent_channels(), ["https://youtube.example/channel"])

    def test_remote_history_merge_preserves_local_progress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            history = HistoryStore(Path(directory))
            local = MediaItem("local", "Local", source="youtube")
            history.record(local, 60, 600)

            changed = history.merge_remote(
                [
                    MediaItem(
                        "local",
                        "Local",
                        subtitle="Local Creator",
                        source="youtube",
                        payload={"channel_url": "https://youtube.example/@local"},
                    ),
                    MediaItem(
                        "remote",
                        "Remote",
                        subtitle="Creator",
                        source="youtube",
                        thumbnail_url="https://example/thumbnail.jpg",
                    ),
                ]
            )

            entries = {entry.item.id: entry for entry in history.list()}
            self.assertEqual(changed, 2)
            self.assertEqual(entries["local"].position, 60)
            self.assertEqual(entries["local"].item.subtitle, "Local Creator")
            self.assertEqual(entries["remote"].position, 0)
            self.assertEqual(entries["remote"].item.subtitle, "Creator")

    def test_resume_position_only_returns_meaningful_unfinished_progress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            history = HistoryStore(Path(directory))
            early = MediaItem("early", "Early", source="youtube")
            resumable = MediaItem("resume", "Resume", source="youtube")
            finished = MediaItem("finished", "Finished", source="youtube")
            history.record(early, 20, 600)
            history.record(resumable, 125, 600)
            history.record(finished, 550, 600)

            self.assertEqual(history.resume_position(early), 0)
            self.assertEqual(history.resume_position(resumable), 125)
            self.assertEqual(history.resume_position(finished), 0)

    def test_channel_subscriptions_persist_notification_preferences(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SubscriptionStore(Path(directory))
            store.subscribe(
                ChannelSubscription("channel", "Creator", "https://youtube.example/channel")
            )

            store.set_notifications("channel", False)
            store.mark_seen("channel", "new-video")

            loaded = store.get("channel")
            self.assertIsNotNone(loaded)
            self.assertFalse(loaded.notifications)  # type: ignore[union-attr]
            self.assertEqual(loaded.last_seen_video_id, "new-video")  # type: ignore[union-attr]

    def test_online_subscriptions_can_be_merged_with_local_subscriptions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SubscriptionStore(Path(directory))
            store.subscribe(
                ChannelSubscription("local", "Local", "https://youtube.example/local")
            )

            store.merge(
                [ChannelSubscription("online", "Online", "https://youtube.example/online")]
            )

            self.assertEqual(
                {subscription.channel_id for subscription in store.list()},
                {"local", "online"},
            )


if __name__ == "__main__":
    unittest.main()
