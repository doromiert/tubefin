from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from tubefin.library import OfflineLibrary
from tubefin.models import DownloadRecord, DownloadStatus, MediaItem
from tubefin.services import ServiceError

ProgressCallback = Callable[[DownloadRecord], None]
DirectResolver = Callable[
    [MediaItem], tuple[str, dict[str, str], dict[str, str]]
]


class DownloadManager:
    """Run bounded, cancellable yt-dlp downloads backed by the offline library."""

    def __init__(
        self,
        library: OfflineLibrary,
        concurrency: int = 2,
        *,
        browser: str = "",
        direct_resolver: DirectResolver | None = None,
    ) -> None:
        self.library = library
        self.executable = shutil.which("yt-dlp")
        self.concurrency = max(1, min(concurrency, 4))
        self.browser = browser
        self.direct_resolver = direct_resolver
        self._semaphore = threading.BoundedSemaphore(self.concurrency)
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._callbacks: dict[str, ProgressCallback] = {}
        self._lock = threading.Lock()
        self._cancelled: set[str] = set()
        self._shutdown = False

    def enqueue(
        self,
        item: MediaItem,
        *,
        quality: str = "best",
        audio_only: bool = False,
        audio_tracks: list[str] | None = None,
        captions: bool = True,
        callback: ProgressCallback | None = None,
    ) -> DownloadRecord:
        if self._shutdown:
            raise ServiceError("The download manager is shutting down.")
        if item.source not in {"youtube", "jellyfin"}:
            raise ServiceError("This source cannot be downloaded for offline playback.")
        if item.source == "youtube" and not self.executable:
            raise ServiceError("yt-dlp is not installed.")
        if item.source == "jellyfin" and not self.direct_resolver:
            raise ServiceError("Connect to Jellyfin before downloading this item.")
        record_id = uuid.uuid4().hex
        directory = self.library.download_directory / record_id
        source_url = str(item.payload.get("webpage_url") or item.payload.get("source_url") or "")
        if item.source == "youtube" and not source_url:
            source_url = f"https://www.youtube.com/watch?v={item.id}"
        download_metadata = {
            "source": item.source,
            "source_url": source_url,
            "channel": item.subtitle,
            "channel_id": str(item.payload.get("channel_id") or ""),
            "channel_url": str(item.payload.get("channel_url") or ""),
            "original_thumbnail_url": (
                "" if item.source == "jellyfin" else item.thumbnail_url or ""
            ),
            "audio_tracks": list(audio_tracks or []),
        }
        if item.source == "jellyfin":
            download_metadata.update(
                {
                    "kind": item.kind,
                    "media_details": dict(item.payload),
                    "series_metadata": dict(item.payload.get("series_metadata") or {}),
                    "season_metadata": dict(item.payload.get("season_metadata") or {}),
                }
            )
        stored_item = replace(
            item,
            thumbnail_url=None if item.source == "jellyfin" else item.thumbnail_url,
            payload={
                **item.payload,
                "webpage_url": source_url,
                "source_url": source_url,
                "download_metadata": download_metadata,
            },
        )
        record = DownloadRecord(
            id=record_id,
            item=stored_item,
            directory=str(directory),
            quality="original" if item.source == "jellyfin" else quality,
            audio_only=audio_only,
            audio_tracks=list(audio_tracks or []),
            created_at=time.time(),
            updated_at=time.time(),
        )
        self.library.upsert(record)
        if callback:
            self._callbacks[record_id] = callback
        target = self._download_direct if item.source == "jellyfin" else self._download
        threading.Thread(
            target=target,
            args=(record_id, captions),
            daemon=True,
            name=f"download-{record_id[:8]}",
        ).start()
        return record

    def retry(self, record_id: str, callback: ProgressCallback | None = None) -> None:
        if self._shutdown:
            raise ServiceError("The download manager is shutting down.")
        record = self.library.get(record_id)
        if not record:
            raise KeyError(record_id)
        if callback:
            self._callbacks[record_id] = callback
        self._cancelled.discard(record_id)
        record.status = DownloadStatus.QUEUED
        record.error = ""
        self.library.upsert(record)
        target = (
            self._download_direct
            if record.item.source == "jellyfin"
            else self._download
        )
        threading.Thread(
            target=target,
            args=(record_id, True),
            daemon=True,
            name=f"download-{record_id[:8]}",
        ).start()

    def cancel(self, record_id: str) -> None:
        self._cancelled.add(record_id)
        with self._lock:
            process = self._processes.get(record_id)
        if process and process.poll() is None:
            process.terminate()
        record = self.library.get(record_id)
        if record:
            record.status = DownloadStatus.CANCELLED
            record.error = "Cancelled"
            self.library.upsert(record)
            self._notify(record)

    def _download_direct(self, record_id: str, _captions: bool) -> None:
        with self._semaphore:
            record = self.library.get(record_id)
            if not record or record_id in self._cancelled or self._shutdown:
                return
            if not self.direct_resolver:
                record.status = DownloadStatus.FAILED
                record.error = "Reconnect to Jellyfin and retry this download."
                self.library.upsert(record)
                self._notify(record)
                return
            directory = Path(record.directory)
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            record.status = DownloadStatus.DOWNLOADING
            record.error = ""
            self.library.upsert(record)
            self._notify(record)
            media_temp: Path | None = None
            try:
                url, headers, artwork_urls = self.direct_resolver(record.item)
                media_sources = record.item.payload.get("MediaSources") or []
                container = str(
                    (media_sources[0] if media_sources else {}).get("Container") or "mkv"
                ).split(",", 1)[0]
                media_path = directory / f"{record.item.id}.{container}"
                media_temp = media_path.with_suffix(f".{container}.part")
                request = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(request, timeout=30) as response, media_temp.open(
                    "wb"
                ) as output:
                    try:
                        total = int(response.headers.get("Content-Length") or 0) or None
                    except ValueError:
                        total = None
                    downloaded = 0
                    last_update = 0.0
                    while chunk := response.read(1024 * 1024):
                        if self._shutdown or record_id in self._cancelled:
                            raise InterruptedError("Download cancelled")
                        output.write(chunk)
                        downloaded += len(chunk)
                        record.bytes_downloaded = downloaded
                        record.total_bytes = total
                        record.progress = downloaded * 100 / total if total else 0
                        now = time.monotonic()
                        if now - last_update >= 0.4:
                            self.library.upsert(record)
                            self._notify(record)
                            last_update = now
                media_temp.replace(media_path)
                record.media_path = str(media_path)
                artwork_url = artwork_urls.get("item", "")
                if artwork_url:
                    artwork_path = directory / f"{record.item.id}.jpg"
                    artwork_request = urllib.request.Request(artwork_url, headers=headers)
                    with urllib.request.urlopen(artwork_request, timeout=20) as response:
                        artwork_path.write_bytes(response.read(12 * 1024 * 1024))
                    record.item.thumbnail_url = artwork_path.resolve().as_uri()
                metadata = dict(record.item.payload.get("download_metadata") or {})
                metadata["local_thumbnail_url"] = record.item.thumbnail_url or ""
                series_artwork_url = artwork_urls.get("series", "")
                if series_artwork_url:
                    series_artwork_path = directory / f"{record.item.id}.series.jpg"
                    series_request = urllib.request.Request(series_artwork_url, headers=headers)
                    with urllib.request.urlopen(series_request, timeout=20) as response:
                        series_artwork_path.write_bytes(response.read(12 * 1024 * 1024))
                    metadata["local_series_artwork_url"] = (
                        series_artwork_path.resolve().as_uri()
                    )
                record.item.payload["download_metadata"] = metadata
                record.status = DownloadStatus.COMPLETE
                record.progress = 100.0
                record.error = ""
                sidecar = directory / f"{record.item.id}.tubefin.json"
                sidecar.write_text(
                    json.dumps(exported_metadata(record), ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                sidecar.chmod(0o600)
            except InterruptedError:
                record.status = DownloadStatus.CANCELLED
                record.error = "Cancelled"
                if media_temp:
                    media_temp.unlink(missing_ok=True)
            except (OSError, urllib.error.URLError, TimeoutError, ServiceError) as error:
                record.status = DownloadStatus.FAILED
                record.error = str(error)
                if media_temp:
                    media_temp.unlink(missing_ok=True)
            self.library.upsert(record)
            self._notify(record)

    def _download(self, record_id: str, captions: bool) -> None:
        with self._semaphore:
            if self._shutdown:
                return
            record = self.library.get(record_id)
            if not record or record.status == DownloadStatus.CANCELLED:
                return
            directory = Path(record.directory)
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            record.status = DownloadStatus.DOWNLOADING
            self.library.upsert(record)
            self._notify(record)
            video_url = str(
                record.item.payload.get("webpage_url")
                or f"https://www.youtube.com/watch?v={record.item.id}"
            )
            template = str(directory / f"{record.item.id}.%(ext)s")
            arguments = [
                str(self.executable),
                "--newline",
                "--no-color",
                "--no-playlist",
                "--write-info-json",
                "--write-thumbnail",
                "--embed-metadata",
                "--embed-thumbnail",
                "--progress-template",
                "download:%(progress._percent_str)s|%(progress.downloaded_bytes)s|%(progress.total_bytes,progress.total_bytes_estimate)s",
                "--output",
                template,
            ]
            if self.browser:
                arguments[1:1] = ["--cookies-from-browser", self.browser]
            selected_audio = [track for track in record.audio_tracks if track]
            if record.audio_only:
                # Always transcode to m4a so thumbnail embedding is supported.
                # A single native audio stream would otherwise stay in a .webm
                # container, which yt-dlp cannot embed a thumbnail into.
                arguments += ["--extract-audio", "--audio-format", "m4a"]
                if selected_audio:
                    arguments += [
                        "--audio-multistreams",
                        "--format",
                        "+".join(selected_audio),
                    ]
            elif selected_audio:
                if record.quality in {"min", "minimum", "worst"}:
                    selector = "+".join(["worstvideo", *selected_audio])
                elif record.quality not in {"", "best", "auto"}:
                    height = "".join(
                        character for character in record.quality if character.isdigit()
                    )
                    selector = "+".join(
                        [f"bestvideo[height<={height}]", *selected_audio]
                    )
                else:
                    selector = "+".join(["bestvideo", *selected_audio])
                arguments += [
                    "--audio-multistreams",
                    "--format",
                    selector,
                    "--merge-output-format",
                    "mkv",
                ]
            elif record.quality in {"min", "minimum", "worst"}:
                arguments += [
                    "--format",
                    "worstvideo+worstaudio/worst",
                    "--merge-output-format",
                    "mp4",
                ]
            elif record.quality not in {"", "best", "auto"}:
                height = "".join(character for character in record.quality if character.isdigit())
                arguments += [
                    "--format",
                    f"bestvideo[height<={height}]+bestaudio/best[height<={height}]",
                    "--merge-output-format",
                    "mp4",
                ]
            else:
                arguments += [
                    "--format",
                    "bestvideo+bestaudio/best",
                    "--merge-output-format",
                    "mp4",
                ]
            if captions:
                arguments += [
                    "--write-subs",
                    "--all-subs",
                    "--sub-format",
                    "vtt/best",
                    "--embed-subs",
                ]
            arguments.append(video_url)

            if self._shutdown:
                return

            process = subprocess.Popen(
                arguments,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            with self._lock:
                if self._shutdown:
                    process.terminate()
                else:
                    self._processes[record_id] = process
            if self._shutdown:
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
                return
            output: list[str] = []
            assert process.stdout
            for line in process.stdout:
                output.append(line.rstrip())
                match = re.search(r"([\d.]+)%\|(\d+|NA)\|(\d+|NA)", line)
                if not match:
                    continue
                record.progress = min(100.0, float(match.group(1)))
                record.bytes_downloaded = int(match.group(2)) if match.group(2) != "NA" else 0
                record.total_bytes = int(match.group(3)) if match.group(3) != "NA" else None
                self.library.upsert(record)
                self._notify(record)
            return_code = process.wait()
            with self._lock:
                self._processes.pop(record_id, None)
            if self._shutdown:
                return
            latest = self.library.get(record_id)
            if latest and latest.status == DownloadStatus.CANCELLED:
                return
            if return_code:
                record.status = DownloadStatus.FAILED
                record.error = next(
                    (line.removeprefix("ERROR: ") for line in reversed(output) if line),
                    "Download failed.",
                )
            else:
                candidates = [
                    path
                    for path in directory.iterdir()
                    if path.is_file()
                    and not path.name.endswith((".json", ".jpg", ".webp", ".png", ".vtt", ".part"))
                ]
                record.media_path = (
                    str(max(candidates, key=lambda path: path.stat().st_size)) if candidates else ""
                )
                thumbnails = [
                    path
                    for path in directory.iterdir()
                    if path.is_file()
                    and path.suffix.casefold() in {".jpg", ".jpeg", ".webp", ".png"}
                ]
                if thumbnails:
                    record.item.thumbnail_url = max(
                        thumbnails, key=lambda path: path.stat().st_size
                    ).resolve().as_uri()
                metadata = dict(record.item.payload.get("download_metadata") or {})
                metadata["local_thumbnail_url"] = record.item.thumbnail_url or ""
                record.item.payload["download_metadata"] = metadata
                record.status = (
                    DownloadStatus.COMPLETE if record.media_path else DownloadStatus.FAILED
                )
                record.progress = 100.0 if record.media_path else record.progress
                record.error = "" if record.media_path else "The downloader produced no media file."
                if record.media_path:
                    try:
                        sidecar = directory / f"{record.item.id}.tubefin.json"
                        sidecar.write_text(
                            json.dumps(exported_metadata(record), ensure_ascii=False, indent=2)
                            + "\n",
                            encoding="utf-8",
                        )
                        sidecar.chmod(0o600)
                    except OSError:
                        pass
            self.library.upsert(record)
            self._notify(record)

    def shutdown(self) -> None:
        """Stop active processes and prevent queued workers from touching disk."""
        self._shutdown = True
        with self._lock:
            processes = list(self._processes.values())
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)

    def _notify(self, record: DownloadRecord) -> None:
        callback = self._callbacks.get(record.id)
        if callback:
            callback(record)


def exported_metadata(record: DownloadRecord) -> dict[str, object]:
    """Stable metadata useful for importing an offline record elsewhere."""
    return {
        "schema": 1,
        "id": record.item.id,
        "source": record.item.source,
        "source_url": str(
            record.item.payload.get("webpage_url")
            or record.item.payload.get("source_url")
            or (record.item.payload.get("download_metadata") or {}).get("source_url")
            or ""
        ),
        "title": record.item.title,
        "channel": record.item.subtitle,
        "channel_id": str(record.item.payload.get("channel_id") or ""),
        "channel_url": str(record.item.payload.get("channel_url") or ""),
        "thumbnail_url": record.item.thumbnail_url or "",
        "duration": record.item.duration_seconds,
        "media_path": os.path.basename(record.media_path),
        "payload": json.loads(json.dumps(record.item.payload)),
    }
