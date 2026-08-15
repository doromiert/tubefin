from __future__ import annotations

from dataclasses import dataclass, field
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


@dataclass(slots=True)
class JellyfinSession:
    server_url: str
    user_id: str
    username: str
    access_token: str
