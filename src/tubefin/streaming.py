from __future__ import annotations

import secrets
import threading
import urllib.error
import urllib.request
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


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
