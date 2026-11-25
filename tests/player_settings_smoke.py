"""Check real pointer/keyboard dismissal and control alignment on a private X server.

Run with Xvfb and xdotool available, using the TubeFin development shell.
"""

# ruff: noqa: E402, F401
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

private = tempfile.TemporaryDirectory(prefix="tubefin-settings-")
for key in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
    os.environ[key] = str(Path(private.name) / key)
    Path(os.environ[key]).mkdir()
server = subprocess.Popen(
    [shutil.which("Xvfb"), "-displayfd", "1", "-screen", "0", "1280x900x24", "-nolisten", "tcp"],
    stdout=subprocess.PIPE,
    stderr=subprocess.DEVNULL,
    text=True,
)
os.environ.update(
    DISPLAY=":" + server.stdout.readline().strip(), GDK_BACKEND="x11", GSK_RENDERER="cairo"
)
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkX11", "4.0")
from gi.repository import Gdk, GdkX11, GLib, Gtk
from tubefin.mpv_player import MpvPlayer

Gtk.init()
provider = Gtk.CssProvider()
provider.load_from_path(str(Path(__file__).resolve().parents[1] / "src/tubefin/style.css"))
Gtk.StyleContext.add_provider_for_display(
    Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
)
window = Gtk.Window(title="TubeFin Settings Regression", default_width=900, default_height=700)
player = MpvPlayer(lambda: None, lambda message: print(message))
window.set_child(player)
window.present()
loop = GLib.MainLoop()
errors = []


def step(callback):
    def run():
        try:
            callback()
        except BaseException as error:
            errors.append(error)
            loop.quit()
        return GLib.SOURCE_REMOVE

    GLib.timeout_add(250, run)


def descendants(widget):
    child = widget.get_first_child()
    while child:
        yield child
        yield from descendants(child)
        child = child.get_next_sibling()


def command(*args):
    subprocess.run(["xdotool", *map(str, args)], check=True)


def click(widget, native):
    ok, bounds = widget.compute_bounds(native)
    assert ok
    command(
        "mousemove",
        "--window",
        native.get_surface().get_xid(),
        int(bounds.get_x() + bounds.get_width() / 2),
        int(bounds.get_y() + bounds.get_height() / 2),
        "click",
        1,
    )


def opened():
    box = player.settings_popover.get_child()
    rectangles = []
    for control in (
        player.quality,
        player.speed_control,
        player.subtitle_offset_control,
        player.volume_control,
    ):
        ok, rect = control.compute_bounds(box)
        assert ok
        rectangles.append((round(rect.get_x()), round(rect.get_width())))
    assert len(set(rectangles)) == 1, rectangles
    print("ALIGNED", rectangles, flush=True)
    if os.environ.get("TUBEFIN_SETTINGS_SCREENSHOT"):
        subprocess.run(
            [
                "magick",
                "import",
                "-window",
                str(player.settings_popover.get_surface().get_xid()),
                os.environ["TUBEFIN_SETTINGS_SCREENSHOT"],
            ],
            check=True,
        )
    click(player.quality, player.settings_popover)
    step(picker_open)


def picker_open():
    global picker
    picker = next(x for x in descendants(player.quality) if isinstance(x, Gtk.Popover))
    assert picker.get_visible()
    label = player.option_rows["Quality"].get_first_child()
    click(label, player.settings_popover)
    step(clicked_parent)


def clicked_parent():
    assert not picker.get_visible(), "Picker did not dismiss"
    assert player.settings_popover.get_visible(), "Click dismissed settings panel"
    click(player.quality, player.settings_popover)
    step(escape)


def escape():
    assert picker.get_visible()
    command("key", "Escape")
    step(escaped)


def escaped():
    assert not picker.get_visible()
    assert player.settings_popover.get_visible(), "Escape dismissed settings panel"
    command("key", "Escape")
    step(dismissed)


def dismissed():
    assert not player.settings_popover.get_visible(), "Second Escape did not dismiss settings"
    print("POINTER_AND_ESCAPE_DISMISSAL_PASS", flush=True)
    loop.quit()


def setup():
    player.syncing_options = True
    player.quality.set_model(Gtk.StringList.new(["Auto · 1080p60", "1080p60", "720p60", "480p"]))
    player.captions.set_model(Gtk.StringList.new(["Captions off", "English"]))
    player.audio.set_model(Gtk.StringList.new(["Original"]))
    for row in player.option_rows.values():
        row.set_visible(True)
    player._normalize_option_widths()
    player.settings_popover.popup()
    step(opened)


try:
    step(setup)
    GLib.timeout_add_seconds(
        10, lambda: (errors.append(AssertionError("UI test timed out")), loop.quit(), False)[-1]
    )
    loop.run()
    if errors:
        raise errors[0]
finally:
    player.shutdown()
    window.destroy()
    server.terminate()
    server.wait(timeout=5)
