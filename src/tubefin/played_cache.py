from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from contextlib import suppress
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from tubefin.models import (
    AudioTrack,
    MediaItem,
    ResolvedStream,
    StreamVariant,
    SubtitleTrack,
    VideoChapter,
)
from tubefin.streaming import PrebufferedStream


class PlayedVideoCache:
    """Persist details and a media prefix only after a video actually plays."""

    def __init__(self, max_bytes: int = 4 << 30) -> None:
        cache_root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        self.directory = cache_root / "tubefin" / "played-videos"
        self.max_bytes = max_bytes
        self.lock = threading.Lock()
        self.cached_this_session: set[str] = set()
        with suppress(OSError):
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._prune_locked()

    @staticmethod
    def key(item: MediaItem) -> str:
        return f"{item.source}:{item.id}"

    @staticmethod
    def _signature(stream: ResolvedStream) -> str:
        parsed = urlparse(stream.url)
        query = parse_qs(parsed.query)
        return "|".join(
            (
                stream.default_label,
                query.get("itag", [""])[0],
                query.get("mime", [""])[0],
                query.get("clen", [""])[0],
            )
        )

    @staticmethod
    def _digest(key: str) -> str:
        return hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()

    def _paths(self, key: str) -> tuple[Path, Path]:
        digest = self._digest(key)
        return (
            self.directory / f"{digest}.bin",
            self.directory / f"{digest}.json",
        )

    def details(self, item: MediaItem) -> tuple[str, str, list[VideoChapter]] | None:
        key = self.key(item)
        data_path, meta_path = self._paths(key)
        with self.lock:
            try:
                metadata = json.loads(meta_path.read_text(encoding="utf-8"))
                if metadata.get("key") != key:
                    return None
                chapters = [
                    VideoChapter(
                        title=str(chapter.get("title") or "Chapter"),
                        start=float(chapter.get("start") or 0),
                        end=float(chapter.get("end") or 0),
                    )
                    for chapter in metadata.get("chapters") or []
                    if isinstance(chapter, dict)
                ]
                now = time.time()
                os.utime(meta_path, (now, now))
                if data_path.exists():
                    os.utime(data_path, (now, now))
                return (
                    str(metadata.get("description") or ""),
                    str(metadata.get("published_date") or ""),
                    chapters,
                )
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                return None

    @staticmethod
    def _stream_metadata(stream: ResolvedStream) -> dict[str, object]:
        return {
            "url": stream.url,
            "headers": stream.headers,
            "default_label": stream.default_label,
            "variants": [
                {"label": value.label, "url": value.url, "headers": value.headers}
                for value in stream.variants
            ],
            "subtitles": [
                {"label": value.label, "language": value.language, "url": value.url}
                for value in stream.subtitles
            ],
            "audio_tracks": [
                {
                    "label": value.label,
                    "language": value.language,
                    "url": value.url,
                    "headers": value.headers,
                    "format_id": value.format_id,
                    "original": value.original,
                }
                for value in stream.audio_tracks
            ],
        }

    @staticmethod
    def _cached_stream(
        metadata: dict[str, object]
    ) -> tuple[ResolvedStream, bool] | None:
        stored = metadata.get("stream")
        if not isinstance(stored, dict):
            return None
        url = str(stored.get("url") or "")
        if not url:
            return None
        query = parse_qs(urlparse(url).query)
        expires = query.get("expire", [""])[0]
        saved_at = metadata.get("saved_at")
        if expires.isdigit():
            expired = int(expires) <= int(time.time()) + 5 * 60
        else:
            expired = (
                not isinstance(saved_at, (int, float))
                or time.time() - saved_at > 30 * 60
            )
        try:
            headers = {
                str(name): str(value)
                for name, value in dict(stored.get("headers") or {}).items()
            }
            variants = [
                StreamVariant(
                    label=str(value.get("label") or "Auto"),
                    url=str(value.get("url") or ""),
                    headers={
                        str(name): str(header)
                        for name, header in dict(value.get("headers") or {}).items()
                    },
                )
                for value in stored.get("variants") or []
                if isinstance(value, dict) and value.get("url")
            ]
            subtitles = [
                SubtitleTrack(
                    label=str(value.get("label") or "Subtitles"),
                    language=str(value.get("language") or ""),
                    url=str(value.get("url") or ""),
                )
                for value in stored.get("subtitles") or []
                if isinstance(value, dict) and value.get("url")
            ]
            audio_tracks = [
                AudioTrack(
                    label=str(value.get("label") or "Audio"),
                    language=str(value.get("language") or ""),
                    url=str(value.get("url") or ""),
                    headers={
                        str(name): str(header)
                        for name, header in dict(value.get("headers") or {}).items()
                    },
                    format_id=str(value.get("format_id") or ""),
                    original=bool(value.get("original")),
                )
                for value in stored.get("audio_tracks") or []
                if isinstance(value, dict)
            ]
        except (TypeError, ValueError):
            return None
        chapters = [
            VideoChapter(
                title=str(value.get("title") or "Chapter"),
                start=float(value.get("start") or 0),
                end=float(value.get("end") or 0),
            )
            for value in metadata.get("chapters") or []
            if isinstance(value, dict)
        ]
        return (
            ResolvedStream(
                url=url,
                headers=headers,
                variants=variants,
                subtitles=subtitles,
                audio_tracks=audio_tracks,
                default_label=str(stored.get("default_label") or "Auto"),
                description=str(metadata.get("description") or ""),
                published_date=str(metadata.get("published_date") or ""),
                chapters=chapters,
            ),
            expired,
        )

    def prepare_cached(
        self, item: MediaItem
    ) -> tuple[ResolvedStream, PrebufferedStream, bool] | None:
        """Return cached media and whether its stored stream URL needs refresh."""
        if item.source != "youtube":
            return None
        key = self.key(item)
        data_path, meta_path = self._paths(key)
        with self.lock:
            try:
                metadata = json.loads(meta_path.read_text(encoding="utf-8"))
                if metadata.get("key") != key:
                    return None
                cached_stream = self._cached_stream(metadata)
                if cached_stream is None:
                    return None
                stream, expired = cached_stream
                prefix = data_path.read_bytes()
                if not prefix:
                    return None
                now = time.time()
                os.utime(data_path, (now, now))
                os.utime(meta_path, (now, now))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                return None
        buffered = PrebufferedStream(
            stream,
            seconds=10,
            max_bytes=max(64 << 10, len(prefix)),
        )
        buffered.prefix = prefix
        buffered.content_type = str(
            metadata.get("content_type") or "application/octet-stream"
        )
        total = metadata.get("total")
        buffered.total = int(total) if isinstance(total, int) else None
        return stream, buffered, expired

    def refresh_stream(
        self,
        item: MediaItem,
        stream: ResolvedStream,
        buffered: PrebufferedStream,
    ) -> None:
        """Refresh expiring stream metadata while retaining the cached prefix."""
        self._store(item, stream, buffered)

    def prepare(
        self,
        item: MediaItem,
        stream: ResolvedStream,
    ) -> PrebufferedStream | None:
        key = self.key(item)
        data_path, meta_path = self._paths(key)
        with self.lock:
            try:
                metadata = json.loads(meta_path.read_text(encoding="utf-8"))
                if (
                    metadata.get("key") != key
                    or metadata.get("signature") != self._signature(stream)
                ):
                    return None
                prefix = data_path.read_bytes()
                if not prefix:
                    return None
                now = time.time()
                os.utime(data_path, (now, now))
                os.utime(meta_path, (now, now))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                return None
        buffered = PrebufferedStream(
            stream,
            seconds=10,
            max_bytes=max(64 << 10, len(prefix)),
        )
        buffered.prefix = prefix
        buffered.content_type = str(
            metadata.get("content_type") or "application/octet-stream"
        )
        total = metadata.get("total")
        buffered.total = int(total) if isinstance(total, int) else None
        return buffered

    def cache_played(self, item: MediaItem, stream: ResolvedStream) -> None:
        key = self.key(item)
        with self.lock:
            if key in self.cached_this_session:
                return
            self.cached_this_session.add(key)
        try:
            target_bytes = 10 * (400 << 10)
            buffered = self.prepare(item, stream)
            if buffered is None or len(buffered.prefix) < target_bytes:
                completed = PrebufferedStream(
                    stream,
                    seconds=10,
                    max_bytes=target_bytes,
                )
                completed.warm()
                if buffered is None or len(completed.prefix) > len(buffered.prefix):
                    buffered = completed
            self._store(item, stream, buffered)
        finally:
            with self.lock:
                self.cached_this_session.discard(key)

    def cache_buffered(
        self,
        item: MediaItem,
        buffered: PrebufferedStream,
    ) -> None:
        key = self.key(item)
        with self.lock:
            if key in self.cached_this_session:
                return
            self.cached_this_session.add(key)
        try:
            target_bytes = 10 * (400 << 10)
            if len(buffered.prefix) < target_bytes:
                completed = PrebufferedStream(
                    buffered.stream,
                    seconds=10,
                    max_bytes=target_bytes,
                )
                completed.warm()
                if len(completed.prefix) > len(buffered.prefix):
                    buffered = completed
            self._store(item, buffered.stream, buffered)
        finally:
            with self.lock:
                self.cached_this_session.discard(key)

    def _store(
        self,
        item: MediaItem,
        stream: ResolvedStream,
        buffered: PrebufferedStream,
    ) -> None:
        key = self.key(item)
        data_path, meta_path = self._paths(key)
        suffix = f".{threading.get_ident()}.part"
        data_temp = data_path.with_name(data_path.name + suffix)
        meta_temp = meta_path.with_name(meta_path.name + suffix)
        metadata = {
            "key": key,
            "signature": self._signature(stream),
            "saved_at": time.time(),
            "description": stream.description,
            "published_date": stream.published_date,
            "chapters": [
                {"title": chapter.title, "start": chapter.start, "end": chapter.end}
                for chapter in stream.chapters
            ],
            "content_type": buffered.content_type,
            "total": buffered.total,
            "stream": self._stream_metadata(stream) if item.source == "youtube" else None,
        }
        with self.lock:
            try:
                self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                if buffered.prefix:
                    data_temp.write_bytes(buffered.prefix)
                    data_temp.replace(data_path)
                meta_temp.write_text(
                    json.dumps(metadata, ensure_ascii=False),
                    encoding="utf-8",
                )
                meta_temp.replace(meta_path)
                self._prune_locked(exclude=data_path)
            except OSError:
                with suppress(FileNotFoundError):
                    data_temp.unlink()
                with suppress(FileNotFoundError):
                    meta_temp.unlink()

    def _prune_locked(self, exclude: Path | None = None) -> None:
        entries: list[tuple[float, int, Path]] = []
        total = 0
        for path in self.directory.glob("*.bin"):
            with suppress(OSError):
                stat = path.stat()
                total += stat.st_size
                if path != exclude:
                    entries.append((stat.st_mtime, stat.st_size, path))
        for _modified, size, path in sorted(entries):
            if total <= self.max_bytes:
                break
            with suppress(OSError):
                path.unlink()
                with suppress(FileNotFoundError):
                    path.with_suffix(".json").unlink()
                total -= size
