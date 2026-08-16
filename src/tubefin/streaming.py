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

    def warm(self) -> None:
        headers = dict(self.stream.headers)
        headers["Range"] = f"bytes=0-{self.max_bytes - 1}"
        headers["Accept-Encoding"] = "identity"
        request = urllib.request.Request(self.stream.url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                self.prefix = response.read(self.max_bytes)
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
                        remainder = None
                        if body and (upstream.total is None or remainder_start < upstream.total):
                            headers = dict(upstream.stream.headers)
                            headers["Range"] = f"bytes={remainder_start}-"
                            headers["Accept-Encoding"] = "identity"
                            try:
                                remainder = urllib.request.urlopen(
                                    urllib.request.Request(upstream.stream.url, headers=headers),
                                    timeout=30,
                                )
                            except (OSError, urllib.error.URLError, TimeoutError):
                                self.send_error(502)
                                return
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
                            try:
                                self.wfile.write(upstream.prefix[start:])
                                if remainder:
                                    while chunk := remainder.read(256 << 10):
                                        self.wfile.write(chunk)
                            except (BrokenPipeError, ConnectionResetError):
                                pass
                            finally:
                                if remainder:
                                    remainder.close()

                    def _forward_range(self, start: int, body: bool) -> None:
                        headers = dict(upstream.stream.headers)
                        headers["Range"] = f"bytes={start}-"
                        headers["Accept-Encoding"] = "identity"
                        try:
                            response = urllib.request.urlopen(
                                urllib.request.Request(upstream.stream.url, headers=headers),
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

    def close(self) -> None:
        if self.proxy:
            self.proxy.stop()
            self.proxy = None


class PrebufferManager:
    """Resolve and warm only a few likely-next visible items."""

    def __init__(self, concurrency: int = 2, capacity: int = 6) -> None:
        self.capacity = max(1, capacity)
        self.semaphore = threading.BoundedSemaphore(max(1, min(concurrency, 3)))
        self.entries: OrderedDict[str, tuple[float, Future[PrebufferedStream]]] = OrderedDict()
        self.active: list[PrebufferedStream] = []
        self.lock = threading.Lock()
        self.closed = False

    @staticmethod
    def key(item: MediaItem) -> str:
        return f"{item.source}:{item.id}"

    def offer(self, item: MediaItem, resolver: Callable[[MediaItem], ResolvedStream]) -> None:
        key = self.key(item)
        with self.lock:
            if self.closed:
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
            args=(future, item, resolver),
            daemon=True,
            name=f"prebuffer-{item.id[:12]}",
        ).start()

    def _run_warm(
        self,
        future: Future[PrebufferedStream],
        item: MediaItem,
        resolver: Callable[[MediaItem], ResolvedStream],
    ) -> None:
        if not future.set_running_or_notify_cancel():
            return
        try:
            with self.semaphore:
                if self.closed:
                    raise RuntimeError("Prebuffering stopped")
                buffered = self._warm(item, resolver)
            if self.closed:
                buffered.close()
            future.set_result(buffered)
        except Exception as error:
            future.set_exception(error)

    def take(self, item: MediaItem) -> ResolvedStream | None:
        with self.lock:
            entry = self.entries.pop(self.key(item), None)
        if not entry:
            return None
        _created, future = entry
        if not future.done() or future.cancelled():
            future.cancel()
            return None
        try:
            buffered = future.result()
            self.active.append(buffered)
            return buffered.playback_stream()
        except Exception:
            return None

    def close(self) -> None:
        with self.lock:
            self.closed = True
            entries = list(self.entries.values())
            self.entries.clear()
        for _created, future in entries:
            if future.done() and not future.cancelled():
                with suppress(Exception):
                    future.result().close()
            else:
                future.cancel()
        for buffered in self.active:
            buffered.close()
        self.active.clear()

    @staticmethod
    def _warm(
        item: MediaItem, resolver: Callable[[MediaItem], ResolvedStream]
    ) -> PrebufferedStream:
        buffered = PrebufferedStream(resolver(item), seconds=10)
        buffered.warm()
        return buffered
