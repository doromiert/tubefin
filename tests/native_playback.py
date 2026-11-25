"""Real libmpv/Wayland regression, using generated media and private app data.

Run with WAYLAND_DEBUG=client to inspect compositor buffers at SURFACE SETTLED
markers as well as the libmpv and responsiveness assertions here.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

private = tempfile.TemporaryDirectory(prefix="tubefin-native-")
for key in ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
    os.environ[key] = str(Path(private.name) / key)
    Path(os.environ[key]).mkdir()

import gi  # noqa: E402
gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402
from tubefin.mpv_player import MpvPlayer, MpvGLArea  # noqa: E402

def timed_method(cls, name):
    original = getattr(cls, name)
    def timed(*args, **kwargs):
        start = time.monotonic()
        try:
            return original(*args, **kwargs)
        finally:
            duration = time.monotonic() - start
            if duration > .05:
                print("SLOW", name, round(duration * 1000, 1), file=sys.stderr, flush=True)
    setattr(cls, name, timed)

for method in ("_set_video_hdr", "_file_loaded", "_load_url"):
    timed_method(MpvPlayer, method)
timed_method(MpvGLArea, "_render")
Gtk.init()
window = Gtk.Window(default_width=600, default_height=400, title="TubeFin Playback Regression")
errors = []
player = MpvPlayer(lambda: None, errors.append)
window.set_child(player)
window.present()
loop = GLib.MainLoop()
last = time.monotonic()
gaps = []
phase = 0
settled = 0
deadline = time.monotonic() + 30
phases = [
    ("sdr.windowed", "/tmp/tubefin-test-sdr.mp4", False, False),
    ("pq.windowed", "/tmp/tubefin-test-hdr.mp4", True, False),
    ("hlg.windowed", "/tmp/tubefin-test-hlg.mp4", True, False),
    ("pq.fullscreen", "/tmp/tubefin-test-hdr.mp4", True, True),
    ("sdr.fullscreen", "/tmp/tubefin-test-sdr.mp4", False, True),
    ("sdr.return", "/tmp/tubefin-test-sdr.mp4", False, False),
]
if os.environ.get("TUBEFIN_WINDOWED"):
    phases = [(name.replace("fullscreen", "repeat"), url, hdr, False)
              for name, url, hdr, fullscreen in phases]
if len(sys.argv) > 1:
    data = json.loads(Path(sys.argv[1]).read_text())
    hdr = " HDR" in data["default_label"]
    phases = [("pq.youtube" if hdr else "sdr.youtube", data["url"], hdr, False)]


def load():
    name, url, _hdr, fullscreen = phases[phase]
    window.fullscreen() if fullscreen else window.unfullscreen()
    print("SURFACE BEGIN", name, file=sys.stderr, flush=True)
    if len(sys.argv) > 1:
        player.pending_seek = float(data.get("resume_position") or 0)
        player.load(url, data["headers"], audio_url=data["audio_url"])
    else:
        player.load(url)


def tick():
    global last, phase, settled
    now = time.monotonic()
    gaps.append(now - last)
    if now - last > .35:
        print("LONG GAP", phase, (now-last)*1000, file=sys.stderr, flush=True)
    last = now
    try:
        assert not errors, errors
        assert now < deadline, ("Playback timeout", phase)
        if float(player.player.time_pos or 0) < 0.3:
            return True
        if not settled:
            settled = now
        if now - settled < 0.7:
            return True
        name, _url, hdr, _fullscreen = phases[phase]
        assert player.video.hdr == hdr, (name, player.player.video_params)
        assert player.player.target_trc == ("pq" if hdr else "srgb")
        if len(sys.argv) > 1:
            assert any(track.get("type") == "audio" for track in player.player.track_list)
            assert player.player.audio_params
            assert float(player.player.time_pos or 0) >= float(data.get("resume_position") or 0)
            print("YOUTUBE", player.player.video_params, flush=True)
        print("SURFACE SETTLED", name, file=sys.stderr, flush=True)
        phase += 1
        settled = 0
        if phase < len(phases):
            load()
            return True
        gtk = sorted({line.split()[-1] for line in Path("/proc/self/maps").read_text().splitlines()
                      if "/libgtk-4.so." in line})
        assert len(gtk) == 1, gtk
        assert max(gaps) < 0.4, sorted(gaps)[-5:]
        print(json.dumps({"phases": len(phases), "max_loop_gap_ms": max(gaps) * 1000, "gtk": gtk}))
    except Exception as error:
        import traceback
        traceback.print_exc()
        errors.append(error)
    loop.quit()
    return False


GLib.timeout_add(20, tick)
load()
loop.run()
player.shutdown()
window.destroy()
raise SystemExit(bool(errors))
