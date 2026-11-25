"""Verify local/online resume offers and explicit user acceptance in the GTK app."""

# ruff: noqa: E402
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

private = tempfile.TemporaryDirectory(prefix="tubefin-app-smoke-")
for k in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
    os.environ[k] = private.name + "/" + k
    Path(os.environ[k]).mkdir()
display = ":" + str(os.getpid() % 10000 + 500)
p = subprocess.Popen(
    [shutil.which("gtk4-broadwayd"), display], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
)
time.sleep(0.3)
os.environ.update(GDK_BACKEND="broadway", BROADWAY_DISPLAY=display)
t = time.monotonic()
from gi.repository import GLib

from tubefin.application import TubeFinApplication


class Smoke(TubeFinApplication):
    def do_activate(self):
        super().do_activate()
        clock = self.window.get_frame_clock()

        def paint(*args):
            clock.disconnect(h)
            print("FIRST_PAINT_MS", (time.monotonic() - t) * 1000, flush=True)
            GLib.timeout_add(100, check)

        h = clock.connect("after-paint", paint)


def check():
    from unittest.mock import Mock

    from tubefin.models import MediaItem, ResolvedStream, StreamVariant

    w = app.window
    original = w.mpv_player
    toasts = []
    saved_add = w.toast_overlay.add_toast
    w.toast_overlay.add_toast = toasts.append
    try:
        for local, online, expected in (
            (120, 0, 120),
            (0, 2891, 2891),
            (120, 2891, 2891),
            (3600, 2891, 3600),
            (0, 0, 0),
        ):
            fake = Mock()
            fake.pending_seek = None
            fake.default_url = "file:///tmp/test.mp4"
            w.mpv_player = fake
            w.current_item = MediaItem("toast-test", "Resume test", source="youtube")
            w.playback_request += 1
            w.resume_position_offer = local
            w.resume_item_id = w.current_item.id if local else ""
            w.resume_offer_shown = False
            w.pending_sync_state = None
            stream = ResolvedStream(
                "https://media.example/test",
                variants=[StreamVariant("1080p", "https://media.example/test")],
                resume_position=online,
            )
            w._start_playback(w.current_item, stream, w.playback_request)
            assert fake.pending_seek is None
            fake.resume_at.assert_not_called()
            w._mpv_ready()
            if expected:
                toast = toasts.pop()
                assert toast.get_button_label() == "Resume"
                assert w._time_label(expected) in toast.get_title()
                assert w.resume_position_offer == expected
                w.lookup_action("resume-playback").activate(None)
                fake.resume_at.assert_called_once_with(expected)
            else:
                assert not toasts
            print("RESUME_TOAST", local, online, expected, flush=True)
    except BaseException:
        import traceback

        traceback.print_exc()
        failures.append(True)
    finally:
        w.mpv_player = original
        w.toast_overlay.add_toast = saved_add
        app.quit()
    return False


failures = []
GLib.timeout_add(5000, lambda: (app.quit(), False)[1])
app = Smoke("com.neg-zero.Tubefin.Smoke" + str(os.getpid()))
try:
    app.run([])
finally:
    p.terminate()
    p.wait()

raise SystemExit(bool(failures))
