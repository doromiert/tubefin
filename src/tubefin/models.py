from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


@dataclass(slots=True)
class MediaItem:
    id: str
    title: str
    subtitle: str = ""
    source: str = ""
    kind: str = "video"
    thumbnail_url: str | None = None
    duration_seconds: int | None = None
    playable: bool = True
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_label(self) -> str:
        if not self.duration_seconds:
            return ""
        hours, remainder = divmod(self.duration_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"


@dataclass(slots=True)
class MediaSection:
    title: str
    items: list[MediaItem]


@dataclass(slots=True)
class ResolvedStream:
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    variants: list[StreamVariant] = field(default_factory=list)
    subtitles: list[SubtitleTrack] = field(default_factory=list)
    audio_tracks: list[AudioTrack] = field(default_factory=list)
    default_label: str = "Auto"
    description: str = ""
    published_date: str = ""


@dataclass(slots=True)
class StreamVariant:
    label: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class SubtitleTrack:
    label: str
    language: str
    url: str


@dataclass(slots=True)
class AudioTrack:
    label: str
    language: str
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    format_id: str = ""
    original: bool = False


class Availability(StrEnum):
    PUBLIC = "public"
    UNAVAILABLE = "unavailable"
    AGE_RESTRICTED = "age_restricted"
    MEMBERS_ONLY = "members_only"
    PRIVATE = "private"


@dataclass(slots=True)
class VideoDetails:
    item: MediaItem
    description: str = ""
    upload_date: str = ""
    view_count: int | None = None
    like_count: int | None = None
    tags: list[str] = field(default_factory=list)
    availability: Availability = Availability.PUBLIC
    availability_message: str = ""


@dataclass(slots=True)
class ChannelDetails:
    id: str
    title: str
    url: str
    description: str = ""
    subscriber_count: int | None = None
    avatar_url: str | None = None
    videos: list[MediaItem] = field(default_factory=list)
    continuation: str | None = None


@dataclass(slots=True)
class Comment:
    id: str
    author: str
    text: str
    timestamp: int | None = None
    like_count: int = 0
    parent_id: str | None = None
    replies: list[Comment] = field(default_factory=list)


@dataclass(slots=True)
class CommentPage:
    comments: list[Comment]
    next_cursor: str | None = None


@dataclass(slots=True, frozen=True)
class SponsorSegment:
    start: float
    end: float
    category: str = "sponsor"


class DownloadStatus(StrEnum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"
    MISSING = "missing"


@dataclass(slots=True)
class DownloadRecord:
    id: str
    item: MediaItem
    directory: str
    media_path: str = ""
    quality: str = "best"
    audio_only: bool = False
    audio_tracks: list[str] = field(default_factory=list)
    status: DownloadStatus = DownloadStatus.QUEUED
    progress: float = 0.0
    error: str = ""
    bytes_downloaded: int = 0
    total_bytes: int | None = None
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass(slots=True)
class LocalPlaylist:
    id: str
    name: str
    items: list[MediaItem] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass(slots=True)
class PlaybackEntry:
    item: MediaItem
    position: float
    duration: float
    updated_at: float


@dataclass(slots=True)
class OAuthAccount:
    id: str
    email: str
    display_name: str
    scopes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class YouTubeBrowserSession:
    browser: str
    channel_id: str
    display_name: str
    channel_url: str = ""
    handle: str = ""


@dataclass(slots=True)
class ChannelSubscription:
    channel_id: str
    title: str
    url: str
    avatar_url: str = ""
    notifications: bool = True
    last_seen_video_id: str = ""
    updated_at: float = 0.0


@dataclass(slots=True)
class JellyfinSession:
    server_url: str
    user_id: str
    username: str
    access_token: str
