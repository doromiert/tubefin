"""Exercise chooser and Google-style username/password fields with fake logins only."""

# ruff: noqa: E402
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

private = tempfile.TemporaryDirectory(prefix="tubefin-autofill-ui-")
for k in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
    os.environ[k] = private.name + "/" + k
    Path(os.environ[k]).mkdir()
display = ":" + str(os.getpid() % 10000 + 600)
server = subprocess.Popen(
    [shutil.which("gtk4-broadwayd"), display], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
)
time.sleep(0.3)
os.environ.update(
    GDK_BACKEND="broadway", BROADWAY_DISPLAY=display, WEBKIT_DISABLE_DMABUF_RENDERER="1"
)
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("WebKit", "6.0")
from gi.repository import Adw, GLib, Gtk, WebKit

from tubefin.keepassxc import KeePassXC

KeePassXC.logins = lambda *a: [
    {
        "login": "tubefin-test-" + str(i) + "@example.invalid",
        "name": "Test " + str(i),
        "password": "FAKE-PASSWORD",
    }
    for i in range(15)
]
WebKit.WebView.load_uri = lambda self, _url: self.load_html(
    '<input id="identifierId" name="identifier" type="text" autocomplete="username webauthn">'
    '<input type="password" name="hiddenPassword" style="display:none">',
    "https://accounts.google.com/",
)
from tubefin.youtube_login import create_login

Adw.init()
parent = Adw.Window(title="Autofill test")
parent.present()
loop = GLib.MainLoop()
errors = []
phase = 0
window = create_login(parent, Path(private.name) / "config" / "cookies.txt", lambda: None)
view = window.get_content().get_content().get_last_child()


def descendants(w):
    yield w
    child = w.get_first_child()
    while child:
        yield from descendants(child)
        child = child.get_next_sibling()


def check_value(source, result):
    try:
        value = source.evaluate_javascript_finish(result).to_boolean()
        print("AUTOFILL_MATCH", value, flush=True)
        assert value
        if phase == 3:
            view.evaluate_javascript(
                "document.body.innerHTML = '<input type=password name=Passwd>'; true",
                -1,
                None,
                None,
                None,
                password_ready,
            )
            return
    except Exception as e:
        errors.append(e)
        print("AUTOFILL_ERROR", type(e).__name__, str(e), flush=True)
    loop.quit()


def password_ready(source, result):
    global phase
    source.evaluate_javascript_finish(result)
    # The selected login carries into the password step without another click.
    phase = 4


def password_checked(source, result):
    try:
        value = source.evaluate_javascript_finish(result).to_boolean()
        assert value
        print("PASSWORD_AUTOFILL_MATCH", value, flush=True)
    except Exception as e:
        errors.append(e)
    loop.quit()


def poll():
    global phase
    try:
        if phase == 0:
            if view.is_loading() or "accounts.google.com" not in (view.get_uri() or ""):
                return True
            fill = next(
                w
                for w in descendants(window)
                if isinstance(w, Gtk.Button) and w.get_label() == "Fill from KeePassXC"
            )
            print("FILL_CLICK", fill.get_sensitive(), flush=True)
            fill.emit("clicked")
            phase = 1
        elif phase == 1:
            chooser = window.get_visible_dialog()
            if not chooser or chooser.get_width() == 0:
                return True
            rows = [w for w in descendants(chooser) if isinstance(w, Adw.ActionRow)]
            print(
                "CHOOSER_ROWS",
                len(rows),
                "size",
                chooser.get_width(),
                chooser.get_height(),
                flush=True,
            )
            assert rows[0].get_parent().get_margin_start() == 12
            assert rows[0].get_parent().get_margin_end() == 12
            rows[0].emit("activated")
            phase = 2
        elif phase == 2:
            status = window.get_content().get_content().get_first_child().get_next_sibling()
            if not status.get_label().startswith("Filled from"):
                return True
            print("STATUS", status.get_label(), flush=True)
            view.evaluate_javascript(
                "document.querySelector('#identifierId').value === "
                "'tubefin-test-0@example.invalid' && "
                "document.querySelector('input[type=password]').value === ''",
                -1,
                None,
                None,
                None,
                check_value,
            )
            phase = 3
        elif phase == 4:
            status = window.get_content().get_content().get_first_child().get_next_sibling()
            if not status.get_label().startswith("Filled from"):
                return True
            view.evaluate_javascript(
                "document.querySelector('input[type=password]').value === 'FAKE-PASSWORD'",
                -1,
                None,
                None,
                None,
                password_checked,
            )
            phase = 5
    except Exception as e:
        errors.append(e)
        print("TEST_ERROR", type(e).__name__, str(e), flush=True)
        loop.quit()
        return False
    return True


def timeout():
    errors.append("timeout")
    print("TIMEOUT", phase, flush=True)
    loop.quit()
    return False


GLib.timeout_add(500, poll)
GLib.timeout_add(20000, timeout)
try:
    loop.run()
finally:
    window.close()
    parent.close()
    server.terminate()
    server.wait()
raise SystemExit(bool(errors))
