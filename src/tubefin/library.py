from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from tubefin.models import (
    ChannelDetails,
    ChannelSubscription,
    DownloadRecord,
    DownloadStatus,
    LocalPlaylist,
    MediaItem,
    PlaybackEntry,
)


def _data_root() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "tubefin"


def _cache_root() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "tubefin"


def _item(data: dict[str, Any]) -> MediaItem:
    fields = {
        "id",
        "title",
        "subtitle",
        "source",
        "kind",
        "thumbnail_url",
        "duration_seconds",
        "playable",
        "payload",
    }
    return MediaItem(**{key: value for key, value in data.items() if key in fields})


class JsonStore:
    """Small crash-safe JSON store shared by local-only TubeFin features."""

    def __init__(self, filename: str, directory: Path | None = None) -> None:
        self.directory = directory or _data_root()
        self.path = self.directory / filename

    def load(self, default: Any) -> Any:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def save(self, value: Any) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{self.path.name}-", dir=self.directory)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


class ChannelFeedCache:
    """Persistent latest-video cache for locally ranked channel shelves."""

    def __init__(self, directory: Path | None = None) -> None:
        self.store = JsonStore("channel-feeds.json", directory or _cache_root())
        self._values: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        if self._values is None:
            values = self.store.load({})
            self._values = values if isinstance(values, dict) else {}
        return self._values

    def get(self, channel_url: str) -> ChannelDetails | None:
        values = self._load()
        entry = values.get(channel_url)
        if not isinstance(entry, dict) or not isinstance(entry.get("channel"), dict):
            return None
        data = dict(entry["channel"])
        try:
            data["videos"] = [
                _item(value)
                for value in data.get("videos") or []
                if isinstance(value, dict)
            ]
            return ChannelDetails(**data)
        except (TypeError, ValueError):
            return None

    def put(self, channel: ChannelDetails) -> None:
        values = dict(self._load())
        values[channel.url] = {
            "updated_at": time.time(),
            "channel": asdict(channel),
        }
        newest = sorted(
            values.items(),
            key=lambda pair: float(pair[1].get("updated_at") or 0)
            if isinstance(pair[1], dict)
            else 0,
            reverse=True,
        )[:100]
        self._values = dict(newest)
        self.store.save(self._values)


class OfflineLibrary:
    def __init__(self, directory: Path | None = None) -> None:
        self.store = JsonStore("downloads.json", directory)
        self.download_directory = (directory or _data_root()) / "downloads"

    def list(self, search: str = "") -> list[DownloadRecord]:
        records = [self._record(value) for value in self.store.load([])]
        changed = self._reconcile(records)
        if changed:
            self.save_all(records)
        query = search.casefold().strip()
        if query:
            records = [
                record
                for record in records
                if query in record.item.title.casefold() or query in record.item.subtitle.casefold()
            ]
        return sorted(records, key=lambda record: record.updated_at, reverse=True)

    def get(self, record_id: str) -> DownloadRecord | None:
        return next((record for record in self.list() if record.id == record_id), None)

    def find_complete_for_item(self, item: MediaItem) -> DownloadRecord | None:
        """Return a usable local copy of the same source item, when available."""
        source = str(item.payload.get("original_source") or item.source)
        for record in self.list():
            record_source = str(
                record.item.payload.get("original_source") or record.item.source
            )
            if (
                record.status == DownloadStatus.COMPLETE
                and record_source == source
                and record.item.id == item.id
                and record.media_path
                and Path(record.media_path).is_file()
            ):
                return record
        return None

    def upsert(self, record: DownloadRecord) -> None:
        records = self.list()
        record.updated_at = time.time()
        for index, current in enumerate(records):
            if current.id == record.id:
                records[index] = record
                break
        else:
            if not record.created_at:
                record.created_at = record.updated_at
            records.append(record)
        self.save_all(records)

    def remove(self, record_id: str, *, delete_file: bool = True) -> None:
        records = self.list()
        record = next((value for value in records if value.id == record_id), None)
        if record and delete_file and record.media_path:
            path = Path(record.media_path)
            if path.is_file() and self.download_directory in path.parents:
                path.unlink()
            for metadata_name in (
                f"{record.item.id}.info.json",
                f"{record.item.id}.tubefin.json",
            ):
                metadata = Path(record.directory) / metadata_name
                if metadata.is_file() and self.download_directory in metadata.parents:
                    metadata.unlink()
        self.save_all([value for value in records if value.id != record_id])

    def storage_usage(self) -> int:
        return sum(
            Path(record.media_path).stat().st_size
            for record in self.list()
            if record.media_path and Path(record.media_path).is_file()
        )

    def find_moved(self, record_id: str, roots: list[Path] | None = None) -> Path | None:
        record = self.get(record_id)
        if not record:
            return None
        roots = roots or [self.download_directory]
        names = {Path(record.media_path).name, f"{record.item.id}.info.json"}
        for root in roots:
            if not root.exists():
                continue
            for candidate in root.rglob("*"):
                if candidate.name not in names:
                    continue
                media = candidate
                if candidate.suffixes[-2:] == [".info", ".json"]:
                    try:
                        data = json.loads(candidate.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        continue
                    if str(data.get("id")) != record.item.id:
                        continue
                    media = next(
                        (
                            sibling
                            for sibling in candidate.parent.glob(f"{candidate.stem[:-5]}.*")
                            if sibling != candidate and not sibling.name.endswith(".part")
                        ),
                        candidate,
                    )
                if media.is_file() and (media != candidate or not candidate.name.endswith(".json")):
                    record.media_path = str(media)
                    record.directory = str(media.parent)
                    record.status = DownloadStatus.COMPLETE
                    record.error = ""
                    self.upsert(record)
                    return media
        return None

    def save_all(self, records: list[DownloadRecord]) -> None:
        self.store.save([asdict(record) for record in records])

    @staticmethod
    def _record(data: dict[str, Any]) -> DownloadRecord:
        value = dict(data)
        value["item"] = _item(value["item"])
        try:
            value["status"] = DownloadStatus(value.get("status", DownloadStatus.QUEUED))
        except ValueError:
            value["status"] = DownloadStatus.FAILED
        return DownloadRecord(**value)

    @staticmethod
    def _reconcile(records: list[DownloadRecord]) -> bool:
        changed = False
        for record in records:
            existing_metadata = record.item.payload.get("download_metadata")
            metadata = (
                dict(existing_metadata) if isinstance(existing_metadata, dict) else {}
            )
            defaults = {
                "source": record.item.source,
                "source_url": str(record.item.payload.get("webpage_url") or ""),
                "channel": record.item.subtitle,
                "channel_id": str(record.item.payload.get("channel_id") or ""),
                "channel_url": str(record.item.payload.get("channel_url") or ""),
                "original_thumbnail_url": record.item.thumbnail_url or "",
            }
            for key, value in defaults.items():
                metadata.setdefault(key, value)
            local_thumbnail = str(metadata.get("local_thumbnail_url") or "")
            local_thumbnail_path: Path | None = None
            if local_thumbnail.startswith("file:"):
                local_thumbnail_path = Path(
                    urllib.request.url2pathname(urllib.parse.urlparse(local_thumbnail).path)
                )
            elif local_thumbnail:
                local_thumbnail_path = Path(local_thumbnail)
            if not local_thumbnail_path or not local_thumbnail_path.is_file():
                directory = Path(record.directory)
                thumbnails = [
                    path
                    for suffix in ("*.jpg", "*.jpeg", "*.webp", "*.png")
                    for path in directory.glob(suffix)
                    if path.is_file() and ".series." not in path.name
                ]
                if thumbnails:
                    local_thumbnail = max(
                        thumbnails, key=lambda path: path.stat().st_size
                    ).resolve().as_uri()
                    metadata["local_thumbnail_url"] = local_thumbnail
                    record.item.thumbnail_url = local_thumbnail
                    changed = True
            if metadata != existing_metadata:
                record.item.payload["download_metadata"] = metadata
                changed = True
            if record.status == DownloadStatus.COMPLETE and (
                not record.media_path or not Path(record.media_path).is_file()
            ):
                record.status = DownloadStatus.MISSING
                record.error = "The downloaded file was moved or deleted."
                changed = True
        return changed


class PlaylistStore:
    def __init__(self, directory: Path | None = None) -> None:
        self.store = JsonStore("playlists.json", directory)

    def list(self) -> list[LocalPlaylist]:
        result: list[LocalPlaylist] = []
        for data in self.store.load([]):
            value = dict(data)
            value["items"] = [_item(item) for item in value.get("items", [])]
            result.append(LocalPlaylist(**value))
        return sorted(result, key=lambda playlist: playlist.updated_at, reverse=True)

    def create(self, name: str) -> LocalPlaylist:
        now = time.time()
        playlist = LocalPlaylist(
            uuid.uuid4().hex, name.strip() or "Untitled playlist", [], now, now
        )
        self._upsert(playlist)
        return playlist

    def rename(self, playlist_id: str, name: str) -> LocalPlaylist:
        playlist = self._require(playlist_id)
        playlist.name = name.strip() or playlist.name
        self._upsert(playlist)
        return playlist

    def delete(self, playlist_id: str) -> None:
        self._save([value for value in self.list() if value.id != playlist_id])

    def export_file(self, path: Path) -> int:
        """Export all local playlists to a portable JSON file."""
        playlists = self.list()
        payload = {
            "format": "tubefin-playlists",
            "version": 1,
            "playlists": [asdict(playlist) for playlist in playlists],
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return len(playlists)

    def import_file(self, path: Path) -> int:
        """Merge playlists from a TubeFin export, preserving existing entries."""
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw_playlists = payload.get("playlists") if isinstance(payload, dict) else payload
        if not isinstance(raw_playlists, list):
            raise ValueError("This file does not contain TubeFin playlists.")

        existing = self.list()
        used_ids = {playlist.id for playlist in existing}
        imported: list[LocalPlaylist] = []
        for raw in raw_playlists:
            if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
                raise ValueError("This file contains an invalid playlist.")
            raw_items = raw.get("items", [])
            if not isinstance(raw_items, list):
                raise ValueError("This file contains an invalid playlist.")
            try:
                items = [_item(item) for item in raw_items if isinstance(item, dict)]
            except (TypeError, ValueError) as error:
                raise ValueError("This file contains an invalid media item.") from error
            playlist_id = str(raw.get("id") or uuid.uuid4().hex)
            if playlist_id in used_ids:
                playlist_id = uuid.uuid4().hex
            used_ids.add(playlist_id)
            now = time.time()
            imported.append(
                LocalPlaylist(
                    playlist_id,
                    raw["name"].strip() or "Untitled playlist",
                    items,
                    float(raw.get("created_at") or now),
                    now,
                )
            )
        self._save([*existing, *imported])
        return len(imported)

    def add(self, playlist_id: str, item: MediaItem) -> LocalPlaylist:
        playlist = self._require(playlist_id)
        playlist.items.append(item)
        self._upsert(playlist)
        return playlist

    def remove(self, playlist_id: str, index: int) -> LocalPlaylist:
        playlist = self._require(playlist_id)
        if 0 <= index < len(playlist.items):
            playlist.items.pop(index)
            self._upsert(playlist)
        return playlist

    def reorder(self, playlist_id: str, old: int, new: int) -> LocalPlaylist:
        playlist = self._require(playlist_id)
        if 0 <= old < len(playlist.items) and 0 <= new < len(playlist.items):
            playlist.items.insert(new, playlist.items.pop(old))
            self._upsert(playlist)
        return playlist

    def _require(self, playlist_id: str) -> LocalPlaylist:
        playlist = next((value for value in self.list() if value.id == playlist_id), None)
        if not playlist:
            raise KeyError(f"Unknown playlist: {playlist_id}")
        return playlist

    def _upsert(self, playlist: LocalPlaylist) -> None:
        playlists = self.list()
        playlist.updated_at = time.time()
        for index, value in enumerate(playlists):
            if value.id == playlist.id:
                playlists[index] = playlist
                break
        else:
            playlists.append(playlist)
        self._save(playlists)

    def _save(self, playlists: list[LocalPlaylist]) -> None:
        self.store.save([asdict(playlist) for playlist in playlists])


class HistoryStore:
    def __init__(self, directory: Path | None = None) -> None:
        self.store = JsonStore("history.json", directory)

    def list(self, limit: int = 50) -> list[PlaybackEntry]:
        entries: list[PlaybackEntry] = []
        for data in self.store.load([]):
            value = dict(data)
            value["item"] = _item(value["item"])
            entries.append(PlaybackEntry(**value))
        return sorted(entries, key=lambda entry: entry.updated_at, reverse=True)[:limit]

    def record(self, item: MediaItem, position: float, duration: float) -> None:
        entries = self.list(500)
        key = (item.source, item.id)
        entries = [entry for entry in entries if (entry.item.source, entry.item.id) != key]
        entries.append(PlaybackEntry(item, max(0, position), max(0, duration), time.time()))
        self.store.save([asdict(entry) for entry in entries[-500:]])

    def resume_position(self, item: MediaItem) -> float:
        entry = next(
            (
                value
                for value in self.list(500)
                if value.item.source == item.source and value.item.id == item.id
            ),
            None,
        )
        if (
            not entry
            or entry.position < 30
            or entry.duration and entry.position >= entry.duration * 0.9
        ):
            return 0.0
        return entry.position

    def merge_remote(self, items: list[MediaItem]) -> int:
        """Add remote history without replacing locally measured playback progress."""
        entries = self.list(500)
        indexed = {(entry.item.source, entry.item.id): entry for entry in entries}
        changed = 0
        now = time.time()
        for index, item in enumerate(items):
            key = (item.source, item.id)
            if not item.id:
                continue
            if existing := indexed.get(key):
                old_score = self._metadata_score(existing.item)
                new_score = self._metadata_score(item)
                if new_score > old_score:
                    existing.item = item
                    changed += 1
                continue
            entry = PlaybackEntry(
                item,
                0,
                float(item.duration_seconds or 0),
                now - index * 0.001,
            )
            entries.append(entry)
            indexed[key] = entry
            changed += 1
        if changed:
            entries.sort(key=lambda entry: entry.updated_at)
            self.store.save([asdict(entry) for entry in entries[-500:]])
        return changed

    @staticmethod
    def _metadata_score(item: MediaItem) -> int:
        channel = item.subtitle.strip()
        return sum(
            (
                bool(channel and channel.casefold() != "youtube"),
                bool(item.payload.get("channel_url")),
                bool(item.payload.get("channel_id")),
                bool(item.payload.get("channel_avatar_url")),
                bool(item.thumbnail_url),
            )
        )

    def continue_watching(self, limit: int = 12) -> list[MediaItem]:
        return [
            entry.item
            for entry in self.list(100)
            if entry.position >= 30
            and (not entry.duration or entry.position < entry.duration * 0.9)
        ][:limit]

    def recent_channels(self, limit: int = 12) -> list[str]:
        channels: list[str] = []
        for entry in self.list(200):
            channel = str(entry.item.payload.get("channel_url") or "")
            if channel and channel not in channels:
                channels.append(channel)
        return channels[:limit]


class SubscriptionStore:
    def __init__(self, directory: Path | None = None) -> None:
        self.store = JsonStore("subscriptions.json", directory)

    def list(self) -> list[ChannelSubscription]:
        subscriptions: list[ChannelSubscription] = []
        for value in self.store.load([]):
            try:
                subscriptions.append(ChannelSubscription(**value))
            except (TypeError, ValueError):
                continue
        return sorted(subscriptions, key=lambda item: item.title.casefold())

    def get(self, channel_id: str) -> ChannelSubscription | None:
        return next((item for item in self.list() if item.channel_id == channel_id), None)

    def subscribe(self, subscription: ChannelSubscription) -> ChannelSubscription:
        values = [item for item in self.list() if item.channel_id != subscription.channel_id]
        subscription.updated_at = time.time()
        values.append(subscription)
        self._save(values)
        return subscription

    def unsubscribe(self, channel_id: str) -> None:
        self._save([item for item in self.list() if item.channel_id != channel_id])

    def merge(self, subscriptions: list[ChannelSubscription]) -> int:
        values = {item.channel_id: item for item in self.list()}
        now = time.time()
        for subscription in subscriptions:
            subscription.updated_at = now
            values[subscription.channel_id] = subscription
        self._save(list(values.values()))
        return len(subscriptions)

    def set_notifications(self, channel_id: str, enabled: bool) -> ChannelSubscription:
        subscription = self.get(channel_id)
        if not subscription:
            raise KeyError(channel_id)
        subscription.notifications = enabled
        return self.subscribe(subscription)

    def mark_seen(self, channel_id: str, video_id: str) -> None:
        subscription = self.get(channel_id)
        if subscription and video_id and subscription.last_seen_video_id != video_id:
            subscription.last_seen_video_id = video_id
            self.subscribe(subscription)

    def mark_seen_many(self, videos: dict[str, str]) -> None:
        if not videos:
            return
        subscriptions = self.list()
        changed = False
        now = time.time()
        for subscription in subscriptions:
            video_id = videos.get(subscription.channel_id, "")
            if video_id and subscription.last_seen_video_id != video_id:
                subscription.last_seen_video_id = video_id
                subscription.updated_at = now
                changed = True
        if changed:
            self._save(subscriptions)

    def _save(self, subscriptions: list[ChannelSubscription]) -> None:
        self.store.save([asdict(item) for item in subscriptions])
