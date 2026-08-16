from __future__ import annotations

import secrets
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from tubefin.models import MediaItem, ResolvedStream


class StreamProxy:
    """Expose an authenticated upstream stream to GIO on loopback."""

    def __init__(self, upstream_url: str, headers: dict[str, str]) -> None:
        self.upstream_url = upstream_url
        self.headers = headers
        self.token = secrets.token_urlsafe(24)
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> str:
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_HEAD(self) -> None:  # noqa: N802
                self._forward(send_body=False)

            def do_GET(self) -> None:  # noqa: N802
                self._forward(send_body=True)

            def _forward(self, *, send_body: bool) -> None:
                if self.path.removeprefix("/") != proxy.token:
                    self.send_error(404)
                    return

                headers = {
                    name: value
                    for name, value in proxy.headers.items()
                    if name.lower() not in {"host", "content-length", "connection"}
                }
                headers["Accept-Encoding"] = "identity"
                if not send_body:
                    # Googlevideo rejects HEAD. A one-byte GET gives us the same
                    # metadata while keeping GIO's initial size probe working.
                    headers["Range"] = "bytes=0-0"
                elif byte_range := self.headers.get("Range"):
                    headers["Range"] = byte_range

                request = urllib.request.Request(
                    proxy.upstream_url,
                    headers=headers,
                    method="GET",
                )
                try:
                    upstream = urllib.request.urlopen(request, timeout=30)
                except urllib.error.HTTPError as error:
                    upstream = error
                except (OSError, urllib.error.URLError, TimeoutError):
                    self.send_error(502, "Upstream media request failed")
                    return

                try:
                    response_status = (
                        200 if not send_body and upstream.status == 206 else upstream.status
                    )
                    self.send_response(response_status)
                    for name in (
                        "Accept-Ranges",
                        "Cache-Control",
                        "Content-Length",
                        "Content-Range",
                        "Content-Type",
                        "ETag",
                        "Last-Modified",
                    ):
                        if name == "Content-Range" and not send_body:
                            continue
                        if name == "Content-Length" and not send_body:
                            content_range = upstream.headers.get("Content-Range", "")
                            value = content_range.rpartition("/")[2]
                        else:
                            value = upstream.headers.get(name)
                        if value:
                            self.send_header(name, value)
                    self.send_header("Connection", "close")
                    self.end_headers()

                    if send_body:
                        while chunk := upstream.read(256 * 1024):
                            self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    upstream.close()

            def log_message(self, _format: str, *_args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
            name="stream-proxy",
        )
        self.thread.start()
        port = self.server.server_address[1]
        return f"http://127.0.0.1:{port}/{self.token}"

    def stop(self) -> None:
        if not self.server:
            return
        server = self.server
        self.server = None

        def shutdown() -> None:
            server.shutdown()
            with suppress(OSError):
                server.server_close()

        threading.Thread(target=shutdown, daemon=True).start()


class PrebufferedStream:
    """A resolved stream with a bounded prefix warmed for immediate playback."""

    def __init__(
        self,
        stream: ResolvedStream,
        seconds: int = 10,
        max_bytes: int | None = None,
    ) -> None:
        self.stream = stream
        self.seconds = max(1, min(seconds, 10))
        max_bytes = max_bytes or self.seconds * (400 << 10)
        self.max_bytes = max(64 << 10, min(max_bytes, 8 << 20))
        self.prefix = b""
        self.content_type = "application/octet-stream"
        self.total: int | None = None
        self.proxy: StreamProxy | None = None
        self.upstream_ready: threading.Event | None = None

    def warm(
        self, finish_early: Callable[[], bool] | None = None
    ) -> None:
        headers = dict(self.stream.headers)
        headers["Range"] = f"bytes=0-{self.max_bytes - 1}"
        headers["Accept-Encoding"] = "identity"
        request = urllib.request.Request(self.stream.url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                chunks: list[bytes] = []
                received = 0
                while received < self.max_bytes:
                    chunk = response.read(
                        min(64 << 10, self.max_bytes - received)
                    )
                    if not chunk:
                        break
                    chunks.append(chunk)
                    received += len(chunk)
                    if finish_early is not None and finish_early():
                        break
                self.prefix = b"".join(chunks)
                self.content_type = response.headers.get("Content-Type", self.content_type)
                content_range = response.headers.get("Content-Range", "")
                total = content_range.rpartition("/")[2]
                self.total = int(total) if total.isdigit() else None
        except (OSError, urllib.error.URLError, TimeoutError):
            self.prefix = b""

    def playback_stream(self) -> ResolvedStream:
        if not self.prefix:
            return self.stream
        upstream = self

        class PrefixProxy(StreamProxy):
            def start(inner_self) -> str:
                token = secrets.token_urlsafe(24)

                class Handler(BaseHTTPRequestHandler):
                    protocol_version = "HTTP/1.1"

                    def do_HEAD(self) -> None:  # noqa: N802
                        self._serve(False)

                    def do_GET(self) -> None:  # noqa: N802
                        self._serve(True)

                    def _serve(self, body: bool) -> None:
                        if self.path.removeprefix("/") != token:
                            self.send_error(404)
                            return
                        requested = self.headers.get("Range", "")
                        match = requested.removeprefix("bytes=").partition("-")[0]
                        start = int(match) if match.isdigit() else 0
                        if start >= len(upstream.prefix):
                            self._forward_range(start, body)
                            return
                        remainder_start = len(upstream.prefix)
                        length = (upstream.total - start) if upstream.total is not None else None
                        self.send_response(206 if requested else 200)
                        self.send_header("Accept-Ranges", "bytes")
                        self.send_header("Content-Type", upstream.content_type)
                        if requested and upstream.total is not None:
                            self.send_header(
                                "Content-Range",
                                f"bytes {start}-{upstream.total - 1}/{upstream.total}",
                            )
                        if length is not None:
                            self.send_header("Content-Length", str(length))
                        self.send_header("Connection", "close")
                        self.end_headers()
                        if body:
                            remainder = None
                            try:
                                self.wfile.write(upstream.prefix[start:])
                                self.wfile.flush()
                                if upstream.total is None or remainder_start < upstream.total:
                                    stream = upstream.wait_for_upstream()
                                    headers = dict(stream.headers)
                                    headers["Range"] = f"bytes={remainder_start}-"
                                    headers["Accept-Encoding"] = "identity"
                                    remainder = urllib.request.urlopen(
                                        urllib.request.Request(
                                            stream.url, headers=headers
                                        ),
                                        timeout=30,
                                    )
                                    while chunk := remainder.read(256 << 10):
                                        self.wfile.write(chunk)
                            except (
                                BrokenPipeError,
                                ConnectionResetError,
                                OSError,
                                urllib.error.URLError,
                                TimeoutError,
                            ):
                                pass
                            finally:
                                if remainder:
                                    remainder.close()

                    def _forward_range(self, start: int, body: bool) -> None:
                        stream = upstream.wait_for_upstream()
                        headers = dict(stream.headers)
                        headers["Range"] = f"bytes={start}-"
                        headers["Accept-Encoding"] = "identity"
                        try:
                            response = urllib.request.urlopen(
                                urllib.request.Request(stream.url, headers=headers),
                                timeout=30,
                            )
                        except (OSError, urllib.error.URLError, TimeoutError):
                            self.send_error(502)
                            return
                        self.send_response(response.status)
                        for name in (
                            "Accept-Ranges",
                            "Content-Length",
                            "Content-Range",
                            "Content-Type",
                        ):
                            if value := response.headers.get(name):
                                self.send_header(name, value)
                        self.send_header("Connection", "close")
                        self.end_headers()
                        if body:
                            try:
                                while chunk := response.read(256 << 10):
                                    self.wfile.write(chunk)
                            except (BrokenPipeError, ConnectionResetError):
                                pass
                        response.close()

                    def log_message(self, _format: str, *_args: object) -> None:
                        pass

                inner_self.token = token
                inner_self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
                inner_self.server.daemon_threads = True
                inner_self.thread = threading.Thread(
                    target=inner_self.server.serve_forever, daemon=True
                )
                inner_self.thread.start()
                return f"http://127.0.0.1:{inner_self.server.server_address[1]}/{token}"

        self.proxy = PrefixProxy(self.stream.url, self.stream.headers)
        return ResolvedStream(
            self.proxy.start(),
            variants=self.stream.variants,
            subtitles=self.stream.subtitles,
            audio_tracks=self.stream.audio_tracks,
            default_label=self.stream.default_label,
            description=self.stream.description,
            published_date=self.stream.published_date,
            chapters=self.stream.chapters,
        )

    @property
    def upstream_is_deferred(self) -> bool:
        return self.upstream_ready is not None and not self.upstream_ready.is_set()

    def defer_upstream(self) -> None:
        self.upstream_ready = threading.Event()

    def replace_upstream(self, stream: ResolvedStream) -> None:
        self.stream = stream
        if self.upstream_ready:
            self.upstream_ready.set()

    def release_deferred_upstream(self) -> None:
        if self.upstream_ready:
            self.upstream_ready.set()

    def wait_for_upstream(self) -> ResolvedStream:
        if self.upstream_ready:
            self.upstream_ready.wait(90)
        return self.stream

    def close(self) -> None:
        if self.proxy:
            self.proxy.stop()
            self.proxy = None


class PrebufferManager:
    """Resolve and warm a bounded, replaceable working set in memory."""

    def __init__(
        self,
        concurrency: int = 2,
        capacity: int = 6,
        *,
        seconds: int = 10,
        memory_budget_bytes: int | None = None,
    ) -> None:
        self.capacity = max(1, capacity)
        self.seconds = max(1, min(seconds, 10))
        self.memory_budget_bytes = (
            max(0, memory_budget_bytes)
            if memory_budget_bytes is not None
            else None
        )
        self.semaphore = threading.BoundedSemaphore(max(1, min(concurrency, 3)))
        self.sidecar_semaphore = threading.BoundedSemaphore(2)
        self.entries: OrderedDict[str, tuple[float, Future[PrebufferedStream]]] = OrderedDict()
        self.sidecars: OrderedDict[str, Future[Any]] = OrderedDict()
        self.claimed_streams: set[Future[PrebufferedStream]] = set()
        self.claimed_sidecars: set[Future[Any]] = set()
        self.active: list[PrebufferedStream] = []
        self.lock = threading.Lock()
        self.closed = False

    @staticmethod
    def key(item: MediaItem) -> str:
        return f"{item.source}:{item.id}"

    def offer(
        self,
        item: MediaItem,
        resolver: Callable[[MediaItem], ResolvedStream],
        *,
        max_bytes: int | None = None,
    ) -> None:
        key = self.key(item)
        with self.lock:
            if self.closed or self.memory_budget_bytes == 0:
                return
            if key in self.entries:
                self.entries.move_to_end(key)
                return
            future: Future[PrebufferedStream] = Future()
            self.entries[key] = (time.monotonic(), future)
            while len(self.entries) > self.capacity:
                _old_key, (_created, old) = self.entries.popitem(last=False)
                if old.done() and not old.cancelled():
                    with suppress(Exception):
                        old.result().close()
                else:
                    old.cancel()
        threading.Thread(
            target=self._run_warm,
            args=(future, item, resolver, max_bytes),
            daemon=True,
            name=f"prebuffer-{item.id[:12]}",
        ).start()

    def offer_sidecar(
        self,
        item: MediaItem,
        resolver: Callable[[MediaItem], Any],
    ) -> None:
        key = self.key(item)
        with self.lock:
            if self.closed or self.memory_budget_bytes == 0:
                return
            if key in self.sidecars:
                self.sidecars.move_to_end(key)
                return
            future: Future[Any] = Future()
            self.sidecars[key] = future
            while len(self.sidecars) > self.capacity:
                _old_key, old = self.sidecars.popitem(last=False)
                old.cancel()
        threading.Thread(
            target=self._run_sidecar,
            args=(future, item, resolver),
            daemon=True,
            name=f"prefetch-metadata-{item.id[:12]}",
        ).start()

    def _run_sidecar(
        self,
        future: Future[Any],
        item: MediaItem,
        resolver: Callable[[MediaItem], Any],
    ) -> None:
        if not future.set_running_or_notify_cancel():
            return
        try:
            with self.sidecar_semaphore:
                key = self.key(item)
                with self.lock:
                    retained = (
                        self.sidecars.get(key) is future
                        or future in self.claimed_sidecars
                    )
                if self.closed or not retained:
                    raise RuntimeError("Prefetching stopped")
                value = resolver(item)
            future.set_result(value)
        except Exception as error:
            future.set_exception(error)

    def _run_warm(
        self,
        future: Future[PrebufferedStream],
        item: MediaItem,
        resolver: Callable[[MediaItem], ResolvedStream],
        max_bytes: int | None,
    ) -> None:
        if not future.set_running_or_notify_cancel():
            return
        try:
            acquired = False
            while not acquired:
                acquired = self.semaphore.acquire(timeout=0.05)
                if acquired or self._stream_is_claimed(future):
                    break
                if self.closed or not self._stream_is_retained(future):
                    raise RuntimeError("Prebuffering stopped")
            try:
                if self.closed or not self._stream_is_retained(future):
                    raise RuntimeError("Prebuffering stopped")
                buffered = self._warm(
                    item,
                    resolver,
                    seconds=self.seconds,
                    max_bytes=max_bytes,
                    finish_early=lambda: self._stream_is_claimed(future),
                )
            finally:
                if acquired:
                    self.semaphore.release()
            if self.closed:
                buffered.close()
            future.set_result(buffered)
        except Exception as error:
            future.set_exception(error)

    def take(self, item: MediaItem) -> ResolvedStream | None:
        key = self.key(item)
        with self.lock:
            entry = self.entries.get(key)
            if entry is None or not entry[1].done():
                return None
        future = self.claim(item)
        if future is None:
            return None
        try:
            return self.activate(future.result())
        except Exception:
            return None

    def claim(
        self, item: MediaItem
    ) -> Future[PrebufferedStream] | None:
        with self.lock:
            entry = self.entries.pop(self.key(item), None)
            if entry is None:
                return None
            _created, future = entry
            if future.cancelled():
                return None
            self.claimed_streams.add(future)
        future.add_done_callback(self._stream_finished)
        return future

    def _stream_finished(self, future: Future[PrebufferedStream]) -> None:
        with self.lock:
            self.claimed_streams.discard(future)

    def _stream_is_claimed(
        self, future: Future[PrebufferedStream]
    ) -> bool:
        with self.lock:
            return future in self.claimed_streams

    def _stream_is_retained(
        self, future: Future[PrebufferedStream]
    ) -> bool:
        with self.lock:
            return future in self.claimed_streams or any(
                candidate is future
                for _created, candidate in self.entries.values()
            )

    def activate(self, buffered: PrebufferedStream) -> ResolvedStream:
        for active in self.active:
            active.close()
        self.active = [buffered]
        return buffered.playback_stream()

    def take_sidecar(self, item: MediaItem) -> Future[Any] | None:
        with self.lock:
            future = self.sidecars.pop(self.key(item), None)
            if future is not None and not future.cancelled():
                self.claimed_sidecars.add(future)
        if future is None or future.cancelled():
            return None
        future.add_done_callback(self._sidecar_finished)
        return future

    def _sidecar_finished(self, future: Future[Any]) -> None:
        with self.lock:
            self.claimed_sidecars.discard(future)

    def release_sidecar(self, future: Future[Any]) -> None:
        with self.lock:
            self.claimed_sidecars.discard(future)
        future.cancel()

    def reconcile(
        self,
        items: list[MediaItem],
        resolver: Callable[[MediaItem], ResolvedStream],
        sidecar_resolver: Callable[[MediaItem], Any] | None = None,
    ) -> None:
        unique: list[MediaItem] = []
        desired: set[str] = set()
        for item in items:
            key = self.key(item)
            if key in desired:
                continue
            desired.add(key)
            unique.append(item)
            if len(unique) >= self.capacity:
                break
        with self.lock:
            stale = [key for key in self.entries if key not in desired]
            for key in stale:
                _created, future = self.entries.pop(key)
                self._discard_future(future)
            stale_sidecars = [
                key for key in self.sidecars if key not in desired
            ]
            for key in stale_sidecars:
                self.sidecars.pop(key).cancel()
            budget = self.memory_budget_bytes
        if not unique or budget == 0:
            return
        target_bytes = self.seconds * (400 << 10)
        if budget is not None:
            per_item_budget = budget // self.capacity
            if sidecar_resolver is not None:
                # Keep room for the first comments page and resolved metadata;
                # the byte prefix remains the dominant and directly bounded part.
                per_item_budget -= 128 << 10
            target_bytes = min(
                target_bytes,
                max(64 << 10, per_item_budget),
            )
        for item in unique:
            self.offer(item, resolver, max_bytes=target_bytes)
            if sidecar_resolver is not None:
                self.offer_sidecar(item, sidecar_resolver)

    def set_memory_budget(self, memory_budget_bytes: int) -> None:
        budget = max(0, memory_budget_bytes)
        with self.lock:
            if budget == self.memory_budget_bytes:
                return
            self.memory_budget_bytes = budget
        self.clear()

    @staticmethod
    def _discard_future(future: Future[PrebufferedStream]) -> None:
        if future.done() and not future.cancelled():
            with suppress(Exception):
                future.result().close()
        else:
            future.cancel()

    def clear(self) -> None:
        with self.lock:
            entries = list(self.entries.values())
            self.entries.clear()
            sidecars = list(self.sidecars.values())
            self.sidecars.clear()
        for _created, future in entries:
            self._discard_future(future)
        for future in sidecars:
            future.cancel()

    def close(self) -> None:
        with self.lock:
            self.closed = True
            entries = list(self.entries.values())
            self.entries.clear()
            sidecars = list(self.sidecars.values())
            self.sidecars.clear()
            claimed_sidecars = list(self.claimed_sidecars)
            self.claimed_sidecars.clear()
            claimed_streams = list(self.claimed_streams)
            self.claimed_streams.clear()
        for _created, future in entries:
            self._discard_future(future)
        for future in sidecars:
            future.cancel()
        for future in claimed_sidecars:
            future.cancel()
        for future in claimed_streams:
            future.cancel()
        for buffered in self.active:
            buffered.close()
        self.active.clear()

    @staticmethod
    def _warm(
        item: MediaItem,
        resolver: Callable[[MediaItem], ResolvedStream],
        *,
        seconds: int,
        max_bytes: int | None,
        finish_early: Callable[[], bool] | None = None,
    ) -> PrebufferedStream:
        buffered = PrebufferedStream(
            resolver(item),
            seconds=seconds,
            max_bytes=max_bytes,
        )
        buffered.warm(finish_early)
        return buffered
