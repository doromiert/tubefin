"""TubeFin-owned YouTube session; credentials are entered only into Google pages."""

from __future__ import annotations

import http.cookiejar
import os
import tempfile
from pathlib import Path


def save_cookies(cookies: list, destination: Path) -> bool:
    """Atomically export only YouTube/Google cookies for TubeFin's resolver."""
    jar = http.cookiejar.MozillaCookieJar()
    for cookie in cookies:
        domain = cookie.get_domain()
        host = domain.lstrip(".").lower()
        if not any(
            host == root or host.endswith("." + root) for root in ("youtube.com", "google.com")
        ):
            continue
        expires = cookie.get_expires()
        jar.set_cookie(
            http.cookiejar.Cookie(
                0,
                cookie.get_name(),
                cookie.get_value(),
                None,
                False,
                domain,
                domain.startswith("."),
                domain.startswith("."),
                cookie.get_path() or "/",
                True,
                cookie.get_secure(),
                expires.to_unix() if expires else None,
                expires is None,
                None,
                None,
                {"HttpOnly": None} if cookie.get_http_only() else {},
                False,
            )
        )
    if not any(cookie.name in {"SAPISID", "__Secure-3PAPISID"} for cookie in jar):
        return False
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".youtube-", dir=destination.parent)
    os.close(fd)
    try:
        jar.save(name, ignore_discard=True, ignore_expires=False)
        os.replace(name, destination)
    finally:
        Path(name).unlink(missing_ok=True)
    return True


def create_login(parent, destination: Path, on_connected):
    # Lazy import keeps WebKit initialization off the cold application startup.
    import gi

    gi.require_version("WebKit", "6.0")
    from gi.repository import Adw, GLib, Gtk, WebKit

    root = destination.parent / "youtube-session"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    session = getattr(parent, "youtube_network_session", None)
    if session is None:
        session = WebKit.NetworkSession.new(str(root), str(root / "cache"))
        parent.youtube_network_session = session
    manager = session.get_cookie_manager()
    manager.set_persistent_storage(
        str(root / "cookies.sqlite"), WebKit.CookiePersistentStorage.SQLITE
    )
    window = Adw.Window(transient_for=parent, modal=True, title="YouTube Sign In")
    window.set_default_size(640, 760)
    toolbar = Adw.ToolbarView()
    header = Adw.HeaderBar()
    title = Adw.WindowTitle(title="YouTube Sign In", subtitle="accounts.google.com")
    header.set_title_widget(title)
    done = Gtk.Button(label="Continue", tooltip_text="Use this YouTube account in TubeFin")
    done.add_css_class("suggested-action")
    header.pack_end(done)
    fill = Gtk.Button(label="Fill from KeePassXC", sensitive=False)
    header.pack_start(fill)
    toolbar.add_top_bar(header)
    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    progress = Gtk.ProgressBar()
    content.append(progress)
    status = Gtk.Label(label="Loading Google sign-in…", wrap=True)
    content.append(status)
    view = WebKit.WebView(network_session=session, vexpand=True, hexpand=True)
    view.get_settings().set_enable_developer_extras(False)
    content.append(view)
    toolbar.set_content(content)
    window.set_content(toolbar)
    selected_login = None
    pending_entry = None
    pending_until = 0.0
    probing_password = False

    def is_google_login() -> bool:
        from urllib.parse import urlsplit

        uri = urlsplit(view.get_uri() or "")
        return (
            uri.scheme == "https"
            and uri.hostname == "accounts.google.com"
            and uri.port in (None, 443)
        )

    def fill_entry(entry) -> None:
        nonlocal selected_login
        if not window.get_visible() or not is_google_login():
            return
        selected_login = entry["login"]
        import json

        # Recheck the origin inside the page too, closing navigation races. Never submit.
        script = (
            """((login, password) => {
            if (location.origin !== 'https://accounts.google.com') return false;
            const visible = e => !e.disabled && e.getClientRects().length &&
                getComputedStyle(e).visibility !== 'hidden';
            const passwordInput = [...document.querySelectorAll('input[type=password]')]
                .find(visible);
            const input = passwordInput || [...document.querySelectorAll(
                '#identifierId, input[name=identifier], ' +
                'input[autocomplete~=username], input[type=email]'
            )].find(visible);
            if (!input) return false;
            const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
            setter.call(input, passwordInput ? password : login);
            input.dispatchEvent(new Event('input', {bubbles: true}));
            input.dispatchEvent(new Event('change', {bubbles: true}));
            input.focus();
            return passwordInput ? 'password' : 'username';
        })"""
            + "("
            + json.dumps(entry["login"])
            + ", "
            + json.dumps(entry["password"])
            + ")"
        )

        def filled(source, result):
            nonlocal pending_entry, pending_until
            try:
                step = source.evaluate_javascript_finish(result).to_string()
                success = step in {"username", "password"}
                if step == "username":
                    import time
                    pending_entry = entry
                    pending_until = time.monotonic() + 120
                elif step == "password":
                    pending_entry = None
                status.set_label(
                    "Filled from KeePassXC. Continue on Google's page."
                    if success
                    else "No sign-in field is ready. Continue on Google's page and try again."
                )
            except GLib.Error:
                status.set_label("The page changed. Choose Fill from KeePassXC again.")

        view.evaluate_javascript(script, -1, None, None, None, filled)

    def password_probe_finished(source, result):
        nonlocal probing_password, pending_entry
        probing_password = False
        try:
            ready = source.evaluate_javascript_finish(result).to_boolean()
        except GLib.Error:
            return
        if ready and pending_entry is not None and window.get_visible() and is_google_login():
            entry = pending_entry
            pending_entry = None
            fill_entry(entry)

    def follow_password_step():
        nonlocal pending_entry, probing_password
        import time
        if not window.get_visible():
            pending_entry = None
            return GLib.SOURCE_REMOVE
        if pending_entry is None or probing_password:
            return GLib.SOURCE_CONTINUE
        if time.monotonic() >= pending_until or not is_google_login():
            pending_entry = None
            return GLib.SOURCE_CONTINUE
        probing_password = True
        view.evaluate_javascript(
            "location.origin === 'https://accounts.google.com' && "
            "[...document.querySelectorAll('input[type=password]')].some(e => "
            "!e.disabled && e.getClientRects().length && "
            "getComputedStyle(e).visibility !== 'hidden')",
            -1, None, None, None, password_probe_finished,
        )
        return GLib.SOURCE_CONTINUE

    GLib.timeout_add(200, follow_password_step)

    def entries_ready(entries):
        fill.set_sensitive(is_google_login())
        if not window.get_visible() or not is_google_login():
            return False
        if not entries:
            status.set_label(
                "No matching login was shared. Add accounts.google.com to the entry in KeePassXC."
            )
            return False
        match = next((entry for entry in entries if entry["login"] == selected_login), None)
        if len(entries) == 1 or match:
            fill_entry(match or entries[0])
            return False
        status.set_label("Choose a KeePassXC login to fill Google's sign-in field.")
        chooser = Adw.Dialog(
            title="Choose a KeePassXC Login", content_width=420, content_height=400
        )
        layout = Adw.ToolbarView()
        layout.add_top_bar(Adw.HeaderBar())
        rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        rows.add_css_class("boxed-list")
        rows.set_margin_start(12)
        rows.set_margin_end(12)
        rows.set_margin_top(12)
        rows.set_margin_bottom(12)
        for entry in entries:
            row = Adw.ActionRow(
                title=entry.get("name") or entry["login"], subtitle=entry["login"], activatable=True
            )

            def choose(_row, value=entry):
                fill_entry(value)
                chooser.close()

            row.connect("activated", choose)
            rows.append(row)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        scroller.set_child(rows)
        layout.set_content(scroller)
        chooser.set_child(layout)
        chooser.present(window)
        return False

    def fill_failed(_error):
        fill.set_sensitive(is_google_login())
        status.set_label(
            "Open and unlock KeePassXC, enable Browser Integration, "
            "and approve TubeFin. Then try again."
        )
        return False

    def request_fill(_button):
        nonlocal pending_entry
        if not is_google_login():
            return
        pending_entry = None
        from tubefin.application import run_async
        from tubefin.keepassxc import KeePassXC

        fill.set_sensitive(False)
        status.set_label("Waiting for KeePassXC. Unlock your database and approve TubeFin there…")
        uri = view.get_uri()
        run_async(
            lambda: KeePassXC(destination.parent / "keepassxc.json").logins(uri),
            entries_ready,
            fill_failed,
        )

    fill.connect("clicked", request_fill)

    def export_finished(source, result):
        try:
            cookies = source.get_all_cookies_finish(result)
        except GLib.Error as error:
            status.set_label(f"Could not save the YouTube session: {error}")
            done.set_sensitive(True)
            return

        def saved(connected):
            if connected:
                on_connected()
                window.close()
            else:
                status.set_label("Finish signing in to YouTube, then choose Continue.")
                done.set_sensitive(True)
            return False

        def save_failed(_error):
            status.set_label("Could not save the YouTube session. Check your available disk space.")
            done.set_sensitive(True)
            return False

        from tubefin.application import run_async

        run_async(lambda: save_cookies(cookies, destination), saved, save_failed)

    def complete(_button):
        status.set_label("Connecting your YouTube account…")
        done.set_sensitive(False)
        manager.get_all_cookies(None, export_finished)

    def loaded(webview, _event):
        from urllib.parse import urlparse

        host = urlparse(webview.get_uri() or "").hostname or ""
        title.set_subtitle(host)
        fill.set_sensitive(is_google_login())
        status.set_label("Sign in on Google's page, then choose Continue.")

    def failed(_view, _event, _uri, _error):
        status.set_label("Could not load sign-in. Check your connection and try again.")
        return False

    done.connect("clicked", complete)
    view.connect("load-changed", loaded)
    view.connect("load-failed", failed)
    view.connect(
        "notify::estimated-load-progress",
        lambda webview, _pspec: progress.set_fraction(webview.get_estimated_load_progress()),
    )
    view.connect("permission-request", lambda _view, request: (request.deny(), True)[1])
    view.connect("create", lambda *_args: None)
    window.present()
    view.load_uri(
        "https://accounts.google.com/ServiceLogin?service=youtube&continue=https://www.youtube.com/"
    )
    # Keep the session alive for the window's lifetime, and permit account removal.
    window.youtube_session = session
    return window
