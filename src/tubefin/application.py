from __future__ import annotations

import threading
from collections.abc import Callable
from importlib import resources
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

from tubefin import __version__  # noqa: E402
from tubefin.config import ConfigStore  # noqa: E402
from tubefin.models import JellyfinSession, MediaItem, ResolvedStream  # noqa: E402
from tubefin.mpv_player import MpvPlayer  # noqa: E402
from tubefin.services import JellyfinService, YouTubeService  # noqa: E402
from tubefin.widgets import MediaGrid, ThumbnailLoader  # noqa: E402

APP_ID = "io.github.doromiert.TubeFin"


def run_async(
    operation: Callable[[], Any],
    on_success: Callable[[Any], None],
    on_error: Callable[[Exception], None],
) -> None:
    def worker() -> None:
        try:
            result = operation()
        except Exception as error:  # callbacks intentionally turn service failures into UI state
            GLib.idle_add(on_error, error)
        else:
            GLib.idle_add(on_success, result)

    threading.Thread(target=worker, daemon=True).start()


class ConnectionWindow(Adw.Window):
    def __init__(
        self,
        parent: Gtk.Window,
        on_connect: Callable[[str, str, str, Gtk.Button], None],
    ) -> None:
        super().__init__(transient_for=parent, modal=True, title="Connect to Jellyfin")
        self.set_default_size(460, 480)
        self.set_resizable(False)

        toolbar = Adw.ToolbarView()
        self.set_content(toolbar)
        header = Adw.HeaderBar()
        header.set_title_widget(
            Adw.WindowTitle(title="Connect to Jellyfin", subtitle="Your media server")
        )
        toolbar.add_top_bar(header)

        clamp = Adw.Clamp(maximum_size=420)
        clamp.set_margin_top(30)
        clamp.set_margin_bottom(30)
        clamp.set_margin_start(24)
        clamp.set_margin_end(24)
        toolbar.set_content(clamp)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        clamp.set_child(content)

        icon = Gtk.Image.new_from_icon_name("network-server-symbolic")
        icon.set_pixel_size(64)
        icon.add_css_class("accent")
        content.append(icon)

        intro = Gtk.Label(
            label="Sign in to browse and play the libraries on your Jellyfin server."
        )
        intro.set_wrap(True)
        intro.set_justify(Gtk.Justification.CENTER)
        intro.add_css_class("dim-label")
        content.append(intro)

        group = Adw.PreferencesGroup()
        content.append(group)

        self.server = Adw.EntryRow(title="Server address")
        self.server.set_text("http://localhost:8096")
        self.server.set_input_purpose(Gtk.InputPurpose.URL)
        group.add(self.server)

        self.username = Adw.EntryRow(title="Username")
        group.add(self.username)

        self.password = Adw.PasswordEntryRow(title="Password")
        group.add(self.password)

        self.error = Gtk.Label()
        self.error.add_css_class("error")
        self.error.set_wrap(True)
        self.error.set_visible(False)
        content.append(self.error)

        self.connect_button = Gtk.Button(label="Connect")
        self.connect_button.add_css_class("suggested-action")
        self.connect_button.add_css_class("pill")
        self.connect_button.set_halign(Gtk.Align.CENTER)
        self.connect_button.connect("clicked", self._connect_clicked, on_connect)
        content.append(self.connect_button)
        self.password.connect("entry-activated", lambda *_: self.connect_button.activate())

    def _connect_clicked(
        self,
        _button: Gtk.Button,
        on_connect: Callable[[str, str, str, Gtk.Button], None],
    ) -> None:
        server = self.server.get_text().strip()
        username = self.username.get_text().strip()
        if not server or not username:
            self.show_error("Enter a server address and username.")
            return
        self.error.set_visible(False)
        self.connect_button.set_sensitive(False)
        self.connect_button.set_label("Connecting…")
        on_connect(server, username, self.password.get_text(), self.connect_button)

    def show_error(self, message: str) -> bool:
        self.error.set_label(message)
        self.error.set_visible(True)
        self.connect_button.set_sensitive(True)
        self.connect_button.set_label("Connect")
        return GLib.SOURCE_REMOVE


class TubeFinWindow(Adw.ApplicationWindow):
    def __init__(self, application: Adw.Application) -> None:
        super().__init__(application=application, title="TubeFin")
        self.set_default_size(1180, 760)
        self.set_size_request(500, 420)

        self.config = ConfigStore()
        self.youtube = YouTubeService()
        self.jellyfin = JellyfinService(self.config.load_session())
        self.thumbnails = ThumbnailLoader()
        self.current_item: MediaItem | None = None
        self.mpv_player: MpvPlayer | None = None
        self.connection_window: ConnectionWindow | None = None
        self.jellyfin_history: list[tuple[str, str]] = []
        self.jellyfin_parent_id = ""
        self.jellyfin_loaded = False
        self.detail_item: MediaItem | None = None
        self.playback_request = 0
        self.expected_page = "home"
        self.active_navigation = "home"
        self.syncing_navigation = False

        self.toast_overlay = Adw.ToastOverlay()
        self.set_content(self.toast_overlay)

        self.split_view = Adw.NavigationSplitView()
        self.toast_overlay.set_child(self.split_view)
        self.split_view.set_sidebar(self._build_sidebar())
        self.split_view.set_content(self._build_content())

        self.disconnect_action = Gio.SimpleAction.new("disconnect", None)
        self.disconnect_action.connect("activate", lambda *_: self.disconnect_jellyfin())
        self.disconnect_action.set_enabled(self.jellyfin.session is not None)
        self.add_action(self.disconnect_action)

        self.navigation.select_row(self.navigation.get_row_at_index(0))
        if self.jellyfin.session:
            self._set_account(self.jellyfin.session)

        controller = Gtk.EventControllerKey()
        controller.connect("key-pressed", self._key_pressed)
        self.add_controller(controller)
        self.connect("close-request", self._close_player)

    def _build_sidebar(self) -> Adw.NavigationPage:
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        header.set_title_widget(Adw.WindowTitle(title="TubeFin", subtitle="Watch your way"))
        toolbar.add_top_bar(header)

        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        sidebar.set_margin_top(12)
        sidebar.set_margin_bottom(12)
        sidebar.set_margin_start(12)
        sidebar.set_margin_end(12)
        toolbar.set_content(sidebar)

        self.navigation = Gtk.ListBox()
        self.navigation.add_css_class("navigation-sidebar")
        self.navigation.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.navigation.connect("row-selected", self._navigation_changed)
        sidebar.append(self.navigation)

        for key, title, icon in [
            ("home", "Home", "user-home-symbolic"),
            ("youtube", "YouTube", "media-playback-start-symbolic"),
            ("jellyfin", "Jellyfin", "network-server-symbolic"),
        ]:
            row = Adw.ActionRow(title=title)
            row.set_name(key)
            row.add_prefix(Gtk.Image.new_from_icon_name(icon))
            self.navigation.append(row)

        spacer = Gtk.Box(vexpand=True)
        sidebar.append(spacer)

        self.account_row = Adw.ActionRow(title="Jellyfin", subtitle="Not connected")
        self.account_row.add_prefix(Gtk.Image.new_from_icon_name("avatar-default-symbolic"))
        self.account_row.set_activatable(True)
        self.account_row.connect("activated", lambda *_: self.open_connection())
        sidebar.append(self.account_row)

        return Adw.NavigationPage(title="TubeFin", child=toolbar)

    def _build_content(self) -> Adw.NavigationPage:
        toolbar = Adw.ToolbarView()
        self.header = Adw.HeaderBar()
        self.window_title = Adw.WindowTitle(title="Home", subtitle="YouTube + Jellyfin")
        self.header.set_title_widget(self.window_title)
        toolbar.add_top_bar(self.header)

        menu = Gio.Menu()
        menu.append("Disconnect Jellyfin", "win.disconnect")
        menu.append("Keyboard Shortcuts", "win.shortcuts")
        menu.append("About TubeFin", "app.about")
        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        self.header.pack_end(menu_button)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        toolbar.set_content(content)

        self.pages = Gtk.Stack()
        self.pages.connect("notify::visible-child-name", self._visible_page_changed)
        self.pages.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.pages.set_transition_duration(180)
        self.pages.set_vexpand(True)
        content.append(self.pages)

        self.pages.add_named(self._build_home_page(), "home")
        self.pages.add_named(self._build_youtube_page(), "youtube")
        self.pages.add_named(self._build_jellyfin_page(), "jellyfin")
        self.pages.add_named(self._build_details_page(), "details")
        self.pages.add_named(self._build_player_page(), "player")

        self.mini_player = self._build_mini_player()
        self.mini_player.set_visible(False)
        content.append(self.mini_player)

        return Adw.NavigationPage(title="Content", child=toolbar)

    def _build_home_page(self) -> Gtk.Widget:
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        clamp = Adw.Clamp(maximum_size=980)
        clamp.set_margin_top(42)
        clamp.set_margin_bottom(42)
        clamp.set_margin_start(24)
        clamp.set_margin_end(24)
        scroller.set_child(clamp)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=34)
        clamp.set_child(content)

        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        hero.add_css_class("hero")
        hero.set_margin_top(8)
        hero.set_margin_bottom(8)

        logo = Gtk.Image.new_from_icon_name("media-playback-start-symbolic")
        logo.set_pixel_size(72)
        logo.add_css_class("accent")
        hero.append(logo)

        greeting = Gtk.Label(label="Everything you watch, in one place")
        greeting.add_css_class("title-1")
        greeting.set_wrap(True)
        greeting.set_justify(Gtk.Justification.CENTER)
        hero.append(greeting)

        copy = Gtk.Label(
            label=(
                "Search YouTube or settle into your own Jellyfin library "
                "in a focused, native player."
            )
        )
        copy.add_css_class("dim-label")
        copy.set_wrap(True)
        copy.set_justify(Gtk.Justification.CENTER)
        hero.append(copy)
        content.append(hero)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        actions.set_halign(Gtk.Align.CENTER)
        youtube_button = Gtk.Button(label="Search YouTube", icon_name="system-search-symbolic")
        youtube_button.add_css_class("suggested-action")
        youtube_button.add_css_class("pill")
        youtube_button.connect("clicked", lambda *_: self._select_page("youtube"))
        actions.append(youtube_button)
        jellyfin_button = Gtk.Button(label="Open Jellyfin", icon_name="network-server-symbolic")
        jellyfin_button.add_css_class("pill")
        jellyfin_button.connect("clicked", lambda *_: self._select_page("jellyfin"))
        actions.append(jellyfin_button)
        content.append(actions)

        features = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18, homogeneous=True)
        for icon, title, description in [
            ("video-display-symbolic", "Native playback", "GStreamer playback in a GTK interface."),
            ("system-search-symbolic", "Fast search", "Find YouTube videos without an API key."),
            ("network-server-symbolic", "Your library", "Browse your private Jellyfin collection."),
        ]:
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            card.add_css_class("feature-card")
            image = Gtk.Image.new_from_icon_name(icon)
            image.set_pixel_size(32)
            image.set_halign(Gtk.Align.START)
            card.append(image)
            label = Gtk.Label(label=title, xalign=0)
            label.add_css_class("heading")
            card.append(label)
            detail = Gtk.Label(label=description, xalign=0)
            detail.add_css_class("dim-label")
            detail.set_wrap(True)
            card.append(detail)
            features.append(card)
        content.append(features)
        return scroller

    def _build_youtube_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        search_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        search_box.add_css_class("search-strip")
        self.youtube_search = Gtk.SearchEntry(placeholder_text="Search YouTube")
        self.youtube_search.set_hexpand(True)
        self.youtube_search.connect("activate", self._youtube_search_requested)
        search_box.append(self.youtube_search)
        page.append(search_box)

        self.youtube_grid = MediaGrid(self.thumbnails, self._activate_item)
        self.youtube_grid.set_status(
            "system-search-symbolic",
            "Search YouTube",
            "Enter a title, creator, or topic to find videos.",
        )
        page.append(self.youtube_grid)
        return page

    def _build_jellyfin_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.jellyfin_toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.jellyfin_toolbar.add_css_class("search-strip")
        self.jellyfin_back = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="Back")
        self.jellyfin_back.set_sensitive(False)
        self.jellyfin_back.connect("clicked", lambda *_: self._jellyfin_go_back())
        self.jellyfin_toolbar.append(self.jellyfin_back)
        self.jellyfin_search = Gtk.SearchEntry(placeholder_text="Search this library")
        self.jellyfin_search.set_hexpand(True)
        self.jellyfin_search.connect("activate", self._jellyfin_search_requested)
        self.jellyfin_toolbar.append(self.jellyfin_search)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh")
        refresh.connect("clicked", lambda *_: self._load_jellyfin_current())
        self.jellyfin_toolbar.append(refresh)
        page.append(self.jellyfin_toolbar)

        self.jellyfin_grid = MediaGrid(self.thumbnails, self._activate_item)
        page.append(self.jellyfin_grid)
        if self.jellyfin.session:
            self.jellyfin_grid.set_loading("Loading your library…")
        else:
            button = Gtk.Button(label="Connect to Jellyfin")
            button.add_css_class("suggested-action")
            button.add_css_class("pill")
            button.set_halign(Gtk.Align.CENTER)
            button.connect("clicked", lambda *_: self.open_connection())
            self.jellyfin_grid.set_status(
                "network-server-symbolic",
                "Bring your own library",
                "Connect to a Jellyfin server to browse and play your media.",
                button,
            )
        return page

    def _build_player_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        player_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        player_bar.add_css_class("player-heading")
        back = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="Back")
        back.connect("clicked", lambda *_: self._leave_player())
        player_bar.append(back)
        self.player_heading = Gtk.Label(label="Now playing", xalign=0, hexpand=True)
        self.player_heading.add_css_class("heading")
        self.player_heading.set_ellipsize(Pango.EllipsizeMode.END)
        player_bar.append(self.player_heading)
        page.append(player_bar)

        stage = Gtk.Overlay()
        stage.add_css_class("player-stage")
        stage.set_vexpand(True)
        self.mpv_player = MpvPlayer(self._mpv_ready, self._mpv_error)
        stage.set_child(self.mpv_player)
        player_status_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        player_status_box.set_halign(Gtk.Align.CENTER)
        player_status_box.set_valign(Gtk.Align.CENTER)
        self.player_spinner = Gtk.Spinner()
        self.player_spinner.set_size_request(32, 32)
        player_status_box.append(self.player_spinner)
        self.player_status = Gtk.Label(label="Preparing video…")
        self.player_status.set_wrap(True)
        self.player_status.set_max_width_chars(60)
        player_status_box.append(self.player_status)
        stage.add_overlay(player_status_box)
        self.player_status_box = player_status_box
        page.append(stage)

        details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        details.add_css_class("player-details")
        self.player_title = Gtk.Label(xalign=0)
        self.player_title.add_css_class("title-2")
        self.player_title.set_ellipsize(Pango.EllipsizeMode.END)
        details.append(self.player_title)
        self.player_subtitle = Gtk.Label(xalign=0)
        self.player_subtitle.add_css_class("dim-label")
        details.append(self.player_subtitle)
        page.append(details)
        return page

    def _build_details_page(self) -> Gtk.Widget:
        scroller = Gtk.ScrolledWindow(vexpand=True)
        clamp = Adw.Clamp(maximum_size=1000)
        clamp.set_margin_top(28)
        clamp.set_margin_bottom(36)
        clamp.set_margin_start(24)
        clamp.set_margin_end(24)
        scroller.set_child(clamp)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        clamp.set_child(content)

        back = Gtk.Button(label="Back to library", icon_name="go-previous-symbolic")
        back.set_halign(Gtk.Align.START)
        back.connect("clicked", lambda *_: self._select_page("jellyfin"))
        content.append(back)
        hero = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24)
        self.details_picture = Gtk.Picture()
        self.details_picture.set_content_fit(Gtk.ContentFit.COVER)
        self.details_picture.set_size_request(360, 203)
        hero.append(self.details_picture)
        copy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, hexpand=True)
        self.details_title = Gtk.Label(xalign=0, wrap=True)
        self.details_title.add_css_class("title-1")
        copy.append(self.details_title)
        self.details_meta = Gtk.Label(xalign=0, wrap=True)
        self.details_meta.add_css_class("dim-label")
        copy.append(self.details_meta)
        self.details_play = Gtk.Button(label="Play", icon_name="media-playback-start-symbolic")
        self.details_play.add_css_class("suggested-action")
        self.details_play.add_css_class("pill")
        self.details_play.set_halign(Gtk.Align.START)
        self.details_play.connect("clicked", lambda *_: self._play_detail_item())
        copy.append(self.details_play)
        hero.append(copy)
        content.append(hero)
        self.details_overview = Gtk.Label(xalign=0, yalign=0, wrap=True, selectable=True)
        self.details_overview.set_max_width_chars(100)
        content.append(self.details_overview)
        return scroller

    def _build_mini_player(self) -> Gtk.Widget:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        bar.add_css_class("mini-player")
        self.mini_icon = Gtk.Image.new_from_icon_name("media-playback-start-symbolic")
        self.mini_icon.set_pixel_size(24)
        bar.append(self.mini_icon)
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        self.mini_title = Gtk.Label(xalign=0)
        self.mini_title.add_css_class("heading")
        self.mini_title.set_ellipsize(Pango.EllipsizeMode.END)
        labels.append(self.mini_title)
        self.mini_subtitle = Gtk.Label(xalign=0)
        self.mini_subtitle.add_css_class("caption")
        self.mini_subtitle.add_css_class("dim-label")
        self.mini_subtitle.set_ellipsize(Pango.EllipsizeMode.END)
        labels.append(self.mini_subtitle)
        bar.append(labels)
        open_player = Gtk.Button(icon_name="view-fullscreen-symbolic", tooltip_text="Open player")
        open_player.connect("clicked", lambda *_: self._show_player())
        bar.append(open_player)
        stop = Gtk.Button(icon_name="media-playback-stop-symbolic", tooltip_text="Stop")
        stop.connect("clicked", lambda *_: self._stop_playback())
        bar.append(stop)
        return bar

    def _navigation_changed(self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if not row or self.syncing_navigation:
            return
        name = row.get_name()
        # Gtk.ListBox may emit the selected row again after focus/layout
        # changes. A duplicate must not unwind a player or details page.
        if name == self.active_navigation:
            return
        self.active_navigation = name
        self._select_page(name)

    def _select_page(self, name: str) -> None:
        titles = {
            "home": ("Home", "YouTube + Jellyfin"),
            "youtube": ("YouTube", "Search and watch"),
            "jellyfin": ("Jellyfin", "Your media library"),
        }
        if name not in titles:
            return
        self.active_navigation = name
        selected = self.navigation.get_selected_row()
        if not selected or selected.get_name() != name:
            row = self.navigation.get_first_child()
            while row and row.get_name() != name:
                row = row.get_next_sibling()
            if row:
                self.syncing_navigation = True
                self.navigation.select_row(row)
                self.syncing_navigation = False
        self._set_visible_page(name)
        title, subtitle = titles[name]
        self.window_title.set_title(title)
        self.window_title.set_subtitle(subtitle)
        self.split_view.set_show_content(True)
        self.mini_player.set_visible(self.current_item is not None)
        if name == "youtube":
            GLib.idle_add(self.youtube_search.grab_focus)
        elif name == "jellyfin" and self.jellyfin.session and not self.jellyfin_loaded:
            self._load_jellyfin_home()

    def _youtube_search_requested(self, entry: Gtk.SearchEntry) -> None:
        query = entry.get_text().strip()
        if not query:
            return
        self.youtube_grid.set_loading(f"Searching for “{query}”…")
        run_async(
            lambda: self.youtube.search(query),
            self._youtube_results,
            lambda error: self._grid_error(self.youtube_grid, error),
        )

    def _youtube_results(self, items: list[MediaItem]) -> bool:
        if items:
            self.youtube_grid.set_items(items)
        else:
            self.youtube_grid.set_status(
                "edit-find-symbolic",
                "No videos found",
                "Try a different search phrase.",
            )
        return GLib.SOURCE_REMOVE

    def _load_jellyfin_home(self) -> None:
        if not self.jellyfin.session:
            return
        self.jellyfin_history.clear()
        self.jellyfin_parent_id = ""
        self.jellyfin_back.set_sensitive(False)
        self.jellyfin_search.set_text("")

        self._load_jellyfin_current()

    def _load_jellyfin_current(self) -> None:
        if not self.jellyfin.session:
            return
        self.jellyfin_grid.set_loading("Loading your library…")
        operation = (
            self.jellyfin.get_libraries
            if not self.jellyfin_parent_id
            else lambda: self.jellyfin.get_items(self.jellyfin_parent_id)
        )

        run_async(
            operation,
            self._jellyfin_results,
            lambda error: self._grid_error(self.jellyfin_grid, error),
        )

    def _jellyfin_search_requested(self, entry: Gtk.SearchEntry) -> None:
        query = entry.get_text().strip()
        if not query:
            self._load_jellyfin_current()
            return
        self.jellyfin_grid.set_loading(f"Searching for “{query}”…")
        run_async(
            lambda: self.jellyfin.get_items(self.jellyfin_parent_id, query),
            self._jellyfin_results,
            lambda error: self._grid_error(self.jellyfin_grid, error),
        )

    def _jellyfin_results(self, items: list[MediaItem]) -> bool:
        self.jellyfin_loaded = True
        if items:
            self.jellyfin_grid.set_items(items)
        else:
            self.jellyfin_grid.set_status(
                "edit-find-symbolic",
                "Nothing here yet",
                "This view did not return any media.",
            )
        return GLib.SOURCE_REMOVE

    def _activate_item(self, item: MediaItem) -> None:
        if item.source == "jellyfin" and not item.playable:
            self.jellyfin_grid.set_loading(f"Opening {item.title}…")
            self.jellyfin_history.append((self.jellyfin_parent_id, item.title))
            self.jellyfin_parent_id = item.id
            self.jellyfin_back.set_sensitive(True)
            self.jellyfin_search.set_text("")
            run_async(
                lambda: self.jellyfin.get_items(item.id),
                self._jellyfin_results,
                lambda error: self._grid_error(self.jellyfin_grid, error),
            )
            return

        if item.source == "jellyfin":
            self._show_details(item)
            return

        self._begin_playback(item)

    def _show_details(self, item: MediaItem) -> None:
        self.detail_item = item
        self.details_title.set_label(item.title)
        payload = item.payload
        meta = [
            str(value)
            for value in (payload.get("ProductionYear"), item.duration_label)
            if value
        ]
        genres = payload.get("Genres") or []
        if genres:
            meta.append(" · ".join(str(genre) for genre in genres[:3]))
        self.details_meta.set_label("  •  ".join(meta) or item.subtitle)
        self.details_overview.set_label(payload.get("Overview") or "No description available.")
        self.details_picture.set_paintable(None)
        if item.thumbnail_url:
            item_id = item.id
            self.thumbnails.load(
                item.thumbnail_url,
                lambda path: self._set_details_picture(item_id, path),
            )
        self._set_visible_page("details")
        self.window_title.set_title(item.title)
        self.window_title.set_subtitle("Jellyfin details")
        self.mini_player.set_visible(self.current_item is not None)

    def _set_details_picture(self, item_id: str, path: object) -> bool:
        if self.detail_item and self.detail_item.id == item_id and path:
            self.details_picture.set_filename(str(path))
        return GLib.SOURCE_REMOVE

    def _play_detail_item(self) -> None:
        if self.detail_item:
            self._begin_playback(self.detail_item)

    def _begin_playback(self, item: MediaItem) -> None:
        self.playback_request += 1
        request_id = self.playback_request
        self._detach_playback()
        # Update this before resolving. Navigation must never consult the item
        # from the stream we just retired while a new selection is loading.
        self.current_item = item

        self.player_heading.set_label(f"Loading {item.title}…")
        self.player_title.set_label(item.title)
        self.player_subtitle.set_label(item.subtitle)
        self._set_visible_page("player")
        self.window_title.set_title("Now Playing")
        self.window_title.set_subtitle(item.source.capitalize())
        self.mini_player.set_visible(False)
        self.player_status.set_label("Preparing video…")
        self.player_status_box.set_visible(True)
        self.player_spinner.start()

        resolver = self.youtube.resolve if item.source == "youtube" else self.jellyfin.stream_url
        run_async(
            lambda: resolver(item),
            lambda url: self._start_playback(item, url, request_id),
            lambda error: self._playback_error(error, request_id),
        )

    def _start_playback(
        self, item: MediaItem, stream: str | ResolvedStream, request_id: int
    ) -> bool:
        if request_id != self.playback_request:
            return GLib.SOURCE_REMOVE
        headers: dict[str, str] = {}
        if isinstance(stream, ResolvedStream):
            url = stream.url
            headers = stream.headers
        else:
            url = stream
        self.player_heading.set_label(item.title)
        self.mini_title.set_label(item.title)
        self.mini_subtitle.set_label(item.subtitle or item.source.capitalize())
        if self.mpv_player:
            self.mpv_player.load(url, headers)
        return GLib.SOURCE_REMOVE

    def _mpv_ready(self) -> None:
        self.player_spinner.stop()
        self.player_status_box.set_visible(False)

    def _jellyfin_go_back(self) -> None:
        if not self.jellyfin_history:
            return
        parent_id, _title = self.jellyfin_history.pop()
        self.jellyfin_parent_id = parent_id
        self.jellyfin_back.set_sensitive(bool(self.jellyfin_history))
        self.jellyfin_search.set_text("")
        self._load_jellyfin_current()

    def _mpv_error(self, message: str) -> None:
        self.player_spinner.stop()
        self.player_status.set_label(f"Playback failed: {message}")
        self.player_status_box.set_visible(True)
        self.toast_overlay.add_toast(Adw.Toast(title=f"Playback failed: {message}"))

    def _playback_error(self, error: Exception, request_id: int) -> bool:
        if request_id != self.playback_request:
            return GLib.SOURCE_REMOVE
        self.player_spinner.stop()
        self.player_status.set_label(str(error))
        self.player_status_box.set_visible(True)
        self.toast_overlay.add_toast(Adw.Toast(title=str(error)))
        return GLib.SOURCE_REMOVE

    def _show_player(self) -> None:
        if not self.current_item:
            return
        self._set_visible_page("player")
        self.window_title.set_title("Now Playing")
        self.window_title.set_subtitle(self.current_item.source.capitalize())
        self.mini_player.set_visible(False)

    def _leave_player(self) -> None:
        source = self.current_item.source if self.current_item else "home"
        destination = source if source in {"youtube", "jellyfin"} else "home"
        self._select_page(destination)

    def _stop_playback(self) -> None:
        self.playback_request += 1
        self._detach_playback()
        self.current_item = None
        self.mini_player.set_visible(False)

    def _detach_playback(self) -> None:
        if self.mpv_player:
            self.mpv_player.stop()

    def _close_player(self, *_args: object) -> bool:
        if self.mpv_player:
            self.mpv_player.shutdown()
            self.mpv_player = None
        return False

    def _set_visible_page(self, name: str) -> None:
        self.expected_page = name
        self.pages.set_visible_child_name(name)

    def _visible_page_changed(self, stack: Gtk.Stack, _property: GObject.ParamSpec) -> None:
        visible = stack.get_visible_child_name()
        if visible != self.expected_page:
            # Player teardown must not unwind app navigation. Only
            # _set_visible_page is allowed to change it.
            GLib.idle_add(self._restore_expected_page)

    def _restore_expected_page(self) -> bool:
        if self.pages.get_visible_child_name() != self.expected_page:
            self.pages.set_visible_child_name(self.expected_page)
        return GLib.SOURCE_REMOVE

    def _grid_error(self, grid: MediaGrid, error: Exception) -> bool:
        grid.set_status(
            "dialog-error-symbolic",
            "Something went wrong",
            str(error),
        )
        return GLib.SOURCE_REMOVE

    def open_connection(self) -> None:
        if self.connection_window:
            self.connection_window.present()
            return
        self.connection_window = ConnectionWindow(self, self._connect_jellyfin)
        self.connection_window.connect("close-request", self._connection_closed)
        self.connection_window.present()

    def _connection_closed(self, *_args: object) -> bool:
        self.connection_window = None
        return False

    def _connect_jellyfin(
        self,
        server: str,
        username: str,
        password: str,
        _button: Gtk.Button,
    ) -> None:
        run_async(
            lambda: self.jellyfin.authenticate(server, username, password),
            self._jellyfin_connected,
            lambda error: self.connection_window.show_error(str(error))
            if self.connection_window
            else None,
        )

    def _jellyfin_connected(self, session: JellyfinSession) -> bool:
        self.config.save_session(session)
        self.jellyfin_loaded = False
        self._set_account(session)
        if self.connection_window:
            self.connection_window.close()
        self.toast_overlay.add_toast(Adw.Toast(title=f"Connected as {session.username}"))
        self._select_page("jellyfin")
        return GLib.SOURCE_REMOVE

    def _set_account(self, session: JellyfinSession) -> None:
        self.account_row.set_title(session.username)
        self.account_row.set_subtitle(session.server_url)
        self.disconnect_action.set_enabled(True)

    def disconnect_jellyfin(self) -> None:
        self.config.clear_session()
        self.jellyfin.session = None
        self.jellyfin_loaded = False
        self.disconnect_action.set_enabled(False)
        self.account_row.set_title("Jellyfin")
        self.account_row.set_subtitle("Not connected")
        self.toast_overlay.add_toast(Adw.Toast(title="Disconnected from Jellyfin"))

    def _key_pressed(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        state: Gdk.ModifierType,
    ) -> bool:
        if keyval == Gdk.KEY_Escape and self.pages.get_visible_child_name() == "player":
            self._leave_player()
            return True
        if keyval == Gdk.KEY_l and state & Gdk.ModifierType.CONTROL_MASK:
            page = self.pages.get_visible_child_name()
            if page == "jellyfin":
                self.jellyfin_search.grab_focus()
            else:
                self._select_page("youtube")
                self.youtube_search.grab_focus()
            return True
        return False

    def show_shortcuts(self) -> None:
        shortcuts = Gtk.ShortcutsWindow(transient_for=self, modal=True)
        section = Gtk.ShortcutsSection(section_name="general", title="General")
        group = Gtk.ShortcutsGroup(title="Navigation")
        group.add_shortcut(Gtk.ShortcutsShortcut(title="Focus search", accelerator="<Control>L"))
        group.add_shortcut(Gtk.ShortcutsShortcut(title="Leave player", accelerator="Escape"))
        group.add_shortcut(Gtk.ShortcutsShortcut(title="Quit", accelerator="<Control>Q"))
        section.add_group(group)
        shortcuts.add_section(section)
        shortcuts.present()


class TubeFinApplication(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.window: TubeFinWindow | None = None

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        self._load_css()
        self._add_action("about", self._show_about)
        self._add_action("quit", lambda *_: self.quit())
        self.set_accels_for_action("app.quit", ["<Control>q"])

    def do_activate(self) -> None:
        if not self.window:
            self.window = TubeFinWindow(self)
            shortcuts = Gio.SimpleAction.new("shortcuts", None)
            shortcuts.connect("activate", lambda *_: self.window.show_shortcuts())
            self.window.add_action(shortcuts)
        self.window.present()

    def _add_action(self, name: str, callback: Callable[..., None]) -> None:
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", callback)
        self.add_action(action)

    @staticmethod
    def _load_css() -> None:
        provider = Gtk.CssProvider()
        css = resources.files("tubefin").joinpath("style.css").read_text(encoding="utf-8")
        provider.load_from_string(css)
        display = Gdk.Display.get_default()
        if display:
            Gtk.StyleContext.add_provider_for_display(
                display,
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )

    def _show_about(self, *_args: object) -> None:
        about = Adw.AboutWindow(
            transient_for=self.window,
            application_name="TubeFin",
            application_icon=APP_ID,
            developer_name="TubeFin contributors",
            version=__version__,
            comments="A native YouTube and Jellyfin client for GNOME.",
            website="https://github.com/doromiert/tubefin",
            issue_url="https://github.com/doromiert/tubefin/issues",
            license_type=Gtk.License.GPL_3_0,
            developers=["TubeFin contributors"],
            copyright="© 2026 TubeFin contributors",
        )
        about.present()
