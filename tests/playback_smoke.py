"""Exercise actual GtkMediaFile playback under a display server."""

from __future__ import annotations

import sys

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gio, GLib, Gtk  # noqa: E402

from tubefin.services import YouTubeService  # noqa: E402
from tubefin.streaming import StreamProxy  # noqa: E402


def main() -> int:
    proxy: StreamProxy | None = None
    if len(sys.argv) == 3 and sys.argv[1] == "--youtube":
        service = YouTubeService()
        results = service.search(sys.argv[2], limit=1)
        if not results:
            print("YouTube search returned no results", file=sys.stderr)
            return 1
        resolved = service.resolve(results[0])
        proxy = StreamProxy(resolved.url, resolved.headers)
        location = proxy.start()
    elif len(sys.argv) == 2:
        location = sys.argv[1]
    else:
        print("usage: playback_smoke.py [--youtube QUERY | MEDIA_LOCATION]", file=sys.stderr)
        return 2

    Gtk.init()
    if location.startswith(("http://", "https://")):
        media_file = Gio.File.new_for_uri(location)
    else:
        media_file = Gio.File.new_for_path(location)
    media = Gtk.MediaFile.new_for_file(media_file)
    loop = GLib.MainLoop()
    wait_ms = 10_000 if proxy else 3_000
    GLib.timeout_add(wait_ms, lambda: (loop.quit(), GLib.SOURCE_REMOVE)[1])
    media.play()
    loop.run()

    error = media.get_error()
    timestamp = media.get_timestamp()
    print(
        f"prepared={media.is_prepared()} playing={media.get_playing()} "
        f"duration={media.get_duration()} timestamp={timestamp}"
    )
    if error:
        print(f"playback error: {error.message}", file=sys.stderr)
        return 1
    if timestamp <= 0:
        print("playback did not advance", file=sys.stderr)
        return 1
    if proxy:
        proxy.stop()
    print(f"playback advanced to {timestamp / 1_000_000:.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
