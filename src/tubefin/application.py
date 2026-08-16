from __future__ import annotations

import shutil
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

from tubefin import __version__  # noqa: E402
from tubefin.config import SPONSORBLOCK_CATEGORIES, ConfigStore  # noqa: E402
from tubefin.downloads import DownloadManager  # noqa: E402
from tubefin.jellyfin_sync import JellyfinSyncPlayClient  # noqa: E402
from tubefin.library import (  # noqa: E402
    ChannelFeedCache,
    HistoryStore,
    OfflineLibrary,
    PlaylistStore,
    SubscriptionStore,
)
from tubefin.models import (  # noqa: E402
    AudioTrack,
    Availability,
    ChannelDetails,
    ChannelSubscription,
    Comment,
    CommentPage,
    DownloadRecord,
    DownloadStatus,
    JellyfinSession,
    MediaItem,
    MediaSection,
    OAuthAccount,
    ResolvedStream,
    SponsorSegment,
    StreamVariant,
    VideoChapter,
    VideoDetails,
    YouTubeBrowserSession,
)
from tubefin.mpris import MprisService  # noqa: E402
from tubefin.mpv_player import MpvPlayer  # noqa: E402
from tubefin.oauth import COMMENT_SCOPE, MANAGE_SCOPE, OAuthClient  # noqa: E402
from tubefin.played_cache import PlayedVideoCache  # noqa: E402
from tubefin.services import (  # noqa: E402
    JellyfinService,
    SeerrService,
    SponsorBlockService,
    YouTubeService,
)
from tubefin.streaming import PrebufferedStream, PrebufferManager  # noqa: E402
from tubefin.sync import RoomState, SyncTubeClient  # noqa: E402
from tubefin.widgets import (  # noqa: E402
    MediaCard,
    MediaGrid,
    SectionShelf,
    ThumbnailLoader,
    icon_label,
    labeled_button,
)

APP_ID = "io.github.doromiert.TubeFin"
PLAYER_SIDEBAR_MIN_WIDTH = 280
SPONSORBLOCK_CATEGORY_DETAILS = {
    "sponsor": ("Sponsors", "Paid promotions and advertisements"),
    "selfpromo": ("Self-promotion", "Merchandise, donations, and unpaid promotion"),
    "interaction": ("Interaction reminders", "Requests to subscribe, like, or comment"),
    "intro": ("Intros", "Intermission and intro animations"),
    "outro": ("Outros", "Endcards and credits"),
    "preview": ("Previews and recaps", "Previews of this or another video"),
    "hook": ("Hooks", "Opening teasers before the main content"),
    "music_offtopic": ("Non-music sections", "Non-music portions of music videos"),
    "filler": ("Filler", "Tangents and content not essential to the main topic"),
}
SPONSORBLOCK_BEHAVIOR_LABELS = ("Auto-skip", "Show skip button", "Ignore")
SPONSORBLOCK_BEHAVIOR_VALUES = ("auto", "button", "ignore")
HOME_SECTION_TITLES = {
    "local_history": "Continue Watching · Local history",
    "offline": "Available Offline · This device",
    "jellyfin_continue": "Continue watching · Jellyfin",
    "jellyfin_recent": "Recently added · Jellyfin",
    "youtube_activity": "Subscription Activity · Provided by YouTube",
    "recommendations": "Recommended · YouTube",
    "watched_channels": "From Channels You Watched · Ranked locally",
}


@dataclass
class PlaybackLoadTrace:
    request_id: int
    title: str
    source: str
    started_at: float
    marks: dict[str, float] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)
    error: str = ""


class VideoAspectFrame(Gtk.AspectFrame):
    """Aspect frame with a non-zero height inside a vertical scroll viewport."""

    def __init__(self, height_limit: Callable[[], int | None] | None = None) -> None:
        super().__init__(ratio=16 / 9, obey_child=False)
        self._height_limit = height_limit
        self._requested_video_height = 360
        self.set_size_request(-1, self._requested_video_height)

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        Gtk.AspectFrame.do_size_allocate(self, width, height, baseline)
        requested = max(1, round(width * 9 / 16))
        if self._height_limit:
            limit = self._height_limit()
            if limit is not None and limit > 0:
                requested = min(requested, limit)
        if requested != self._requested_video_height:
            self._requested_video_height = requested
            GLib.idle_add(self._apply_video_height, requested)

    def _apply_video_height(self, requested: int) -> bool:
        if requested == self._requested_video_height:
            self.set_size_request(-1, requested)
        return GLib.SOURCE_REMOVE


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
        on_quick_connect: Callable[[str, Gtk.Button], None],
    ) -> None:
        super().__init__(transient_for=parent, modal=True, title="Connect to Jellyfin")
        self.set_default_size(480, 500)
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

        intro = Gtk.Label(
            label="Enter your server once, then approve TubeFin from Jellyfin in your browser."
        )
        intro.set_wrap(True)
        intro.set_justify(Gtk.Justification.LEFT)
        intro.set_xalign(0)
        intro.add_css_class("dim-label")
        content.append(intro)

        group = Adw.PreferencesGroup()
        content.append(group)

        self.server = Adw.EntryRow(title="Server address")
        self.server.set_text("http://localhost:8096")
        self.server.set_input_purpose(Gtk.InputPurpose.URL)
        group.add(self.server)

        self.error = Gtk.Label()
        self.error.add_css_class("error")
        self.error.set_wrap(True)
        self.error.set_visible(False)
        content.append(self.error)

        self.quick_button = Gtk.Button(
            label="Sign in with browser", icon_name="web-browser-symbolic"
        )
        self.quick_button.add_css_class("suggested-action")
        self.quick_button.add_css_class("pill")
        self.quick_button.set_halign(Gtk.Align.CENTER)
        self.quick_button.connect("clicked", self._quick_clicked, on_quick_connect)
        content.append(self.quick_button)

        self.quick_status = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.quick_status.add_css_class("dim-label")
        self.quick_status.set_visible(False)
        content.append(self.quick_status)

        manual = Gtk.Expander(label="Use username and password instead")
        manual_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        manual_group = Adw.PreferencesGroup()
        self.username = Adw.EntryRow(title="Username")
        manual_group.add(self.username)
        self.password = Adw.PasswordEntryRow(title="Password")
        manual_group.add(self.password)
        manual_content.append(manual_group)
        self.connect_button = Gtk.Button(label="Sign in with password")
        self.connect_button.set_halign(Gtk.Align.END)
        self.connect_button.connect("clicked", self._connect_clicked, on_connect)
        manual_content.append(self.connect_button)
        manual.set_child(manual_content)
        content.append(manual)
        self.password.connect("entry-activated", lambda *_: self.connect_button.activate())

    def _quick_clicked(
        self,
        _button: Gtk.Button,
        on_quick_connect: Callable[[str, Gtk.Button], None],
    ) -> None:
        server = self.server.get_text().strip()
        if not server:
            self.show_error("Enter your Jellyfin server address.")
            return
        self.error.set_visible(False)
        self.quick_button.set_sensitive(False)
        self.quick_button.set_label("Requesting code…")
        on_quick_connect(server, self.quick_button)

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
        self.connect_button.set_label("Sign in with password")
        self.quick_button.set_sensitive(True)
        self.quick_button.set_label("Sign in with browser")
        self.quick_status.set_visible(False)
        return GLib.SOURCE_REMOVE

    def show_quick_code(self, code: str) -> None:
        self.quick_button.set_label("Waiting for approval…")
        self.quick_status.set_label(f"In Jellyfin, open Settings → Quick Connect and enter: {code}")
        self.quick_status.set_visible(True)


class TubeFinWindow(Adw.ApplicationWindow):
    def __init__(self, application: Adw.Application) -> None:
        super().__init__(application=application, title="TubeFin")
        self.set_default_size(1180, 760)
        self.set_size_request(100, 180)

        self.config = ConfigStore()
        self.home_section_order = self.config.load_home_section_order()
        self.home_section_widgets: dict[str, SectionShelf] = {}
        self.home_pull_distance = 0.0
        self.home_pull_refreshing = False
        self.player_settings = self.config.load_player_settings()
        self.sync_settings = self.config.load_sync_settings()
        oauth_settings = self.config.load_oauth_settings()
        self.youtube = YouTubeService(str(oauth_settings["browser"]))
        self.sponsorblock = SponsorBlockService()
        self.sponsorblock_enabled = bool(self.player_settings["sponsorblock_enabled"])
        self.sponsorblock_categories = dict(
            self.player_settings["sponsorblock_categories"]  # type: ignore[arg-type]
        )
        self.default_caption_language = str(
            self.player_settings["default_caption_language"]
        )
        self.preferred_audio_language = str(
            self.player_settings["preferred_audio_language"]
        )
        self.youtube_browser_session: YouTubeBrowserSession | None = None
        self.youtube_browser_checking = bool(self.youtube.browser)
        self.youtube_browser_error = ""
        self.jellyfin = JellyfinService(self.config.load_session())
        self.seerr_settings = self.config.load_seerr_settings()
        self.seerr = SeerrService(
            self.seerr_settings["url"],
            self.seerr_settings["api_key"],
            self.config.directory / "seerr-cookies.txt",
        )
        self.seerr_available = False
        self.seerr_authenticated = bool(
            self.seerr_settings["api_key"] or self.seerr.has_session
        )
        self.seerr_authenticating = False
        self.seerr_auto_auth_attempted_url = ""
        self.pending_seerr_credentials: tuple[str, str] | None = None
        self.seerr_search_generation = 0
        self.offline = OfflineLibrary()
        self.downloads = DownloadManager(
            self.offline,
            browser=self.youtube.browser,
            direct_resolver=self.jellyfin.download_source,
        )
        self.playlists = PlaylistStore()
        self.history = HistoryStore()
        self.channel_feeds = ChannelFeedCache()
        self.locally_marked_watched = {
            (entry.item.source, entry.item.id)
            for entry in self.history.list(500)
            if entry.duration and entry.position >= entry.duration * 0.9
        }
        self.subscriptions = SubscriptionStore()
        self.prebuffer = PrebufferManager()
        self.played_cache = PlayedVideoCache()
        self.played_cache_source: tuple[str, ResolvedStream] | None = None
        self.played_cache_buffered: PrebufferedStream | None = None
        self.played_cache_active: PrebufferedStream | None = None
        self.played_cache_request_item_id = ""
        self.oauth = OAuthClient(str(oauth_settings["client_id"]))
        self.oauth_accounts: list[OAuthAccount] = list(oauth_settings["accounts"])  # type: ignore[arg-type]
        self.active_oauth_account = next(
            (
                account
                for account in self.oauth_accounts
                if account.id == oauth_settings["active_account_id"]
            ),
            None,
        )
        self.sync_client: SyncTubeClient | None = None
        self.sync_role = ""
        self.synctube_known_members: set[str] = set()
        self.synctube_roster_initialized = False
        self.sync_window: Adw.Window | None = None
        self.sync_room_entry: Gtk.Entry | None = None
        self.sync_status_label: Gtk.Label | None = None
        self.sync_members_list: Gtk.ListBox | None = None
        self.sync_create_button: Gtk.Button | None = None
        self.sync_join_button: Gtk.Button | None = None
        self.sync_disconnect_button: Gtk.Button | None = None
        self.jellyfin_sync_client: JellyfinSyncPlayClient | None = None
        self.jellyfin_sync_window: Adw.Window | None = None
        self.jellyfin_sync_groups: Gtk.ListBox | None = None
        self.jellyfin_sync_status: Gtk.Label | None = None
        self.jellyfin_sync_name: Gtk.Entry | None = None
        self.jellyfin_sync_create_button: Gtk.Button | None = None
        self.jellyfin_sync_join_button: Gtk.Button | None = None
        self.jellyfin_sync_leave_button: Gtk.Button | None = None
        self.jellyfin_sync_selected_group = ""
        self.jellyfin_sync_published_item = ""
        self.jellyfin_sync_published_position = 0.0
        self.jellyfin_sync_published_at = 0.0
        self.jellyfin_sync_published_paused: bool | None = None
        self.jellyfin_sync_applying_until = 0.0
        self.jellyfin_sync_playlist_item = ""
        self.last_history_update = 0.0
        self.last_jellyfin_update = 0.0
        self.last_playback_position = 0.0
        self.last_playback_duration = 0.0
        self.last_playback_paused = True
        self.playback_started_at = 0.0
        self.queue_advance_item_id = ""
        self.last_reported_pause: bool | None = None
        self.resume_position_offer = 0.0
        self.resume_item_id = ""
        self.resume_offer_shown = False
        self.sponsor_segments: list[SponsorSegment] = []
        self.skipped_sponsor_segments: set[tuple[float, float]] = set()
        self.manual_sponsor_segment: SponsorSegment | None = None
        self.sponsor_undo_position = 0.0
        self.sponsor_undo_item_id = ""
        self.youtube_marked_watched: set[str] = set()
        self.pending_sync_state: RoomState | None = None
        self.thumbnails = ThumbnailLoader()
        self.current_item: MediaItem | None = None
        self.mpv_player: MpvPlayer | None = None
        self.connection_window: ConnectionWindow | None = None
        self.settings_window: Adw.Window | None = None
        self.seerr_login_window: Adw.Window | None = None
        self.jellyfin_history: list[tuple[str, str]] = []
        self.jellyfin_parent_id = ""
        self.jellyfin_loaded = False
        self.detail_item: MediaItem | None = None
        self.detail_series_play_item: MediaItem | None = None
        self.detail_series_episodes: list[MediaItem] = []
        self.detail_series_generation = 0
        self.active_playlist_id = ""
        self.youtube_playlist_items: list[MediaItem] = []
        self.remote_playlist_active = False
        self.pre_fullscreen_sidebar_widths = (180.0, 280.0, 0.25)
        self.resolved_stream_cache: dict[str, tuple[float, ResolvedStream]] = {}
        self.resolved_stream_inflight: dict[str, threading.Event] = {}
        self.resolved_stream_lock = threading.Lock()
        self.playback_request = 0
        self.playback_load_traces: list[PlaybackLoadTrace] = []
        self.playback_load_trace_lock = threading.Lock()
        self.playback_load_window: Adw.Window | None = None
        self.playback_load_summary: Gtk.Label | None = None
        self.playback_load_list: Gtk.ListBox | None = None
        self.playback_load_refresh_source = 0
        self.expected_page = "home"
        self.player_expanded = False
        self.player_navigation_guard_until = 0.0
        self.active_navigation = "home"
        self.syncing_navigation = False
        self.queue: list[MediaItem] = []
        self.queue_index = -1
        self.queue_loop = False
        self.comment_cursor: str | None = None
        self.comments_loading = False
        self.subscription_updates_loading = False
        self.channel_page = 1
        self.channel_loading = False
        self.channel_has_more = False
        self.channel_url = ""
        self.current_channel: ChannelDetails | None = None
        self.syncing_subscription_controls = False
        self.browse_search_results = False
        self.browse_cache: dict[str, tuple[float, list[MediaItem]]] = {}
        self.channel_cache: dict[str, tuple[float, ChannelDetails]] = {}
        self.recommendation_shelf: SectionShelf | None = None
        self.recommendation_items: list[MediaItem] = []
        self.recommendation_next = 1
        self.recommendation_loading = False
        self.recommendation_exhausted = False
        self.recommendation_generation = 0
        self.subscriptions_syncing = False
        self.history_syncing = False
        self.clearing_all_data = False
        self.seek_feedback_source = 0
        self.navigation_history: list[dict[str, Any]] = []

        self.toast_overlay = Adw.ToastOverlay()
        self.set_content(self.toast_overlay)

        self.split_view = Adw.NavigationSplitView()
        self.toast_overlay.set_child(self.split_view)
        self.sidebar_page = self._build_sidebar()
        self.split_view.set_sidebar(self.sidebar_page)
        self.content_page = self._build_content()
        self.split_view.set_content(self.content_page)

        self.disconnect_action = Gio.SimpleAction.new("disconnect", None)
        self.disconnect_action.connect("activate", lambda *_: self.disconnect_jellyfin())
        self.disconnect_action.set_enabled(self.jellyfin.session is not None)
        self.add_action(self.disconnect_action)
        sync_action = Gio.SimpleAction.new("sync-room", None)
        sync_action.connect("activate", lambda *_: self.open_sync_room())
        self.add_action(sync_action)
        jellyfin_sync_action = Gio.SimpleAction.new("jellyfin-sync-room", None)
        jellyfin_sync_action.connect(
            "activate", lambda *_: self.open_jellyfin_sync_room()
        )
        self.add_action(jellyfin_sync_action)
        settings_action = Gio.SimpleAction.new("settings", None)
        settings_action.connect("activate", lambda *_: self.open_settings())
        self.add_action(settings_action)
        resume_action = Gio.SimpleAction.new("resume-playback", None)
        resume_action.connect("activate", lambda *_: self._resume_playback())
        self.add_action(resume_action)
        undo_sponsor_action = Gio.SimpleAction.new("undo-sponsor-skip", None)
        undo_sponsor_action.connect(
            "activate", lambda *_: self._undo_sponsor_skip()
        )
        self.add_action(undo_sponsor_action)

        self.navigation.select_row(self.navigation.get_row_at_index(0))
        if self.jellyfin.session:
            self._set_account(self.jellyfin.session)
            self._discover_seerr()
        self._load_home_sections()
        self._sync_online_subscriptions()
        self._sync_online_history()
        GLib.timeout_add_seconds(15 * 60, self._poll_subscription_updates)
        if self.youtube.browser:
            self._verify_youtube_browser_session()

        controller = Gtk.EventControllerKey()
        controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        controller.connect("key-pressed", self._key_pressed)
        self.add_controller(controller)
        self.connect("close-request", self._close_player)
        self.mpris = MprisService(self)

    def _build_sidebar(self) -> Adw.NavigationPage:
        toolbar = Adw.ToolbarView()
        toolbar.set_size_request(190, -1)
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        title = Gtk.Label(label="tubefin", xalign=0)
        title.add_css_class("sidebar-brand")
        header.set_title_widget(title)
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
            ("browse", "Browse", "compass2-symbolic"),
            ("library", "Library", "library-symbolic"),
        ]:
            row = Adw.ActionRow(title=title)
            row.set_name(key)
            row.add_prefix(Gtk.Image.new_from_icon_name(icon))
            self.navigation.append(row)

        self.requests_navigation_row = Adw.ActionRow(title="Requests")
        self.requests_navigation_row.set_name("requests")
        self.requests_navigation_row.add_prefix(
            Gtk.Image.new_from_icon_name("edit-find-symbolic")
        )
        self.requests_navigation_row.set_visible(self.jellyfin.session is not None)
        self.navigation.append(self.requests_navigation_row)

        spacer = Gtk.Box(vexpand=True)
        sidebar.append(spacer)

        self.sidebar_download = Gtk.Button()
        self.sidebar_download.add_css_class("flat")
        self.sidebar_download.add_css_class("sidebar-download")
        self.sidebar_download.set_tooltip_text("Open downloads")
        self.sidebar_download.connect("clicked", lambda *_: self._select_page("downloads"))
        download_row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        download_title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        download_title_row.append(Gtk.Image.new_from_icon_name("folder-download-symbolic"))
        self.sidebar_download_title = Gtk.Label(label="Downloads", xalign=0, hexpand=True)
        download_title_row.append(self.sidebar_download_title)
        download_row.append(download_title_row)
        self.sidebar_download_detail = Gtk.Label(xalign=0)
        self.sidebar_download_detail.add_css_class("caption")
        self.sidebar_download_detail.add_css_class("dim-label")
        download_row.append(self.sidebar_download_detail)
        self.sidebar_download_progress = Gtk.ProgressBar()
        download_row.append(self.sidebar_download_progress)
        self.sidebar_download.set_child(download_row)
        sidebar.append(self.sidebar_download)
        self._refresh_sidebar_downloads()

        # Kept as state for the connection callbacks; account controls live in Settings.
        self.account_row = Adw.ActionRow(title="Jellyfin", subtitle="Not connected")

        return Adw.NavigationPage(title="TubeFin", child=toolbar)

    def _build_content(self) -> Adw.NavigationPage:
        toolbar = Adw.ToolbarView()
        self.header = Adw.HeaderBar()
        self.header.add_css_class("content-header-overlay")
        self.window_title = Adw.WindowTitle(title="TubeFin", subtitle="")
        self.global_search = Gtk.SearchEntry(placeholder_text="Search")
        self.global_search.set_hexpand(True)
        self.global_search.connect("activate", self._global_search_requested)
        self.global_search_clamp = Adw.Clamp(maximum_size=400)
        self.global_search_clamp.set_child(self.global_search)
        self.header.set_title_widget(self.global_search_clamp)
        header_overlay = Gtk.Overlay()
        self.header_overlay = header_overlay
        header_background = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.header_background = header_background
        header_background.add_css_class("content-header-background")
        header_background.append(Gtk.Box(hexpand=True))
        self.header_sidebar_background = Gtk.Box(width_request=340)
        self.header_sidebar_background.add_css_class(
            "player-sidebar-header-background"
        )
        self.header_sidebar_background.set_visible(False)
        header_background.append(self.header_sidebar_background)
        header_overlay.set_child(header_background)
        header_overlay.add_overlay(self.header)
        header_overlay.set_measure_overlay(self.header, True)
        toolbar.add_top_bar(header_overlay)
        self.context_back = Gtk.Button(
            icon_name="go-previous-symbolic", tooltip_text="Back", visible=False
        )
        self.context_back.connect("clicked", lambda *_: self._context_back_requested())
        self.header.pack_start(self.context_back)
        self.home_refresh = Gtk.Button(
            icon_name="view-refresh-symbolic", tooltip_text="Refresh Home"
        )
        self.home_refresh.connect("clicked", lambda *_: self._refresh_home())
        self.header.pack_start(self.home_refresh)

        menu = Gio.Menu()
        menu.append("About TubeFin", "app.about")
        menu.append("Keyboard Shortcuts", "win.shortcuts")
        menu.append("Settings", "win.settings")
        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        self.header.pack_end(menu_button)
        self.account_sync_status = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=6
        )
        self.account_sync_status.set_tooltip_text(
            "TubeFin is downloading account data"
        )
        self.account_sync_spinner = Gtk.Spinner()
        self.account_sync_status.append(self.account_sync_spinner)
        self.account_sync_status.set_visible(False)
        self.header.pack_end(self.account_sync_status)
        watch_menu = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        watch_menu.set_margin_top(8)
        watch_menu.set_margin_bottom(8)
        watch_menu.set_margin_start(8)
        watch_menu.set_margin_end(8)
        self.watch_youtube_button = labeled_button(
            "YouTube · SyncTube", "video-x-generic-symbolic"
        )
        self.watch_youtube_button.add_css_class("flat")
        watch_menu.append(self.watch_youtube_button)
        self.watch_jellyfin_button = labeled_button(
            "Jellyfin · SyncPlay", "network-server-symbolic"
        )
        self.watch_jellyfin_button.add_css_class("flat")
        self.watch_jellyfin_button.set_sensitive(bool(self.jellyfin.session))
        watch_menu.append(self.watch_jellyfin_button)
        self.watch_together_popover = Gtk.Popover(child=watch_menu)
        self.watch_together_button = Gtk.MenuButton(
            icon_name="people-symbolic",
            tooltip_text="Start or join a watch party",
            popover=self.watch_together_popover,
        )
        self.watch_youtube_button.connect(
            "clicked", lambda *_: self._open_watch_together_choice("youtube")
        )
        self.watch_jellyfin_button.connect(
            "clicked", lambda *_: self._open_watch_together_choice("jellyfin")
        )
        # Compatibility alias for existing state updates and smoke checks.
        self.syncplay_button = self.watch_together_button
        self.header.pack_end(self.watch_together_button)

        self.content_overlay = Gtk.Overlay()
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.content_overlay.set_child(content)
        toolbar.set_content(self.content_overlay)
        self.sync_banner = Adw.Banner(
            title="Connected to SyncTube, Jellyfin streaming is unsupported"
        )
        self.sync_banner.set_button_label("Disconnect")
        self.sync_banner.connect(
            "button-clicked", lambda *_: self._disconnect_watch_together()
        )
        self.sync_banner.set_revealed(False)
        content.append(self.sync_banner)

        self.pages = Gtk.Stack()
        self.pages.set_hhomogeneous(False)
        self.pages.set_vhomogeneous(False)
        self.pages.connect("notify::visible-child-name", self._visible_page_changed)
        self.pages.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.pages.set_transition_duration(180)
        self.pages.set_vexpand(True)
        self.pages.add_named(self._build_home_page(), "home")
        self.pages.add_named(self._build_browse_page(), "browse")
        self.pages.add_named(self._build_browse_category_page(), "browse-category")
        self.pages.add_named(self._build_library_page(), "library")
        self.pages.add_named(self._build_requests_page(), "requests")
        self.pages.add_named(self._build_offline_page(), "downloads")
        self.pages.add_named(self._build_history_page(), "history")
        self.pages.add_named(self._build_playlist_page(), "playlist")
        self.pages.add_named(self._build_details_page(), "details")
        self.pages.add_named(self._build_channel_page(), "channel")

        self.page_overlay = Gtk.Overlay()
        self.page_overlay.set_vexpand(True)
        self.page_overlay.set_child(self.pages)
        self.player_page = self._build_player_page()
        self.player_page.add_css_class("player-page")
        self.player_page.set_hexpand(True)
        self.player_page.set_vexpand(True)
        self.player_revealer = Gtk.Revealer()
        self.player_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self.player_revealer.set_transition_duration(220)
        self.player_revealer.set_hexpand(True)
        self.player_revealer.set_vexpand(True)
        self.player_revealer.set_child(self.player_page)
        self.player_revealer.set_reveal_child(False)
        self.player_revealer.set_can_target(False)
        self.player_revealer.connect(
            "notify::child-revealed", self._player_reveal_finished
        )
        self.page_overlay.add_overlay(self.player_revealer)
        content.append(self.page_overlay)

        self.mini_player = self._build_mini_player()
        self.mini_player.set_visible(False)
        content.append(self.mini_player)

        return Adw.NavigationPage(title="Content", child=toolbar)

    def _build_home_page(self) -> Gtk.Widget:
        scroller = Gtk.ScrolledWindow(vexpand=True)
        self.home_scroller = scroller
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.get_vadjustment().connect("value-changed", self._home_scroll_changed)
        clamp = Adw.Clamp(maximum_size=2400, tightening_threshold=1600)
        clamp.set_margin_top(18)
        clamp.set_margin_bottom(36)
        clamp.set_margin_start(24)
        clamp.set_margin_end(24)
        scroller.set_child(clamp)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        clamp.set_child(content)

        self.home_pull_revealer = Gtk.Revealer()
        self.home_pull_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        pull_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        pull_row.set_halign(Gtk.Align.CENTER)
        self.home_pull_spinner = Gtk.Spinner()
        pull_row.append(self.home_pull_spinner)
        self.home_pull_label = Gtk.Label(label="Pull to refresh")
        self.home_pull_label.add_css_class("dim-label")
        pull_row.append(self.home_pull_label)
        self.home_pull_revealer.set_child(pull_row)
        content.append(self.home_pull_revealer)

        scroll_controller = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.VERTICAL
        )
        scroll_controller.connect("scroll", self._home_overscroll)
        scroller.add_controller(scroll_controller)
        pull_gesture = Gtk.GestureDrag()
        pull_gesture.set_touch_only(True)
        pull_gesture.connect("drag-begin", self._home_pull_begin)
        pull_gesture.connect("drag-update", self._home_pull_update)
        pull_gesture.connect("drag-end", self._home_pull_end)
        scroller.add_controller(pull_gesture)

        signed_out = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.home_signed_out = signed_out
        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        hero.add_css_class("hero")
        hero.set_margin_top(8)
        hero.set_margin_bottom(8)

        greeting = Gtk.Label(label="Welcome to TubeFin")
        greeting.add_css_class("title-1")
        greeting.set_wrap(True)
        greeting.set_justify(Gtk.Justification.CENTER)
        hero.append(greeting)

        copy = Gtk.Label(label="Sign in for recommendations")
        copy.add_css_class("dim-label")
        copy.set_wrap(True)
        copy.set_justify(Gtk.Justification.CENTER)
        hero.append(copy)
        signed_out.append(hero)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        actions.set_halign(Gtk.Align.CENTER)
        youtube_button = Gtk.Button(icon_name="media-playback-start-symbolic")
        youtube_button.set_tooltip_text("Sign in to YouTube")
        youtube_button.add_css_class("service-button")
        youtube_button.connect("clicked", lambda *_: self.open_settings())
        actions.append(youtube_button)
        jellyfin_button = Gtk.Button(icon_name="network-server-symbolic")
        jellyfin_button.set_tooltip_text("Connect Jellyfin")
        jellyfin_button.add_css_class("service-button")
        jellyfin_button.connect("clicked", lambda *_: self.open_connection())
        self.home_jellyfin_button = jellyfin_button
        actions.append(jellyfin_button)
        signed_out.append(actions)
        content.append(signed_out)

        self.home_sections = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=28)
        content.append(self.home_sections)
        self.recommendation_loading_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8
        )
        self.recommendation_loading_row.set_halign(Gtk.Align.CENTER)
        self.recommendation_spinner = Gtk.Spinner()
        self.recommendation_loading_row.append(self.recommendation_spinner)
        recommendation_loading_label = Gtk.Label(
            label="Downloading more recommendations…"
        )
        recommendation_loading_label.add_css_class("dim-label")
        self.recommendation_loading_row.append(recommendation_loading_label)
        self.recommendation_loading_row.set_visible(False)
        content.append(self.recommendation_loading_row)
        return scroller

    def _build_browse_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        responsive = Adw.BreakpointBin(vexpand=True)
        responsive.set_child(page)
        self.youtube_search = self.global_search
        self.jellyfin_search = self.global_search
        categories = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24)
        categories.add_css_class("browse-destinations")
        categories.set_halign(Gtk.Align.CENTER)
        categories.set_valign(Gtk.Align.CENTER)
        categories.set_vexpand(True)
        narrow_browse = Adw.Breakpoint.new(
            Adw.BreakpointCondition.parse("max-width: 680px")
        )
        narrow_browse.add_setter(
            categories, "orientation", Gtk.Orientation.VERTICAL
        )
        responsive.add_breakpoint(narrow_browse)
        self.browse_mode = "youtube"
        self.browse_buttons: dict[str, Gtk.Button] = {}
        for key, title, icon in (
            ("movies", "Movies", "video-display-symbolic"),
            ("shows", "Shows", "folder-videos-symbolic"),
            ("channels", "Channels", "avatar-default-symbolic"),
        ):
            destination = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=9)
            destination.set_halign(Gtk.Align.CENTER)
            destination.set_valign(Gtk.Align.CENTER)
            image = Gtk.Image.new_from_icon_name(icon)
            image.set_pixel_size(48)
            destination.append(image)
            label = Gtk.Label(label=title)
            label.add_css_class("title-3")
            destination.append(label)
            button = Gtk.Button(child=destination)
            button.add_css_class("browse-destination")
            button.set_valign(Gtk.Align.CENTER)
            button.connect("clicked", lambda _button, value=key: self._open_browse_category(value))
            categories.append(button)
            self.browse_buttons[key] = button
        page.append(categories)
        return responsive

    def _build_requests_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        page.set_margin_top(24)
        page.set_margin_bottom(24)
        page.set_margin_start(24)
        page.set_margin_end(24)
        search_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.seerr_search = Gtk.SearchEntry(
            placeholder_text="Search movies and shows", hexpand=True
        )
        self.seerr_search.set_sensitive(False)
        self.seerr_search.connect("activate", self._seerr_search_requested)
        search_row.append(self.seerr_search)
        self.seerr_connect = Gtk.Button(label="Configure Seerr")
        self.seerr_connect.set_tooltip_text("Set the Seerr address in Settings")
        self.seerr_connect.connect("clicked", self._seerr_connect_clicked)
        search_row.append(self.seerr_connect)
        page.append(search_row)
        self.seerr_status = Adw.StatusPage(
            icon_name="edit-find-symbolic",
            title="Find something to watch",
            description="Search Seerr, then request a movie or every season of a show.",
        )
        page.append(self.seerr_status)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        self.seerr_results = Gtk.FlowBox()
        self.seerr_results.add_css_class("media-grid")
        self.seerr_results.set_selection_mode(Gtk.SelectionMode.NONE)
        self.seerr_results.set_homogeneous(True)
        self.seerr_results.set_min_children_per_line(1)
        self.seerr_results.set_max_children_per_line(5)
        self.seerr_results.set_column_spacing(16)
        self.seerr_results.set_row_spacing(18)
        scroller.set_child(self.seerr_results)
        page.append(scroller)
        return page

    def _build_browse_category_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        # Category navigation lives in the window header. Keep these widgets as
        # non-rendered state holders for Jellyfin's nested folder history.
        self.browse_category_heading = Gtk.Box(visible=False)
        self.jellyfin_back = Gtk.Button(visible=False)
        self.browse_category_title = Gtk.Label(label="Browse", xalign=0, hexpand=True)
        self.browse_channel_positions: dict[str, int] = {}
        self.browse_channel_alphabet = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=2
        )
        self.browse_channel_alphabet.add_css_class("channel-alphabet-rail")
        self.browse_channel_alphabet.set_halign(Gtk.Align.CENTER)
        for letter in ("#", *"ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
            jump = Gtk.Button(label=letter, tooltip_text=f"Jump to {letter}")
            jump.add_css_class("flat")
            jump.add_css_class("alphabet-jump")
            jump.connect(
                "clicked",
                lambda _button, value=letter: self._jump_to_browse_channel(value),
            )
            self.browse_channel_alphabet.append(jump)
        self.browse_channel_alphabet_scroller = Gtk.ScrolledWindow()
        self.browse_channel_alphabet_scroller.set_policy(
            Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC
        )
        self.browse_channel_alphabet_scroller.set_propagate_natural_width(True)
        self.browse_channel_alphabet_scroller.set_child(
            self.browse_channel_alphabet
        )
        self.browse_channel_alphabet_scroller.set_visible(False)
        self.youtube_grid = MediaGrid(
            self.thumbnails,
            self._activate_item,
            self._add_to_queue,
            self._prebuffer_item,
            self._add_to_queue_next,
            self._save_item,
            self._watch_later,
            avatar_resolver=self.youtube.channel_avatar,
            on_download=self._download_item,
            on_mark_watched=self._mark_watched,
            on_share=self._share_item,
        )
        self.jellyfin_grid = self.youtube_grid
        self.youtube_grid.status.set_visible(False)
        browse_results = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8, vexpand=True
        )
        self.youtube_grid.set_hexpand(True)
        browse_results.append(self.youtube_grid)
        browse_results.append(self.browse_channel_alphabet_scroller)
        page.append(browse_results)
        return page

    def _build_library_page(self) -> Gtk.Widget:
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        clamp = Adw.Clamp(maximum_size=2400, tightening_threshold=1600)
        clamp.set_margin_top(24)
        clamp.set_margin_bottom(36)
        clamp.set_margin_start(24)
        clamp.set_margin_end(24)
        scroller.set_child(clamp)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=28)
        clamp.set_child(content)

        history_heading = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        history_title = Gtk.Label(label="History", xalign=0, hexpand=True)
        history_title.add_css_class("title-2")
        history_heading.append(history_title)
        history_more = Gtk.Button(label="More", icon_name="go-next-symbolic")
        history_more.add_css_class("flat")
        history_more.connect("clicked", lambda *_: self._open_full_history())
        history_heading.append(history_more)
        content.append(history_heading)
        self.library_history = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(self.library_history)

        subscriptions_title = Gtk.Label(label="Channel subscriptions", xalign=0)
        subscriptions_title.add_css_class("title-2")
        self.subscription_lookup: dict[str, ChannelSubscription] = {}
        self.subscription_positions: dict[str, int] = {}
        self.subscription_model = Gtk.StringList.new([])
        subscription_selection = Gtk.NoSelection(model=self.subscription_model)
        subscription_factory = Gtk.SignalListItemFactory()
        subscription_factory.connect("setup", self._subscription_row_setup)
        subscription_factory.connect("bind", self._subscription_row_bind)
        self.library_subscriptions = Gtk.ListView(
            model=subscription_selection,
            factory=subscription_factory,
        )
        self.library_subscriptions.add_css_class("boxed-list")
        self.library_subscriptions.set_single_click_activate(False)
        self.subscription_scroller = Gtk.ScrolledWindow()
        self.subscription_scroller.set_policy(
            Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC
        )
        self.subscription_scroller.set_min_content_height(480)
        self.subscription_scroller.set_max_content_height(620)
        self.subscription_scroller.set_propagate_natural_height(True)
        self.subscription_scroller.set_child(self.library_subscriptions)
        self.subscription_empty = Adw.StatusPage(
            icon_name="avatar-default-symbolic",
            title="No channel subscriptions yet",
            description="Open a YouTube channel and select Subscribe.",
        )
        self.subscription_stack = Gtk.Stack()
        self.subscription_stack.add_named(self.subscription_scroller, "subscriptions")
        self.subscription_stack.add_named(self.subscription_empty, "empty")
        self.subscription_alphabet = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=2
        )
        self.subscription_alphabet.set_halign(Gtk.Align.CENTER)
        for letter in ("#", *"ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
            jump = Gtk.Button(label=letter, tooltip_text=f"Jump to {letter}")
            jump.add_css_class("flat")
            jump.add_css_class("alphabet-jump")
            jump.connect(
                "clicked", lambda _button, value=letter: self._jump_to_subscription(value)
            )
            self.subscription_alphabet.append(jump)
        self.subscription_alphabet_scroller = Gtk.ScrolledWindow()
        self.subscription_alphabet_scroller.set_policy(
            Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER
        )
        self.subscription_alphabet_scroller.set_propagate_natural_height(True)
        self.subscription_alphabet_scroller.set_child(self.subscription_alphabet)

        playlist_heading = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.library_playlist_heading = playlist_heading
        playlist_title = Gtk.Label(label="Playlists", xalign=0)
        playlist_title.add_css_class("title-2")
        playlist_heading.append(playlist_title)
        playlist_title.set_hexpand(True)
        self.new_playlist_name = Gtk.Entry(placeholder_text="New playlist", hexpand=True)
        self.new_playlist_name.connect("activate", lambda *_: self._create_playlist())
        create = Gtk.Button(label="Create", icon_name="list-add-symbolic")
        create.connect("clicked", lambda *_: self._create_playlist())
        self.youtube_playlist_url = Gtk.Entry(placeholder_text="YouTube playlist URL", hexpand=True)
        browse_playlist = Gtk.Button(label="Browse", icon_name="folder-open-symbolic")
        browse_playlist.connect("clicked", lambda *_: self._browse_youtube_playlist())
        playlist_actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        playlist_actions.set_margin_top(10)
        playlist_actions.set_margin_bottom(10)
        playlist_actions.set_margin_start(10)
        playlist_actions.set_margin_end(10)
        new_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        new_row.append(self.new_playlist_name)
        new_row.append(create)
        playlist_actions.append(new_row)
        import_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        import_row.append(self.youtube_playlist_url)
        import_row.append(browse_playlist)
        playlist_actions.append(import_row)
        manage_playlists = Gtk.MenuButton(
            icon_name="list-add-symbolic",
            tooltip_text="Create or import a playlist",
            popover=Gtk.Popover(child=playlist_actions),
        )
        manage_playlists.connect(
            "notify::active",
            lambda button, _property: (
                GLib.idle_add(self.new_playlist_name.grab_focus) if button.get_active() else None
            ),
        )
        self.manage_playlists_button = manage_playlists
        manage_playlists.add_css_class("square-button")
        manage_playlists.set_size_request(40, 40)
        manage_playlists.set_valign(Gtk.Align.CENTER)
        playlist_heading.append(manage_playlists)
        import_playlists = Gtk.Button(
            icon_name="document-open-symbolic", tooltip_text="Import playlists"
        )
        import_playlists.connect("clicked", lambda *_: self._choose_playlist_import())
        import_playlists.add_css_class("square-button")
        import_playlists.set_size_request(40, 40)
        import_playlists.set_valign(Gtk.Align.CENTER)
        playlist_heading.append(import_playlists)
        export_playlists = Gtk.Button(
            icon_name="document-save-symbolic", tooltip_text="Export playlists"
        )
        export_playlists.connect("clicked", lambda *_: self._choose_playlist_export())
        export_playlists.add_css_class("square-button")
        export_playlists.set_size_request(40, 40)
        export_playlists.set_valign(Gtk.Align.CENTER)
        playlist_heading.append(export_playlists)
        content.append(playlist_heading)
        self.playlists_box = Gtk.FlowBox()
        self.playlists_box.add_css_class("playlist-grid")
        self.playlists_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self.playlists_box.set_halign(Gtk.Align.CENTER)
        self.playlists_box.set_homogeneous(False)
        self.playlists_box.set_min_children_per_line(1)
        self.playlists_box.set_max_children_per_line(3)
        self.playlists_box.set_column_spacing(18)
        self.playlists_box.set_row_spacing(18)
        content.append(self.playlists_box)
        self.library_subscriptions_title = subscriptions_title
        content.append(subscriptions_title)
        content.append(self.subscription_alphabet_scroller)
        content.append(self.subscription_stack)

        return scroller

    def _build_history_page(self) -> Gtk.Widget:
        self.history_grid = MediaGrid(
            self.thumbnails,
            self._activate_item,
            self._add_to_queue,
            self._prebuffer_item,
            self._add_to_queue_next,
            self._save_item,
            self._watch_later,
            avatar_resolver=self.youtube.channel_avatar,
            on_download=self._download_item,
            on_mark_watched=self._mark_watched,
            on_share=self._share_item,
        )
        self.history_grid.status.set_visible(False)
        return self.history_grid

    def _build_playlist_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        heading = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        heading.add_css_class("search-strip")
        self.playlist_title = Gtk.Label(label="Playlist")
        self.playlist_title.add_css_class("title-1")
        self.playlist_title.set_xalign(0)
        self.playlist_title.set_hexpand(True)
        heading.append(self.playlist_title)
        self.playlist_play = Gtk.Button(label="Play all", icon_name="media-playback-start-symbolic")
        self.playlist_play.add_css_class("suggested-action")
        self.playlist_play.add_css_class("pill")
        self.playlist_play.connect("clicked", lambda *_: self._play_active_playlist())
        heading.append(self.playlist_play)
        playlist_menu = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        playlist_menu.set_margin_top(10)
        playlist_menu.set_margin_bottom(10)
        playlist_menu.set_margin_start(10)
        playlist_menu.set_margin_end(10)
        self.playlist_name_entry = Gtk.Entry(placeholder_text="Playlist name")
        self.playlist_name_entry.connect(
            "activate",
            lambda entry: self._rename_playlist(self.active_playlist_id, entry.get_text()),
        )
        playlist_menu.append(self.playlist_name_entry)
        delete = Gtk.Button(label="Delete playlist", icon_name="edit-delete-symbolic")
        delete.add_css_class("destructive-action")
        delete.connect("clicked", lambda *_: self._delete_active_playlist())
        playlist_menu.append(delete)
        playlist_options = Gtk.MenuButton(
            icon_name="view-more-symbolic",
            tooltip_text="Playlist options",
            popover=Gtk.Popover(child=playlist_menu),
        )
        playlist_options.connect(
            "notify::active",
            lambda button, _property: (
                GLib.idle_add(self.playlist_name_entry.grab_focus) if button.get_active() else None
            ),
        )
        self.playlist_options_button = playlist_options
        playlist_options.add_css_class("square-button")
        playlist_options.set_size_request(40, 40)
        playlist_options.set_valign(Gtk.Align.CENTER)
        heading.append(playlist_options)
        export_playlist = Gtk.Button(
            icon_name="document-save-symbolic", tooltip_text="Export playlists"
        )
        export_playlist.connect("clicked", lambda *_: self._choose_playlist_export())
        export_playlist.add_css_class("square-button")
        export_playlist.set_size_request(40, 40)
        export_playlist.set_valign(Gtk.Align.CENTER)
        heading.append(export_playlist)
        page.append(heading)
        self.playlist_header_controls: list[Gtk.Widget] = [
            playlist_options,
            export_playlist,
        ]

        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.playlist_items = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.playlist_items.add_css_class("playlist-items")
        scroller.set_child(self.playlist_items)
        page.append(scroller)
        return page

    def _build_offline_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        toolbar.add_css_class("search-strip")
        self.offline_search = Gtk.SearchEntry(placeholder_text="Search offline library")
        self.offline_search.set_hexpand(True)
        self.offline_search.connect("search-changed", lambda *_: self._load_offline())
        toolbar.append(self.offline_search)
        self.offline_usage = Gtk.Label()
        self.offline_usage.add_css_class("dim-label")
        toolbar.append(self.offline_usage)
        page.append(toolbar)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.offline_grid = Gtk.FlowBox()
        self.offline_grid.add_css_class("media-grid")
        self.offline_grid.set_halign(Gtk.Align.START)
        self.offline_grid.set_valign(Gtk.Align.START)
        self.offline_grid.set_hexpand(True)
        self.offline_grid.set_selection_mode(Gtk.SelectionMode.NONE)
        self.offline_grid.set_homogeneous(True)
        self.offline_grid.set_min_children_per_line(1)
        self.offline_grid.set_max_children_per_line(20)
        self.offline_grid.set_column_spacing(10)
        self.offline_grid.set_row_spacing(14)
        self.offline_grid.set_margin_top(18)
        self.offline_grid.set_margin_bottom(28)
        self.offline_grid.set_margin_start(18)
        self.offline_grid.set_margin_end(18)
        scroller.set_child(self.offline_grid)
        page.append(scroller)
        return page

    def _build_playlists_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        toolbar.add_css_class("search-strip")
        self.new_playlist_name = Gtk.Entry(placeholder_text="New local playlist", hexpand=True)
        self.new_playlist_name.connect("activate", lambda *_: self._create_playlist())
        toolbar.append(self.new_playlist_name)
        create = Gtk.Button(label="Create", icon_name="list-add-symbolic")
        create.connect("clicked", lambda *_: self._create_playlist())
        toolbar.append(create)
        self.youtube_playlist_url = Gtk.Entry(placeholder_text="YouTube playlist URL", hexpand=True)
        toolbar.append(self.youtube_playlist_url)
        browse = Gtk.Button(label="Browse", icon_name="folder-open-symbolic")
        browse.connect("clicked", lambda *_: self._browse_youtube_playlist())
        toolbar.append(browse)
        page.append(toolbar)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        self.playlists_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.playlists_box.set_margin_top(18)
        self.playlists_box.set_margin_bottom(24)
        self.playlists_box.set_margin_start(18)
        self.playlists_box.set_margin_end(18)
        scroller.set_child(self.playlists_box)
        page.append(scroller)
        return page

    def _build_account_page(self) -> Gtk.Widget:
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        content.set_margin_top(18)
        content.set_margin_bottom(18)
        intro = Gtk.Label(
            label=(
                "Configure this when developing TubeFin, posting YouTube comments, or editing "
                "remote YouTube playlists. Sign-in, personal feeds, browsing, and playback use "
                "the browser session above."
            ),
            xalign=0,
            wrap=True,
        )
        intro.add_css_class("dim-label")
        content.append(intro)
        group = Adw.PreferencesGroup(title="Developer credentials")
        self.oauth_client_id = Adw.EntryRow(title="Desktop client ID")
        self.oauth_client_id.set_text(str(self.config.load_oauth_settings()["client_id"]))
        group.add(self.oauth_client_id)
        save_row = Adw.ActionRow(
            title="Client configuration",
            subtitle="Save the desktop OAuth client ID entered above.",
        )
        save_client = Gtk.Button(label="Save", valign=Gtk.Align.CENTER)
        save_client.connect("clicked", lambda *_: self._save_oauth_client())
        save_row.add_suffix(save_client)
        group.add(save_row)
        content.append(group)

        access = Adw.PreferencesGroup(title="API account access")
        standard = Adw.ActionRow(
            title="Standard account",
            subtitle="Read account activity and post comments.",
        )
        sign_in = Gtk.Button(label="Connect", valign=Gtk.Align.CENTER)
        sign_in.add_css_class("suggested-action")
        sign_in.connect("clicked", lambda *_: self._oauth_sign_in(False))
        standard.add_suffix(sign_in)
        access.add(standard)
        managed = Adw.ActionRow(
            title="Playlist editing account",
            subtitle="Also create, edit, and delete remote YouTube playlists.",
        )
        manage = Gtk.Button(label="Connect", valign=Gtk.Align.CENTER)
        manage.connect("clicked", lambda *_: self._oauth_sign_in(True))
        managed.add_suffix(manage)
        access.add(managed)
        content.append(access)

        accounts = Adw.PreferencesGroup(title="Connected API accounts")
        self.account_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.account_list.add_css_class("boxed-list")
        accounts.add(self.account_list)
        content.append(accounts)

        feeds = Adw.PreferencesGroup(title="Account data")
        for label, feed in (
            ("Subscriptions", "subscriptions"),
            ("Liked videos", "liked"),
            ("History / activity", "history"),
            ("Account playlists", "playlists"),
        ):
            row = Adw.ActionRow(title=label)
            button = Gtk.Button(label="Open", valign=Gtk.Align.CENTER)
            button.connect("clicked", lambda _button, name=feed: self._load_account_feed(name))
            row.add_suffix(button)
            feeds.add(row)
        content.append(feeds)

        playlists = Adw.PreferencesGroup(title="Remote playlists")
        self.account_playlist_name = Adw.EntryRow(title="New account playlist")
        create_account_playlist = Gtk.Button(
            label="Create", icon_name="list-add-symbolic", valign=Gtk.Align.CENTER
        )
        create_account_playlist.connect("clicked", lambda *_: self._create_account_playlist())
        self.account_playlist_name.add_suffix(create_account_playlist)
        playlists.add(self.account_playlist_name)
        content.append(playlists)
        self.account_playlist_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        content.append(self.account_playlist_box)
        self.account_grid = MediaGrid(
            self.thumbnails,
            self._activate_item,
            self._add_to_queue,
            on_queue_next=self._add_to_queue_next,
            on_save=self._save_item,
            on_watch_later=self._watch_later,
            avatar_resolver=self.youtube.channel_avatar,
            on_download=self._download_item,
            on_mark_watched=self._mark_watched,
            on_share=self._share_item,
        )
        self.account_grid.set_size_request(-1, 400)
        content.append(self.account_grid)
        return content

    def _build_channel_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        heading = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        heading.add_css_class("search-strip")
        channel_avatar_frame = Gtk.Overlay(width_request=52, height_request=52)
        channel_avatar_frame.set_size_request(52, 52)
        channel_avatar_frame.set_overflow(Gtk.Overflow.HIDDEN)
        channel_avatar_frame.add_css_class("channel-avatar")
        channel_avatar_frame.set_child(Gtk.Image.new_from_icon_name("avatar-default-symbolic"))
        self.channel_avatar = Gtk.Picture(width_request=52, height_request=52)
        self.channel_avatar.set_content_fit(Gtk.ContentFit.COVER)
        self.channel_avatar.set_visible(False)
        channel_avatar_frame.add_overlay(self.channel_avatar)
        heading.append(channel_avatar_frame)
        channel_copy = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=True
        )
        self.channel_heading = Gtk.Label(xalign=0)
        self.channel_heading.add_css_class("title-2")
        channel_copy.append(self.channel_heading)
        self.channel_subscriber_count = Gtk.Label(xalign=0)
        self.channel_subscriber_count.add_css_class("caption")
        self.channel_subscriber_count.add_css_class("dim-label")
        channel_copy.append(self.channel_subscriber_count)
        heading.append(channel_copy)
        self.channel_subscribe = Gtk.Button(
            icon_name="list-add-symbolic",
            tooltip_text="Subscribe",
        )
        self.channel_subscribe.add_css_class("circular")
        self.channel_subscribe.set_size_request(40, 40)
        self.channel_subscribe.set_valign(Gtk.Align.CENTER)
        self.channel_subscribe.connect("clicked", self._channel_subscription_clicked)
        heading.append(self.channel_subscribe)
        self.channel_notifications = Gtk.ToggleButton(
            icon_name="preferences-system-notifications-symbolic",
            tooltip_text="New-video notifications",
        )
        self.channel_notifications.add_css_class("square-button")
        self.channel_notifications.set_size_request(40, 40)
        self.channel_notifications.set_valign(Gtk.Align.CENTER)
        self.channel_notifications.connect("toggled", self._channel_notifications_toggled)
        heading.append(self.channel_notifications)
        self.channel_share = Gtk.Button(
            icon_name="send-to-symbolic", tooltip_text="Copy channel link"
        )
        self.channel_share.add_css_class("square-button")
        self.channel_share.set_size_request(40, 40)
        self.channel_share.set_valign(Gtk.Align.CENTER)
        self.channel_share.set_sensitive(False)
        self.channel_share.connect("clicked", lambda *_: self._share_channel())
        heading.append(self.channel_share)
        self.channel_more_spinner = Gtk.Spinner(tooltip_text="Loading more videos")
        self.channel_more_spinner.set_visible(False)
        heading.append(self.channel_more_spinner)
        page.append(heading)
        self.channel_grid = MediaGrid(
            self.thumbnails,
            self._activate_item,
            self._add_to_queue,
            self._prebuffer_item,
            self._add_to_queue_next,
            self._save_item,
            self._watch_later,
            avatar_resolver=self.youtube.channel_avatar,
            on_download=self._download_item,
            show_channel=False,
            on_mark_watched=self._mark_watched,
            on_share=self._share_item,
        )
        self.channel_grid.scroller.get_vadjustment().connect(
            "value-changed", self._channel_scroll_changed
        )
        page.append(self.channel_grid)
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

        self.jellyfin_grid = MediaGrid(
            self.thumbnails,
            self._activate_item,
            self._add_to_queue,
            self._prebuffer_item,
            on_mark_watched=self._mark_watched,
            on_share=self._share_item,
        )
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
        page = Gtk.Overlay()
        self.player_page_overlay = page
        player_column = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=0, hexpand=True
        )
        self.player_column = player_column
        page.set_child(player_column)
        back = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="Back")
        back.connect("clicked", lambda *_: self._leave_player())
        self.header.pack_start(back)
        self.player_heading = Gtk.Label()
        self.player_heading.add_css_class("heading")
        self.player_heading.set_ellipsize(Pango.EllipsizeMode.END)
        self.player_live_chat_button = Gtk.ToggleButton(
            icon_name="chat-bubbles-empty-symbolic", tooltip_text="Live chat"
        )
        self.player_live_chat_button.connect("toggled", self._toggle_player_live_chat)
        self.header.pack_end(self.player_live_chat_button)
        self.queue_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.queue_box.set_margin_top(10)
        self.queue_box.set_margin_bottom(10)
        self.queue_box.set_margin_start(10)
        self.queue_box.set_margin_end(10)
        queue_popover = Gtk.Popover(child=self.queue_box)
        self.queue_button = Gtk.MenuButton(
            icon_name="view-list-symbolic", tooltip_text="Queue", popover=queue_popover
        )
        self.header.pack_end(self.queue_button)
        self.player_header_controls = [
            back,
            self.player_live_chat_button,
            self.queue_button,
        ]
        for control in self.player_header_controls:
            control.set_visible(False)
        self.player_bar = self.header

        self.player_controls_host = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.player_controls_host.set_visible(False)
        self.reparenting_player_controls = False
        player_column.append(self.player_controls_host)
        self.player_scroller = Gtk.ScrolledWindow(vexpand=True)
        self.player_scroller.set_policy(
            Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC
        )
        player_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        player_content.set_hexpand(True)
        self.player_scroller.set_child(player_content)
        player_column.append(self.player_scroller)

        playback = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        playback.set_hexpand(True)
        stage = Gtk.Overlay()
        stage.add_css_class("player-stage")
        stage.set_hexpand(True)
        stage.set_vexpand(True)
        self.mpv_player = MpvPlayer(
            self._mpv_ready,
            self._mpv_error,
            on_previous=lambda: self._play_previous_queued(reveal_player=True),
            on_next=lambda: self._play_next_queued(reveal_player=True),
            on_fullscreen=self._toggle_fullscreen,
            on_collapse=self._leave_player,
            on_fullscreen_swipe=self._set_fullscreen_swipe_progress,
            on_seek_feedback=self._show_seek_feedback,
            on_controls_visibility=self._set_fullscreen_chrome_visible,
            buffer_seconds=int(self.player_settings["buffer_seconds"]),
            on_buffer_changed=lambda seconds: self.config.save_player_settings(
                buffer_seconds=seconds
            ),
            on_state_changed=self._player_state_changed,
            default_caption_language=self.default_caption_language,
            default_audio_language=self.preferred_audio_language,
        )
        stage.set_child(self.mpv_player)
        self.fullscreen_title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.fullscreen_title.add_css_class("fullscreen-title")
        self.fullscreen_title.set_halign(Gtk.Align.FILL)
        self.fullscreen_title.set_valign(Gtk.Align.START)
        self.fullscreen_title.set_can_target(False)
        self.fullscreen_title.set_visible(False)
        stage.add_overlay(self.fullscreen_title)
        self.fullscreen_comments_button = Gtk.ToggleButton(
            icon_name="user-available-symbolic",
            tooltip_text="Comments",
        )
        self.fullscreen_comments_button.add_css_class("fullscreen-comments-button")
        self.fullscreen_comments_button.set_size_request(42, 42)
        self.fullscreen_comments_button.set_visible(False)
        self.fullscreen_comments_button.connect(
            "toggled", self._toggle_player_comments
        )
        self.fullscreen_live_chat_button = Gtk.ToggleButton(
            label="Live chat",
            icon_name="chat-bubbles-empty-symbolic",
            tooltip_text="Live chat",
        )
        self.fullscreen_live_chat_button.add_css_class(
            "fullscreen-sidebar-button"
        )
        self.fullscreen_live_chat_button.set_visible(False)
        self.fullscreen_live_chat_button.connect(
            "toggled", self._toggle_player_live_chat
        )
        self.fullscreen_sidebar_controls = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )
        self.fullscreen_sidebar_controls.set_halign(Gtk.Align.END)
        self.fullscreen_sidebar_controls.set_valign(Gtk.Align.START)
        self.fullscreen_sidebar_controls.set_margin_top(16)
        self.fullscreen_sidebar_controls.set_margin_end(16)
        self.fullscreen_sidebar_controls.append(self.fullscreen_comments_button)
        self.fullscreen_sidebar_controls.append(self.fullscreen_live_chat_button)
        stage.add_overlay(self.fullscreen_sidebar_controls)
        self.seek_feedback = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=6
        )
        self.seek_feedback.add_css_class("seek-feedback")
        self.seek_feedback.set_halign(Gtk.Align.CENTER)
        self.seek_feedback.set_valign(Gtk.Align.CENTER)
        self.seek_feedback_icon = Gtk.Image()
        self.seek_feedback_icon.set_pixel_size(42)
        self.seek_feedback.append(self.seek_feedback_icon)
        self.seek_feedback_label = Gtk.Label()
        self.seek_feedback_label.add_css_class("heading")
        self.seek_feedback.append(self.seek_feedback_label)
        self.seek_feedback.set_visible(False)
        stage.add_overlay(self.seek_feedback)
        self.sponsor_skip_button = Gtk.Button(
            label="Skip segment",
            icon_name="media-skip-forward-symbolic",
            tooltip_text="Skip this SponsorBlock segment",
        )
        self.sponsor_skip_button.add_css_class("sponsor-skip-button")
        self.sponsor_skip_button.set_halign(Gtk.Align.END)
        self.sponsor_skip_button.set_valign(Gtk.Align.END)
        self.sponsor_skip_button.set_margin_end(24)
        self.sponsor_skip_button.set_margin_bottom(80)
        self.sponsor_skip_button.set_visible(False)
        self.sponsor_skip_button.connect(
            "clicked", lambda *_: self._skip_manual_sponsor_segment()
        )
        stage.add_overlay(self.sponsor_skip_button)
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
        stage_frame = VideoAspectFrame(self._normal_video_height_limit)
        stage_frame.set_hexpand(True)
        stage_frame.set_child(stage)
        playback.append(stage_frame)

        self.player_sidebar_width = 340
        self.player_sidebar_drag_width = self.player_sidebar_width
        self.player_page_active = False
        self.player_comments_panel = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=0,
            width_request=self.player_sidebar_width,
        )
        self.player_comments_panel.add_css_class("comments-sidebar")
        self.player_comments_panel.set_halign(Gtk.Align.END)
        self.player_comments_panel.set_valign(Gtk.Align.FILL)
        self.player_comments_panel.set_vexpand(True)
        self.player_comments_panel.set_visible(False)
        comments_resize = self._player_sidebar_resize_handle()
        self.player_comments_panel.append(comments_resize)
        comments_content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=10, hexpand=True
        )
        comments_content.add_css_class("player-sidebar-content")
        self.player_comments_panel.append(comments_content)
        comments_heading = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        comments_title = Gtk.Label(label="Comments", xalign=0, hexpand=True)
        comments_title.add_css_class("title-2")
        comments_heading.append(comments_title)
        self.comments_more = Gtk.Button(label="Load", icon_name="view-more-symbolic")
        self.comments_more.connect("clicked", lambda *_: self._load_comments())
        self.comments_more.set_visible(False)
        comments_heading.append(self.comments_more)
        comments_close = Gtk.Button(
            icon_name="window-close-symbolic", tooltip_text="Close comments"
        )
        comments_close.add_css_class("flat")
        comments_close.connect(
            "clicked", lambda *_: self._close_player_sidebar()
        )
        comments_heading.append(comments_close)
        comments_content.append(comments_heading)
        self.comment_composer = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=8
        )
        self.comment_composer.add_css_class("comment-composer")
        editor_overlay = Gtk.Overlay()
        comment_editor = Gtk.ScrolledWindow()
        self.comment_editor = comment_editor
        comment_editor.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        comment_editor.set_min_content_height(44)
        comment_editor.set_max_content_height(128)
        comment_editor.set_propagate_natural_height(True)
        self.comment_entry = Gtk.TextView(
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
            accepts_tab=False,
            hexpand=True,
        )
        self.comment_entry.add_css_class("comment-entry")
        self.comment_entry.get_buffer().connect(
            "changed", lambda *_: self._comment_text_changed()
        )
        comment_keys = Gtk.EventControllerKey()
        comment_keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        comment_keys.connect("key-pressed", self._comment_entry_key_pressed)
        self.comment_entry.add_controller(comment_keys)
        comment_editor.set_child(self.comment_entry)
        editor_overlay.set_child(comment_editor)
        self.comment_placeholder = Gtk.Label(
            label="Add a comment…", xalign=0, yalign=0, wrap=True
        )
        self.comment_placeholder.add_css_class("comment-placeholder")
        self.comment_placeholder.set_halign(Gtk.Align.FILL)
        self.comment_placeholder.set_valign(Gtk.Align.START)
        self.comment_placeholder.set_can_target(False)
        editor_overlay.add_overlay(self.comment_placeholder)
        self.comment_composer.append(editor_overlay)
        comment_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        comment_actions.append(Gtk.Box(hexpand=True))
        self.comment_send = Gtk.Button(tooltip_text="Post comment")
        comment_send_content = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=6,
        )
        comment_send_content.append(Gtk.Label(label="Post comment"))
        comment_send_content.append(
            Gtk.Image.new_from_icon_name("paper-plane-symbolic")
        )
        self.comment_send.set_child(comment_send_content)
        self.comment_send.connect("clicked", lambda *_: self._post_comment())
        comment_actions.append(self.comment_send)
        self.comment_composer.append(comment_actions)
        self.comment_posting = False
        comments_content.append(self.comment_composer)
        self.comments_loading_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8
        )
        self.comments_loading_row.set_halign(Gtk.Align.CENTER)
        self.comments_spinner = Gtk.Spinner()
        self.comments_loading_row.append(self.comments_spinner)
        self.comments_loading_label = Gtk.Label(label="Loading comments…")
        self.comments_loading_row.append(self.comments_loading_label)
        self.comments_loading_row.set_visible(False)
        comments_content.append(self.comments_loading_row)
        comments_scroller = Gtk.ScrolledWindow(vexpand=True)
        self.comments_scroller = comments_scroller
        comments_scroller.get_vadjustment().connect(
            "value-changed", self._comments_scroll_changed
        )
        self.comments_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        comments_scroller.set_child(self.comments_box)
        comments_content.append(comments_scroller)
        self.content_overlay.add_overlay(self.player_comments_panel)

        self.player_live_chat_panel = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=0,
            width_request=self.player_sidebar_width,
        )
        self.player_live_chat_panel.add_css_class("live-chat-sidebar")
        self.player_live_chat_panel.set_halign(Gtk.Align.END)
        self.player_live_chat_panel.set_valign(Gtk.Align.FILL)
        self.player_live_chat_panel.set_vexpand(True)
        self.player_live_chat_panel.set_visible(False)
        live_chat_resize = self._player_sidebar_resize_handle()
        self.player_live_chat_panel.append(live_chat_resize)
        live_chat_content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=10, hexpand=True
        )
        live_chat_content.add_css_class("player-sidebar-content")
        self.player_live_chat_panel.append(live_chat_content)
        live_chat_heading = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8
        )
        live_chat_title = Gtk.Label(label="Live chat", xalign=0, hexpand=True)
        live_chat_title.add_css_class("title-2")
        live_chat_heading.append(live_chat_title)
        live_chat_close = Gtk.Button(
            icon_name="window-close-symbolic", tooltip_text="Close live chat"
        )
        live_chat_close.add_css_class("flat")
        live_chat_close.connect(
            "clicked", lambda *_: self._close_player_sidebar()
        )
        live_chat_heading.append(live_chat_close)
        live_chat_content.append(live_chat_heading)
        self.live_chat_status = Gtk.Label(xalign=0, wrap=True)
        self.live_chat_status.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.live_chat_status.set_max_width_chars(1)
        self.live_chat_status.add_css_class("dim-label")
        live_chat_content.append(self.live_chat_status)
        self.live_chat_scroller = Gtk.ScrolledWindow(vexpand=True)
        self.live_chat_scroller.set_policy(
            Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC
        )
        self.live_chat_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.live_chat_rows: dict[str, Gtk.Widget] = {}
        self.live_chat_scroller.set_child(self.live_chat_box)
        live_chat_content.append(self.live_chat_scroller)
        live_chat_input = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.live_chat_entry = Gtk.Entry(
            placeholder_text="Send a message…", hexpand=True, max_length=200
        )
        # Gtk.Entry otherwise contributes a theme-dependent default width that
        # can make this panel wider than the comments panel at its minimum.
        self.live_chat_entry.set_width_chars(1)
        self.live_chat_entry.set_max_width_chars(1)
        self.live_chat_entry.connect("activate", lambda *_: self._send_live_chat())
        live_chat_input.append(self.live_chat_entry)
        self.live_chat_send = Gtk.Button(
            icon_name="paper-plane-symbolic", tooltip_text="Send message"
        )
        self.live_chat_send.connect("clicked", lambda *_: self._send_live_chat())
        live_chat_input.append(self.live_chat_send)
        live_chat_content.append(live_chat_input)
        self.content_overlay.add_overlay(self.player_live_chat_panel)
        self.player_playback_row = playback
        player_content.append(playback)
        self.player_inline_controls_host = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL
        )
        player_content.append(self.player_inline_controls_host)
        self._set_player_controls_sticky(False)
        self.player_scroller.get_vadjustment().connect(
            "value-changed", self._player_scroll_changed
        )

        self.player_details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        details = self.player_details
        details.add_css_class("player-details")
        self.player_title = Gtk.Label(xalign=0)
        self.player_title.add_css_class("title-2")
        self.player_title.set_ellipsize(Pango.EllipsizeMode.END)
        self.player_title.set_hexpand(True)
        self.player_title.set_max_width_chars(1)
        details.append(self.player_title)
        player_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        channel_cluster = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=10, hexpand=True
        )
        channel_cluster.set_valign(Gtk.Align.CENTER)
        self.player_avatar = Gtk.Overlay(width_request=36, height_request=36)
        self.player_avatar.set_size_request(36, 36)
        self.player_avatar.set_valign(Gtk.Align.CENTER)
        self.player_avatar.set_overflow(Gtk.Overflow.HIDDEN)
        self.player_avatar.add_css_class("channel-avatar")
        player_avatar_fallback = Gtk.Image.new_from_icon_name("avatar-default-symbolic")
        player_avatar_fallback.set_pixel_size(20)
        self.player_avatar.set_child(player_avatar_fallback)
        self.player_avatar_picture = Gtk.Picture(width_request=36, height_request=36)
        self.player_avatar_picture.set_size_request(36, 36)
        self.player_avatar_picture.set_content_fit(Gtk.ContentFit.COVER)
        self.player_avatar_picture.set_visible(False)
        self.player_avatar.add_overlay(self.player_avatar_picture)
        self.player_avatar_button = Gtk.Button(child=self.player_avatar)
        self.player_avatar_button.add_css_class("flat")
        self.player_avatar_button.add_css_class("channel-avatar-button")
        self.player_avatar_button.set_valign(Gtk.Align.CENTER)
        self.player_avatar_button.set_tooltip_text("Open channel")
        self.player_avatar_button.connect("clicked", lambda *_: self._open_current_channel())
        channel_cluster.append(self.player_avatar_button)
        self.player_subtitle = Gtk.Label(xalign=0)
        self.player_subtitle.add_css_class("dim-label")
        self.player_subtitle.set_ellipsize(Pango.EllipsizeMode.END)
        self.player_channel_button = Gtk.Button(child=self.player_subtitle)
        self.player_channel_button.add_css_class("flat")
        self.player_channel_button.add_css_class("channel-name-button")
        self.player_channel_button.set_halign(Gtk.Align.START)
        self.player_channel_button.set_valign(Gtk.Align.CENTER)
        self.player_channel_button.set_tooltip_text("Open channel")
        self.player_channel_button.connect("clicked", lambda *_: self._open_current_channel())
        channel_cluster.append(self.player_channel_button)
        self.player_subscribe = Gtk.Button(
            icon_name="list-add-symbolic", tooltip_text="Subscribe"
        )
        self.player_subscribe.add_css_class("circular")
        self.player_subscribe.set_size_request(40, 40)
        self.player_subscribe.set_valign(Gtk.Align.CENTER)
        self.player_subscribe.connect("clicked", lambda *_: self._toggle_current_subscription())
        channel_cluster.append(self.player_subscribe)
        player_actions.append(channel_cluster)
        save_current = Gtk.Button(
            icon_name="list-add-symbolic", tooltip_text="Save to playlist"
        )
        save_current.set_valign(Gtk.Align.CENTER)
        save_current.connect(
            "clicked", lambda *_: self.current_item and self._save_item(self.current_item)
        )
        player_actions.append(save_current)
        share_current = Gtk.Button(
            icon_name="send-to-symbolic", tooltip_text="Copy share link"
        )
        share_current.set_valign(Gtk.Align.CENTER)
        share_current.connect(
            "clicked", lambda *_: self.current_item and self._share_item(self.current_item)
        )
        player_actions.append(share_current)
        self.player_download = Gtk.Button(
            icon_name="folder-download-symbolic", tooltip_text="Download"
        )
        self.player_download.set_valign(Gtk.Align.CENTER)
        self.player_download.connect(
            "clicked", lambda *_: self.current_item and self._download_item(self.current_item)
        )
        player_actions.append(self.player_download)
        self.player_comments_button = Gtk.ToggleButton(
            icon_name="user-available-symbolic", tooltip_text="Comments"
        )
        self.player_comments_button.set_valign(Gtk.Align.CENTER)
        self.player_comments_button.connect("toggled", self._toggle_player_comments)
        player_actions.append(self.player_comments_button)
        details.append(player_actions)
        self.player_actions = player_actions
        self.player_description = Gtk.Label(
            xalign=0, yalign=0, wrap=True
        )
        self.player_description.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.player_description.set_max_width_chars(1)
        self.player_description.add_css_class("dim-label")
        self.player_description.set_hexpand(True)
        details.append(self.player_description)
        player_content.append(details)
        page.connect("notify::width", self._update_player_sidebar_layout)
        self._refresh_queue()
        return page

    def _close_player_sidebar(self) -> None:
        self.player_navigation_guard_until = time.monotonic() + 0.75
        self.syncing_navigation = True
        self.navigation.unselect_all()
        self.syncing_navigation = False
        if self.mpv_player:
            self.mpv_player.grab_focus()
        if self.player_comments_panel.get_visible():
            self.player_comments_button.set_active(False)
        elif self.player_live_chat_panel.get_visible():
            self.player_live_chat_button.set_active(False)

    def _player_sidebar_resize_handle(self) -> Gtk.Widget:
        handle = Gtk.Box(width_request=10)
        handle.add_css_class("player-sidebar-resize-handle")
        handle.set_cursor_from_name("col-resize")
        drag = Gtk.GestureDrag(button=1)
        drag.connect("drag-begin", self._player_sidebar_drag_begin)
        drag.connect("drag-update", self._player_sidebar_drag_update)
        handle.add_controller(drag)
        return handle

    def _player_sidebar_drag_begin(
        self, _gesture: Gtk.GestureDrag, _x: float, _y: float
    ) -> None:
        self.player_sidebar_drag_width = self.player_sidebar_width

    def _player_sidebar_drag_update(
        self, _gesture: Gtk.GestureDrag, offset_x: float, _offset_y: float
    ) -> None:
        page_width = max(360, self.player_page_overlay.get_width())
        width = round(self.player_sidebar_drag_width - offset_x)
        self.player_sidebar_width = max(
            PLAYER_SIDEBAR_MIN_WIDTH, min(width, page_width - 80)
        )
        for panel in (self.player_comments_panel, self.player_live_chat_panel):
            panel.set_size_request(self.player_sidebar_width, -1)
        self._comment_text_changed()
        self._update_player_sidebar_layout()

    def _update_player_sidebar_layout(self, *_args: object) -> None:
        visible = self.player_page_active and (
            self.player_comments_panel.get_visible()
            or self.player_live_chat_panel.get_visible()
        )
        page_width = self.player_page_overlay.get_width()
        if page_width > 0:
            maximum = max(PLAYER_SIDEBAR_MIN_WIDTH, page_width - 80)
            if self.player_sidebar_width > maximum:
                self.player_sidebar_width = maximum
                for panel in (
                    self.player_comments_panel,
                    self.player_live_chat_panel,
                ):
                    panel.set_size_request(self.player_sidebar_width, -1)
        self.header_sidebar_background.set_size_request(
            self.player_sidebar_width, -1
        )
        self.header_sidebar_background.set_visible(visible)
        reserved = (
            min(self.player_sidebar_width, max(0, page_width - 640))
            if visible
            else 0
        )
        if self.player_column.get_margin_end() != reserved:
            self.player_column.set_margin_end(reserved)

    def _normal_video_height_limit(self) -> int | None:
        if self._player_is_fullscreen():
            return None
        viewport_height = self.player_scroller.get_height()
        if viewport_height <= 0:
            return None
        # Keep the transport row plus the title and primary actions visible
        # without scrolling in a normally sized window.
        return max(180, viewport_height - 176)

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

        self.details_back = self.context_back
        hero = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=28)
        hero.add_css_class("details-hero")
        artwork = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, hexpand=True)
        self.details_picture = Gtk.Picture()
        self.details_picture.set_content_fit(Gtk.ContentFit.COVER)
        self.details_picture.set_can_shrink(True)
        self.details_picture.set_hexpand(True)
        picture_frame = Gtk.AspectFrame(ratio=16 / 9, obey_child=False)
        picture_frame.set_hexpand(True)
        picture_frame.set_child(self.details_picture)
        self.details_picture.add_css_class("details-picture")
        artwork.append(picture_frame)
        self.details_title = Gtk.Label(xalign=0, wrap=True)
        self.details_title.add_css_class("title-1")
        artwork.append(self.details_title)
        self.details_meta = Gtk.Label(xalign=0, wrap=True)
        self.details_meta.add_css_class("dim-label")
        artwork.append(self.details_meta)
        self.details_overview = Gtk.Label(xalign=0, yalign=0, wrap=True, selectable=True)
        self.details_overview.set_max_width_chars(80)
        artwork.append(self.details_overview)
        hero.append(artwork)

        actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        actions.add_css_class("details-actions")
        actions.set_valign(Gtk.Align.START)
        self.details_play = labeled_button("Play", "media-playback-start-symbolic")
        self.details_play.add_css_class("suggested-action")
        self.details_play.add_css_class("pill")
        self.details_play.connect("clicked", lambda *_: self._play_detail_item())
        actions.append(self.details_play)
        self.details_queue = labeled_button("Add to queue", "list-add-symbolic")
        self.details_queue.connect("clicked", lambda *_: self._queue_detail_item())
        actions.append(self.details_queue)
        self.details_watch_later = labeled_button("Watch later", "alarm-symbolic")
        self.details_watch_later.connect(
            "clicked", lambda *_: self.detail_item and self._watch_later(self.detail_item)
        )
        actions.append(self.details_watch_later)
        self.details_share = labeled_button("Copy share link", "send-to-symbolic")
        self.details_share.connect(
            "clicked", lambda *_: self.detail_item and self._share_item(self.detail_item)
        )
        actions.append(self.details_share)
        self.details_channel = labeled_button("Open channel", "avatar-default-symbolic")
        self.details_channel.connect("clicked", lambda *_: self._open_detail_channel())
        actions.append(self.details_channel)
        download_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        download_row.set_hexpand(True)
        self.details_download_quality = Gtk.DropDown.new_from_strings(
            ["Best quality", "1080p", "720p", "480p", "Audio only"]
        )
        download_row.append(self.details_download_quality)
        self.details_download = labeled_button("Download", "folder-download-symbolic")
        self.details_download.set_hexpand(True)
        self.details_download.connect("clicked", lambda *_: self._download_detail_item())
        download_row.append(self.details_download)
        actions.append(download_row)
        self.details_playlist = labeled_button("Save to local playlist", "view-list-symbolic")
        self.details_playlist.connect("clicked", lambda *_: self._save_detail_to_playlist())
        actions.append(self.details_playlist)
        hero.append(actions)
        responsive = Adw.BreakpointBin()
        responsive.set_child(hero)
        narrow = Adw.Breakpoint.new(
            Adw.BreakpointCondition.parse("max-width: 720px")
        )
        narrow.add_setter(hero, "orientation", Gtk.Orientation.VERTICAL)
        responsive.add_breakpoint(narrow)
        content.append(responsive)
        self.details_series_loading = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8
        )
        self.details_series_loading.set_halign(Gtk.Align.CENTER)
        self.details_series_spinner = Gtk.Spinner()
        self.details_series_loading.append(self.details_series_spinner)
        self.details_series_loading.append(Gtk.Label(label="Loading seasons and episodes…"))
        self.details_series_loading.set_visible(False)
        content.append(self.details_series_loading)
        self.details_seasons = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=12
        )
        self.details_seasons.set_visible(False)
        content.append(self.details_seasons)
        return scroller

    def _build_mini_player(self) -> Gtk.Widget:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        bar.add_css_class("mini-player")
        self.mini_previous = Gtk.Button(
            icon_name="media-skip-backward-symbolic", tooltip_text="Previous"
        )
        self.mini_previous.add_css_class("mini-control")
        self.mini_previous.connect(
            "clicked", lambda *_: self._play_previous_queued(reveal_player=False)
        )
        bar.append(self.mini_previous)
        self.mini_play = Gtk.Button(
            icon_name="media-playback-start-symbolic", tooltip_text="Play"
        )
        self.mini_play.add_css_class("mini-control")
        self.mini_play.connect("clicked", lambda *_: self._mini_play_pause())
        bar.append(self.mini_play)
        self.mini_next = Gtk.Button(
            icon_name="media-skip-forward-symbolic", tooltip_text="Next"
        )
        self.mini_next.add_css_class("mini-control")
        self.mini_next.connect(
            "clicked", lambda *_: self._play_next_queued(reveal_player=False)
        )
        bar.append(self.mini_next)
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
        open_player.add_css_class("mini-control")
        open_player.connect("clicked", lambda *_: self._show_player())
        bar.append(open_player)
        close = Gtk.Button(icon_name="window-close-symbolic", tooltip_text="Close and clear queue")
        close.add_css_class("mini-control")
        close.connect("clicked", lambda *_: self._close_mini_player())
        bar.append(close)
        return bar

    def _navigation_changed(self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if not row or self.syncing_navigation:
            return
        if self.player_expanded and time.monotonic() < self.player_navigation_guard_until:
            self.syncing_navigation = True
            self.navigation.unselect_all()
            self.syncing_navigation = False
            return
        name = row.get_name()
        # Gtk.ListBox may emit the selected row again after focus/layout
        # changes. A duplicate must not unwind a player or details page.
        if name == self.active_navigation:
            return
        self._select_page(name)

    def _select_page(self, name: str, *, record: bool = True) -> None:
        titles = {
            "home": (
                "Home",
                (
                    "YouTube"
                    if self._synctube_active()
                    else "Jellyfin"
                    if self._jellyfin_syncplay_active()
                    else "YouTube + Jellyfin"
                ),
            ),
            "browse": (
                "Browse",
                (
                    "YouTube channels"
                    if self._synctube_active()
                    else "Jellyfin movies and shows"
                    if self._jellyfin_syncplay_active()
                    else "Movies, shows, channels, and YouTube"
                ),
            ),
            "library": ("Library", "History, subscriptions, and playlists"),
            "requests": ("Requests", "Search and request from Seerr"),
            "downloads": ("Downloads", "Offline videos and active transfers"),
        }
        if name not in titles:
            return
        self._set_visible_page(name, record=record)
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
            else:
                self.syncing_navigation = True
                self.navigation.unselect_all()
                self.syncing_navigation = False
        title, subtitle = titles[name]
        self.window_title.set_title(title)
        self.window_title.set_subtitle(subtitle)
        self.split_view.set_show_content(True)
        self.mini_player.set_visible(bool(self.current_item or self.queue))
        if name == "browse":
            self.browse_mode = "movies" if self._jellyfin_syncplay_active() else "youtube"
            self.global_search.set_placeholder_text(
                "Search Jellyfin" if self._jellyfin_syncplay_active() else "Search YouTube"
            )
            if not self._jellyfin_syncplay_active():
                GLib.idle_add(self.global_search.grab_focus)
        elif name == "library":
            self._load_library()
        elif name == "downloads":
            self._load_offline()

    def _global_search_requested(self, entry: Gtk.SearchEntry) -> None:
        if self._jellyfin_syncplay_active():
            self.toast_overlay.add_toast(
                Adw.Toast(title="Leave the Jellyfin watch party to search YouTube")
            )
            return
        if self._visible_page_name() != "browse-category":
            self.browse_mode = "youtube"
        self.active_navigation = "browse"
        self._select_navigation_row("browse")
        if self.youtube.playlist_id_from_url(entry.get_text()):
            self._open_youtube_playlist(entry.get_text().strip())
        elif self.youtube.video_id_from_url(entry.get_text()):
            self._youtube_search_requested(entry)
        elif self.browse_mode == "channels":
            query = entry.get_text().strip()
            if not query:
                return
            self.browse_search_results = True
            self.browse_category_heading.set_visible(False)
            self.window_title.set_title("Channel search")
            self.window_title.set_subtitle("")
            cache_key = f"youtube:channel-search:{query.casefold()}"
            if cached := self._browse_cache_get(cache_key):
                self._youtube_channels_loaded(cached)
                return
            self.youtube_grid.set_loading(f"Searching channels for “{query}”…")
            run_async(
                lambda: self._youtube_search(query, channels=True),
                lambda items: self._youtube_channels_loaded(items, cache_key),
                lambda error: self._grid_error(self.youtube_grid, error),
            )
        elif self.browse_mode == "youtube":
            self._youtube_search_requested(entry)
        else:
            self._jellyfin_search_requested(entry)

    def _open_browse_category(self, category: str) -> None:
        if self._synctube_active() and category in {"movies", "shows"}:
            self._select_page("browse", record=False)
            return
        if self._jellyfin_syncplay_active() and category in {"youtube", "channels"}:
            self._select_page("browse", record=False)
            return
        self.browse_mode = category
        self.browse_search_results = False
        self.browse_category_heading.set_visible(True)
        self.jellyfin_history.clear()
        self.jellyfin_parent_id = ""
        self.jellyfin_back.set_label("Browse")
        self.global_search.set_placeholder_text(f"Search {category}")
        self.browse_category_title.set_label(category.title())
        self.browse_channel_alphabet_scroller.set_visible(False)
        self._set_visible_page("browse-category")
        if category == "channels":
            cache_key = "youtube:subscriptions"
            if cached := self._browse_cache_get(cache_key):
                self._youtube_channels_loaded(cached)
                return
            self.youtube_grid.set_loading("Loading YouTube channels…")
            run_async(
                self._browse_youtube_channels,
                lambda items: self._youtube_channels_loaded(items, cache_key),
                lambda error: self._grid_error(self.youtube_grid, error),
            )
            return
        if not self.jellyfin.session:
            connect = Gtk.Button(label="Connect Jellyfin")
            connect.add_css_class("suggested-action")
            connect.connect("clicked", lambda *_: self.open_connection())
            self.jellyfin_grid.set_status(
                "network-server-symbolic",
                "Connect Jellyfin",
                f"Connect your server to browse {category}.",
                connect,
            )
            return
        cache_key = f"jellyfin:{self.jellyfin.session.user_id}:category:{category}"
        if cached := self._browse_cache_get(cache_key):
            self._jellyfin_results(cached)
            return
        self.jellyfin_grid.set_loading(f"Loading {category}…")
        run_async(
            lambda: self.jellyfin.browse(category),
            lambda items: self._jellyfin_results(items, cache_key),
            lambda error: self._grid_error(self.jellyfin_grid, error),
        )

    def _browse_youtube_channels(self) -> list[MediaItem]:
        return [
                MediaItem(
                    id=subscription.channel_id,
                    title=subscription.title,
                    subtitle="Subscribed in TubeFin",
                    source="youtube-channel",
                    thumbnail_url=subscription.avatar_url or None,
                    playable=False,
                    payload={
                        "channel_url": subscription.url,
                        "channel_avatar_url": subscription.avatar_url,
                    },
                )
            for subscription in self.subscriptions.list()
        ]

    def _youtube_search(self, query: str, *, channels: bool = False) -> list[MediaItem]:
        if self.active_oauth_account:
            token = self.oauth.access_token(self.active_oauth_account)
            return self.youtube.api_search(token, query, channels=channels)
        return (
            self.youtube.search_channels(query)
            if channels
            else self.youtube.search(query)
        )

    def _browse_cache_get(self, key: str) -> list[MediaItem] | None:
        cached = self.browse_cache.get(key)
        if not cached:
            return None
        created, items = cached
        if time.monotonic() - created > 10 * 60:
            self.browse_cache.pop(key, None)
            return None
        return list(items)

    def _browse_cache_put(self, key: str, items: list[MediaItem]) -> None:
        self.browse_cache[key] = (time.monotonic(), list(items))

    def _online_subscription_items(self) -> list[MediaItem]:
        if self.active_oauth_account:
            try:
                items = self._account_feed_items("subscriptions")
                if items:
                    return items
            except Exception:
                if not self.youtube_browser_session:
                    raise
        if not self.youtube_browser_session:
            return []
        channels = self.youtube.subscriptions()
        if channels:
            return channels
        # Older yt-dlp/YouTube combinations may not expose /feed/channels.
        # The subscriptions video feed is incomplete, but is still useful as
        # a fallback instead of silently syncing nothing.
        fallback: list[MediaItem] = []
        for video in self.youtube.personal_feed("subscriptions", limit=100):
            channel_url = str(video.payload.get("channel_url") or "")
            if channel_url:
                fallback.append(
                    MediaItem(
                        id=str(video.payload.get("channel_id") or channel_url),
                        title=video.subtitle or "YouTube channel",
                        subtitle="YouTube subscription",
                        source="youtube-channel",
                        playable=False,
                        payload={"channel_url": channel_url},
                    )
                )
        return fallback

    def _sync_online_subscriptions(self) -> None:
        if (
            self.subscriptions_syncing
            or not self.active_oauth_account
            and not self.youtube_browser_session
        ):
            return
        self.subscriptions_syncing = True
        self._update_account_sync_status()
        run_async(
            self._online_subscription_items,
            self._online_subscriptions_loaded,
            self._online_subscriptions_error,
        )

    def _sync_online_history(self) -> None:
        if self.history_syncing or not self.youtube_browser_session:
            return
        self.history_syncing = True
        self._update_account_sync_status()
        run_async(
            lambda: self.youtube.personal_feed("history", limit=50),
            self._online_history_loaded,
            self._online_history_error,
        )

    def _update_account_sync_status(self) -> None:
        active = []
        if self.subscriptions_syncing:
            active.append("subscriptions")
        if self.history_syncing:
            active.append("history")
        if active:
            self.account_sync_status.set_tooltip_text(
                f"Syncing {' and '.join(active)}…"
            )
            self.account_sync_spinner.start()
            self.account_sync_status.set_visible(True)
        else:
            self.account_sync_spinner.stop()
            self.account_sync_status.set_visible(False)

    def _online_subscriptions_error(self, error: Exception) -> bool:
        self.subscriptions_syncing = False
        self._update_account_sync_status()
        self.toast_overlay.add_toast(
            Adw.Toast(title=f"Subscription sync failed: {error}")
        )
        return GLib.SOURCE_REMOVE

    def _online_history_error(self, _error: Exception) -> bool:
        self.history_syncing = False
        self._update_account_sync_status()
        return GLib.SOURCE_REMOVE

    def _online_history_loaded(self, items: list[MediaItem]) -> bool:
        self.history_syncing = False
        self._update_account_sync_status()
        if self.clearing_all_data:
            return GLib.SOURCE_REMOVE
        changed = self.history.merge_remote(items)
        visible = self._visible_page_name()
        if changed and visible == "library":
            self._load_library()
        elif changed and visible == "history":
            self._open_full_history()
        return GLib.SOURCE_REMOVE

    def _online_subscriptions_loaded(self, items: list[MediaItem]) -> bool:
        self.subscriptions_syncing = False
        self._update_account_sync_status()
        if self.clearing_all_data:
            return GLib.SOURCE_REMOVE
        synced: list[ChannelSubscription] = []
        existing_subscriptions = {
            subscription.channel_id: subscription
            for subscription in self.subscriptions.list()
        }
        for item in items:
            channel_url = str(item.payload.get("channel_url") or "")
            if not item.id or not channel_url:
                continue
            existing = existing_subscriptions.get(item.id)
            avatar = str(
                item.payload.get("channel_avatar_url") or item.thumbnail_url or ""
            )
            synced.append(
                ChannelSubscription(
                    item.id,
                    item.title,
                    channel_url,
                    avatar or (existing.avatar_url if existing else ""),
                    existing.notifications if existing else True,
                    existing.last_seen_video_id if existing else "",
                )
            )
        if synced:
            self.subscriptions.merge(synced)
            self._invalidate_subscription_browse_cache()
        if synced and self._visible_page_name() == "library":
            self._load_subscriptions()
        elif (
            synced
            and self._visible_page_name() == "browse-category"
            and self.browse_mode == "channels"
            and not self.browse_search_results
        ):
            channels = self._browse_youtube_channels()
            self._browse_cache_put("youtube:subscriptions", channels)
            self._youtube_channels_loaded(channels)
        return GLib.SOURCE_REMOVE

    def _youtube_channels_loaded(
        self, items: list[MediaItem], cache_key: str = ""
    ) -> bool:
        if cache_key:
            self._browse_cache_put(cache_key, items)
        self.browse_channel_positions = self._alphabet_positions(
            [item.title for item in items]
        )
        self.browse_channel_alphabet_scroller.set_visible(
            bool(items) and not self.browse_search_results
        )
        if items:
            self.youtube_grid.set_items(items)
        else:
            self.youtube_grid.set_status(
                "avatar-default-symbolic",
                "No YouTube channels yet",
                "Sign in to see subscriptions, or search for a channel above.",
            )
        return GLib.SOURCE_REMOVE

    def _browse_destination_back(self) -> None:
        if self.jellyfin_history:
            self._jellyfin_go_back()
        elif not self._go_back():
            self._select_page("browse")

    def _select_navigation_row(self, name: str) -> None:
        row = self.navigation.get_first_child()
        while row and row.get_name() != name:
            row = row.get_next_sibling()
        if row and self.navigation.get_selected_row() != row:
            self.syncing_navigation = True
            self.navigation.select_row(row)
            self.syncing_navigation = False

    def _load_library(self) -> None:
        self._clear_box(self.library_history)
        recent = self._history_display_items(8)
        if recent:
            self.library_history.append(
                SectionShelf(
                    MediaSection("", recent),
                    self.thumbnails,
                    self._activate_item,
                    self._add_to_queue,
                    self._add_to_queue_next,
                    self._save_item,
                    self._watch_later,
                    horizontal=True,
                    avatar_resolver=self.youtube.channel_avatar,
                    on_download=self._download_item,
                    on_mark_watched=self._mark_watched,
                    on_share=self._share_item,
                )
            )
        else:
            empty = Gtk.Label(label="Videos you play will appear here.", xalign=0)
            empty.add_css_class("dim-label")
            self.library_history.append(empty)
        self._load_playlists()
        self._load_subscriptions()

    def _load_subscriptions(self) -> None:
        subscriptions = self.subscriptions.list()
        self.subscription_lookup = {
            subscription.channel_id: subscription for subscription in subscriptions
        }
        self.subscription_positions = {}
        for index, subscription in enumerate(subscriptions):
            first = subscription.title[:1].upper()
            letter = first if "A" <= first <= "Z" else "#"
            self.subscription_positions.setdefault(letter, index)
        self.subscription_model.splice(
            0,
            self.subscription_model.get_n_items(),
            [subscription.channel_id for subscription in subscriptions],
        )
        self.subscription_alphabet_scroller.set_visible(bool(subscriptions))
        self.subscription_stack.set_visible_child_name(
            "subscriptions" if subscriptions else "empty"
        )

    def _subscription_row_setup(
        self, _factory: Gtk.SignalListItemFactory, list_item: Gtk.ListItem
    ) -> None:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row.add_css_class("subscription-row")
        row.set_margin_start(10)
        row.set_margin_end(10)
        row.set_margin_top(6)
        row.set_margin_bottom(6)
        avatar_frame = Gtk.Overlay(width_request=36, height_request=36)
        avatar_frame.set_size_request(36, 36)
        avatar_frame.set_valign(Gtk.Align.CENTER)
        avatar_frame.set_overflow(Gtk.Overflow.HIDDEN)
        avatar_frame.add_css_class("channel-avatar")
        fallback = Gtk.Image.new_from_icon_name("avatar-default-symbolic")
        fallback.set_pixel_size(20)
        avatar_frame.set_child(fallback)
        avatar = Gtk.Picture(width_request=36, height_request=36)
        avatar.set_size_request(36, 36)
        avatar.set_content_fit(Gtk.ContentFit.COVER)
        avatar.set_visible(False)
        avatar_frame.add_overlay(avatar)
        row.append(avatar_frame)
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        title.add_css_class("heading")
        labels.append(title)
        subtitle = Gtk.Label(xalign=0)
        subtitle.add_css_class("caption")
        subtitle.add_css_class("dim-label")
        labels.append(subtitle)
        open_channel = Gtk.Button(child=labels, hexpand=True)
        open_channel.add_css_class("flat")
        open_channel.set_halign(Gtk.Align.FILL)
        open_channel.connect(
            "clicked", lambda *_args, bound=list_item: self._open_bound_subscription(bound)
        )
        row.append(open_channel)
        notifications = Gtk.ToggleButton(
            icon_name="preferences-system-notifications-symbolic",
            tooltip_text="New-video notifications",
        )
        notifications.set_valign(Gtk.Align.CENTER)
        notifications.connect(
            "toggled",
            lambda button, bound=list_item: self._bound_subscription_notifications(
                bound, button
            ),
        )
        row.append(notifications)
        remove = Gtk.Button(icon_name="list-remove-symbolic", tooltip_text="Unsubscribe")
        remove.set_valign(Gtk.Align.CENTER)
        remove.connect(
            "clicked", lambda *_args, bound=list_item: self._remove_bound_subscription(bound)
        )
        row.append(remove)
        list_item.subscription_title = title  # type: ignore[attr-defined]
        list_item.subscription_subtitle = subtitle  # type: ignore[attr-defined]
        list_item.subscription_avatar = avatar  # type: ignore[attr-defined]
        list_item.subscription_notifications = notifications  # type: ignore[attr-defined]
        list_item.subscription_binding = False  # type: ignore[attr-defined]
        list_item.set_child(row)

    def _subscription_row_bind(
        self, _factory: Gtk.SignalListItemFactory, list_item: Gtk.ListItem
    ) -> None:
        subscription = self._bound_subscription(list_item)
        if not subscription:
            return
        title: Gtk.Label = list_item.subscription_title  # type: ignore[attr-defined]
        subtitle: Gtk.Label = list_item.subscription_subtitle  # type: ignore[attr-defined]
        avatar: Gtk.Picture = list_item.subscription_avatar  # type: ignore[attr-defined]
        notifications: Gtk.ToggleButton = (  # type: ignore[attr-defined]
            list_item.subscription_notifications
        )
        title.set_label(subscription.title)
        subtitle.set_label(
            "Notifications on" if subscription.notifications else "Notifications off"
        )
        avatar.set_paintable(None)
        avatar.set_visible(False)
        list_item.subscription_binding = True  # type: ignore[attr-defined]
        notifications.set_active(subscription.notifications)
        list_item.subscription_binding = False  # type: ignore[attr-defined]
        if subscription.avatar_url:
            self.thumbnails.load(
                subscription.avatar_url,
                lambda path, bound=list_item, channel_id=subscription.channel_id: (
                    self._set_bound_subscription_avatar(bound, channel_id, path)
                ),
            )
        elif subscription.url:
            run_async(
                lambda url=subscription.url: self.youtube.channel_avatar(url),
                lambda url, bound=list_item, channel_id=subscription.channel_id: (
                    self._load_bound_subscription_avatar(bound, channel_id, url)
                ),
                lambda _error: None,
            )

    def _bound_subscription(self, list_item: Gtk.ListItem) -> ChannelSubscription | None:
        item = list_item.get_item()
        if not isinstance(item, Gtk.StringObject):
            return None
        return self.subscription_lookup.get(item.get_string())

    def _open_bound_subscription(self, list_item: Gtk.ListItem) -> None:
        if subscription := self._bound_subscription(list_item):
            self._open_subscription_channel(subscription.url)

    def _bound_subscription_notifications(
        self, list_item: Gtk.ListItem, button: Gtk.ToggleButton
    ) -> None:
        if list_item.subscription_binding:  # type: ignore[attr-defined]
            return
        subscription = self._bound_subscription(list_item)
        if not subscription:
            return
        self._set_library_subscription_notifications(
            subscription.channel_id, button.get_active()
        )
        subscription.notifications = button.get_active()
        subtitle: Gtk.Label = list_item.subscription_subtitle  # type: ignore[attr-defined]
        subtitle.set_label(
            "Notifications on" if subscription.notifications else "Notifications off"
        )

    def _remove_bound_subscription(self, list_item: Gtk.ListItem) -> None:
        if subscription := self._bound_subscription(list_item):
            self._remove_library_subscription(subscription.channel_id)

    def _set_bound_subscription_avatar(
        self, list_item: Gtk.ListItem, channel_id: str, path: object
    ) -> bool:
        subscription = self._bound_subscription(list_item)
        if subscription and subscription.channel_id == channel_id and path:
            avatar: Gtk.Picture = list_item.subscription_avatar  # type: ignore[attr-defined]
            avatar.set_filename(str(path))
            avatar.set_visible(True)
        return GLib.SOURCE_REMOVE

    def _load_bound_subscription_avatar(
        self, list_item: Gtk.ListItem, channel_id: str, url: str | None
    ) -> bool:
        if url:
            self.thumbnails.load(
                url,
                lambda path: self._set_bound_subscription_avatar(
                    list_item, channel_id, path
                ),
            )
        return GLib.SOURCE_REMOVE

    def _jump_to_subscription(self, letter: str) -> None:
        position = self._alphabet_position(self.subscription_positions, letter)
        if position is not None:
            self.library_subscriptions.scroll_to(
                position, Gtk.ListScrollFlags.FOCUS, None
            )

    def _jump_to_browse_channel(self, letter: str) -> None:
        position = self._alphabet_position(self.browse_channel_positions, letter)
        if position is not None:
            self.youtube_grid.scroll_to_item(position)

    @staticmethod
    def _alphabet_positions(titles: list[str]) -> dict[str, int]:
        positions: dict[str, int] = {}
        for index, title in enumerate(titles):
            first = title[:1].upper()
            letter = first if "A" <= first <= "Z" else "#"
            positions.setdefault(letter, index)
        return positions

    @staticmethod
    def _alphabet_position(positions: dict[str, int], letter: str) -> int | None:
        position = positions.get(letter)
        if position is not None or letter == "#":
            return position
        return next(
            (
                positions[value]
                for value in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                if value >= letter and value in positions
            ),
            None,
        )

    def _open_subscription_channel(self, channel_url: str) -> None:
        if not channel_url:
            return
        self.channel_url = channel_url
        self.channel_page = 1
        self._load_channel(1)

    def _set_library_subscription_notifications(self, channel_id: str, enabled: bool) -> None:
        with suppress(KeyError):
            self.subscriptions.set_notifications(channel_id, enabled)

    def _remove_library_subscription(self, channel_id: str) -> None:
        self.subscriptions.unsubscribe(channel_id)
        self._invalidate_subscription_browse_cache()
        self._load_subscriptions()

    def _invalidate_subscription_browse_cache(self) -> None:
        self.browse_cache.pop("youtube:subscriptions", None)

    def _open_full_history(self) -> None:
        items = self._history_display_items(500)
        if items:
            self.history_grid.set_items(items)
        else:
            self.history_grid.set_status(
                "document-open-recent-symbolic",
                "No history yet",
                "Videos you play will appear here.",
            )
        self.window_title.set_title("History")
        self.window_title.set_subtitle("")
        self._set_visible_page("history")

    def _history_display_items(self, limit: int) -> list[MediaItem]:
        lookup: dict[str, ChannelSubscription] = {}
        for subscription in self.subscriptions.list():
            lookup[f"id:{subscription.channel_id}"] = subscription
            lookup[f"url:{subscription.url.rstrip('/')}"] = subscription
        return [
            self._history_display_item(entry.item, lookup)
            for entry in self.history.list(limit)
            if not self._synctube_active() or entry.item.source != "jellyfin"
            if not self._jellyfin_syncplay_active() or entry.item.source != "youtube"
        ]

    def _history_display_item(
        self,
        item: MediaItem,
        subscriptions: dict[str, ChannelSubscription],
    ) -> MediaItem:
        item = self._refresh_stored_artwork(item)
        if item.source != "youtube":
            return item
        payload = dict(item.payload)
        channel_id = str(payload.get("channel_id") or "")
        channel_url = str(payload.get("channel_url") or "")
        subscription = subscriptions.get(f"id:{channel_id}") if channel_id else None
        if not subscription and channel_url:
            subscription = subscriptions.get(f"url:{channel_url.rstrip('/')}")
        if subscription:
            payload["channel_id"] = channel_id or subscription.channel_id
            payload["channel_url"] = channel_url or subscription.url
            payload["channel_avatar_url"] = (
                payload.get("channel_avatar_url") or subscription.avatar_url
            )
            subtitle = (
                item.subtitle
                if item.subtitle and item.subtitle.casefold() != "youtube"
                else subscription.title
            )
            return replace(item, subtitle=subtitle, payload=payload)
        if channel_url and (cached_channel := self.channel_feeds.get(channel_url.rstrip("/"))):
            payload["channel_id"] = channel_id or cached_channel.id
            payload["channel_url"] = channel_url
            payload["channel_avatar_url"] = (
                payload.get("channel_avatar_url") or cached_channel.avatar_url or ""
            )
            subtitle = (
                item.subtitle
                if item.subtitle and item.subtitle.casefold() != "youtube"
                else cached_channel.title
            )
            return replace(item, subtitle=subtitle, payload=payload)
        if channel_id and not channel_url:
            payload["channel_url"] = f"https://www.youtube.com/channel/{channel_id}"
            return replace(item, payload=payload)
        return item

    def _youtube_search_requested(self, entry: Gtk.SearchEntry) -> None:
        query = entry.get_text().strip()
        if not query:
            return
        if self.youtube.playlist_id_from_url(query):
            self._open_youtube_playlist(query)
            return
        self.browse_mode = "youtube"
        self.browse_channel_alphabet_scroller.set_visible(False)
        self.browse_search_results = True
        self.browse_category_heading.set_visible(False)
        self.window_title.set_title("Search")
        self.window_title.set_subtitle("")
        self._set_visible_page("browse-category")
        video_id = self.youtube.video_id_from_url(query)
        if video_id:
            webpage_url = f"https://www.youtube.com/watch?v={video_id}"
            item = MediaItem(
                id=video_id,
                title="YouTube video",
                subtitle="YouTube",
                source="youtube",
                thumbnail_url=f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
                payload={"webpage_url": webpage_url},
            )
            self.youtube_grid.set_loading("Opening YouTube video…")
            run_async(
                lambda: self.youtube.details(item),
                self._youtube_link_loaded,
                lambda error: self._grid_error(self.youtube_grid, error),
            )
            return
        cache_key = f"youtube:video-search:{query.casefold()}"
        if cached := self._browse_cache_get(cache_key):
            self._youtube_results(cached)
            return
        self.youtube_grid.set_loading(f"Searching for “{query}”…")
        run_async(
            lambda: self._youtube_search(query),
            lambda items: self._youtube_results(items, cache_key),
            lambda error: self._grid_error(self.youtube_grid, error),
        )

    def _youtube_link_loaded(self, details: VideoDetails) -> bool:
        if details.availability != Availability.PUBLIC:
            self.youtube_grid.set_status(
                "dialog-error-symbolic",
                "Video unavailable",
                details.availability_message or "This video cannot be played.",
            )
            return GLib.SOURCE_REMOVE
        self._play_selected(details.item)
        return GLib.SOURCE_REMOVE

    def _youtube_results(self, items: list[MediaItem], cache_key: str = "") -> bool:
        self.browse_channel_alphabet_scroller.set_visible(False)
        if cache_key:
            self._browse_cache_put(cache_key, items)
        if items:
            self.youtube_grid.set_items(items)
        else:
            self.youtube_grid.set_status(
                "edit-find-symbolic",
                "No videos found",
                "Try a different search phrase.",
            )
        return GLib.SOURCE_REMOVE

    def _prebuffer_item(self, item: MediaItem) -> None:
        if item.source in {"youtube", "jellyfin"}:
            self.prebuffer.offer(item, self._resolve_item)

    @staticmethod
    def _clear_box(box: Gtk.Box | Gtk.ListBox | Gtk.FlowBox) -> None:
        child = box.get_first_child()
        while child:
            following = child.get_next_sibling()
            box.remove(child)
            child = following

    def _refresh_home(self, *, pulled: bool = False) -> None:
        self.home_scroller.get_vadjustment().set_value(0)
        self._load_home_sections(refresh_channel_feeds=True)
        self._sync_online_subscriptions()
        self._sync_online_history()
        if not pulled:
            self.toast_overlay.add_toast(Adw.Toast(title="Refreshing Home"))

    def _home_overscroll(
        self, _controller: Gtk.EventControllerScroll, _dx: float, dy: float
    ) -> bool:
        if self.home_scroller.get_vadjustment().get_value() > 0.5 or dy >= 0:
            self.home_pull_distance = 0.0
            return False
        self.home_pull_distance += min(28.0, abs(dy) * 18.0)
        self._show_home_pull_progress()
        if self.home_pull_distance >= 72.0:
            self._trigger_home_pull_refresh()
        return False

    def _home_pull_begin(
        self, _gesture: Gtk.GestureDrag, _start_x: float, _start_y: float
    ) -> None:
        self.home_pull_distance = (
            0.0 if self.home_scroller.get_vadjustment().get_value() <= 0.5 else -1.0
        )

    def _home_pull_update(
        self, _gesture: Gtk.GestureDrag, _offset_x: float, offset_y: float
    ) -> None:
        if self.home_pull_distance < 0 or offset_y <= 0:
            return
        self.home_pull_distance = min(96.0, offset_y * 0.55)
        self._show_home_pull_progress()

    def _home_pull_end(
        self, _gesture: Gtk.GestureDrag, _offset_x: float, _offset_y: float
    ) -> None:
        if self.home_pull_distance >= 72.0:
            self._trigger_home_pull_refresh()
        elif not self.home_pull_refreshing:
            self.home_pull_revealer.set_reveal_child(False)
        self.home_pull_distance = 0.0

    def _show_home_pull_progress(self) -> None:
        self.home_pull_label.set_label(
            "Release to refresh" if self.home_pull_distance >= 72.0 else "Pull to refresh"
        )
        self.home_pull_revealer.set_reveal_child(True)

    def _trigger_home_pull_refresh(self) -> None:
        if self.home_pull_refreshing:
            return
        self.home_pull_refreshing = True
        self.home_pull_distance = 0.0
        self.home_pull_label.set_label("Refreshing…")
        self.home_pull_spinner.start()
        self.home_pull_revealer.set_reveal_child(True)
        self._refresh_home(pulled=True)
        GLib.timeout_add(900, self._finish_home_pull_refresh)

    def _finish_home_pull_refresh(self) -> bool:
        self.home_pull_spinner.stop()
        self.home_pull_refreshing = False
        self.home_pull_revealer.set_reveal_child(False)
        return GLib.SOURCE_REMOVE

    def _set_home_section(self, key: str, shelf: SectionShelf) -> None:
        self.home_section_widgets[key] = shelf
        self._rebuild_home_sections()

    def _rebuild_home_sections(self) -> None:
        self._clear_box(self.home_sections)
        keys = list(self.home_section_order)
        keys.extend(key for key in self.home_section_widgets if key not in keys)
        for key in keys:
            shelf = self.home_section_widgets.get(key)
            if shelf:
                self.home_sections.append(shelf)

    def _load_home_sections(self, *, refresh_channel_feeds: bool = False) -> None:
        self._clear_box(self.home_sections)
        self.home_section_widgets = {}
        self.recommendation_generation += 1
        self.recommendation_shelf = None
        self.recommendation_items = []
        self.recommendation_next = 1
        self.recommendation_loading = False
        self.recommendation_exhausted = False
        self.recommendation_spinner.stop()
        self.recommendation_loading_row.set_visible(False)
        signed_in = bool(
            self.active_oauth_account
            or (self.jellyfin.session and not self._synctube_active())
            or self.youtube_browser_session
        )
        self.home_signed_out.set_visible(not signed_in)
        history = [
            self._refresh_stored_artwork(item)
            for item in self.history.continue_watching()
            if not self._is_jellyfin_music_item(item)
            if not self._synctube_active() or item.source != "jellyfin"
            if not self._jellyfin_syncplay_active() or item.source != "youtube"
        ]
        if history:
            self._set_home_section(
                "local_history",
                SectionShelf(
                    MediaSection("Continue Watching · Local history", history),
                    self.thumbnails,
                    self._activate_item,
                    self._add_to_queue,
                    self._add_to_queue_next,
                    self._save_item,
                    self._watch_later,
                    avatar_resolver=self.youtube.channel_avatar,
                    on_download=self._download_item,
                    expand_cards=True,
                    on_mark_watched=self._mark_watched,
                    on_share=self._share_item,
                )
            )
        downloaded = [self._offline_item(record) for record in self.offline.list()]
        downloaded = [
            item
            for item in downloaded
            if item.playable and not self._is_marked_watched(item)
        ][:12]
        if downloaded and not self._jellyfin_syncplay_active():
            self._set_home_section(
                "offline",
                SectionShelf(
                    MediaSection("Available Offline · This device", downloaded),
                    self.thumbnails,
                    self._activate_item,
                    self._add_to_queue,
                    self._add_to_queue_next,
                    self._save_item,
                    self._watch_later,
                    avatar_resolver=self.youtube.channel_avatar,
                    on_download=self._download_item,
                    expand_cards=True,
                    on_mark_watched=self._mark_watched,
                    on_share=self._share_item,
                )
            )
        if self.jellyfin.session and not self._synctube_active():
            run_async(
                self.jellyfin.get_home,
                self._append_jellyfin_home,
                lambda _error: None,
            )
        if self.active_oauth_account and not self._jellyfin_syncplay_active():
            run_async(
                lambda: self._account_feed_items("history"),
                lambda items: self._append_home_section(
                    "Subscription Activity · Provided by YouTube", items,
                    "youtube_activity",
                ),
                lambda _error: None,
            )
        elif self.youtube_browser_session and not self._jellyfin_syncplay_active():
            self._load_more_recommendations()
        elif not self._jellyfin_syncplay_active():
            channels = self.history.recent_channels(3)
            if channels:
                run_async(
                    lambda: self._watched_channel_videos(
                        channels, refresh=refresh_channel_feeds
                    ),
                    lambda items: self._append_home_section(
                        "From Channels You Watched · Ranked locally", items,
                        "watched_channels",
                    ),
                    lambda _error: None,
                )
        if (
            self.active_oauth_account or self.youtube_browser_session
        ) and self.subscriptions.list():
            self._request_subscription_update_check()

    def _watched_channel_videos(
        self, channel_urls: list[str], *, refresh: bool
    ) -> list[MediaItem]:
        metadata: dict[str, tuple[str, str, str]] = {}
        for entry in self.history.list(200):
            url = str(entry.item.payload.get("channel_url") or "").rstrip("/")
            if not url or url in metadata:
                continue
            title = (
                entry.item.subtitle
                if entry.item.subtitle.casefold() != "youtube"
                else ""
            )
            metadata[url] = (
                title,
                str(entry.item.payload.get("channel_id") or ""),
                str(entry.item.payload.get("channel_avatar_url") or ""),
            )
        for subscription in self.subscriptions.list():
            url = subscription.url.rstrip("/")
            previous = metadata.get(url, ("", "", ""))
            metadata[url] = (
                previous[0] or subscription.title,
                previous[1] or subscription.channel_id,
                previous[2] or subscription.avatar_url,
            )

        channels: list[ChannelDetails | None] = []
        missing: list[tuple[int, str]] = []
        for url in channel_urls:
            normalized = url.rstrip("/")
            local = metadata.get(normalized, ("", "", ""))
            if local[2]:
                self.youtube.avatar_cache.setdefault(normalized, local[2])
            cached = self.channel_feeds.get(normalized)
            channels.append(cached)
            if refresh or cached is None:
                missing.append((len(channels) - 1, normalized))

        if missing:
            def load_channel(url: str) -> ChannelDetails | None:
                try:
                    return self.youtube.channel(url, page_size=4)
                except Exception:
                    return None

            with ThreadPoolExecutor(max_workers=min(3, len(missing))) as executor:
                loaded = list(
                    executor.map(
                        load_channel,
                        [url for _index, url in missing],
                    )
                )
            for (index, _url), channel in zip(missing, loaded, strict=True):
                if not channel:
                    continue
                channels[index] = channel
                self.channel_feeds.put(channel)

        videos: list[MediaItem] = []
        for channel in channels:
            if not channel:
                continue
            fallback = metadata.get(channel.url.rstrip("/"), ("", "", ""))
            title = channel.title or fallback[0]
            channel_id = channel.id or fallback[1]
            avatar = channel.avatar_url or fallback[2]
            for item in channel.videos:
                payload = dict(item.payload)
                payload["channel_url"] = channel.url
                payload["channel_id"] = payload.get("channel_id") or channel_id
                payload["channel_avatar_url"] = (
                    payload.get("channel_avatar_url") or avatar or ""
                )
                subtitle = item.subtitle
                if not subtitle or subtitle.casefold() == "youtube":
                    subtitle = title or fallback[0] or "YouTube"
                videos.append(replace(item, subtitle=subtitle, payload=payload))
        return videos

    def _home_scroll_changed(self, adjustment: Gtk.Adjustment) -> None:
        if (
            adjustment.get_value() + adjustment.get_page_size()
            >= adjustment.get_upper() - 1400
        ):
            self._load_more_recommendations()

    def _load_more_recommendations(self) -> None:
        if (
            not self.youtube_browser_session
            or self._jellyfin_syncplay_active()
            or self.recommendation_loading
            or self.recommendation_exhausted
        ):
            return
        self.recommendation_loading = True
        self.recommendation_spinner.start()
        self.recommendation_loading_row.set_visible(True)
        start = self.recommendation_next
        generation = self.recommendation_generation
        page_size = 36
        run_async(
            lambda: self.youtube.personal_feed_page("home", start, page_size),
            lambda items: self._recommendations_loaded(generation, start, page_size, items),
            lambda error: self._recommendations_error(generation, error),
        )

    def _recommendations_loaded(
        self,
        generation: int,
        start: int,
        page_size: int,
        items: list[MediaItem],
    ) -> bool:
        if generation != self.recommendation_generation:
            return GLib.SOURCE_REMOVE
        self.recommendation_loading = False
        self.recommendation_spinner.stop()
        self.recommendation_loading_row.set_visible(False)
        existing = {item.id for item in self.recommendation_items}
        fresh = [
            replace(item, payload={**item.payload, "recommendation": True})
            for item in items
            if item.id
            and item.id not in existing
            and not self._is_marked_watched(item)
        ]
        if not fresh:
            self.recommendation_exhausted = True
            return GLib.SOURCE_REMOVE
        self.recommendation_items.extend(fresh)
        self.recommendation_next = start + len(items)
        self.recommendation_exhausted = len(items) < page_size
        if self.recommendation_shelf:
            self.recommendation_shelf.append_items(fresh)
        else:
            self.recommendation_shelf = SectionShelf(
                MediaSection("Recommended · YouTube", fresh),
                self.thumbnails,
                self._activate_item,
                self._add_to_queue,
                self._add_to_queue_next,
                self._save_item,
                self._watch_later,
                avatar_resolver=self.youtube.channel_avatar,
                on_download=self._download_item,
                on_dismiss=self._dismiss_recommendation,
                expand_cards=True,
                on_mark_watched=self._mark_watched,
                on_share=self._share_item,
            )
            self._set_home_section("recommendations", self.recommendation_shelf)
        return GLib.SOURCE_REMOVE

    def _recommendations_error(self, generation: int, error: Exception) -> bool:
        if generation == self.recommendation_generation:
            self.recommendation_loading = False
            self.recommendation_spinner.stop()
            self.recommendation_loading_row.set_visible(False)
            if not self.recommendation_items:
                self.toast_overlay.add_toast(Adw.Toast(title=f"Recommendations: {error}"))
        return GLib.SOURCE_REMOVE

    def _dismiss_recommendation(self, item: MediaItem) -> None:
        self.recommendation_items = [
            value for value in self.recommendation_items if value.id != item.id
        ]
        if self.recommendation_shelf:
            self.recommendation_shelf.remove_item(item.id)
        run_async(
            lambda: self.youtube.dismiss_recommendation(item.id),
            lambda _result: self.toast_overlay.add_toast(
                Adw.Toast(title="Recommendation removed from YouTube")
            ),
            lambda error: self.toast_overlay.add_toast(
                Adw.Toast(title=f"Removed locally; YouTube feedback failed: {error}")
            ),
        )

    def _append_home_section(
        self, title: str, items: list[MediaItem], key: str = ""
    ) -> bool:
        if self._jellyfin_syncplay_active():
            return GLib.SOURCE_REMOVE
        items = [item for item in items if not self._is_marked_watched(item)]
        if items:
            shelf = SectionShelf(
                    MediaSection(title, items[:12]),
                    self.thumbnails,
                    self._activate_item,
                    self._add_to_queue,
                    self._add_to_queue_next,
                    self._save_item,
                    self._watch_later,
                    avatar_resolver=self.youtube.channel_avatar,
                    on_download=self._download_item,
                    expand_cards=True,
                    on_mark_watched=self._mark_watched,
                    on_share=self._share_item,
                )
            self._set_home_section(key or title.casefold().replace(" ", "_"), shelf)
        return GLib.SOURCE_REMOVE

    def _local_subscription_updates(
        self,
    ) -> list[tuple[ChannelSubscription, MediaItem | None]]:
        subscriptions = self.subscriptions.list()
        if not subscriptions:
            return []
        if self.youtube_browser_session:
            recent = self.youtube.personal_feed("subscriptions", limit=100)
            newest_by_channel: dict[str, MediaItem] = {}
            for video in recent:
                keys = {
                    str(video.payload.get("channel_id") or ""),
                    str(video.payload.get("channel_url") or "").rstrip("/"),
                }
                for key in keys - {""}:
                    newest_by_channel.setdefault(key, video)
            return [
                (
                    subscription,
                    newest_by_channel.get(subscription.channel_id)
                    or newest_by_channel.get(subscription.url.rstrip("/")),
                )
                for subscription in subscriptions
            ]
        checked = [subscription for subscription in subscriptions if subscription.notifications][
            :50
        ]
        if not checked:
            return []
        with ThreadPoolExecutor(max_workers=min(4, len(checked))) as executor:
            latest = list(
                executor.map(
                    lambda subscription: self.youtube.channel_latest(subscription.url),
                    checked,
                )
            )
        return list(zip(checked, latest, strict=True))

    def _request_subscription_update_check(self) -> None:
        if self.subscription_updates_loading or not self.subscriptions.list():
            return
        self.subscription_updates_loading = True
        run_async(
            self._local_subscription_updates,
            self._local_subscription_updates_loaded,
            self._local_subscription_updates_error,
        )

    def _poll_subscription_updates(self) -> bool:
        if self.active_oauth_account or self.youtube_browser_session:
            self._request_subscription_update_check()
        return GLib.SOURCE_CONTINUE

    def _local_subscription_updates_error(self, _error: Exception) -> bool:
        self.subscription_updates_loading = False
        return GLib.SOURCE_REMOVE

    def _local_subscription_updates_loaded(
        self, updates: list[tuple[ChannelSubscription, MediaItem | None]]
    ) -> bool:
        self.subscription_updates_loading = False
        if self.clearing_all_data:
            return GLib.SOURCE_REMOVE
        seen: dict[str, str] = {}
        for subscription, newest in updates:
            if not newest:
                continue
            if (
                subscription.notifications
                and subscription.last_seen_video_id
                and subscription.last_seen_video_id != newest.id
            ):
                notification = Gio.Notification.new(f"New from {subscription.title}")
                notification.set_body(newest.title)
                notification.set_icon(Gio.ThemedIcon.new("video-x-generic-symbolic"))
                application = self.get_application()
                if application:
                    application.send_notification(
                        f"new-upload-{subscription.channel_id}-{newest.id}",
                        notification,
                    )
            seen[subscription.channel_id] = newest.id
        self.subscriptions.mark_seen_many(seen)
        return GLib.SOURCE_REMOVE

    def _append_jellyfin_home(self, sections: list[MediaSection]) -> bool:
        if self._synctube_active():
            return GLib.SOURCE_REMOVE
        for section in sections:
            items = [
                item for item in section.items if not self._is_marked_watched(item)
            ]
            if items:
                shelf = SectionShelf(
                        MediaSection(f"{section.title} · Jellyfin", items),
                        self.thumbnails,
                        self._activate_item,
                        self._add_to_queue,
                        self._add_to_queue_next,
                        self._save_item,
                        self._watch_later,
                        expand_cards=True,
                        on_mark_watched=self._mark_watched,
                        on_share=self._share_item,
                    )
                title = section.title.casefold()
                key = "jellyfin_continue" if "continue" in title else "jellyfin_recent"
                self._set_home_section(key, shelf)
        return GLib.SOURCE_REMOVE

    def _discover_seerr(self) -> None:
        session = self.jellyfin.session
        if not session:
            self._seerr_discovered("")
            return
        run_async(
            lambda: self.seerr.discover(
                session.server_url, self.seerr_settings["url"]
            ),
            self._seerr_discovered,
            lambda _error: self._seerr_discovered(""),
        )

    def _seerr_discovered(self, url: str) -> bool:
        self.seerr_available = bool(url)
        has_access = self.seerr_available and self.seerr_authenticated
        if hasattr(self, "requests_navigation_row"):
            self.requests_navigation_row.set_visible(self.jellyfin.session is not None)
        if hasattr(self, "seerr_search"):
            self.seerr_search.set_sensitive(has_access)
        if hasattr(self, "seerr_connect"):
            self.seerr_connect.set_label(
                "Seerr connected"
                if has_access
                else "Sign in to Seerr"
                if url
                else "Configure Seerr"
            )
            self.seerr_connect.set_tooltip_text(
                "Requests use the saved Seerr API key"
                if has_access and self.seerr_settings["api_key"]
                else "Signed in to Seerr"
                if has_access
                else "Sign in to Seerr with your Jellyfin account"
                if url
                else "Set the Seerr address in Settings"
            )
            self.seerr_connect.set_sensitive(not has_access)
        if hasattr(self, "seerr_status"):
            self.seerr_status.set_title(
                "Find something to watch"
                if has_access
                else "Sign in to search Seerr"
                if url
                else "Seerr was not found"
            )
            self.seerr_status.set_description(
                "Search Seerr, then request a movie or every season of a show."
                if has_access
                else "Use your Jellyfin account, or configure a Seerr API key in Settings."
                if url
                else (
                    "Automatic discovery could not reach Seerr. Set its address "
                    "in Settings, then return here to search and request media."
                )
            )
        if url and not has_access and self.seerr_auto_auth_attempted_url != url:
            self._authenticate_seerr_from_jellyfin(url)
        elif has_access or not url:
            self.pending_seerr_credentials = None
        return GLib.SOURCE_REMOVE

    def _authenticate_seerr_from_jellyfin(self, url: str) -> None:
        if self.seerr_authenticating or not self.jellyfin.session:
            return
        credentials = self.pending_seerr_credentials
        self.pending_seerr_credentials = None
        self.seerr_authenticating = True
        self.seerr_auto_auth_attempted_url = url
        self.seerr_connect.set_label("Connecting Seerr…")
        self.seerr_connect.set_sensitive(False)
        operation = (
            (lambda: self.seerr.jellyfin_login(*credentials))
            if credentials is not None
            else self._seerr_quick_connect_from_jellyfin
        )
        run_async(operation, self._seerr_auto_connected, self._seerr_auto_failed)

    def _seerr_quick_connect_from_jellyfin(self) -> dict[str, Any]:
        initiated = self.seerr.quick_connect_initiate()
        code = str(initiated.get("code") or "")
        secret = str(initiated.get("secret") or "")
        if not code or not secret:
            raise RuntimeError("Seerr did not return a Jellyfin Quick Connect code.")
        self.jellyfin.authorize_quick_connect(code)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.seerr.quick_connect_check(secret):
                return self.seerr.quick_connect_authenticate(secret)
            time.sleep(0.25)
        raise RuntimeError("Seerr did not accept the existing Jellyfin session.")

    def _seerr_auto_connected(self, _result: object) -> bool:
        self.seerr_authenticating = False
        if not self.jellyfin.session:
            self.seerr.clear_session()
            return GLib.SOURCE_REMOVE
        self.seerr_authenticated = True
        self.seerr_search.set_sensitive(True)
        self.seerr_connect.set_label("Seerr connected")
        self.seerr_connect.set_tooltip_text("Using your Jellyfin account")
        self.seerr_connect.set_sensitive(False)
        self.seerr_status.set_title("Find something to watch")
        self.seerr_status.set_description(
            "Search Seerr, then request a movie or every season of a show."
        )
        return GLib.SOURCE_REMOVE

    def _seerr_auto_failed(self, error: Exception) -> bool:
        self.seerr_authenticating = False
        if not self.jellyfin.session:
            return GLib.SOURCE_REMOVE
        self.seerr_authenticated = False
        self.seerr_search.set_sensitive(False)
        self.seerr_connect.set_label("Sign in to Seerr")
        self.seerr_connect.set_sensitive(True)
        self.seerr_status.set_title("One-time Seerr sign-in needed")
        self.seerr_status.set_description(
            "This Seerr version could not reuse the active Jellyfin session. "
            f"Sign in once to save its session. {error}"
        )
        return GLib.SOURCE_REMOVE

    def _seerr_connect_clicked(self, _button: Gtk.Button) -> None:
        if not self.seerr_available:
            self.open_settings()
            return
        self._open_seerr_login()

    def _seerr_search_requested(self, entry: Gtk.SearchEntry) -> None:
        query = entry.get_text().strip()
        if not query or not self.seerr_available or not self.seerr_authenticated:
            return
        self.seerr_search_generation += 1
        generation = self.seerr_search_generation
        self._clear_box(self.seerr_results)
        self.seerr_status.set_title("Searching Seerr…")
        self.seerr_status.set_description("")
        self.seerr_status.set_visible(True)
        run_async(
            lambda: self.seerr.search(query),
            lambda results: self._seerr_results_loaded(generation, results),
            lambda error: self._seerr_error(generation, error),
        )

    def _seerr_results_loaded(
        self, generation: int, results: list[dict[str, Any]]
    ) -> bool:
        if generation != self.seerr_search_generation:
            return GLib.SOURCE_REMOVE
        supported = [
            item for item in results if item.get("mediaType") in {"movie", "tv"}
        ]
        self.seerr_status.set_visible(not supported)
        if not supported:
            self.seerr_status.set_title("No movies or shows found")
            self.seerr_status.set_description("Try another title.")
            return GLib.SOURCE_REMOVE
        for result in supported:
            self.seerr_results.append(self._seerr_result_card(result))
        return GLib.SOURCE_REMOVE

    def _seerr_error(self, generation: int, error: Exception) -> bool:
        if generation != self.seerr_search_generation:
            return GLib.SOURCE_REMOVE
        self._clear_box(self.seerr_results)
        message = str(error)
        needs_sign_in = "sign in" in message.casefold()
        if needs_sign_in:
            self.seerr_authenticated = False
            self.seerr_search.set_sensitive(False)
            self.seerr_connect.set_label("Sign in to Seerr")
            self.seerr_connect.set_sensitive(True)
        self.seerr_status.set_visible(True)
        self.seerr_status.set_title(
            "Sign in to search Seerr" if needs_sign_in else "Seerr search failed"
        )
        self.seerr_status.set_description(message)
        return GLib.SOURCE_REMOVE

    def _seerr_result_card(self, result: dict[str, Any]) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.add_css_class("seerr-card")
        poster_overlay = Gtk.Overlay()
        poster_overlay.set_child(
            Gtk.Image.new_from_icon_name("image-missing-symbolic")
        )
        poster = Gtk.Picture()
        poster.set_content_fit(Gtk.ContentFit.COVER)
        poster.set_can_shrink(True)
        poster.set_hexpand(True)
        poster.set_vexpand(True)
        poster_overlay.add_overlay(poster)
        frame = Gtk.AspectFrame(ratio=2 / 3, obey_child=False)
        frame.set_size_request(180, 270)
        frame.set_child(poster_overlay)
        card.append(frame)
        title = str(result.get("title") or result.get("name") or "Untitled")
        title_label = Gtk.Label(label=title, xalign=0, wrap=True, lines=2)
        title_label.add_css_class("heading")
        title_label.set_ellipsize(Pango.EllipsizeMode.END)
        card.append(title_label)
        media_type = str(result.get("mediaType") or "movie")
        date = str(result.get("releaseDate") or result.get("firstAirDate") or "")
        meta = Gtk.Label(
            label=f"{'Show' if media_type == 'tv' else 'Movie'}{f' · {date[:4]}' if date else ''}",
            xalign=0,
        )
        meta.add_css_class("caption")
        meta.add_css_class("dim-label")
        card.append(meta)
        media_info = result.get("mediaInfo") or {}
        status = int(media_info.get("status") or 0) if isinstance(media_info, dict) else 0
        requested = status > 1
        button = Gtk.Button(
            label="Available" if status == 5 else "Requested" if requested else "Request",
            icon_name="object-select-symbolic" if requested else "list-add-symbolic",
        )
        button.set_sensitive(not requested and status != 5)
        button.add_css_class("suggested-action")
        media_id = int(result.get("id") or 0)
        button.connect(
            "clicked",
            lambda clicked, kind=media_type, identifier=media_id: self._seerr_request(
                clicked, kind, identifier
            ),
        )
        card.append(button)
        poster_path = str(
            result.get("posterPath") or result.get("poster_path") or ""
        )
        if poster_path:
            if poster_path.startswith("//"):
                poster_url = f"https:{poster_path}"
            elif poster_path.startswith(("http://", "https://")):
                poster_url = poster_path
            else:
                poster_url = (
                    "https://image.tmdb.org/t/p/w300_and_h450_face/"
                    f"{poster_path.lstrip('/')}"
                )
            self.thumbnails.load(
                poster_url,
                lambda path, picture=poster: self._set_details_result_picture(picture, path),
            )
        return card

    @staticmethod
    def _set_details_result_picture(picture: Gtk.Picture, path: object) -> bool:
        if path:
            picture.set_filename(str(path))
        return GLib.SOURCE_REMOVE

    def _seerr_request(self, button: Gtk.Button, media_type: str, media_id: int) -> None:
        button.set_sensitive(False)
        button.set_label("Requesting…")
        run_async(
            lambda: self.seerr.request_media(media_type, media_id),
            lambda _result: self._seerr_request_done(button),
            lambda error: self._seerr_request_error(button, error),
        )

    def _seerr_request_done(self, button: Gtk.Button) -> bool:
        button.set_label("Requested")
        self.toast_overlay.add_toast(Adw.Toast(title="Request sent to Seerr"))
        return GLib.SOURCE_REMOVE

    def _seerr_request_error(self, button: Gtk.Button, error: Exception) -> bool:
        button.set_label("Request")
        button.set_sensitive(True)
        self.toast_overlay.add_toast(Adw.Toast(title=str(error)))
        return GLib.SOURCE_REMOVE

    def _open_seerr_login(self) -> None:
        if self.seerr_login_window:
            self.seerr_login_window.present()
            return
        window = Adw.Window(transient_for=self, modal=True, title="Sign in to Seerr")
        window.set_default_size(480, 300)
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(
            Adw.WindowTitle(title="Sign in to Seerr", subtitle="Jellyfin account")
        )
        toolbar.add_top_bar(header)
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(
            title="Jellyfin account",
            description=(
                "Sign in through Seerr with your Jellyfin username and password. "
                "The password is not saved by TubeFin."
            ),
        )
        username = Adw.EntryRow(title="Username")
        if self.jellyfin.session:
            username.set_text(self.jellyfin.session.username)
        password = Adw.PasswordEntryRow(title="Password")
        group.add(username)
        group.add(password)
        action = Adw.ActionRow(
            title="Seerr session",
            subtitle="Use an API key in Settings if password sign-in is disabled.",
        )
        sign_in = Gtk.Button(label="Sign in", valign=Gtk.Align.CENTER)
        sign_in.add_css_class("suggested-action")
        sign_in.connect(
            "clicked",
            lambda *_: self._seerr_credentials_login(
                username, password, sign_in, window
            ),
        )
        action.add_suffix(sign_in)
        group.add(action)
        page.add(group)
        toolbar.set_content(page)
        window.set_content(toolbar)
        self.seerr_login_window = window
        window.connect("close-request", self._seerr_login_closed)
        window.present()

    def _seerr_credentials_login(
        self,
        username: Adw.EntryRow,
        password: Adw.PasswordEntryRow,
        button: Gtk.Button,
        window: Adw.Window,
    ) -> None:
        name = username.get_text().strip()
        secret = password.get_text()
        if not name or not secret:
            self.toast_overlay.add_toast(
                Adw.Toast(title="Enter your Jellyfin username and password")
            )
            return
        button.set_label("Signing in…")
        button.set_sensitive(False)
        run_async(
            lambda: self.seerr.jellyfin_login(name, secret),
            lambda _result: self._seerr_credentials_connected(window),
            lambda error: self._seerr_credentials_error(button, error),
        )

    def _seerr_credentials_connected(self, window: Adw.Window) -> bool:
        self.seerr_authenticated = True
        self.seerr_search.set_sensitive(True)
        self.seerr_connect.set_label("Connected to Seerr")
        self.seerr_connect.set_sensitive(False)
        self.seerr_status.set_title("Find something to watch")
        self.seerr_status.set_description(
            "Search Seerr, then request a movie or every season of a show."
        )
        window.close()
        self.toast_overlay.add_toast(Adw.Toast(title="Connected to Seerr"))
        return GLib.SOURCE_REMOVE

    def _seerr_credentials_error(
        self, button: Gtk.Button, error: Exception
    ) -> bool:
        button.set_label("Sign in")
        button.set_sensitive(True)
        self.toast_overlay.add_toast(Adw.Toast(title=str(error)))
        return GLib.SOURCE_REMOVE

    def _seerr_login_closed(self, _window: Adw.Window) -> bool:
        self.seerr_login_window = None
        return False

    def _load_offline(self) -> None:
        self._clear_box(self.offline_grid)
        records = self.offline.list(self.offline_search.get_text())
        usage = self.offline.storage_usage()
        self.offline_usage.set_label(f"{len(records)} items · {self._size_label(usage)}")
        self._refresh_sidebar_downloads()
        if not records:
            empty = Gtk.Label(label="No downloaded videos yet.", xalign=0)
            empty.add_css_class("dim-label")
            self.offline_grid.append(empty)
            return
        for record in records:
            display_item = replace(
                self._offline_item(record),
                subtitle=self._download_subtitle(record),
                payload={
                    **self._offline_item(record).payload,
                    "download_progress": record.progress,
                },
            )
            card_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            card_box.set_size_request(270, -1)
            card_box.set_halign(Gtk.Align.START)
            card_box.append(
                MediaCard(
                    display_item,
                    self.thumbnails,
                    self._activate_item,
                    on_queue=self._add_to_queue,
                    on_queue_next=self._add_to_queue_next,
                    on_save=self._save_item,
                    on_watch_later=self._watch_later,
                    on_remove=self._delete_download_item,
                    on_mark_watched=self._mark_watched,
                    on_share=self._share_item,
                )
            )
            controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            controls.set_halign(Gtk.Align.END)
            if record.status == DownloadStatus.DOWNLOADING:
                progress = Gtk.ProgressBar(width_request=130, hexpand=True)
                progress.set_fraction(max(0, min(1, record.progress / 100)))
                progress.set_text(f"{record.progress:.0f}%")
                progress.set_show_text(True)
                controls.append(progress)
                cancel = labeled_button("Cancel", "process-stop-symbolic")
                cancel.connect(
                    "clicked", lambda _button, value=record.id: self.downloads.cancel(value)
                )
                controls.append(cancel)
            elif record.status in {DownloadStatus.FAILED, DownloadStatus.CANCELLED}:
                retry = labeled_button("Retry", "view-refresh-symbolic")
                retry.connect(
                    "clicked",
                    lambda _button, value=record.id: self.downloads.retry(
                        value, self._download_changed
                    ),
                )
                controls.append(retry)
            elif record.status == DownloadStatus.MISSING:
                locate = labeled_button("Find file", "edit-find-symbolic")
                locate.connect(
                    "clicked", lambda _button, value=record.id: self._find_download(value)
                )
                controls.append(locate)
            if controls.get_first_child():
                card_box.append(controls)
            self.offline_grid.append(card_box)

    def _refresh_sidebar_downloads(self) -> None:
        records = self.offline.list()
        active = [
            record
            for record in records
            if record.status in {DownloadStatus.QUEUED, DownloadStatus.DOWNLOADING}
        ]
        self.sidebar_download.set_visible(True)
        if not records:
            self.sidebar_download_title.set_label("Downloads")
            self.sidebar_download_detail.set_label("0 items · 0.0 B")
            self.sidebar_download_progress.set_visible(False)
            return
        if active:
            progress = sum(record.progress for record in active) / len(active)
            self.sidebar_download_title.set_label(
                "Downloading video" if len(active) == 1 else f"Downloading {len(active)} videos"
            )
            self.sidebar_download_detail.set_label(f"{progress:.0f}% complete")
            self.sidebar_download_progress.set_fraction(max(0, min(1, progress / 100)))
            self.sidebar_download_progress.set_visible(True)
        else:
            self.sidebar_download_title.set_label("Downloads")
            self.sidebar_download_detail.set_label(
                f"{len(records)} items · {self._size_label(self.offline.storage_usage())}"
            )
            self.sidebar_download_progress.set_visible(False)

    @staticmethod
    def _size_label(size: int) -> str:
        value = float(size)
        for unit in ("B", "KiB", "MiB", "GiB"):
            if value < 1024 or unit == "GiB":
                return f"{value:.1f} {unit}"
            value /= 1024
        return f"{value:.1f} GiB"

    def _download_subtitle(self, record: DownloadRecord) -> str:
        detail = (
            f"{record.progress:.0f}%"
            if record.status == DownloadStatus.DOWNLOADING
            else record.status.value.replace("_", " ").title()
        )
        source = record.item.source.capitalize()
        channel = record.item.subtitle or "Unknown channel"
        return (
            f"{channel} · {source} · {detail} · {record.quality}"
            + (f" · {record.error}" if record.error else "")
        )

    @staticmethod
    def _offline_item(record: DownloadRecord) -> MediaItem:
        metadata = record.item.payload.get("download_metadata") or {}
        thumbnail_url = str(metadata.get("local_thumbnail_url") or "")
        if thumbnail_url.startswith("file:"):
            parsed = GLib.filename_from_uri(thumbnail_url)
            if not parsed or not Path(parsed[0]).is_file():
                thumbnail_url = ""
        elif thumbnail_url and not Path(thumbnail_url).is_file():
            thumbnail_url = ""
        if not thumbnail_url:
            directory = Path(record.directory)
            local_thumbnails = [
                path
                for suffix in ("*.jpg", "*.jpeg", "*.webp", "*.png")
                for path in directory.glob(suffix)
                if path.is_file() and ".series." not in path.name
            ]
            if local_thumbnails:
                thumbnail_url = max(
                    local_thumbnails, key=lambda path: path.stat().st_size
                ).resolve().as_uri()
        return replace(
            record.item,
            source="offline",
            thumbnail_url=thumbnail_url or record.item.thumbnail_url,
            playable=record.status == DownloadStatus.COMPLETE,
            payload={
                **record.item.payload,
                "media_path": record.media_path,
                "download_id": record.id,
                "original_source": record.item.source,
                "original_channel": record.item.subtitle,
            },
        )

    def _download_detail_item(self) -> None:
        if not self.detail_item or self.detail_item.source not in {"youtube", "jellyfin"}:
            return
        self._download_item(self.detail_item)

    def _download_item(
        self,
        item: MediaItem,
        *,
        quality: str | None = None,
        audio_only: bool = False,
        captions: bool = True,
    ) -> None:
        if item.source == "jellyfin" and self._synctube_active():
            return
        if item.source == "jellyfin":
            self._start_download(item, "original", False, True)
            return
        if quality is not None:
            self._start_download(item, quality, audio_only, captions)
            return
        dialog = Adw.Window(transient_for=self, modal=True, title="Download video")
        dialog.set_default_size(430, 520)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        dialog.set_content(toolbar)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        content.set_margin_top(24)
        content.set_margin_bottom(24)
        content.set_margin_start(24)
        content.set_margin_end(24)
        toolbar.set_content(content)
        title = Gtk.Label(label=item.title, xalign=0, wrap=True)
        title.add_css_class("title-2")
        content.append(title)
        quality_choices = [
            "Automatic",
            "Maximum quality",
            "Minimum quality",
            "No video (audio only)",
        ]
        quality = Gtk.DropDown.new_from_strings(quality_choices)
        quality.set_hexpand(True)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row.append(Gtk.Label(label="Quality", xalign=0, hexpand=True))
        row.append(quality)
        content.append(row)
        captions = Gtk.CheckButton(label="Include subtitles when available")
        captions.set_active(True)
        content.append(captions)
        audio_title = Gtk.Label(label="Audio tracks", xalign=0)
        audio_title.add_css_class("heading")
        content.append(audio_title)
        audio_scroller = Gtk.ScrolledWindow()
        audio_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        audio_scroller.set_max_content_height(150)
        audio_scroller.set_propagate_natural_height(True)
        audio_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        original_audio = Gtk.CheckButton(label="Original audio")
        original_audio.set_active(True)
        audio_box.append(original_audio)
        audio_scroller.set_child(audio_box)
        content.append(audio_scroller)
        audio_buttons: list[tuple[AudioTrack, Gtk.CheckButton]] = []
        loading_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        loading_row.set_halign(Gtk.Align.START)
        spinner = Gtk.Spinner(spinning=True)
        loading_row.append(spinner)
        loading_label = Gtk.Label(
            label="Loading exact qualities and audio tracks…", xalign=0
        )
        loading_label.add_css_class("dim-label")
        loading_row.append(loading_label)
        content.append(loading_row)
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        actions.set_halign(Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: dialog.close())
        actions.append(cancel)
        download = Gtk.Button(label="Download", icon_name="folder-download-symbolic")
        download.add_css_class("suggested-action")

        def begin(*_args: object) -> None:
            selected = quality.get_selected()
            choice = (
                quality_choices[selected]
                if selected < len(quality_choices)
                else "Maximum quality"
            )
            audio_only = choice == "No video (audio only)"
            selected_quality = {
                "Automatic": "best",
                "Maximum quality": "best",
                "Minimum quality": "worst",
                "No video (audio only)": "best",
            }.get(choice, choice)
            self._start_download(
                item,
                selected_quality,
                audio_only,
                captions.get_active(),
                [
                    track.format_id
                    for track, button in audio_buttons
                    if button.get_active() and track.format_id
                ],
            )
            dialog.close()

        download.connect("clicked", begin)
        actions.append(download)
        content.append(actions)
        dialog.present()
        run_async(
            lambda: self.youtube.download_options(item),
            lambda options: self._download_options_loaded(
                dialog,
                quality,
                captions,
                loading_row,
                spinner,
                loading_label,
                quality_choices,
                audio_box,
                audio_buttons,
                options,
            ),
            lambda error: self._download_options_error(
                dialog, spinner, loading_label, error
            ),
        )

    def _download_options_loaded(
        self,
        dialog: Adw.Window,
        quality: Gtk.DropDown,
        captions: Gtk.CheckButton,
        loading_row: Gtk.Box,
        spinner: Gtk.Spinner,
        loading_label: Gtk.Label,
        quality_choices: list[str],
        audio_box: Gtk.Box,
        audio_buttons: list[tuple[AudioTrack, Gtk.CheckButton]],
        options: tuple[list[str], bool, list[AudioTrack]],
    ) -> bool:
        if not dialog.get_visible():
            return GLib.SOURCE_REMOVE
        qualities, has_subtitles, audio_tracks = options
        selected = quality.get_selected()
        selected_label = quality_choices[selected] if selected < len(quality_choices) else ""
        exact = [value for value in qualities if value not in quality_choices]
        quality_choices[3:3] = exact
        quality.set_model(Gtk.StringList.new(quality_choices))
        if selected_label in quality_choices:
            quality.set_selected(quality_choices.index(selected_label))
        captions.set_label(
            "Include creator-provided subtitles"
            if has_subtitles
            else "No creator-provided subtitles found"
        )
        if not has_subtitles:
            captions.set_active(False)
        captions.set_sensitive(has_subtitles)
        self._clear_box(audio_box)
        preferred_index = -1
        if self.preferred_audio_language and audio_tracks:
            scores = [
                MpvPlayer.language_match_score(
                    self.preferred_audio_language, track.label, track.language
                )
                for track in audio_tracks
            ]
            best = max(range(len(scores)), key=scores.__getitem__)
            if scores[best] >= 0.55:
                preferred_index = best
        for index, track in enumerate(audio_tracks):
            button = Gtk.CheckButton(label=track.label)
            button.set_active(track.original or index == preferred_index)
            audio_box.append(button)
            audio_buttons.append((track, button))
        if audio_tracks and not any(button.get_active() for _, button in audio_buttons):
            audio_buttons[0][1].set_active(True)
        if not audio_tracks:
            audio_box.append(Gtk.Label(label="Original audio", xalign=0))
        spinner.stop()
        loading_row.set_visible(False)
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _download_options_error(
        dialog: Adw.Window,
        spinner: Gtk.Spinner,
        loading_label: Gtk.Label,
        error: Exception,
    ) -> bool:
        if dialog.get_visible():
            spinner.stop()
            spinner.set_visible(False)
            loading_label.set_label(f"Exact qualities unavailable; defaults still work. {error}")
            loading_label.set_wrap(True)
        return GLib.SOURCE_REMOVE

    def _start_download(
        self,
        item: MediaItem,
        quality: str,
        audio_only: bool,
        captions: bool,
        audio_tracks: list[str] | None = None,
    ) -> None:
        try:
            record = self.downloads.enqueue(
                item,
                quality=quality,
                audio_only=audio_only,
                audio_tracks=audio_tracks,
                captions=captions,
                callback=self._download_changed,
            )
        except Exception as error:
            self.toast_overlay.add_toast(Adw.Toast(title=str(error)))
            return
        self.toast_overlay.add_toast(Adw.Toast(title=f"Downloading {record.item.title}"))
        self._refresh_sidebar_downloads()
        if self._visible_page_name() == "downloads":
            self._load_offline()

    def _download_changed(self, record: DownloadRecord) -> None:
        GLib.idle_add(self._download_changed_ui, record)

    def _download_changed_ui(self, record: DownloadRecord) -> bool:
        self._refresh_sidebar_downloads()
        if self._visible_page_name() == "downloads":
            self._load_offline()
        if record.status in {DownloadStatus.COMPLETE, DownloadStatus.FAILED}:
            title = (
                f"Downloaded {record.item.title}"
                if record.status == DownloadStatus.COMPLETE
                else f"Download failed: {record.error}"
            )
            self.toast_overlay.add_toast(Adw.Toast(title=title))
        return GLib.SOURCE_REMOVE

    def _delete_download(self, record_id: str) -> None:
        self.downloads.cancel(record_id)
        self.offline.remove(record_id)
        self._load_offline()

    def _delete_download_item(self, item: MediaItem) -> None:
        record_id = str(item.payload.get("download_id") or "")
        if record_id:
            self._delete_download(record_id)

    def _find_download(self, record_id: str) -> None:
        found = self.offline.find_moved(record_id)
        self.toast_overlay.add_toast(
            Adw.Toast(title="Moved file found." if found else "No matching file was found.")
        )
        self._load_offline()

    def _create_playlist(self) -> None:
        name = self.new_playlist_name.get_text().strip()
        if not name:
            return
        self.playlists.create(name)
        self.new_playlist_name.set_text("")
        self._load_playlists()

    def _choose_playlist_import(self) -> None:
        chooser = Gtk.FileChooserNative(
            title="Import TubeFin playlists",
            transient_for=self,
            action=Gtk.FileChooserAction.OPEN,
            accept_label="Import",
            cancel_label="Cancel",
        )
        playlist_filter = Gtk.FileFilter()
        playlist_filter.set_name("JSON playlists")
        playlist_filter.add_mime_type("application/json")
        playlist_filter.add_pattern("*.json")
        chooser.add_filter(playlist_filter)
        chooser.connect("response", self._playlist_import_response)
        self.playlist_file_chooser = chooser
        chooser.show()

    def _playlist_import_response(
        self, chooser: Gtk.FileChooserNative, response: int
    ) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            selected = chooser.get_file()
            path = selected.get_path() if selected else None
            try:
                if not path:
                    raise ValueError("Choose a local playlist file.")
                count = self.playlists.import_file(Path(path))
            except (OSError, TypeError, ValueError) as error:
                self.toast_overlay.add_toast(Adw.Toast(title=f"Could not import: {error}"))
            else:
                self._load_playlists()
                self.toast_overlay.add_toast(
                    Adw.Toast(title=f"Imported {count} {'playlist' if count == 1 else 'playlists'}")
                )
        chooser.hide()
        self.playlist_file_chooser = None

    def _choose_playlist_export(self) -> None:
        chooser = Gtk.FileChooserNative(
            title="Export TubeFin playlists",
            transient_for=self,
            action=Gtk.FileChooserAction.SAVE,
            accept_label="Export",
            cancel_label="Cancel",
        )
        chooser.set_current_name("tubefin-playlists.json")
        chooser.connect("response", self._playlist_export_response)
        self.playlist_file_chooser = chooser
        chooser.show()

    def _playlist_export_response(
        self, chooser: Gtk.FileChooserNative, response: int
    ) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            selected = chooser.get_file()
            path = selected.get_path() if selected else None
            try:
                if not path:
                    raise ValueError("Choose a local destination.")
                count = self.playlists.export_file(Path(path))
            except (OSError, TypeError, ValueError) as error:
                self.toast_overlay.add_toast(Adw.Toast(title=f"Could not export: {error}"))
            else:
                self.toast_overlay.add_toast(
                    Adw.Toast(title=f"Exported {count} {'playlist' if count == 1 else 'playlists'}")
                )
        chooser.hide()
        self.playlist_file_chooser = None

    def _load_playlists(self) -> None:
        self._clear_box(self.playlists_box)
        playlists = self.playlists.list()
        if not playlists:
            empty = Gtk.Label(label="No local playlists yet.")
            empty.add_css_class("dim-label")
            self.playlists_box.append(empty)
            return
        for playlist in playlists:
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=9)
            card.set_size_request(270, -1)
            card.set_halign(Gtk.Align.START)
            preview = Gtk.Overlay()
            preview.add_css_class("playlist-preview")
            preview.set_size_request(270, 152)
            preview.set_halign(Gtk.Align.START)
            preview.set_vexpand(False)
            preview.set_overflow(Gtk.Overflow.HIDDEN)
            fallback = Gtk.Image.new_from_icon_name("view-list-symbolic")
            fallback.set_pixel_size(44)
            fallback.add_css_class("dim-label")
            preview.set_child(fallback)
            first_item = self._refresh_stored_artwork(playlist.items[0]) if playlist.items else None
            if first_item and first_item.thumbnail_url:
                picture = Gtk.Picture()
                picture.set_content_fit(Gtk.ContentFit.COVER)
                picture.set_hexpand(True)
                picture.set_vexpand(True)
                picture.set_visible(False)
                preview.add_overlay(picture)
                self.thumbnails.load(
                    first_item.thumbnail_url,
                    lambda path, target=picture: self._set_playlist_picture(target, path),
                )
            copy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            name = Gtk.Label(label=playlist.name, xalign=0)
            name.add_css_class("media-title")
            copy.append(name)
            count = len(playlist.items)
            subtitle = Gtk.Label(label=f"{count} {'item' if count == 1 else 'items'}", xalign=0)
            subtitle.add_css_class("dim-label")
            subtitle.add_css_class("caption")
            copy.append(subtitle)
            aspect = Gtk.AspectFrame(ratio=16 / 9, obey_child=False)
            aspect.set_size_request(270, 152)
            aspect.set_halign(Gtk.Align.START)
            aspect.set_vexpand(False)
            aspect.set_child(preview)
            card.append(aspect)
            card.append(copy)
            open_playlist = Gtk.Button(child=card)
            open_playlist.set_size_request(270, -1)
            open_playlist.set_halign(Gtk.Align.START)
            open_playlist.set_valign(Gtk.Align.START)
            open_playlist.add_css_class("flat")
            open_playlist.add_css_class("playlist-card")
            open_playlist.connect(
                "clicked", lambda _button, value=playlist.id: self._show_playlist(value)
            )
            self.playlists_box.append(open_playlist)

    @staticmethod
    def _set_playlist_picture(picture: Gtk.Picture, path: object) -> bool:
        if path:
            picture.set_filename(str(path))
            picture.set_visible(True)
        return GLib.SOURCE_REMOVE

    def _show_playlist(self, playlist_id: str) -> None:
        playlist = next((value for value in self.playlists.list() if value.id == playlist_id), None)
        if not playlist:
            self._select_page("library")
            return
        self.remote_playlist_active = False
        self.youtube_playlist_items = []
        self.active_playlist_id = playlist.id
        self.playlist_title.set_label(playlist.name)
        self.playlist_name_entry.set_text(playlist.name)
        self.playlist_play.set_sensitive(bool(playlist.items))
        self._render_playlist_items(playlist.items, playlist.id)
        self._set_visible_page("playlist")
        self.mini_player.set_visible(bool(self.current_item or self.queue))

    def _render_playlist_items(
        self, items: list[MediaItem], playlist_id: str = ""
    ) -> None:
        self._clear_box(self.playlist_items)
        for index, item in enumerate(items):
            display_item = self._refresh_stored_artwork(item)
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            row.add_css_class("playlist-item")
            if playlist_id:
                drag_handle = Gtk.Image.new_from_icon_name("list-drag-handle-symbolic")
                drag_handle.set_tooltip_text("Drag to reorder")
                drag_handle.add_css_class("dim-label")
                row.append(drag_handle)
            number = Gtk.Label(label=str(index + 1), width_chars=2)
            number.add_css_class("dim-label")
            row.append(number)
            picture = Gtk.Picture()
            picture.set_content_fit(Gtk.ContentFit.COVER)
            picture.set_can_shrink(True)
            picture.add_css_class("playlist-item-picture")
            picture_frame = Gtk.AspectFrame(ratio=16 / 9, obey_child=False)
            picture_frame.set_size_request(270, 152)
            picture_frame.set_halign(Gtk.Align.START)
            picture_frame.set_valign(Gtk.Align.CENTER)
            picture_frame.set_child(picture)
            row.append(picture_frame)
            if display_item.thumbnail_url:
                self.thumbnails.load(
                    display_item.thumbnail_url,
                    lambda path, target=picture: self._set_playlist_picture(target, path),
                )
            copy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
            title = Gtk.Label(label=item.title, xalign=0, ellipsize=Pango.EllipsizeMode.END)
            title.add_css_class("heading")
            copy.append(title)
            subtitle = Gtk.Label(label=item.subtitle, xalign=0)
            subtitle.add_css_class("dim-label")
            subtitle.add_css_class("caption")
            copy.append(subtitle)
            activate = Gtk.Button(child=copy, hexpand=True)
            activate.add_css_class("flat")
            activate.connect("clicked", lambda _button, value=item: self._activate_item(value))
            row.append(activate)
            if playlist_id:
                remove = Gtk.Button(icon_name="list-remove-symbolic", tooltip_text="Remove")
                remove.add_css_class("square-button")
                remove.set_size_request(40, 40)
                remove.set_valign(Gtk.Align.CENTER)
                remove.connect(
                    "clicked",
                    lambda _button, pid=playlist_id, position=index: (
                        self._remove_playlist_item(pid, position)
                    ),
                )
                row.append(remove)
                drag_source = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
                drag_source.connect("prepare", self._prepare_playlist_drag, playlist_id, index)
                row.add_controller(drag_source)
                drop_target = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE)
                drop_target.set_preload(True)
                drop_target.connect("drop", self._drop_playlist_item, playlist_id, index)
                row.add_controller(drop_target)
            self.playlist_items.append(row)

    def _refresh_stored_artwork(self, item: MediaItem) -> MediaItem:
        if item.source != "jellyfin" or not self.jellyfin.session:
            return item
        try:
            return self.jellyfin.refresh_item_artwork(item)
        except (KeyError, TypeError, ValueError):
            return item

    @staticmethod
    def _prepare_playlist_drag(
        _source: Gtk.DragSource,
        _x: float,
        _y: float,
        playlist_id: str,
        index: int,
    ) -> Gdk.ContentProvider:
        value = GObject.Value()
        value.init(GObject.TYPE_STRING)
        value.set_string(f"{playlist_id}\n{index}")
        return Gdk.ContentProvider.new_for_value(value)

    def _drop_playlist_item(
        self,
        _target: Gtk.DropTarget,
        value: str,
        _x: float,
        _y: float,
        playlist_id: str,
        index: int,
    ) -> bool:
        try:
            source_playlist, source_index = value.rsplit("\n", 1)
            old = int(source_index)
        except (AttributeError, ValueError):
            return False
        if source_playlist != playlist_id or old == index:
            return source_playlist == playlist_id
        self._move_playlist_item(playlist_id, old, index)
        return True

    def _rename_playlist(self, playlist_id: str, name: str) -> None:
        self.playlists.rename(playlist_id, name)
        self._load_playlists()
        if self._visible_page_name() == "playlist":
            self._show_playlist(playlist_id)

    def _delete_playlist(self, playlist_id: str) -> None:
        self.playlists.delete(playlist_id)
        self._load_playlists()

    def _delete_active_playlist(self) -> None:
        if not self.active_playlist_id:
            return
        self._delete_playlist(self.active_playlist_id)
        self.active_playlist_id = ""
        self._select_page("library")

    def _remove_playlist_item(self, playlist_id: str, index: int) -> None:
        self.playlists.remove(playlist_id, index)
        self._load_playlists()
        if self._visible_page_name() == "playlist":
            self._show_playlist(playlist_id)

    def _move_playlist_item(self, playlist_id: str, old: int, new: int) -> None:
        self.playlists.reorder(playlist_id, old, new)
        self._load_playlists()
        if self._visible_page_name() == "playlist":
            self._show_playlist(playlist_id)

    def _play_playlist(self, playlist_id: str) -> None:
        playlist = next((value for value in self.playlists.list() if value.id == playlist_id), None)
        if not playlist or not playlist.items:
            return
        self.queue = list(playlist.items)
        self.queue_index = 0
        self._refresh_queue()
        self._begin_playback(self.queue[0])

    def _play_active_playlist(self) -> None:
        if not self.remote_playlist_active:
            self._play_playlist(self.active_playlist_id)
            return
        if not self.youtube_playlist_items:
            return
        self.queue = list(self.youtube_playlist_items)
        self.queue_index = 0
        self._refresh_queue()
        self._begin_playback(self.queue[0])

    def _save_detail_to_playlist(self) -> None:
        if not self.detail_item:
            return
        self._save_item(self.detail_item)

    def _browse_youtube_playlist(self) -> None:
        url = self.youtube_playlist_url.get_text().strip()
        if not url:
            return
        self._open_youtube_playlist(url)

    def _open_youtube_playlist(self, url: str) -> None:
        self.remote_playlist_active = True
        self.active_playlist_id = ""
        self.youtube_playlist_items = []
        self.playlist_title.set_label("Loading YouTube playlist…")
        self.playlist_play.set_sensitive(False)
        self._clear_box(self.playlist_items)
        loading = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        loading.set_halign(Gtk.Align.CENTER)
        loading.set_margin_top(48)
        spinner = Gtk.Spinner(spinning=True)
        loading.append(spinner)
        loading.append(Gtk.Label(label="Loading playlist…"))
        self.playlist_items.append(loading)
        self._set_visible_page("playlist")
        run_async(
            lambda: self.youtube.playlist(url, page_size=100),
            self._youtube_playlist_loaded,
            self._youtube_playlist_error,
        )

    def _youtube_playlist_loaded(self, result: tuple[str, list[MediaItem], str | None]) -> bool:
        title, items, _cursor = result
        if not self.remote_playlist_active:
            return GLib.SOURCE_REMOVE
        self.youtube_playlist_items = list(items)
        self.playlist_title.set_label(title)
        self.window_title.set_subtitle(title)
        self.playlist_play.set_sensitive(bool(items))
        self._render_playlist_items(items)
        if not items:
            self.playlist_items.append(Gtk.Label(label="This playlist has no playable videos."))
        return GLib.SOURCE_REMOVE

    def _youtube_playlist_error(self, error: Exception) -> bool:
        if not self.remote_playlist_active:
            return GLib.SOURCE_REMOVE
        self.playlist_title.set_label("YouTube playlist")
        self._clear_box(self.playlist_items)
        status = Adw.StatusPage(
            icon_name="dialog-error-symbolic",
            title="Could not load playlist",
            description=str(error),
        )
        self.playlist_items.append(status)
        return GLib.SOURCE_REMOVE

    def _save_oauth_client(self, *, notify: bool = True) -> None:
        client_id = self.oauth_client_id.get_text().strip()
        self.config.save_oauth_client_id(client_id)
        self.oauth = OAuthClient(client_id)
        if notify:
            self.toast_overlay.add_toast(Adw.Toast(title="Google sign-in configuration saved"))

    def _oauth_sign_in(self, manage: bool) -> None:
        self._save_oauth_client(notify=False)
        run_async(
            lambda: self.oauth.authorize(
                manage_playlists=manage,
                url_opener=self._launch_default_uri,
            ),
            self._oauth_connected,
            lambda error: self.toast_overlay.add_toast(Adw.Toast(title=str(error))),
        )

    def _oauth_connected(self, account: OAuthAccount) -> bool:
        self.config.save_oauth_account(account)
        self.oauth_accounts = [value for value in self.oauth_accounts if value.id != account.id]
        self.oauth_accounts.append(account)
        self.active_oauth_account = account
        self.browse_cache.clear()
        self.channel_cache.clear()
        self._refresh_accounts()
        self._sync_online_subscriptions()
        self.home_signed_out.set_visible(False)
        run_async(
            lambda: self._account_feed_items("history"),
            lambda items: self._append_home_section(
                "Subscription Activity · Provided by YouTube", items
            ),
            lambda _error: None,
        )
        if hasattr(self, "youtube_settings_status"):
            self.youtube_settings_status.set_subtitle(self._youtube_connection_status())
        if hasattr(self, "comment_composer"):
            self._refresh_comment_composer()
        self.toast_overlay.add_toast(Adw.Toast(title=f"Signed in as {account.display_name}"))
        return GLib.SOURCE_REMOVE

    def _refresh_accounts(self) -> None:
        self._clear_box(self.account_list)
        if not self.oauth_accounts:
            self.account_list.append(Adw.ActionRow(title="No YouTube accounts connected"))
        for account in self.oauth_accounts:
            active = account == self.active_oauth_account
            row = Adw.ActionRow(
                title=account.display_name,
                subtitle=f"{account.email} · {'Active' if active else 'Available'}",
            )
            use = Gtk.Button(label="Active" if active else "Use")
            use.set_sensitive(not active)
            use.connect("clicked", lambda _button, value=account: self._switch_account(value))
            row.add_suffix(use)
            revoke = Gtk.Button(
                icon_name="system-log-out-symbolic", tooltip_text="Sign out and revoke"
            )
            revoke.connect("clicked", lambda _button, value=account: self._oauth_sign_out(value))
            row.add_suffix(revoke)
            self.account_list.append(row)

    def _switch_account(self, account: OAuthAccount) -> None:
        self.active_oauth_account = account
        self.config.set_active_oauth_account(account.id)
        self.browse_cache.clear()
        self.channel_cache.clear()
        self._refresh_accounts()
        self._sync_online_subscriptions()
        if hasattr(self, "comment_composer"):
            self._refresh_comment_composer()

    def _oauth_sign_out(self, account: OAuthAccount) -> None:
        run_async(
            lambda: self.oauth.sign_out(account),
            lambda _result: self._oauth_signed_out(account),
            lambda error: self.toast_overlay.add_toast(Adw.Toast(title=str(error))),
        )

    def _oauth_signed_out(self, account: OAuthAccount) -> bool:
        self.config.remove_oauth_account(account.id)
        self.oauth_accounts = [value for value in self.oauth_accounts if value.id != account.id]
        if self.active_oauth_account == account:
            self.active_oauth_account = self.oauth_accounts[0] if self.oauth_accounts else None
            if self.active_oauth_account:
                self.config.set_active_oauth_account(self.active_oauth_account.id)
        self._refresh_accounts()
        if hasattr(self, "youtube_settings_status"):
            self.youtube_settings_status.set_subtitle(self._youtube_connection_status())
        if hasattr(self, "comment_composer"):
            self._refresh_comment_composer()
        return GLib.SOURCE_REMOVE

    def _load_account_feed(self, feed: str) -> None:
        if not self.active_oauth_account and not self.youtube_browser_session:
            self.account_grid.set_status(
                "avatar-default-symbolic",
                "Sign in first",
                "Connect YouTube in Accounts above, or add optional API access.",
            )
            return
        if feed == "playlists":
            if not self.active_oauth_account:
                self.account_grid.set_status(
                    "dialog-information-symbolic",
                    "API access required",
                    "Remote playlist editing requires optional YouTube API access.",
                )
                return
            if MANAGE_SCOPE not in self.active_oauth_account.scopes:
                self.account_grid.set_status(
                    "dialog-information-symbolic",
                    "Standard account",
                    "Sign in with playlist access to create, edit, and delete account playlists.",
                )
                return
            run_async(
                self._account_playlists,
                self._account_playlists_loaded,
                lambda error: self._grid_error(self.account_grid, error),
            )
            return
        self.account_grid.set_loading(f"Loading {feed}…")
        run_async(
            (
                lambda: self.youtube.personal_feed(feed)
                if self.youtube_browser_session
                else self._account_feed_items(feed)
            ),
            self._account_feed_loaded,
            lambda error: self._grid_error(self.account_grid, error),
        )

    def _account_feed_items(self, feed: str) -> list[MediaItem]:
        if not self.active_oauth_account:
            return []
        token = self.oauth.access_token(self.active_oauth_account)
        values, cursor = self.youtube.api_feed(token, feed)
        pages = 1
        while feed == "subscriptions" and cursor and pages < 200:
            following, cursor = self.youtube.api_feed(token, feed, cursor)
            values.extend(following)
            pages += 1
        items: list[MediaItem] = []
        for value in values:
            snippet = value.get("snippet") or {}
            details = value.get("contentDetails") or {}
            resource = snippet.get("resourceId") or {}
            if feed == "subscriptions" and (channel_id := resource.get("channelId")):
                thumbnails = snippet.get("thumbnails") or {}
                thumbnail = next(
                    (
                        candidate.get("url")
                        for candidate in reversed(list(thumbnails.values()))
                        if candidate.get("url")
                    ),
                    None,
                )
                items.append(
                    MediaItem(
                        id=str(channel_id),
                        title=str(snippet.get("title") or "YouTube channel"),
                        subtitle="Subscription",
                        source="youtube-channel",
                        thumbnail_url=thumbnail,
                        playable=False,
                        payload={
                            "channel_url": f"https://www.youtube.com/channel/{channel_id}",
                            "channel_avatar_url": thumbnail or "",
                            "online_subscription_id": str(value.get("id") or ""),
                        },
                    )
                )
                continue
            video_id = (
                (value.get("id") if isinstance(value.get("id"), str) and feed == "liked" else None)
                or resource.get("videoId")
                or details.get("upload", {}).get("videoId")
            )
            if not video_id:
                continue
            items.append(
                self.youtube._item(
                    {
                        "id": video_id,
                        "title": snippet.get("title"),
                        "channel": snippet.get("channelTitle"),
                        "channel_id": snippet.get("channelId"),
                        "thumbnails": list((snippet.get("thumbnails") or {}).values()),
                    }
                )
            )
        return items

    def _account_feed_loaded(self, items: list[MediaItem]) -> bool:
        if items:
            self.account_grid.set_items(items)
        else:
            self.account_grid.set_status(
                "edit-find-symbolic",
                "Nothing available",
                "The account API returned no playable videos.",
            )
        return GLib.SOURCE_REMOVE

    def _account_playlists(self) -> list[dict[str, Any]]:
        assert self.active_oauth_account
        return self.youtube.api_playlists(self.oauth.access_token(self.active_oauth_account))

    def _account_playlists_loaded(self, values: list[dict[str, Any]]) -> bool:
        self._clear_box(self.account_playlist_box)
        for value in values:
            playlist_id = str(value.get("id") or "")
            snippet = value.get("snippet") or {}
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            name = Gtk.Entry(text=str(snippet.get("title") or "Playlist"), hexpand=True)
            name.connect(
                "activate",
                lambda entry, pid=playlist_id: self._update_account_playlist(pid, entry.get_text()),
            )
            row.append(name)
            browse = Gtk.Button(icon_name="folder-open-symbolic", tooltip_text="Browse")
            browse.connect(
                "clicked",
                lambda _button, pid=playlist_id: self._browse_account_playlist(pid),
            )
            row.append(browse)
            delete = Gtk.Button(icon_name="edit-delete-symbolic", tooltip_text="Delete")
            delete.connect(
                "clicked",
                lambda _button, pid=playlist_id: self._delete_account_playlist(pid),
            )
            row.append(delete)
            self.account_playlist_box.append(row)
        items = [
            MediaItem(
                id=str(value.get("id") or ""),
                title=str((value.get("snippet") or {}).get("title") or "Playlist"),
                subtitle=f"{(value.get('contentDetails') or {}).get('itemCount', 0)} videos",
                source="youtube-playlist",
                playable=False,
                payload={
                    "webpage_url": f"https://www.youtube.com/playlist?list={value.get('id', '')}"
                },
            )
            for value in values
        ]
        if items:
            self.account_grid.set_items(items)
        else:
            self.account_grid.set_status(
                "view-list-symbolic",
                "No account playlists",
                "Create one from the YouTube account API.",
            )
        return GLib.SOURCE_REMOVE

    def _account_manage_token(self) -> str:
        if not self.active_oauth_account or MANAGE_SCOPE not in self.active_oauth_account.scopes:
            raise PermissionError("Sign in with playlist access first.")
        return self.oauth.access_token(self.active_oauth_account)

    def _create_account_playlist(self) -> None:
        title = self.account_playlist_name.get_text().strip()
        if not title:
            return
        run_async(
            lambda: self.youtube.api_create_playlist(self._account_manage_token(), title),
            lambda _result: self._account_playlist_changed("Playlist created"),
            lambda error: self.toast_overlay.add_toast(Adw.Toast(title=str(error))),
        )

    def _update_account_playlist(self, playlist_id: str, title: str) -> None:
        run_async(
            lambda: self.youtube.api_update_playlist(
                self._account_manage_token(), playlist_id, title
            ),
            lambda _result: self._account_playlist_changed("Playlist updated"),
            lambda error: self.toast_overlay.add_toast(Adw.Toast(title=str(error))),
        )

    def _delete_account_playlist(self, playlist_id: str) -> None:
        run_async(
            lambda: self.youtube.api_delete_playlist(self._account_manage_token(), playlist_id),
            lambda _result: self._account_playlist_changed("Playlist deleted"),
            lambda error: self.toast_overlay.add_toast(Adw.Toast(title=str(error))),
        )

    def _account_playlist_changed(self, title: str) -> bool:
        self.account_playlist_name.set_text("")
        self.toast_overlay.add_toast(Adw.Toast(title=title))
        self._load_account_feed("playlists")
        return GLib.SOURCE_REMOVE

    def _browse_account_playlist(self, playlist_id: str) -> None:
        self.youtube_playlist_url.set_text(f"https://www.youtube.com/playlist?list={playlist_id}")
        self._browse_youtube_playlist()

    def _load_jellyfin_home(self) -> None:
        if not self.jellyfin.session or self._synctube_active():
            return
        self.jellyfin_history.clear()
        self.jellyfin_parent_id = ""
        self.jellyfin_back.set_sensitive(True)
        self.jellyfin_search.set_text("")

        self._load_jellyfin_current()

    def _load_jellyfin_current(self) -> None:
        if not self.jellyfin.session or self._synctube_active():
            return
        cache_key = (
            f"jellyfin:{self.jellyfin.session.user_id}:parent:"
            f"{self.jellyfin_parent_id or 'root'}"
        )
        if cached := self._browse_cache_get(cache_key):
            self._jellyfin_results(cached)
            return
        self.jellyfin_grid.set_loading("Loading your library…")
        operation = (
            self.jellyfin.get_libraries
            if not self.jellyfin_parent_id
            else lambda: self.jellyfin.get_items(self.jellyfin_parent_id)
        )

        run_async(
            operation,
            lambda items: self._jellyfin_results(items, cache_key),
            lambda error: self._grid_error(self.jellyfin_grid, error),
        )

    def _jellyfin_search_requested(self, entry: Gtk.SearchEntry) -> None:
        if self._synctube_active() or not self.jellyfin.session:
            return
        query = entry.get_text().strip()
        if not query:
            self._load_jellyfin_current()
            return
        cache_key = (
            f"jellyfin:{self.jellyfin.session.user_id}:search:"
            f"{self.jellyfin_parent_id}:{query.casefold()}"
        )
        if cached := self._browse_cache_get(cache_key):
            self._jellyfin_results(cached)
            return
        self.jellyfin_grid.set_loading(f"Searching for “{query}”…")
        run_async(
            lambda: self.jellyfin.get_items(self.jellyfin_parent_id, query),
            lambda items: self._jellyfin_results(items, cache_key),
            lambda error: self._grid_error(self.jellyfin_grid, error),
        )

    def _jellyfin_results(
        self, items: list[MediaItem], cache_key: str = ""
    ) -> bool:
        if self._synctube_active():
            return GLib.SOURCE_REMOVE
        if cache_key:
            self._browse_cache_put(cache_key, items)
        self.browse_channel_alphabet_scroller.set_visible(False)
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
        if item.source == "jellyfin" and self._synctube_active():
            self.toast_overlay.add_toast(
                Adw.Toast(title="Disconnect from SyncTube to use Jellyfin")
            )
            return
        if item.source.startswith("youtube") and self._jellyfin_syncplay_active():
            self.toast_overlay.add_toast(
                Adw.Toast(title="Leave the Jellyfin watch party to use YouTube")
            )
            return
        if item.source == "youtube-channel":
            self.channel_url = str(item.payload.get("channel_url") or "")
            self._load_channel(1)
            return
        if item.source == "youtube-playlist":
            self.youtube_playlist_url.set_text(str(item.payload.get("webpage_url") or ""))
            self._browse_youtube_playlist()
            return
        if item.source == "jellyfin" and item.kind == "Series":
            self._show_details(item)
            return
        if item.source == "jellyfin" and not item.playable:
            self.jellyfin_grid.set_loading(f"Opening {item.title}…")
            self.jellyfin_history.append(
                (self.jellyfin_parent_id, self.browse_category_title.get_label())
            )
            self.jellyfin_parent_id = item.id
            self.browse_category_title.set_label(item.title)
            self.jellyfin_back.set_label("Back")
            self.jellyfin_back.set_sensitive(True)
            self.jellyfin_search.set_text("")
            assert self.jellyfin.session
            cache_key = (
                f"jellyfin:{self.jellyfin.session.user_id}:parent:{item.id}"
            )
            if cached := self._browse_cache_get(cache_key):
                self._jellyfin_results(cached)
                return
            run_async(
                lambda: self.jellyfin.get_items(item.id),
                lambda items: self._jellyfin_results(items, cache_key),
                lambda error: self._grid_error(self.jellyfin_grid, error),
            )
            return

        if item.source == "offline":
            if item.playable:
                self._play_selected(self._history_item(item))
            return

        if item.source == "youtube":
            self._play_selected(item)
        else:
            self._show_details(item)

    def _show_details(self, item: MediaItem) -> None:
        if item.source == "jellyfin" and self._synctube_active():
            return
        self.detail_item = item
        self.detail_series_play_item = None
        self.detail_series_episodes = []
        self.detail_series_generation += 1
        series_generation = self.detail_series_generation
        self.details_play.set_child(icon_label("Play", "media-playback-start-symbolic"))
        self._clear_box(self.details_seasons)
        is_series = item.source == "jellyfin" and item.kind == "Series"
        self.details_seasons.set_visible(is_series)
        self.details_series_loading.set_visible(is_series)
        if is_series:
            self.details_series_spinner.start()
        self.details_title.set_label(item.title)
        payload = item.payload
        meta = [
            str(value) for value in (payload.get("ProductionYear"), item.duration_label) if value
        ]
        genres = payload.get("Genres") or []
        if genres:
            meta.append(" · ".join(str(genre) for genre in genres[:3]))
        self.details_meta.set_label("  •  ".join(meta) or item.subtitle)
        self.details_overview.set_label(payload.get("Overview") or "No description available.")
        self.details_channel.set_visible(False)
        self.details_download.set_visible(item.source == "jellyfin" and item.playable)
        self.details_download_quality.set_visible(False)
        self.comments_more.set_visible(False)
        self.details_playlist.set_visible(item.playable and not is_series)
        self.details_queue.set_visible(item.playable and not is_series)
        self.details_watch_later.set_visible(item.playable and not is_series)
        self.details_play.set_sensitive(item.playable and not is_series)
        self.comment_cursor = None
        self.details_picture.set_paintable(None)
        if item.thumbnail_url:
            item_id = item.id
            self.thumbnails.load(
                item.thumbnail_url,
                lambda path: self._set_details_picture(item_id, path),
            )
        self._set_visible_page("details")
        self.window_title.set_title(item.title)
        self.window_title.set_subtitle(f"{item.source.capitalize()} details")
        self.mini_player.set_visible(bool(self.current_item or self.queue))
        if is_series:
            run_async(
                lambda: self.jellyfin.get_series_view(item.id),
                lambda result: self._jellyfin_series_loaded(
                    series_generation, result
                ),
                lambda error: self._jellyfin_series_error(
                    series_generation, error
                ),
            )

    def _jellyfin_series_loaded(
        self,
        generation: int,
        result: tuple[MediaItem, list[MediaSection], MediaItem | None],
    ) -> bool:
        series, sections, resume = result
        if (
            generation != self.detail_series_generation
            or not self.detail_item
            or self.detail_item.id != series.id
        ):
            return GLib.SOURCE_REMOVE
        self.detail_item = series
        self.detail_series_play_item = resume
        self.detail_series_episodes = [
            episode
            for section in sections
            for episode in section.items
            if episode.playable
        ]
        self.details_play.set_sensitive(resume is not None)
        if resume and (resume.payload.get("UserData") or {}).get("LastPlayedDate"):
            self.details_play.set_child(
                icon_label("Resume", "media-playback-start-symbolic")
            )
        self.details_series_spinner.stop()
        self.details_series_loading.set_visible(False)
        self.details_seasons.set_visible(True)
        seasons_group = Adw.PreferencesGroup()
        for index, section in enumerate(sections):
            episode_count = len(section.items)
            expander = Adw.ExpanderRow(
                title=section.title,
                subtitle=f"{episode_count} episode{'s' if episode_count != 1 else ''}",
            )
            expander.set_expanded(index == 0)
            for episode in section.items:
                expander.add_row(self._series_episode_row(episode))
            seasons_group.add(expander)
        if sections:
            self.details_seasons.append(seasons_group)
        if not sections:
            empty = Adw.StatusPage(
                icon_name="folder-videos-symbolic",
                title="No episodes available",
                description="This series does not expose any playable seasons.",
            )
            self.details_seasons.append(empty)
        return GLib.SOURCE_REMOVE

    def _series_episode_row(self, episode: MediaItem) -> Adw.ActionRow:
        season = int(episode.payload.get("ParentIndexNumber") or 0)
        number = int(episode.payload.get("IndexNumber") or 0)
        position = (episode.payload.get("UserData") or {}).get("PlaybackPositionTicks") or 0
        progress = (
            f" · Resume at {self._time_label(float(position) / 10_000_000)}"
            if position
            else ""
        )
        episode_number = f"S{season:02d}E{number:02d}"
        row = Adw.ActionRow(
            title=episode.title,
            subtitle=(
                f"{episode_number} · {episode.duration_label or 'Episode'}{progress}"
            ),
        )
        row.set_margin_top(8)
        row.set_margin_bottom(8)
        picture = Gtk.Picture(width_request=128, height_request=72)
        picture.set_content_fit(Gtk.ContentFit.COVER)
        picture.set_overflow(Gtk.Overflow.HIDDEN)
        picture.add_css_class("episode-thumbnail")
        row.add_prefix(picture)
        if episode.thumbnail_url:
            self.thumbnails.load(
                episode.thumbnail_url,
                lambda path, target=picture: self._set_details_result_picture(target, path),
            )
        play = Gtk.Button(
            icon_name="media-playback-start-symbolic",
            tooltip_text=f"Play {episode.title}",
            valign=Gtk.Align.CENTER,
        )
        play.add_css_class("square-button")
        play.connect(
            "clicked", lambda *_args, value=episode: self._play_series_episode(value)
        )
        row.add_suffix(play)
        download = Gtk.Button(
            icon_name="folder-download-symbolic",
            tooltip_text=f"Download {episode.title}",
            valign=Gtk.Align.CENTER,
        )
        download.add_css_class("square-button")
        download.connect("clicked", lambda *_args, value=episode: self._download_item(value))
        row.add_suffix(download)
        return row

    def _jellyfin_series_error(self, generation: int, error: Exception) -> bool:
        if generation != self.detail_series_generation:
            return GLib.SOURCE_REMOVE
        self.details_series_spinner.stop()
        self.details_series_loading.set_visible(False)
        self.details_play.set_sensitive(False)
        self.details_seasons.append(
            Adw.StatusPage(
                icon_name="dialog-error-symbolic",
                title="Could not load episodes",
                description=str(error),
            )
        )
        return GLib.SOURCE_REMOVE

    def _youtube_details_loaded(self, details: VideoDetails) -> bool:
        if not self.detail_item or details.item.id != self.detail_item.id:
            return GLib.SOURCE_REMOVE
        self.detail_item = details.item
        meta: list[str] = []
        if details.upload_date:
            date = details.upload_date
            meta.append(f"{date[:4]}-{date[4:6]}-{date[6:8]}" if len(date) == 8 else date)
        if details.view_count is not None:
            meta.append(f"{details.view_count:,} views")
        if details.like_count is not None:
            meta.append(f"{details.like_count:,} likes")
        if details.item.duration_label:
            meta.append(details.item.duration_label)
        self.details_meta.set_label("  •  ".join(meta) or details.item.subtitle)
        self.details_overview.set_label(
            details.availability_message or details.description or "No description available."
        )
        available = details.availability == Availability.PUBLIC
        self.details_play.set_sensitive(available)
        self.details_download.set_sensitive(available)
        self.details_channel.set_visible(bool(details.item.payload.get("channel_url")))
        return GLib.SOURCE_REMOVE

    def _details_error(self, error: Exception) -> bool:
        self.details_overview.set_label(str(error))
        self.details_play.set_sensitive(False)
        self.details_download.set_sensitive(False)
        return GLib.SOURCE_REMOVE

    def _set_details_picture(self, item_id: str, path: object) -> bool:
        if self.detail_item and self.detail_item.id == item_id and path:
            self.details_picture.set_filename(str(path))
        return GLib.SOURCE_REMOVE

    def _play_detail_item(self) -> None:
        if self.detail_series_play_item:
            self._play_series_episode(self.detail_series_play_item)
        elif self.detail_item:
            self._play_selected(self.detail_item)

    def _play_series_episode(self, episode: MediaItem) -> None:
        if episode.source == "jellyfin" and self._synctube_active():
            return
        index = next(
            (
                position
                for position, item in enumerate(self.detail_series_episodes)
                if item.id == episode.id
            ),
            -1,
        )
        if index < 0:
            self._play_selected(episode)
            return
        self.queue = list(self.detail_series_episodes)
        self.queue_index = index
        self._refresh_queue()
        if index + 1 < len(self.queue):
            self._prebuffer_item(self.queue[index + 1])
        if self.current_item and self.current_item.id == episode.id:
            self._show_player()
        else:
            self._begin_playback(episode)

    def _queue_detail_item(self) -> None:
        if self.detail_item:
            self._add_to_queue(self.detail_item)

    def _open_detail_channel(self) -> None:
        if not self.detail_item:
            return
        url = str(self.detail_item.payload.get("channel_url") or "")
        if not url:
            return
        self.channel_url = url
        self._load_channel(1)

    def _open_current_channel(self) -> None:
        if not self.current_item:
            return
        url = str(self.current_item.payload.get("channel_url") or "")
        if not url:
            return
        self.channel_url = url
        self._load_channel(1)

    def _load_channel(self, page: int) -> None:
        if not self.channel_url or self.channel_loading:
            return
        if page == 1 and (cached := self.channel_cache.get(self.channel_url)):
            created, channel = cached
            if time.monotonic() - created <= 10 * 60:
                self._set_visible_page("channel")
                self._channel_loaded(channel, 1, cache_result=False)
                return
            self.channel_cache.pop(self.channel_url, None)
        self.channel_loading = True
        if page == 1:
            self.channel_share.set_sensitive(False)
            self.channel_grid.set_loading("Loading channel…")
        else:
            self.channel_more_spinner.set_visible(True)
            self.channel_more_spinner.start()
        self._set_visible_page("channel")
        run_async(
            lambda: self.youtube.channel(self.channel_url, page=page),
            lambda channel: self._channel_loaded(channel, page),
            lambda error: self._channel_error(error, page),
        )

    def _channel_loaded(
        self,
        channel: ChannelDetails,
        page: int = 1,
        *,
        cache_result: bool = True,
    ) -> bool:
        self.channel_loading = False
        self.channel_more_spinner.stop()
        self.channel_more_spinner.set_visible(False)
        self.current_channel = channel
        self.channel_share.set_sensitive(bool(channel.url))
        if page == 1 and self.channel_url and cache_result:
            self.channel_cache[self.channel_url] = (time.monotonic(), channel)
        self.channel_page = page
        self.channel_has_more = channel.continuation is not None
        self.channel_heading.set_label(channel.title)
        self.channel_subscriber_count.set_label(
            self._subscriber_count_label(channel.subscriber_count)
        )
        self.channel_subscriber_count.set_visible(channel.subscriber_count is not None)
        self.channel_avatar.set_visible(False)
        if channel.avatar_url:
            self.thumbnails.load(
                channel.avatar_url,
                lambda path: self._set_channel_avatar(channel.id, path),
            )
        self._refresh_channel_subscription_controls()
        if page == 1:
            self.channel_grid.set_items(channel.videos)
        else:
            self.channel_grid.append_items(channel.videos)
        self.window_title.set_title(channel.title)
        self.window_title.set_subtitle("YouTube channel")
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _subscriber_count_label(count: int | None) -> str:
        if count is None:
            return ""
        if count < 10_000:
            value = f"{count:,}"
        elif count < 1_000_000:
            value = f"{count / 1_000:.2f}".rstrip("0").rstrip(".") + "K"
        else:
            value = f"{count / 1_000_000:.2f}".rstrip("0").rstrip(".") + "M"
        return f"{value} subscribers"

    def _channel_error(self, error: Exception, page: int) -> bool:
        self.channel_loading = False
        self.channel_more_spinner.stop()
        self.channel_more_spinner.set_visible(False)
        if page == 1:
            self._grid_error(self.channel_grid, error)
        else:
            self.toast_overlay.add_toast(Adw.Toast(title=str(error)))
        return GLib.SOURCE_REMOVE

    def _channel_scroll_changed(self, adjustment: Gtk.Adjustment) -> None:
        if (
            self.channel_has_more
            and not self.channel_loading
            and adjustment.get_value() + adjustment.get_page_size()
            >= adjustment.get_upper() - 320
        ):
            self._load_channel(self.channel_page + 1)

    def _set_channel_avatar(self, channel_id: str, path: object) -> bool:
        if self.current_channel and self.current_channel.id == channel_id and path:
            self.channel_avatar.set_filename(str(path))
            self.channel_avatar.set_visible(True)
        return GLib.SOURCE_REMOVE

    def _refresh_channel_subscription_controls(self) -> None:
        if not self.current_channel:
            return
        subscription = self.subscriptions.get(self.current_channel.id)
        self.syncing_subscription_controls = True
        self._set_subscribe_button_state(
            self.channel_subscribe, subscription is not None
        )
        self.channel_notifications.set_sensitive(subscription is not None)
        self.channel_notifications.set_active(bool(subscription and subscription.notifications))
        self.syncing_subscription_controls = False

    def _channel_subscription_clicked(self, _button: Gtk.Button) -> None:
        if self.syncing_subscription_controls or not self.current_channel:
            return
        channel = self.current_channel
        if not self.subscriptions.get(channel.id):
            latest = channel.videos[0].id if channel.videos else ""
            self.subscriptions.subscribe(
                ChannelSubscription(
                    channel.id,
                    channel.title,
                    channel.url,
                    channel.avatar_url or "",
                    True,
                    latest,
                )
            )
            self.toast_overlay.add_toast(Adw.Toast(title=f"Subscribed to {channel.title}"))
        else:
            self.subscriptions.unsubscribe(channel.id)
            self.toast_overlay.add_toast(Adw.Toast(title=f"Unsubscribed from {channel.title}"))
        self._invalidate_subscription_browse_cache()
        self._refresh_channel_subscription_controls()

    def _channel_notifications_toggled(self, button: Gtk.ToggleButton) -> None:
        if self.syncing_subscription_controls or not self.current_channel:
            return
        try:
            self.subscriptions.set_notifications(self.current_channel.id, button.get_active())
        except KeyError:
            return
        state = "on" if button.get_active() else "off"
        self.toast_overlay.add_toast(Adw.Toast(title=f"Channel notifications {state}"))

    def _share_channel(self) -> None:
        channel = self.current_channel
        display = Gdk.Display.get_default()
        if not channel or not channel.url or not display:
            self.toast_overlay.add_toast(
                Adw.Toast(title="This channel does not have a shareable link")
            )
            return
        display.get_clipboard().set(channel.url)
        self.toast_overlay.add_toast(Adw.Toast(title="Channel link copied"))

    def _toggle_current_subscription(self) -> None:
        if not self.current_item:
            return
        channel_id = str(self.current_item.payload.get("channel_id") or "")
        channel_url = str(self.current_item.payload.get("channel_url") or "")
        if not channel_id or not channel_url:
            self._open_current_channel()
            return
        existing = self.subscriptions.get(channel_id)
        if existing:
            self.subscriptions.unsubscribe(channel_id)
            self.toast_overlay.add_toast(
                Adw.Toast(title=f"Unsubscribed from {self.current_item.subtitle}")
            )
        else:
            self.subscriptions.subscribe(
                ChannelSubscription(
                    channel_id,
                    self.current_item.subtitle or "YouTube channel",
                    channel_url,
                    str(self.current_item.payload.get("channel_avatar_url") or ""),
                    True,
                    self.current_item.id,
                )
            )
            self.toast_overlay.add_toast(
                Adw.Toast(title=f"Subscribed to {self.current_item.subtitle}")
            )
        self._invalidate_subscription_browse_cache()
        self._refresh_player_subscription()

    def _refresh_player_subscription(self) -> None:
        if not self.current_item:
            return
        channel_id = str(self.current_item.payload.get("channel_id") or "")
        subscribed = bool(channel_id and self.subscriptions.get(channel_id))
        self._set_subscribe_button_state(self.player_subscribe, subscribed)

    @staticmethod
    def _set_subscribe_button_state(
        button: Gtk.Button, subscribed: bool
    ) -> None:
        button.set_icon_name(
            "object-select-symbolic" if subscribed else "list-add-symbolic"
        )
        button.set_tooltip_text("Unsubscribe" if subscribed else "Subscribe")
        if subscribed:
            button.remove_css_class("suggested-action")
        else:
            button.add_css_class("suggested-action")

    def _load_comments(self) -> None:
        if (
            self.comments_loading
            or not self.current_item
            or self.current_item.source != "youtube"
        ):
            return
        item = self.current_item
        self.comments_loading = True
        self.comments_more.set_sensitive(False)
        self.comments_more.set_label("Loading…")
        self.comments_loading_label.set_label(
            "Loading comments…" if self.comment_cursor is None else "Loading more comments…"
        )
        self.comments_loading_row.set_visible(True)
        self.comments_spinner.start()
        run_async(
            lambda: self.youtube.comments(item, cursor=self.comment_cursor),
            lambda page: self._comments_loaded_for(item.id, page),
            self._comments_error,
        )

    def _comments_loaded_for(self, item_id: str, page: CommentPage) -> bool:
        if not self.current_item or self.current_item.id != item_id:
            return GLib.SOURCE_REMOVE
        return self._comments_loaded(page)

    def _toggle_player_comments(self, button: Gtk.ToggleButton) -> None:
        visible = button.get_active() and bool(
            self.current_item and self.current_item.source == "youtube"
        )
        for toggle in (
            self.player_comments_button,
            self.fullscreen_comments_button,
        ):
            if toggle is not button and toggle.get_active() != visible:
                toggle.set_active(visible)
        if visible and self.player_live_chat_button.get_active():
            self.player_live_chat_button.set_active(False)
        self.player_comments_panel.set_visible(visible)
        self._update_player_sidebar_layout()
        self._refresh_comment_composer()
        if visible and not self.comments_box.get_first_child():
            self._load_comments()

    def _refresh_comment_composer(self) -> None:
        available = self._commenting_available()
        self.comment_composer.set_visible(True)
        self.comment_entry.set_editable(available)
        self.comment_entry.set_cursor_visible(available)
        if available:
            placeholder = "Add a comment…"
        elif self.youtube_browser_checking:
            placeholder = "Checking your YouTube sign-in…"
        else:
            placeholder = "Sign in to YouTube or connect an API account to post comments"
        self.comment_placeholder.set_label(placeholder)
        self._comment_text_changed()

    def _commenting_available(self) -> bool:
        return bool(
            not self.comment_posting
            and (
                self.youtube_browser_session
                or (
                    self.active_oauth_account
                    and COMMENT_SCOPE in self.active_oauth_account.scopes
                )
            )
        )

    def _comment_text(self) -> str:
        buffer = self.comment_entry.get_buffer()
        start, end = buffer.get_bounds()
        return buffer.get_text(start, end, True)

    def _comment_entry_key_pressed(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        state: Gdk.ModifierType,
    ) -> bool:
        if (
            keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter)
            and state & Gdk.ModifierType.CONTROL_MASK
        ):
            self._post_comment()
            return True
        return False

    def _comment_text_changed(self) -> None:
        text = self._comment_text()
        columns = max(18, (self.player_sidebar_width - 72) // 8)
        visual_lines = sum(
            max(1, (len(line) + columns - 1) // columns)
            for line in text.split("\n")
        )
        self.comment_editor.set_min_content_height(
            max(44, min(128, 24 + visual_lines * 20))
        )
        available = self._commenting_available()
        self.comment_placeholder.set_visible(not text)
        self.comment_send.set_sensitive(available and bool(text.strip()))

    def _post_comment(self) -> None:
        text = self._comment_text().strip()
        item = self.current_item
        account = self.active_oauth_account
        browser_session = self.youtube_browser_session
        if not text or not item or item.source != "youtube":
            return
        if not browser_session and (
            not account or COMMENT_SCOPE not in account.scopes
        ):
            self.toast_overlay.add_toast(
                Adw.Toast(title="Sign in to YouTube before posting a comment")
            )
            return
        self.comment_posting = True
        self.comment_entry.set_editable(False)
        self.comment_send.set_sensitive(False)
        if browser_session:
            operation = lambda: self.youtube.browser_create_comment(
                item,
                text,
                browser_session.display_name,
            )
        else:
            operation = lambda: self.youtube.api_create_comment(
                self.oauth.access_token(account),
                item.id,
                str(item.payload.get("channel_id") or ""),
                text,
            )
        run_async(
            operation,
            lambda comment: self._comment_posted(item.id, comment),
            self._comment_post_error,
        )

    def _comment_posted(self, item_id: str, comment: Comment) -> bool:
        self.comment_posting = False
        self._refresh_comment_composer()
        if not self.current_item or self.current_item.id != item_id:
            return GLib.SOURCE_REMOVE
        self.comment_entry.get_buffer().set_text("")
        child = self.comments_box.get_first_child()
        while child:
            following = child.get_next_sibling()
            if child.has_css_class("comments-empty"):
                self.comments_box.remove(child)
            child = following
        self.comments_box.prepend(self._comment_card(comment))
        self.toast_overlay.add_toast(Adw.Toast(title="Comment posted"))
        return GLib.SOURCE_REMOVE

    def _comment_post_error(self, error: Exception) -> bool:
        self.comment_posting = False
        self._refresh_comment_composer()
        self.toast_overlay.add_toast(Adw.Toast(title=str(error)))
        return GLib.SOURCE_REMOVE

    def _toggle_player_live_chat(self, button: Gtk.ToggleButton) -> None:
        visible = button.get_active() and self._synctube_active()
        for toggle in (
            self.player_live_chat_button,
            self.fullscreen_live_chat_button,
        ):
            if toggle is not button and toggle.get_active() != visible:
                toggle.set_active(visible)
        if visible and self.player_comments_button.get_active():
            self.player_comments_button.set_active(False)
        self.player_live_chat_panel.set_visible(visible)
        self._update_player_sidebar_layout()
        self._refresh_live_chat_controls()
        if visible:
            GLib.idle_add(self._scroll_live_chat_to_bottom)

    def _refresh_live_chat_controls(self) -> None:
        connected = self._synctube_active()
        allowed = bool(
            connected
            and self.sync_client
            and self.sync_client.has_permission("chat")
        )
        self.live_chat_entry.set_sensitive(allowed)
        self.live_chat_send.set_sensitive(allowed)
        if not connected:
            status = "Join a SyncTube room to use live chat."
        elif not allowed:
            status = "This room does not allow you to send chat messages."
        else:
            status = "Messages are shared with everyone in the SyncTube room."
        self.live_chat_status.set_label(status)

    def _send_live_chat(self) -> None:
        message = self.live_chat_entry.get_text().strip()
        if not message or not self.sync_client:
            return
        try:
            self.sync_client.send_chat(message)
        except (ConnectionError, PermissionError, OSError) as error:
            self.toast_overlay.add_toast(Adw.Toast(title=str(error)))
            self._refresh_live_chat_controls()
            return
        self.live_chat_entry.set_text("")

    def _replace_live_chat(self, values: object) -> None:
        self._clear_box(self.live_chat_box)
        self.live_chat_rows.clear()
        messages = values if isinstance(values, list) else []
        for message in messages:
            self._append_live_chat_message(message, scroll=False)
        GLib.idle_add(self._scroll_live_chat_to_bottom)

    def _append_live_chat_message(self, value: object, *, scroll: bool = True) -> None:
        if not isinstance(value, dict):
            return
        author_text = str(value.get("author") or "Anonymous")
        message_text = str(value.get("text") or "").strip()
        if not message_text:
            return
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        card.add_css_class("live-chat-message")
        author = Gtk.Label(label=author_text, xalign=0)
        author.set_ellipsize(Pango.EllipsizeMode.END)
        author.set_max_width_chars(1)
        author.set_hexpand(True)
        author.add_css_class("heading")
        color = str(value.get("color") or "")
        rgba = Gdk.RGBA()
        if color and rgba.parse(color):
            author.set_markup(
                f'<span foreground="{color}">{GLib.markup_escape_text(author_text)}</span>'
            )
        card.append(author)
        text = Gtk.Label(label=message_text, xalign=0, wrap=True)
        text.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        text.set_max_width_chars(1)
        card.append(text)
        self.live_chat_box.append(card)
        message_id = str(value.get("id") or "")
        if message_id:
            self.live_chat_rows[message_id] = card
        if scroll:
            GLib.idle_add(self._scroll_live_chat_to_bottom)

    def _remove_live_chat_message(self, message_id: object) -> None:
        row = self.live_chat_rows.pop(str(message_id or ""), None)
        if row:
            self.live_chat_box.remove(row)

    def _scroll_live_chat_to_bottom(self) -> bool:
        adjustment = self.live_chat_scroller.get_vadjustment()
        adjustment.set_value(
            max(
                adjustment.get_lower(),
                adjustment.get_upper() - adjustment.get_page_size(),
            )
        )
        return GLib.SOURCE_REMOVE

    def _comments_scroll_changed(self, adjustment: Gtk.Adjustment) -> None:
        if (
            self.player_comments_panel.get_visible()
            and self.comment_cursor is not None
            and adjustment.get_value() + adjustment.get_page_size()
            >= adjustment.get_upper() - 240
        ):
            self._load_comments()

    @staticmethod
    def _toggle_comment_replies(
        _button: Gtk.Button,
        replies: Gtk.Box,
        indicator: Gtk.Label,
        disclosure: Gtk.Image,
        comment: Comment,
    ) -> None:
        visible = not replies.get_visible()
        replies.set_visible(visible)
        reply_count = len(comment.replies)
        indicator.set_label(
            "Hide replies"
            if visible
            else (
                f"View {reply_count} {'reply' if reply_count == 1 else 'replies'}"
                if reply_count
                else "View replies"
            )
        )
        disclosure.set_from_icon_name(
            "pan-down-symbolic" if visible else "go-next-symbolic"
        )

    def _comments_loaded(self, page: CommentPage) -> bool:
        self.comments_loading = False
        self.comments_spinner.stop()
        self.comments_loading_row.set_visible(False)
        for comment in page.comments:
            self._mark_own_comment(comment)
            self.comments_box.append(self._comment_card(comment))
        if not page.comments and not self.comments_box.get_first_child():
            empty = Gtk.Label(label="No comments yet.", wrap=True)
            empty.add_css_class("dim-label")
            empty.add_css_class("comments-empty")
            self.comments_box.append(empty)
        self.comment_cursor = page.next_cursor
        self.comments_more.set_sensitive(page.next_cursor is not None)
        self.comments_more.set_label("Load more" if page.next_cursor else "All comments loaded")
        self.comments_more.set_visible(page.next_cursor is not None)
        return GLib.SOURCE_REMOVE

    def _mark_own_comment(self, comment: Comment) -> None:
        browser = self.youtube_browser_session
        account = self.active_oauth_account
        comment.is_own = bool(
            comment.is_own
            or (
                browser
                and (
                    comment.author_id == browser.channel_id
                    or comment.author == browser.display_name
                )
            )
            or (account and comment.author == account.display_name)
        )
        for reply in comment.replies:
            self._mark_own_comment(reply)

    @staticmethod
    def _empty_replies_label() -> Gtk.Label:
        empty = Gtk.Label(
            label="No replies to this comment yet.", xalign=0
        )
        empty.add_css_class("dim-label")
        empty.add_css_class("comments-empty")
        return empty

    def _comment_card(self, comment: Comment) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        card.add_css_class("comment-card")
        card.set_overflow(Gtk.Overflow.HIDDEN)
        summary = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        summary.add_css_class("comment-summary")
        author = Gtk.Label(label=comment.author, xalign=0)
        author.add_css_class("heading")
        summary.append(author)
        text = Gtk.Label(
            label=comment.text,
            xalign=0,
            wrap=True,
        )
        summary.append(text)
        card.append(summary)

        reply_count = len(comment.replies)
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        actions.add_css_class("comment-actions")
        like = Gtk.Button(
            label=f"{comment.like_count:,}",
            icon_name="emblem-favorite-symbolic",
            tooltip_text="Like this comment",
        )
        like.add_css_class("flat")
        like.connect("clicked", self._like_comment, comment)
        actions.append(like)
        reply = Gtk.Button(
            icon_name="mail-reply-sender-symbolic",
            tooltip_text="Reply to this comment",
        )
        reply.add_css_class("flat")
        actions.append(reply)
        if comment.is_own:
            delete = Gtk.Button(
                icon_name="edit-delete-symbolic",
                tooltip_text="Delete your comment",
            )
            delete.add_css_class("flat")
            delete.add_css_class("destructive-action")
            delete.connect(
                "clicked",
                self._delete_comment,
                comment,
                self.comments_box,
                card,
                None,
                None,
                None,
            )
            actions.append(delete)
        actions.append(Gtk.Box(hexpand=True))
        replies_toggle_content = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=5
        )
        indicator = Gtk.Label(
            label=(
                f"View {reply_count} {'reply' if reply_count == 1 else 'replies'}"
                if reply_count
                else "View replies"
            ),
            xalign=0,
        )
        indicator.add_css_class("caption")
        indicator.add_css_class("comment-replies-control")
        replies_toggle_content.append(indicator)
        disclosure = Gtk.Image.new_from_icon_name("go-next-symbolic")
        disclosure.add_css_class("comment-replies-control")
        replies_toggle_content.append(disclosure)
        replies_toggle = Gtk.Button(child=replies_toggle_content)
        replies_toggle.add_css_class("flat")
        replies = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        replies_toggle.connect(
            "clicked",
            self._toggle_comment_replies,
            replies,
            indicator,
            disclosure,
            comment,
        )
        actions.append(replies_toggle)
        card.append(actions)
        for reply in comment.replies:
            replies.append(
                self._comment_reply_row(
                    reply,
                    comment,
                    replies,
                    indicator,
                    disclosure,
                )
            )
        if not comment.replies:
            replies.append(self._empty_replies_label())
        replies.set_visible(False)
        replies.set_margin_start(16)
        replies.set_margin_end(16)
        replies.set_margin_bottom(10)
        card.append(replies)

        reply_composer = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=6
        )
        reply_composer.add_css_class("comment-reply-composer")
        reply_entry = Gtk.Entry(
            placeholder_text=f"Reply to {comment.author}…",
            hexpand=True,
        )
        reply_composer.append(reply_entry)
        post_reply = Gtk.Button(
            label="Post reply",
            icon_name="paper-plane-symbolic",
        )
        reply_composer.append(post_reply)
        reply.connect(
            "clicked",
            self._toggle_reply_composer,
            reply_composer,
            reply_entry,
        )
        post_reply.connect(
            "clicked",
            self._post_reply,
            comment,
            reply_entry,
            post_reply,
            replies,
            indicator,
            disclosure,
        )
        reply_keys = Gtk.EventControllerKey()
        reply_keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        reply_keys.connect(
            "key-pressed",
            self._reply_entry_key_pressed,
            comment,
            reply_entry,
            post_reply,
            replies,
            indicator,
            disclosure,
        )
        reply_entry.add_controller(reply_keys)
        reply_entry.connect(
            "activate",
            self._post_reply,
            comment,
            reply_entry,
            post_reply,
            replies,
            indicator,
            disclosure,
        )
        reply_composer.set_visible(False)
        card.append(reply_composer)
        return card

    def _comment_reply_row(
        self,
        reply: Comment,
        parent: Comment,
        replies: Gtk.Box,
        indicator: Gtk.Label,
        disclosure: Gtk.Image,
    ) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        row.add_css_class("comment-reply")
        reply_text = Gtk.Label(
            label=f"↳ {reply.author}: {reply.text}",
            xalign=0,
            wrap=True,
            hexpand=True,
        )
        reply_text.add_css_class("dim-label")
        row.append(reply_text)
        like = Gtk.Button(
            label=f"{reply.like_count:,}",
            icon_name="emblem-favorite-symbolic",
            tooltip_text="Like this reply",
        )
        like.add_css_class("flat")
        like.connect("clicked", self._like_comment, reply)
        row.append(like)
        if reply.is_own:
            delete = Gtk.Button(
                icon_name="edit-delete-symbolic",
                tooltip_text="Delete your reply",
            )
            delete.add_css_class("flat")
            delete.add_css_class("destructive-action")
            delete.connect(
                "clicked",
                self._delete_comment,
                reply,
                replies,
                row,
                parent,
                indicator,
                disclosure,
            )
            row.append(delete)
        return row

    @staticmethod
    def _toggle_reply_composer(
        _button: Gtk.Button,
        composer: Gtk.Box,
        entry: Gtk.Entry,
    ) -> None:
        visible = not composer.get_visible()
        composer.set_visible(visible)
        if visible:
            entry.grab_focus()

    def _reply_entry_key_pressed(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        state: Gdk.ModifierType,
        parent: Comment,
        entry: Gtk.Entry,
        post_button: Gtk.Button,
        replies: Gtk.Box,
        indicator: Gtk.Label,
        disclosure: Gtk.Image,
    ) -> bool:
        if (
            keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter)
            and state & Gdk.ModifierType.CONTROL_MASK
        ):
            self._post_reply(
                post_button,
                parent,
                entry,
                post_button,
                replies,
                indicator,
                disclosure,
            )
            return True
        return False

    def _post_reply(
        self,
        _button: Gtk.Widget,
        parent: Comment,
        entry: Gtk.Entry,
        post_button: Gtk.Button,
        replies: Gtk.Box,
        indicator: Gtk.Label,
        disclosure: Gtk.Image,
    ) -> None:
        text = entry.get_text().strip()
        item = self.current_item
        browser = self.youtube_browser_session
        account = self.active_oauth_account
        if not text or not item or item.source != "youtube":
            return
        if not browser and (
            not account or COMMENT_SCOPE not in account.scopes
        ):
            self.toast_overlay.add_toast(
                Adw.Toast(title="Sign in to YouTube before replying")
            )
            return
        entry.set_sensitive(False)
        post_button.set_sensitive(False)
        if browser:
            operation = lambda: self.youtube.browser_reply_to_comment(
                item,
                parent.id,
                text,
                browser.display_name,
            )
        else:
            operation = lambda: self.youtube.api_reply_to_comment(
                self.oauth.access_token(account),
                parent.id,
                text,
            )
        run_async(
            operation,
            lambda reply: self._reply_posted(
                item.id,
                parent,
                reply,
                entry,
                post_button,
                replies,
                indicator,
                disclosure,
            ),
            lambda error: self._reply_post_error(
                error, entry, post_button
            ),
        )

    def _reply_posted(
        self,
        item_id: str,
        parent: Comment,
        reply: Comment,
        entry: Gtk.Entry,
        post_button: Gtk.Button,
        replies: Gtk.Box,
        indicator: Gtk.Label,
        disclosure: Gtk.Image,
    ) -> bool:
        entry.set_sensitive(True)
        post_button.set_sensitive(True)
        if not self.current_item or self.current_item.id != item_id:
            return GLib.SOURCE_REMOVE
        entry.set_text("")
        composer = entry.get_parent()
        if composer:
            composer.set_visible(False)
        child = replies.get_first_child()
        while child:
            following = child.get_next_sibling()
            if child.has_css_class("comments-empty"):
                replies.remove(child)
            child = following
        self._mark_own_comment(reply)
        parent.replies.append(reply)
        replies.append(
            self._comment_reply_row(
                reply,
                parent,
                replies,
                indicator,
                disclosure,
            )
        )
        replies.set_visible(True)
        indicator.set_label("Hide replies")
        disclosure.set_from_icon_name("pan-down-symbolic")
        self.toast_overlay.add_toast(Adw.Toast(title="Reply posted"))
        return GLib.SOURCE_REMOVE

    def _reply_post_error(
        self,
        error: Exception,
        entry: Gtk.Entry,
        post_button: Gtk.Button,
    ) -> bool:
        entry.set_sensitive(True)
        post_button.set_sensitive(True)
        self.toast_overlay.add_toast(Adw.Toast(title=str(error)))
        return GLib.SOURCE_REMOVE

    def _like_comment(self, button: Gtk.Button, comment: Comment) -> None:
        item = self.current_item
        if not self.youtube_browser_session:
            self.toast_overlay.add_toast(
                Adw.Toast(
                    title="Connect your YouTube browser session to like comments"
                )
            )
            return
        if not item or item.source != "youtube" or not comment.id:
            return
        button.set_sensitive(False)
        run_async(
            lambda: self.youtube.browser_like_comment(
                item,
                comment.id,
                comment.parent_id or "",
            ),
            lambda _result: self._comment_liked(comment, button),
            lambda error: self._comment_action_error(error, button),
        )

    def _comment_liked(
        self, comment: Comment, button: Gtk.Button
    ) -> bool:
        comment.like_count += 1
        button.set_label(f"{comment.like_count:,}")
        button.set_tooltip_text("You liked this comment")
        button.add_css_class("suggested-action")
        return GLib.SOURCE_REMOVE

    def _delete_comment(
        self,
        button: Gtk.Button,
        comment: Comment,
        container: Gtk.Box,
        row: Gtk.Widget,
        parent: Comment | None,
        indicator: Gtk.Label | None,
        disclosure: Gtk.Image | None,
    ) -> None:
        item = self.current_item
        browser = self.youtube_browser_session
        account = self.active_oauth_account
        if not item or item.source != "youtube" or not comment.id:
            return
        if not browser and (
            not account or COMMENT_SCOPE not in account.scopes
        ):
            self.toast_overlay.add_toast(
                Adw.Toast(title="Sign in to YouTube before deleting comments")
            )
            return
        button.set_sensitive(False)
        if browser:
            operation = lambda: self.youtube.browser_delete_comment(
                item,
                comment.id,
                comment.delete_action,
                comment.parent_id or "",
            )
        else:
            operation = lambda: self.youtube.api_delete_comment(
                self.oauth.access_token(account), comment.id
            )
        run_async(
            operation,
            lambda _result: self._comment_deleted(
                comment,
                container,
                row,
                parent,
                indicator,
                disclosure,
            ),
            lambda error: self._comment_action_error(error, button),
        )

    def _comment_deleted(
        self,
        comment: Comment,
        container: Gtk.Box,
        row: Gtk.Widget,
        parent: Comment | None,
        indicator: Gtk.Label | None,
        disclosure: Gtk.Image | None,
    ) -> bool:
        if row.get_parent() is container:
            container.remove(row)
        if parent is not None:
            parent.replies = [
                reply for reply in parent.replies if reply.id != comment.id
            ]
            if not parent.replies:
                container.append(self._empty_replies_label())
            if indicator:
                count = len(parent.replies)
                indicator.set_label(
                    "Hide replies"
                    if container.get_visible()
                    else (
                        f"View {count} {'reply' if count == 1 else 'replies'}"
                        if count
                        else "View replies"
                    )
                )
            if disclosure and not container.get_visible():
                disclosure.set_from_icon_name("go-next-symbolic")
        elif not container.get_first_child():
            empty = Gtk.Label(label="No comments yet.", wrap=True)
            empty.add_css_class("dim-label")
            empty.add_css_class("comments-empty")
            container.append(empty)
        self.toast_overlay.add_toast(Adw.Toast(title="Comment deleted"))
        return GLib.SOURCE_REMOVE

    def _comment_action_error(
        self, error: Exception, button: Gtk.Button
    ) -> bool:
        button.set_sensitive(True)
        self.toast_overlay.add_toast(Adw.Toast(title=str(error)))
        return GLib.SOURCE_REMOVE

    def _comments_error(self, error: Exception) -> bool:
        self.comments_loading = False
        self.comments_spinner.stop()
        self.comments_loading_row.set_visible(False)
        self.comments_more.set_sensitive(True)
        self.comments_more.set_label("Retry comments")
        self.comments_more.set_visible(True)
        self.toast_overlay.add_toast(Adw.Toast(title=str(error)))
        return GLib.SOURCE_REMOVE

    def _play_selected(self, item: MediaItem) -> None:
        if self._is_jellyfin_music_item(item):
            self.toast_overlay.add_toast(
                Adw.Toast(title="TubeFin does not support music playback")
            )
            return
        if item.source == "jellyfin" and self._synctube_active():
            self.toast_overlay.add_toast(
                Adw.Toast(title="Disconnect from SyncTube to use Jellyfin")
            )
            return
        if item.source == "youtube" and self._jellyfin_syncplay_active():
            self.toast_overlay.add_toast(
                Adw.Toast(title="Leave the Jellyfin watch party to use YouTube")
            )
            return
        if self.current_item and self.current_item.id == item.id:
            self._show_player()
            return
        if (
            self.current_item
            and 0 <= self.queue_index < len(self.queue)
            and self.queue[self.queue_index].id == self.current_item.id
        ):
            position = self.queue_index + 1
        elif self.current_item:
            self.queue.insert(0, self.current_item)
            self.queue_index = 0
            position = 1
        else:
            position = max(0, self.queue_index + 1)
        self.queue.insert(position, item)
        self.queue_index = position
        self._refresh_queue()
        self._begin_playback(item)

    @staticmethod
    def _is_jellyfin_music_item(item: MediaItem) -> bool:
        return item.source == "jellyfin" and (
            item.kind.casefold()
            in {"audio", "audiobook", "musicalbum", "musicartist"}
            or str(item.payload.get("MediaType") or "").casefold() == "audio"
            or str(item.payload.get("CollectionType") or "").casefold()
            in {"music", "audiobooks"}
        )

    def _new_playback_load_trace(
        self, item: MediaItem, request_id: int
    ) -> PlaybackLoadTrace:
        started_at = time.monotonic()
        trace = PlaybackLoadTrace(
            request_id=request_id,
            title=item.title,
            source=item.source,
            started_at=started_at,
            marks={"selected": started_at},
        )
        with self.playback_load_trace_lock:
            self.playback_load_traces.append(trace)
            del self.playback_load_traces[:-20]
        return trace

    def _cancel_pending_playback_load_trace(self, request_id: int) -> None:
        with self.playback_load_trace_lock:
            trace = next(
                (
                    value
                    for value in reversed(self.playback_load_traces)
                    if value.request_id == request_id
                ),
                None,
            )
            if trace and not any(
                mark in trace.marks
                for mark in ("first_playback", "failed", "cancelled")
            ):
                trace.marks["cancelled"] = time.monotonic()

    def _mark_playback_load(
        self,
        request_id: int,
        mark: str | None = None,
        *,
        error: object | None = None,
        only_once: bool = False,
        after: str | None = None,
        **notes: object,
    ) -> None:
        with self.playback_load_trace_lock:
            trace = next(
                (
                    value
                    for value in reversed(self.playback_load_traces)
                    if value.request_id == request_id
                ),
                None,
            )
            if trace is None:
                return
            if after and after not in trace.marks:
                return
            if mark and (not only_once or mark not in trace.marks):
                trace.marks[mark] = time.monotonic()
            trace.notes.update(
                {name: str(value) for name, value in notes.items() if value is not None}
            )
            if error is not None:
                trace.error = str(error)
        if self.playback_load_window:
            GLib.idle_add(self._refresh_playback_load_window_once)

    @staticmethod
    def _load_duration_label(seconds: float) -> str:
        if seconds < 0.001:
            return "<1 ms"
        if seconds < 1:
            return f"{seconds * 1000:.0f} ms"
        return f"{seconds:.2f} s"

    @staticmethod
    def _byte_size_label(size: int) -> str:
        if size < 1 << 10:
            return f"{size} B"
        if size < 1 << 20:
            return f"{size / (1 << 10):.0f} KiB"
        return f"{size / (1 << 20):.1f} MiB"

    @staticmethod
    def _playback_load_segments(
        trace: PlaybackLoadTrace,
    ) -> list[tuple[str, str, float | None]]:
        marks = trace.marks
        definitions = (
            ("Player UI setup", "selected", "details_cache_start"),
            ("Played-details disk cache", "details_cache_start", "details_cache_end"),
            ("Queue prebuffer wait", "queue_wait_start", "worker_ready"),
            ("Background worker dispatch", "worker_queued", "worker_started"),
            ("Stream resolver", "resolver_start", "resolver_end"),
            ("Played-video disk cache", "played_cache_start", "played_cache_end"),
            ("Legacy cache validation", "legacy_cache_start", "legacy_cache_end"),
            ("Worker → GTK handoff", "worker_done", "worker_ready"),
            ("Cached-prefix proxy", "proxy_start", "proxy_end"),
            ("mpv file open / first byte", "mpv_load", "file_loaded"),
            ("file loaded → playback", "file_loaded", "first_playback"),
        )
        segments: list[tuple[str, str, float | None]] = []
        for label, start_name, end_name in definitions:
            start = marks.get(start_name)
            end = marks.get(end_name)
            if start is None:
                continue
            if end is None:
                state = (
                    "cancelled"
                    if "cancelled" in marks
                    else "failed"
                    if "failed" in marks
                    else "running…"
                )
                segments.append((label, state, None))
            else:
                duration = max(0.0, end - start)
                segments.append((label, TubeFinWindow._load_duration_label(duration), duration))
        return segments

    def _show_playback_load_window(self) -> None:
        if self.playback_load_window:
            self.playback_load_window.present()
            return
        window = Adw.Window(
            transient_for=self,
            modal=False,
            title="Video load diagnostics",
        )
        window.set_default_size(760, 620)
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(
            Adw.WindowTitle(
                title="Video load diagnostics",
                subtitle="Resolver, cache, proxy, and mpv timing",
            )
        )
        toolbar.add_top_bar(header)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_margin_top(18)
        content.set_margin_bottom(18)
        content.set_margin_start(18)
        content.set_margin_end(18)
        summary = Gtk.Label(xalign=0, wrap=True)
        summary.add_css_class("dim-label")
        content.append(summary)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        trace_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        trace_list.add_css_class("boxed-list")
        scroller.set_child(trace_list)
        content.append(scroller)
        toolbar.set_content(content)
        window.set_content(toolbar)
        self.playback_load_window = window
        self.playback_load_summary = summary
        self.playback_load_list = trace_list
        window.connect("close-request", self._playback_load_window_closed)
        self.playback_load_refresh_source = GLib.timeout_add(
            500, self._refresh_playback_load_window
        )
        self._refresh_playback_load_window()
        window.present()

    def _playback_load_window_closed(self, *_args: object) -> bool:
        if self.playback_load_refresh_source:
            GLib.source_remove(self.playback_load_refresh_source)
            self.playback_load_refresh_source = 0
        self.playback_load_window = None
        self.playback_load_summary = None
        self.playback_load_list = None
        return False

    def _refresh_playback_load_window_once(self) -> bool:
        self._refresh_playback_load_window()
        return GLib.SOURCE_REMOVE

    def _refresh_playback_load_window(self) -> bool:
        if not self.playback_load_window or not self.playback_load_list:
            return GLib.SOURCE_REMOVE
        with self.playback_load_trace_lock:
            traces = [
                PlaybackLoadTrace(
                    request_id=trace.request_id,
                    title=trace.title,
                    source=trace.source,
                    started_at=trace.started_at,
                    marks=dict(trace.marks),
                    notes=dict(trace.notes),
                    error=trace.error,
                )
                for trace in self.playback_load_traces
            ]
        self._clear_box(self.playback_load_list)
        now = time.monotonic()
        if self.playback_load_summary:
            self.playback_load_summary.set_label(
                "Start a video to capture a trace. The slowest completed stage is "
                "called out automatically."
                if not traces
                else (
                    f"{len(traces)} recent load attempt"
                    f"{'s' if len(traces) != 1 else ''} · newest first"
                )
            )
        for trace in reversed(traces):
            marks = trace.marks
            segments = self._playback_load_segments(trace)
            completed = [segment for segment in segments if segment[2] is not None]
            slowest = max(completed, key=lambda segment: segment[2] or 0, default=None)
            finished = (
                marks.get("first_playback")
                or marks.get("failed")
                or marks.get("cancelled")
            )
            elapsed = max(0.0, (finished or now) - trace.started_at)
            status = (
                f"Failed · {trace.error}"
                if trace.error
                else "Playing"
                if "first_playback" in marks
                else "Cancelled"
                if "cancelled" in marks
                else "mpv opened the file"
                if "file_loaded" in marks
                else "Loading"
            )
            route = trace.notes.get("route", "Preparing route")
            heading = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            heading.set_margin_top(10)
            heading.set_margin_bottom(10)
            heading.set_margin_start(12)
            heading.set_margin_end(12)
            labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
            title = Gtk.Label(label=trace.title, xalign=0, ellipsize=Pango.EllipsizeMode.END)
            title.add_css_class("heading")
            labels.append(title)
            elapsed_label = self._load_duration_label(elapsed)
            meta = Gtk.Label(
                label=(
                    f"{trace.source.capitalize()} · {route} · {status} · "
                    f"time to playback {elapsed_label}"
                ),
                xalign=0,
                wrap=True,
            )
            meta.add_css_class("dim-label")
            labels.append(meta)
            details: list[str] = []
            running = next(
                (segment for segment in reversed(segments) if segment[1] == "running…"),
                None,
            )
            if running:
                details.append(f"Currently waiting on: {running[0]}")
            if slowest:
                details.append(f"Slowest: {slowest[0]} ({slowest[1]})")
            details.extend(f"{label}: {value}" for label, value, _duration in segments)
            note_labels = {
                "details_cache": "Details cache",
                "resolver": "Resolver result",
                "resolver_refresh": "Resolver refresh",
                "played_cache": "Played cache",
                "prefix": "Cached prefix",
                "stream_label": "Stream",
                "queue_prebuffer": "Queue prebuffer",
            }
            details.extend(
                f"{label}: {trace.notes[name]}"
                for name, label in note_labels.items()
                if name in trace.notes
            )
            timing = Gtk.Label(label="\n".join(details), xalign=0, wrap=True)
            timing.set_selectable(False)
            labels.append(timing)
            heading.append(labels)
            row = Gtk.ListBoxRow(child=heading, selectable=False, activatable=False)
            self.playback_load_list.append(row)
        return GLib.SOURCE_CONTINUE

    def _begin_playback(self, item: MediaItem, reveal_player: bool = True) -> None:
        self._cancel_pending_playback_load_trace(self.playback_request)
        self.playback_request += 1
        request_id = self.playback_request
        self._new_playback_load_trace(item, request_id)
        self._detach_playback()
        # Update this before resolving. Navigation must never consult the item
        # from the stream we just retired while a new selection is loading.
        self.current_item = item
        self.playback_started_at = time.monotonic()
        self.queue_advance_item_id = ""
        self.pending_sync_state = None
        self.resume_position_offer = self.history.resume_position(item)
        self.resume_item_id = item.id if self.resume_position_offer else ""
        self.resume_offer_shown = False
        self.sponsor_segments = []
        self.skipped_sponsor_segments.clear()
        self.manual_sponsor_segment = None
        self.sponsor_undo_position = 0.0
        self.sponsor_undo_item_id = ""
        self.sponsor_skip_button.set_visible(False)
        if self.mpv_player:
            self.mpv_player.pending_seek = None
        sponsor_categories = tuple(
            category
            for category in SPONSORBLOCK_CATEGORIES
            if self.sponsorblock_categories.get(category) != "ignore"
        )
        if item.source == "youtube" and self.sponsorblock_enabled and sponsor_categories:
            run_async(
                lambda: self.sponsorblock.segments(item.id, sponsor_categories),
                lambda segments: self._sponsor_segments_loaded(item.id, segments),
                lambda _error: None,
            )
        self.last_playback_position = 0.0
        self.last_playback_duration = 0.0
        self.last_playback_paused = True
        self.last_reported_pause = None
        self.last_jellyfin_update = time.monotonic()
        self.comment_cursor = None
        self.comments_loading = False
        self.comments_spinner.stop()
        self.comments_loading_row.set_visible(False)
        self._clear_box(self.comments_box)
        self.player_comments_button.set_visible(item.source == "youtube")
        self.fullscreen_comments_button.set_visible(
            item.source == "youtube" and self._player_is_fullscreen()
        )
        self._refresh_comment_composer()
        live_chat_supported = item.source == "youtube" and self._synctube_active()
        live_chat_available = bool(
            live_chat_supported
            and (reveal_player or self._visible_page_name() == "player")
        )
        self.player_live_chat_button.set_visible(live_chat_available)
        self.fullscreen_live_chat_button.set_visible(
            live_chat_available and self._player_is_fullscreen()
        )
        if not live_chat_supported:
            self.player_live_chat_button.set_active(False)
            self.player_live_chat_panel.set_visible(False)
        self._refresh_live_chat_controls()
        self.player_subscribe.set_visible(
            item.source == "youtube" and bool(item.payload.get("channel_url"))
        )
        self._refresh_player_subscription()
        has_channel = item.source == "youtube" and bool(item.payload.get("channel_url"))
        self.player_avatar_button.set_visible(has_channel)
        self.player_channel_button.set_visible(has_channel)
        channel_name = item.subtitle or "channel"
        self.player_avatar_button.set_tooltip_text(f"Open {channel_name}")
        self.player_channel_button.set_tooltip_text(f"Open {channel_name}")
        self._load_player_avatar(item)
        self.player_download.set_visible(item.source in {"youtube", "jellyfin"})
        self.comments_more.set_label("Load")
        self.comments_more.set_sensitive(True)
        self.comments_more.set_visible(False)
        self.player_comments_button.set_active(False)
        self.player_comments_panel.set_visible(False)
        self.fullscreen_title.set_label(item.title)

        self.player_title.set_label(item.title)
        self.player_subtitle.set_label(item.subtitle)
        self._set_player_description(item)
        self._mark_playback_load(request_id, "details_cache_start")
        cached_details = self.played_cache.details(item)
        self._mark_playback_load(
            request_id,
            "details_cache_end",
            details_cache="hit" if cached_details is not None else "miss",
        )
        if cached_details is not None:
            description, published_date, _chapters = cached_details
            self._set_player_description(item, description, published_date)
        if reveal_player:
            self.player_navigation_guard_until = time.monotonic() + 0.75
            self._set_visible_page("player")
            self.window_title.set_title("Now Playing")
            self.window_title.set_subtitle(item.source.capitalize())
            self.mini_player.set_visible(False)
        else:
            self.mini_player.set_visible(True)
        self.player_status.set_label("Preparing video…")
        self.player_status_box.set_visible(True)
        self.player_spinner.start()

        local_stream = self._local_download_stream(item)
        if local_stream:
            self._mark_playback_load(request_id, route="Local download")
            self._start_playback(item, local_stream, request_id)
            return
        prefetch_manager = self.prebuffer
        prefetched = prefetch_manager.claim(item)
        if prefetched is not None:
            self._mark_playback_load(
                request_id,
                "queue_wait_start",
                route="Queue prebuffer",
            )
            run_async(
                prefetched.result,
                lambda buffered: self._prefetched_stream_ready(
                    prefetch_manager,
                    item,
                    buffered,
                    request_id,
                ),
                lambda error: self._prefetched_stream_failed(
                    item, error, request_id
                ),
            )
            return
        self._mark_playback_load(request_id, route="Resolve + played cache")
        self._mark_playback_load(request_id, "worker_queued")
        run_async(
            lambda: self._prepare_played_item(item, request_id),
            lambda prepared: self._played_item_ready(
                item, prepared, request_id
            ),
            lambda error: self._playback_error(error, request_id),
        )

    def _prepare_played_item(
        self, item: MediaItem, request_id: int
    ) -> tuple[ResolvedStream, PrebufferedStream | None]:
        self._mark_playback_load(request_id, "worker_started")
        self._mark_playback_load(request_id, "played_cache_start")
        cached = self.played_cache.prepare_cached(item)
        if cached is not None:
            stream, buffered, needs_refresh = cached
            if needs_refresh:
                buffered.defer_upstream()
            self._mark_playback_load(
                request_id,
                "played_cache_end",
                played_cache=(
                    "hit; stale URL refreshing behind cached playback"
                    if needs_refresh
                    else "hit; resolver bypassed"
                ),
                prefix=self._byte_size_label(len(buffered.prefix)),
                route=(
                    "Cached prefix + background refresh"
                    if needs_refresh
                    else "Played cache fast path"
                ),
            )
            self._mark_playback_load(request_id, "worker_done")
            return stream, buffered
        self._mark_playback_load(
            request_id,
            "played_cache_end",
            played_cache="miss or expired stream URL",
            prefix="none",
        )
        self._mark_playback_load(request_id, "resolver_start")
        stream = self._resolve_item(item, request_id)
        self._mark_playback_load(request_id, "resolver_end")
        self._mark_playback_load(request_id, "legacy_cache_start")
        buffered = self.played_cache.prepare(item, stream)
        self._mark_playback_load(
            request_id,
            "legacy_cache_end",
            played_cache="legacy hit after resolve" if buffered is not None else "miss",
            prefix=self._byte_size_label(len(buffered.prefix)) if buffered else "none",
        )
        self._mark_playback_load(request_id, "worker_done")
        return stream, buffered

    def _played_item_ready(
        self,
        item: MediaItem,
        prepared: tuple[ResolvedStream, PrebufferedStream | None],
        request_id: int,
    ) -> bool:
        stream, buffered = prepared
        self._mark_playback_load(request_id, "worker_ready")
        if request_id != self.playback_request:
            if buffered:
                buffered.close()
            return GLib.SOURCE_REMOVE
        if not buffered or not buffered.upstream_is_deferred:
            self.played_cache_source = (item.id, stream)
        self.played_cache_buffered = buffered
        playback_stream = stream
        if buffered:
            self.played_cache_active = buffered
            self._mark_playback_load(request_id, "proxy_start")
            playback_stream = buffered.playback_stream()
            self._mark_playback_load(request_id, "proxy_end")
            if buffered.upstream_is_deferred:
                run_async(
                    lambda: self._refresh_cached_playback(
                        item, buffered, request_id
                    ),
                    lambda refreshed: self._cached_playback_refreshed(
                        item, buffered, refreshed, request_id
                    ),
                    lambda error: self._cached_playback_refresh_failed(
                        buffered, error, request_id
                    ),
                )
        return self._start_playback(item, playback_stream, request_id)

    def _refresh_cached_playback(
        self,
        item: MediaItem,
        buffered: PrebufferedStream,
        request_id: int,
    ) -> ResolvedStream:
        self._mark_playback_load(request_id, "resolver_start")
        try:
            stream = self._resolve_item(item, request_id)
        except Exception:
            buffered.release_deferred_upstream()
            self._mark_playback_load(request_id, "resolver_end")
            raise
        buffered.replace_upstream(stream)
        self.played_cache.refresh_stream(item, stream, buffered)
        self._mark_playback_load(request_id, "resolver_end")
        return stream

    def _cached_playback_refreshed(
        self,
        item: MediaItem,
        buffered: PrebufferedStream,
        stream: ResolvedStream,
        request_id: int,
    ) -> bool:
        if request_id != self.playback_request:
            return GLib.SOURCE_REMOVE
        self.played_cache_source = (item.id, stream)
        self.played_cache_buffered = buffered
        if self.mpv_player:
            self.mpv_player.set_variants(stream.variants)
            self.mpv_player.set_audio_tracks(stream.audio_tracks)
            self.mpv_player.set_chapters(stream.chapters)
        return GLib.SOURCE_REMOVE

    def _cached_playback_refresh_failed(
        self,
        buffered: PrebufferedStream,
        error: Exception,
        request_id: int,
    ) -> bool:
        buffered.release_deferred_upstream()
        self._mark_playback_load(
            request_id,
            resolver_refresh=f"failed: {error}",
        )
        return GLib.SOURCE_REMOVE

    def _prefetched_stream_ready(
        self,
        manager: PrebufferManager,
        item: MediaItem,
        buffered: PrebufferedStream,
        request_id: int,
    ) -> bool:
        self._mark_playback_load(request_id, "worker_ready")
        if request_id != self.playback_request:
            buffered.close()
            return GLib.SOURCE_REMOVE
        self.played_cache_source = (item.id, buffered.stream)
        self.played_cache_buffered = buffered
        try:
            self._mark_playback_load(request_id, "proxy_start")
            stream = manager.activate(buffered)
            self._mark_playback_load(
                request_id,
                "proxy_end",
                prefix=self._byte_size_label(len(buffered.prefix)),
            )
        except Exception as error:
            return self._prefetched_stream_failed(item, error, request_id)
        return self._start_playback(item, stream, request_id)

    def _prefetched_stream_failed(
        self, item: MediaItem, _error: Exception, request_id: int
    ) -> bool:
        if request_id != self.playback_request:
            return GLib.SOURCE_REMOVE
        self._mark_playback_load(
            request_id,
            queue_prebuffer="failed; resolving normally",
            route="Queue prebuffer fallback",
        )
        self._mark_playback_load(request_id, "worker_queued")
        run_async(
            lambda: self._prepare_played_item(item, request_id),
            lambda prepared: self._played_item_ready(
                item, prepared, request_id
            ),
            lambda error: self._playback_error(error, request_id),
        )
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _release_date_label(value: object) -> str:
        raw = str(value or "").strip()
        if len(raw) == 8 and raw.isdigit():
            return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
        if len(raw) >= 10 and raw[4:5] == "-" and raw[7:8] == "-":
            return raw[:10]
        return raw

    def _set_player_description(
        self,
        item: MediaItem,
        description: str = "",
        published_date: str = "",
    ) -> None:
        description = description or str(
            item.payload.get("description") or item.payload.get("Overview") or ""
        )
        published_date = published_date or str(
            item.payload.get("upload_date")
            or item.payload.get("PremiereDate")
            or item.payload.get("ProductionYear")
            or ""
        )
        released = self._release_date_label(published_date)
        parts = [description]
        if released:
            parts.insert(0, f"Released {released}")
        text = "\n".join(part for part in parts if part)
        self.player_description.set_label(text)
        self.player_description.set_visible(bool(text))

    def _load_player_avatar(self, item: MediaItem) -> None:
        self.player_avatar_picture.set_visible(False)
        if item.source != "youtube":
            return
        avatar_url = str(item.payload.get("channel_avatar_url") or "")
        if avatar_url:
            self.thumbnails.load(
                avatar_url,
                lambda path, item_id=item.id: self._set_player_avatar(item_id, path),
            )
            return
        channel_url = str(item.payload.get("channel_url") or "")
        if channel_url:
            run_async(
                lambda: self.youtube.channel_avatar(channel_url),
                lambda url, item_id=item.id: self._player_avatar_resolved(item_id, url),
                lambda _error: None,
            )

    def _player_avatar_resolved(self, item_id: str, url: str | None) -> bool:
        if url and self.current_item and self.current_item.id == item_id:
            self.thumbnails.load(
                url,
                lambda path: self._set_player_avatar(item_id, path),
            )
        return GLib.SOURCE_REMOVE

    def _set_player_avatar(self, item_id: str, path: object) -> bool:
        if path and self.current_item and self.current_item.id == item_id:
            self.player_avatar_picture.set_filename(str(path))
            self.player_avatar_picture.set_visible(True)
        return GLib.SOURCE_REMOVE

    def _resolve_item(
        self, item: MediaItem, trace_request_id: int | None = None
    ) -> ResolvedStream:
        local_stream = self._local_download_stream(item)
        if local_stream:
            if trace_request_id is not None:
                self._mark_playback_load(
                    trace_request_id, resolver="local download"
                )
            return local_stream
        key = f"{item.source}:{item.id}"
        waited_for_inflight = False
        while True:
            with self.resolved_stream_lock:
                cached = self.resolved_stream_cache.get(key)
                if cached and time.monotonic() - cached[0] <= 10 * 60:
                    if trace_request_id is not None:
                        self._mark_playback_load(
                            trace_request_id,
                            resolver=(
                                "shared in-flight result"
                                if waited_for_inflight
                                else "10-minute memory cache hit"
                            ),
                        )
                    return cached[1]
                if cached:
                    self.resolved_stream_cache.pop(key, None)
                event = self.resolved_stream_inflight.get(key)
                if event is None:
                    event = threading.Event()
                    self.resolved_stream_inflight[key] = event
                    owner = True
                else:
                    owner = False
            if owner:
                break
            waited_for_inflight = True
            if trace_request_id is not None:
                self._mark_playback_load(
                    trace_request_id, resolver="waiting for an in-flight resolver"
                )
            if not event.wait(90):
                raise TimeoutError("Timed out while preparing the stream.")

        try:
            if trace_request_id is not None:
                self._mark_playback_load(
                    trace_request_id, resolver=f"fresh {item.source} request"
                )
            if item.source == "youtube":
                stream = self.youtube.resolve(item)
            elif item.source == "jellyfin":
                stream = self.jellyfin.resolve(item)
            elif item.source == "offline":
                path = Path(str(item.payload.get("media_path") or ""))
                if not path.is_file():
                    raise FileNotFoundError("The offline media file is missing.")
                stream = ResolvedStream(
                    path.resolve().as_uri(), default_label="Local download"
                )
            else:
                raise ValueError(f"Unsupported media source: {item.source}")
            with self.resolved_stream_lock:
                self.resolved_stream_cache[key] = (time.monotonic(), stream)
            return stream
        finally:
            with self.resolved_stream_lock:
                self.resolved_stream_inflight.pop(key, None)
                event.set()

    def _local_download_stream(self, item: MediaItem) -> ResolvedStream | None:
        if item.source not in {"youtube", "jellyfin"}:
            return None
        record = self.offline.find_complete_for_item(item)
        if not record:
            return None
        return ResolvedStream(
            Path(record.media_path).resolve().as_uri(),
            default_label="Local download",
        )

    def _online_variants(
        self, item: MediaItem
    ) -> tuple[list[StreamVariant], list[AudioTrack], list[VideoChapter]]:
        online = self.youtube.resolve(item)
        variants = [StreamVariant("Online · Auto", online.url, online.headers)]
        variants.extend(
            StreamVariant(f"Online · {variant.label}", variant.url, variant.headers)
            for variant in online.variants
        )
        return variants, online.audio_tracks, list(online.chapters)

    def _online_variants_loaded(
        self,
        item_id: str,
        request_id: int,
        options: tuple[list[StreamVariant], list[AudioTrack], list[VideoChapter]],
    ) -> bool:
        if (
            request_id == self.playback_request
            and self.current_item
            and self.current_item.id == item_id
            and self.mpv_player
        ):
            variants, audio_tracks, chapters = options
            self.mpv_player.set_variants(variants)
            self.mpv_player.set_audio_tracks(audio_tracks)
            self.mpv_player.set_chapters(chapters)
        return GLib.SOURCE_REMOVE

    def _start_playback(
        self, item: MediaItem, stream: str | ResolvedStream, request_id: int
    ) -> bool:
        if request_id != self.playback_request:
            return GLib.SOURCE_REMOVE
        headers: dict[str, str] = {}
        variants = []
        subtitles = []
        audio_tracks = []
        chapters = []
        if isinstance(stream, ResolvedStream):
            url = stream.url
            headers = stream.headers
            variants = stream.variants
            subtitles = stream.subtitles
            audio_tracks = stream.audio_tracks
            chapters = stream.chapters
            default_label = stream.default_label
            self._set_player_description(
                item, stream.description, stream.published_date
            )
        else:
            url = stream
            default_label = "Auto"
        self._mark_playback_load(
            request_id,
            "mpv_load",
            stream_label=default_label,
        )
        self.playback_started_at = time.monotonic()
        self.queue_advance_item_id = ""
        self.mini_title.set_label(item.title)
        self.mini_subtitle.set_label(item.subtitle or item.source.capitalize())
        if self.mpv_player:
            self.mpv_player.load(
                url,
                headers,
                variants,
                subtitles,
                audio_tracks,
                chapters,
                default_label,
            )
        if item.source == "youtube" and default_label == "Local download":
            run_async(
                lambda: self._online_variants(item),
                lambda choices: self._online_variants_loaded(
                    item.id, request_id, choices
                ),
                lambda _error: None,
            )
        elif item.source == "jellyfin" and default_label == "Local download":
            run_async(
                lambda: self.jellyfin.resolve(item),
                lambda online: self._online_variants_loaded(
                    item.id,
                    request_id,
                    (
                        [StreamVariant("Online · Original", online.url, online.headers)],
                        [],
                        list(online.chapters),
                    ),
                ),
                lambda _error: None,
            )
        if item.source == "jellyfin":
            self._report_jellyfin_playback(item, "start", 0.0, False)
            self.last_reported_pause = False
        return GLib.SOURCE_REMOVE

    def _mpv_ready(self) -> None:
        self._mark_playback_load(
            self.playback_request,
            "file_loaded",
            only_once=True,
            after="mpv_load",
        )
        self.player_spinner.stop()
        self.player_status_box.set_visible(False)
        if (
            self.pending_sync_state
            and self.current_item
            and self.pending_sync_state.media_id == self.current_item.id
        ):
            state = self.pending_sync_state
            self.pending_sync_state = None
            self.resume_position_offer = 0
            self.resume_item_id = ""
            self._apply_sync_state(state, "ready", state.position)
        if (
            self.current_item
            and self.resume_item_id == self.current_item.id
            and self.resume_position_offer
            and not self.resume_offer_shown
        ):
            toast = Adw.Toast(
                title=f"Resume from {self._time_label(self.resume_position_offer)}?"
            )
            toast.set_button_label("Resume")
            toast.set_action_name("win.resume-playback")
            self.toast_overlay.add_toast(toast)
            self.resume_offer_shown = True

    def _cache_current_played_video(self) -> None:
        if not self.current_item or not self.played_cache_source:
            return
        item = self.current_item
        if self.played_cache_request_item_id == item.id:
            return
        item_id, stream = self.played_cache_source
        if (
            item.id != item_id
            or item.source not in {"youtube", "jellyfin"}
            or stream.default_label == "Local download"
        ):
            return
        self.played_cache_request_item_id = item.id
        buffered = self.played_cache_buffered

        def cache() -> None:
            if buffered:
                self.played_cache.cache_buffered(item, buffered)
            else:
                self.played_cache.cache_played(item, stream)

        threading.Thread(
            target=cache,
            daemon=True,
            name=f"played-video-cache-{item.id[:12]}",
        ).start()

    @staticmethod
    def _time_label(seconds: float) -> str:
        total = max(0, int(seconds))
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        return (
            f"{hours}:{minutes:02d}:{seconds:02d}"
            if hours
            else f"{minutes}:{seconds:02d}"
        )

    def _resume_playback(self) -> None:
        if (
            self.mpv_player
            and self.current_item
            and self.current_item.id == self.resume_item_id
            and self.resume_position_offer
        ):
            self.mpv_player.resume_at(self.resume_position_offer)
            self.resume_position_offer = 0
            self.resume_item_id = ""

    def _sponsor_segments_loaded(
        self, item_id: str, segments: list[SponsorSegment]
    ) -> bool:
        if self.current_item and self.current_item.id == item_id:
            self.sponsor_segments = segments
        return GLib.SOURCE_REMOVE

    def _set_manual_sponsor_segment(self, segment: SponsorSegment | None) -> None:
        if segment == self.manual_sponsor_segment:
            return
        self.manual_sponsor_segment = segment
        if not segment:
            self.sponsor_skip_button.set_visible(False)
            return
        label = SPONSORBLOCK_CATEGORY_DETAILS.get(
            segment.category, ("Segment", "")
        )[0]
        self.sponsor_skip_button.set_label(f"Skip {label}")
        self.sponsor_skip_button.set_visible(True)

    def _skip_manual_sponsor_segment(self) -> None:
        segment = self.manual_sponsor_segment
        if not segment or not self.mpv_player:
            return
        undo_position = self.mpv_player.position
        self.skipped_sponsor_segments.add((segment.start, segment.end))
        self.mpv_player.seek_absolute(segment.end)
        self._set_manual_sponsor_segment(None)
        category = SPONSORBLOCK_CATEGORY_DETAILS.get(
            segment.category, ("SponsorBlock segment", "")
        )[0]
        self._show_sponsor_skip_toast(
            max(1, round(segment.end - undo_position)), category, undo_position
        )

    def _show_sponsor_skip_toast(
        self, skipped: int, category: str, undo_position: float
    ) -> None:
        if not self.current_item:
            return
        self.sponsor_undo_position = undo_position
        self.sponsor_undo_item_id = self.current_item.id
        toast = Adw.Toast(title=f"Skipped {skipped}s · {category}")
        toast.set_button_label("Undo")
        toast.set_action_name("win.undo-sponsor-skip")
        self.toast_overlay.add_toast(toast)

    def _undo_sponsor_skip(self) -> None:
        if (
            self.mpv_player
            and self.current_item
            and self.current_item.id == self.sponsor_undo_item_id
        ):
            self.mpv_player.seek_absolute(self.sponsor_undo_position)
        self.sponsor_undo_position = 0.0
        self.sponsor_undo_item_id = ""

    def _jellyfin_go_back(self) -> None:
        if not self.jellyfin_history:
            return
        parent_id, title = self.jellyfin_history.pop()
        self.jellyfin_parent_id = parent_id
        self.browse_category_title.set_label(title)
        self.jellyfin_back.set_label("Back" if self.jellyfin_history else "Browse")
        self.jellyfin_back.set_sensitive(True)
        self.jellyfin_search.set_text("")
        self._load_jellyfin_current()

    def _mpv_error(self, message: str) -> None:
        self._mark_playback_load(
            self.playback_request,
            "failed",
            error=message,
            only_once=True,
            after="mpv_load",
        )
        self.player_spinner.stop()
        self.player_status.set_label(f"Playback failed: {message}")
        self.player_status_box.set_visible(True)
        self.toast_overlay.add_toast(Adw.Toast(title=f"Playback failed: {message}"))

    def _queue_current(self) -> None:
        if self.current_item:
            self._add_to_queue(self.current_item)

    def _add_to_queue(self, item: MediaItem) -> None:
        if item.source == "jellyfin" and self._synctube_active():
            return
        if item.source == "youtube" and self._jellyfin_syncplay_active():
            return
        self.queue.append(item)
        self._prebuffer_item(item)
        self._refresh_queue()
        if not self.current_item:
            self.mini_title.set_label(item.title)
            self.mini_subtitle.set_label(f"Queued · {item.subtitle or item.source.capitalize()}")
            self.mini_player.set_visible(True)
        self.toast_overlay.add_toast(Adw.Toast(title=f"Added {item.title} to queue"))

    def _add_to_queue_next(self, item: MediaItem) -> None:
        if item.source == "jellyfin" and self._synctube_active():
            return
        if item.source == "youtube" and self._jellyfin_syncplay_active():
            return
        position = min(len(self.queue), max(0, self.queue_index + 1))
        self.queue.insert(position, item)
        self._prebuffer_item(item)
        self._refresh_queue()
        self.toast_overlay.add_toast(Adw.Toast(title=f"{item.title} will play next"))

    def _save_item(self, item: MediaItem) -> None:
        picker = getattr(self, "playlist_picker", None)
        if picker:
            picker.close()

        window = Adw.Window(transient_for=self, modal=True, title="Save to playlist")
        window.set_default_size(420, 360)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        window.set_content(toolbar)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        content.set_margin_top(18)
        content.set_margin_bottom(18)
        content.set_margin_start(18)
        content.set_margin_end(18)
        intro = Gtk.Label(label=f"Choose where to save “{item.title}”.", xalign=0, wrap=True)
        intro.add_css_class("dim-label")
        content.append(intro)

        playlists = [
            playlist for playlist in self.playlists.list() if playlist.name != "Watch Later"
        ]
        playlist_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        playlist_list.add_css_class("boxed-list")
        for playlist in playlists:
            row = Adw.ActionRow(
                title=playlist.name,
                subtitle=f"{len(playlist.items)} {'item' if len(playlist.items) == 1 else 'items'}",
                activatable=True,
            )
            row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            row.connect(
                "activated",
                lambda _row, playlist_id=playlist.id: self._save_to_playlist(
                    playlist_id, item
                ),
            )
            playlist_list.append(row)
        if playlists:
            content.append(playlist_list)

        create_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        new_name = Gtk.Entry(placeholder_text="New playlist", hexpand=True)
        create_row.append(new_name)
        create = Gtk.Button(label="Create and save", icon_name="list-add-symbolic")
        create.connect(
            "clicked", lambda *_: self._create_playlist_for_item(new_name.get_text(), item)
        )
        create_row.append(create)
        new_name.connect("activate", lambda *_: create.activate())
        content.append(create_row)
        scroller.set_child(content)
        toolbar.set_content(scroller)
        self.playlist_picker = window
        window.connect("close-request", lambda *_: self._playlist_picker_closed())
        window.present()

    def _share_item(self, item: MediaItem) -> None:
        source = str(item.payload.get("original_source") or item.source)
        if source == "youtube" or item.source.startswith("youtube"):
            url = str(
                item.payload.get("webpage_url")
                or item.payload.get("source_url")
                or f"https://www.youtube.com/watch?v={item.id}"
            )
        elif source == "jellyfin" and self.jellyfin.session:
            url = (
                f"{self.jellyfin.session.server_url.rstrip('/')}/web/"
                f"#/details?id={item.id}"
            )
        else:
            metadata = item.payload.get("download_metadata") or {}
            url = str(metadata.get("source_url") or item.payload.get("source_url") or "")
        display = Gdk.Display.get_default()
        if not url or not display:
            self.toast_overlay.add_toast(
                Adw.Toast(title="This video does not have a shareable source link")
            )
            return
        display.get_clipboard().set(url)
        self.toast_overlay.add_toast(Adw.Toast(title="Video link copied"))

    def _playlist_picker_closed(self) -> bool:
        self.playlist_picker = None
        return False

    def _save_to_playlist(self, playlist_id: str, item: MediaItem) -> None:
        try:
            playlist = self.playlists.add(playlist_id, item)
        except KeyError:
            self.toast_overlay.add_toast(Adw.Toast(title="That playlist no longer exists."))
            return
        if getattr(self, "playlist_picker", None):
            self.playlist_picker.close()
        self.toast_overlay.add_toast(Adw.Toast(title=f"Saved to {playlist.name}"))
        if self._visible_page_name() == "library":
            self._load_playlists()

    def _create_playlist_for_item(self, name: str, item: MediaItem) -> None:
        if not name.strip():
            return
        playlist = self.playlists.create(name)
        self._save_to_playlist(playlist.id, item)

    def _watch_later(self, item: MediaItem) -> None:
        playlist = next(
            (value for value in self.playlists.list() if value.name == "Watch Later"),
            None,
        )
        if not playlist:
            playlist = self.playlists.create("Watch Later")
        self.playlists.add(playlist.id, item)
        self.toast_overlay.add_toast(Adw.Toast(title="Added to Watch Later"))

    @staticmethod
    def _history_item(item: MediaItem) -> MediaItem:
        if item.source != "offline":
            return item
        return replace(
            item,
            source=str(item.payload.get("original_source") or "offline"),
            subtitle=str(item.payload.get("original_channel") or item.subtitle),
            payload={
                key: value
                for key, value in item.payload.items()
                if key not in {"media_path", "download_id", "download_progress"}
            },
        )

    def _is_marked_watched(self, item: MediaItem) -> bool:
        canonical = self._history_item(item)
        return (canonical.source, canonical.id) in self.locally_marked_watched

    def _mark_watched(self, item: MediaItem) -> None:
        canonical = self._history_item(item)
        duration = float(canonical.duration_seconds or 1)
        self.history.record(canonical, duration, duration)
        self.locally_marked_watched.add((canonical.source, canonical.id))
        self.recommendation_items = [
            value for value in self.recommendation_items if value.id != canonical.id
        ]
        child = self.home_sections.get_first_child()
        while child:
            following = child.get_next_sibling()
            if isinstance(child, SectionShelf):
                child.remove_item(canonical.id)
            child = following
        if self._visible_page_name() == "library":
            self._load_library()
        elif self._visible_page_name() == "history":
            self._open_full_history()
        self.toast_overlay.add_toast(Adw.Toast(title="Marked as watched"))
        if canonical.source == "youtube":
            self.youtube_marked_watched.add(canonical.id)
            if not self.youtube.browser:
                self.toast_overlay.add_toast(
                    Adw.Toast(
                        title="Saved locally; YouTube history needs a browser session"
                    )
                )
                return
            run_async(
                lambda: self.youtube.mark_watched(canonical),
                lambda _result: None,
                lambda error: self.toast_overlay.add_toast(
                    Adw.Toast(title=f"Saved locally; YouTube history failed: {error}")
                ),
            )
        elif canonical.source == "jellyfin" and self.jellyfin.session:
            run_async(
                lambda: self.jellyfin.mark_watched(canonical),
                lambda _result: None,
                lambda error: self.toast_overlay.add_toast(
                    Adw.Toast(title=f"Saved locally; Jellyfin update failed: {error}")
                ),
            )

    def _play_next_queued(self, reveal_player: bool | None = None) -> None:
        if not self.queue:
            return
        if reveal_player is None:
            reveal_player = self.player_expanded
        next_index = self.queue_index + 1
        if next_index >= len(self.queue):
            if self.queue_loop:
                next_index = 0
            else:
                self._clear_queue()
                return
        self.queue_index = next_index
        item = self.queue[self.queue_index]
        self._refresh_queue()
        self._begin_playback(item, reveal_player=reveal_player)

    def _skip_queued(self) -> None:
        if self.queue:
            self._play_next_queued()

    def _play_previous_queued(self, reveal_player: bool | None = None) -> None:
        if not self.queue:
            return
        if reveal_player is None:
            reveal_player = self.player_expanded
        if self.queue_index > 0:
            self.queue_index -= 1
        elif self.queue_loop:
            self.queue_index = len(self.queue) - 1
        else:
            return
        self._refresh_queue()
        self._begin_playback(
            self.queue[self.queue_index], reveal_player=reveal_player
        )

    def _mini_play_pause(self) -> None:
        if self.current_item and self.mpv_player:
            self.mpv_player.set_paused(not self.last_playback_paused)
            return
        if self.queue:
            self._play_queued(max(0, self.queue_index))

    def _play_queued(self, index: int) -> None:
        if index >= len(self.queue):
            return
        reveal_player = self.player_expanded
        self.queue_index = index
        item = self.queue[index]
        self._refresh_queue()
        self._begin_playback(item, reveal_player=reveal_player)

    def _remove_queued(self, index: int) -> None:
        if index < len(self.queue):
            self.queue.pop(index)
            if index < self.queue_index:
                self.queue_index -= 1
            elif index == self.queue_index:
                self.queue_index = -1
            self._refresh_queue()

    def _move_queued(self, old: int, new: int) -> None:
        if not (0 <= old < len(self.queue) and 0 <= new < len(self.queue)):
            return
        item = self.queue.pop(old)
        self.queue.insert(new, item)
        if self.queue_index == old:
            self.queue_index = new
        elif old < self.queue_index <= new:
            self.queue_index -= 1
        elif new <= self.queue_index < old:
            self.queue_index += 1
        self._refresh_queue()

    def _queue_loop_toggled(self, button: Gtk.ToggleButton) -> None:
        self.queue_loop = button.get_active()
        self._update_transport_buttons()

    def _clear_queue(self) -> None:
        self.queue.clear()
        self.queue_index = -1
        self._refresh_queue()
        if not self.current_item:
            self.mini_player.set_visible(False)
        self._clear_queued_search_results()

    def _clear_queued_search_results(self) -> None:
        if not self.browse_search_results:
            return
        self.browse_search_results = False
        self.global_search.set_text("")
        self.navigation_history = [
            state
            for state in self.navigation_history
            if not (
                state.get("page") == "browse-category"
                and state.get("browse_search_results")
            )
        ]
        if self.pages.get_visible_child_name() != "browse-category":
            return
        if self._visible_page_name() == "player":
            self.pages.set_visible_child_name("browse")
        else:
            self._select_page("browse", record=False)

    def _refresh_queue(self) -> None:
        child = self.queue_box.get_first_child()
        while child:
            following = child.get_next_sibling()
            self.queue_box.remove(child)
            child = following
        queue_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        loop = Gtk.ToggleButton(label="Loop", icon_name="media-playlist-repeat-symbolic")
        loop.set_active(self.queue_loop)
        loop.connect("toggled", self._queue_loop_toggled)
        queue_actions.append(loop)
        clear = Gtk.Button(label="Clear", icon_name="edit-clear-all-symbolic")
        clear.connect("clicked", lambda *_: self._clear_queue())
        queue_actions.append(clear)
        self.queue_box.append(queue_actions)
        if not self.queue:
            self.queue_box.append(Gtk.Label(label="Queue is empty"))
        for index, item in enumerate(self.queue):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            play = Gtk.Button(label=item.title, hexpand=True)
            if index == self.queue_index:
                play.add_css_class("suggested-action")
            play.connect("clicked", lambda _button, position=index: self._play_queued(position))
            row.append(play)
            up = Gtk.Button(icon_name="go-up-symbolic", tooltip_text="Move up")
            up.set_sensitive(index > 0)
            up.connect(
                "clicked", lambda _button, position=index: self._move_queued(position, position - 1)
            )
            row.append(up)
            down = Gtk.Button(icon_name="go-down-symbolic", tooltip_text="Move down")
            down.set_sensitive(index + 1 < len(self.queue))
            down.connect(
                "clicked", lambda _button, position=index: self._move_queued(position, position + 1)
            )
            row.append(down)
            remove = Gtk.Button(icon_name="edit-delete-symbolic", tooltip_text="Remove")
            remove.connect("clicked", lambda _button, position=index: self._remove_queued(position))
            row.append(remove)
            self.queue_box.append(row)
        self.queue_button.set_tooltip_text(f"Queue ({len(self.queue)})")
        self._update_transport_buttons()

    def _update_transport_buttons(self) -> None:
        multiple = len(self.queue) > 1
        has_previous = bool(
            self.queue
            and (self.queue_index > 0 or self.queue_loop and multiple)
        )
        has_next = bool(
            self.queue
            and (
                self.queue_index + 1 < len(self.queue)
                or self.queue_loop and multiple
            )
        )
        if self.mpv_player:
            fullscreen = self.mpv_player.fullscreen_mode
            self.mpv_player.previous_button.set_visible(fullscreen or has_previous)
            self.mpv_player.previous_button.set_sensitive(has_previous)
            self.mpv_player.next_button.set_visible(fullscreen or has_next)
            self.mpv_player.next_button.set_sensitive(has_next)
        if hasattr(self, "mini_previous"):
            self.mini_previous.set_visible(has_previous)
            self.mini_next.set_visible(has_next)

    def _playback_error(self, error: Exception, request_id: int) -> bool:
        if request_id != self.playback_request:
            return GLib.SOURCE_REMOVE
        self._mark_playback_load(
            request_id,
            "failed",
            error=error,
            only_once=True,
        )
        if self.current_item:
            with self.resolved_stream_lock:
                self.resolved_stream_cache.pop(
                    f"{self.current_item.source}:{self.current_item.id}", None
                )
        self.player_spinner.stop()
        self.player_status.set_label(str(error))
        self.player_status_box.set_visible(True)
        self.toast_overlay.add_toast(Adw.Toast(title=str(error)))
        return GLib.SOURCE_REMOVE

    def _show_player(self) -> None:
        if not self.current_item:
            if self.queue:
                self.queue_index = max(0, self.queue_index)
                self._begin_playback(self.queue[self.queue_index])
            return
        self._set_visible_page("player")
        self.window_title.set_title("Now Playing")
        self.window_title.set_subtitle(self.current_item.source.capitalize())
        self.mini_player.set_visible(False)

    def _set_player_controls_sticky(self, sticky: bool) -> None:
        if (
            not self.mpv_player
            or self.mpv_player.fullscreen_mode
            or self.reparenting_player_controls
        ):
            return
        controls = self.mpv_player.controls
        destination = (
            self.player_controls_host if sticky else self.player_inline_controls_host
        )
        if controls.get_parent() == destination:
            self.player_controls_host.set_visible(sticky)
            return
        self.reparenting_player_controls = True
        try:
            minimum, natural, _minimum_baseline, _natural_baseline = controls.measure(
                Gtk.Orientation.VERTICAL, -1
            )
            controls_height = max(1, controls.get_height(), minimum, natural)
            self._detach_player_controls()
            if sticky:
                self.player_inline_controls_host.set_size_request(-1, controls_height)
                controls.set_valign(Gtk.Align.FILL)
                self.player_controls_host.append(controls)
                self.player_controls_host.set_visible(True)
            else:
                self.player_controls_host.set_visible(False)
                self.player_inline_controls_host.set_size_request(-1, -1)
                controls.set_valign(Gtk.Align.FILL)
                self.player_inline_controls_host.append(controls)
        finally:
            self.reparenting_player_controls = False

    def _detach_player_controls(self) -> None:
        if not self.mpv_player:
            return
        controls = self.mpv_player.controls
        parent = controls.get_parent()
        if parent == self.mpv_player.playback_overlay:
            self.mpv_player.playback_overlay.remove_overlay(controls)
        elif parent == self.player_controls_host:
            self.player_controls_host.remove(controls)
        elif parent == self.player_inline_controls_host:
            self.player_inline_controls_host.remove(controls)

    def _player_scroll_changed(self, adjustment: Gtk.Adjustment) -> None:
        if (
            not self.mpv_player
            or self.mpv_player.fullscreen_mode
            or self.reparenting_player_controls
        ):
            return
        controls_top = max(1, self.player_playback_row.get_height())
        self._set_player_controls_sticky(
            adjustment.get_value() >= controls_top - 1
        )

    def _set_player_controls_fullscreen(self, fullscreen: bool) -> None:
        if not self.mpv_player:
            return
        controls = self.mpv_player.controls
        self.reparenting_player_controls = True
        try:
            self._detach_player_controls()
            self.player_controls_host.set_visible(False)
            if fullscreen:
                controls.set_valign(Gtk.Align.END)
                self.mpv_player.playback_overlay.add_overlay(controls)
                return
            self.player_inline_controls_host.set_size_request(-1, -1)
            controls.set_valign(Gtk.Align.FILL)
            self.player_inline_controls_host.append(controls)
        finally:
            self.reparenting_player_controls = False

        self._player_scroll_changed(self.player_scroller.get_vadjustment())

    def _player_is_fullscreen(self) -> bool:
        return self.is_fullscreen()

    def _toggle_fullscreen(self) -> None:
        if not self.mpv_player:
            return
        if self.is_fullscreen():
            self.unfullscreen()
            minimum, maximum, fraction = self.pre_fullscreen_sidebar_widths
            self.split_view.set_min_sidebar_width(minimum)
            self.split_view.set_max_sidebar_width(maximum)
            self.split_view.set_sidebar_width_fraction(fraction)
            self.sidebar_page.set_visible(True)
            self.sidebar_page.set_opacity(1)
            self.header_overlay.set_visible(True)
            self.header.set_visible(True)
            self.header.set_opacity(1)
            self.player_details.set_visible(True)
            self.player_details.set_opacity(1)
            self.sync_banner.set_visible(True)
            self.fullscreen_title.set_visible(False)
            self.fullscreen_title.set_opacity(0)
            self.mpv_player.set_fullscreen_mode(False)
            self._set_player_controls_fullscreen(False)
            self.fullscreen_comments_button.set_visible(False)
            self.fullscreen_live_chat_button.set_visible(False)
            self._update_transport_buttons()
            return
        self.pre_fullscreen_sidebar_widths = (
            self.split_view.get_min_sidebar_width(),
            self.split_view.get_max_sidebar_width(),
            self.split_view.get_sidebar_width_fraction(),
        )
        self.fullscreen()
        self.sidebar_page.set_visible(False)
        self.header_overlay.set_visible(False)
        self.split_view.set_min_sidebar_width(0)
        self.split_view.set_max_sidebar_width(0)
        self.split_view.set_sidebar_width_fraction(0)
        self.header.set_visible(False)
        self.player_details.set_visible(False)
        self.sync_banner.set_visible(False)
        self.player_scroller.get_vadjustment().set_value(0)
        self._set_player_controls_fullscreen(True)
        self.fullscreen_title.set_visible(True)
        self.fullscreen_title.set_opacity(1)
        self.fullscreen_comments_button.set_visible(
            bool(self.current_item and self.current_item.source == "youtube")
        )
        self.fullscreen_live_chat_button.set_visible(
            bool(
                self._synctube_active()
                and self.current_item
                and self.current_item.source == "youtube"
            )
        )
        self.mpv_player.set_fullscreen_mode(True)
        self._update_transport_buttons()

    def _set_fullscreen_swipe_progress(self, progress: float) -> None:
        if not self.mpv_player or not self.mpv_player.fullscreen_mode:
            return
        progress = max(0.0, min(progress, 1.0))
        if self.fullscreen_title.get_visible():
            self.fullscreen_title.set_opacity(1 - progress * 0.5)

    def _show_seek_feedback(self, seconds: int) -> None:
        if self.seek_feedback_source:
            GLib.source_remove(self.seek_feedback_source)
        forward = seconds > 0
        self.seek_feedback_icon.set_from_icon_name(
            "media-seek-forward-symbolic"
            if forward
            else "media-seek-backward-symbolic"
        )
        self.seek_feedback_label.set_label(
            f"{abs(seconds)} seconds {'forward' if forward else 'back'}"
        )
        self.seek_feedback.set_visible(True)
        self.seek_feedback_source = GLib.timeout_add(850, self._hide_seek_feedback)

    def _hide_seek_feedback(self) -> bool:
        self.seek_feedback.set_visible(False)
        self.seek_feedback_source = 0
        return GLib.SOURCE_REMOVE

    def _set_fullscreen_chrome_visible(self, visible: bool) -> None:
        if self.mpv_player and self.mpv_player.fullscreen_mode:
            self.fullscreen_title.set_visible(visible)
            self.fullscreen_title.set_opacity(1 if visible else 0)
            for button in (
                self.fullscreen_comments_button,
                self.fullscreen_live_chat_button,
            ):
                button.set_opacity(1 if visible else 0)
                button.set_can_target(visible)

    def _player_reveal_finished(
        self, revealer: Gtk.Revealer, _property: GObject.ParamSpec | None
    ) -> None:
        if not revealer.get_reveal_child() or not revealer.get_child_revealed():
            return
        # Relayout can reselect the row which held focus before the player was
        # expanded. Keep navigation events blocked through the reveal, then
        # clear any stale selection once the overlay is fully in place.
        self.navigation.unselect_all()
        self.syncing_navigation = False

    def _leave_player(self) -> None:
        if not self._go_back():
            self._select_page("browse", record=False)

    def _stop_playback(self) -> None:
        self._cancel_pending_playback_load_trace(self.playback_request)
        self.playback_request += 1
        self._detach_playback()
        self.current_item = None
        self.mini_play.set_icon_name("media-playback-start-symbolic")
        self.mini_play.set_tooltip_text("Play")
        if self.queue:
            preview_index = max(0, self.queue_index)
            preview = self.queue[min(preview_index, len(self.queue) - 1)]
            self.mini_title.set_label(preview.title)
            self.mini_subtitle.set_label(f"Queued · {preview.subtitle}")
            self.mini_player.set_visible(True)
        else:
            self.mini_player.set_visible(False)

    def _close_mini_player(self) -> None:
        self._cancel_pending_playback_load_trace(self.playback_request)
        self.playback_request += 1
        self._detach_playback()
        self.current_item = None
        self.mini_play.set_icon_name("media-playback-start-symbolic")
        self.mini_play.set_tooltip_text("Play")
        self.queue.clear()
        self.queue_index = -1
        self._refresh_queue()
        self.mini_player.set_visible(False)
        self._clear_queued_search_results()

    def _detach_playback(self) -> None:
        if self.current_item and self.current_item.source == "jellyfin":
            self._report_jellyfin_playback(
                self.current_item,
                "stop",
                self.last_playback_position,
                self.last_playback_paused,
            )
        if self.mpv_player:
            self.mpv_player.stop()
        if self.played_cache_active:
            self.played_cache_active.close()
            self.played_cache_active = None
        self.played_cache_source = None
        self.played_cache_buffered = None
        self.played_cache_request_item_id = ""

    def _report_jellyfin_playback(
        self,
        item: MediaItem,
        event: str,
        position: float,
        paused: bool,
    ) -> None:
        run_async(
            lambda: self.jellyfin.report_playback(
                item, position, paused, event=event
            ),
            lambda _result: None,
            lambda _error: None,
        )

    def _close_player(self, *_args: object) -> bool:
        self.mpris.close()
        self.prebuffer.close()
        sync_client = self.sync_client
        self.sync_client = None
        jellyfin_sync_client = self.jellyfin_sync_client
        self.jellyfin_sync_client = None
        if self.mpv_player:
            self._detach_playback()
            self.mpv_player.shutdown()
            self.mpv_player = None

        def finish_shutdown() -> None:
            if sync_client:
                sync_client.close()
            if jellyfin_sync_client:
                jellyfin_sync_client.leave()
            if not self.clearing_all_data:
                self.downloads.shutdown()
                self.thumbnails.shutdown()

        threading.Thread(
            target=finish_shutdown, daemon=True, name="tubefin-shutdown"
        ).start()
        return False

    def _capture_navigation_state(self) -> dict[str, Any] | None:
        page = self._visible_page_name()
        if not page:
            return None
        return {
            "page": page,
            "active_navigation": self.active_navigation,
            "title": self.window_title.get_title(),
            "subtitle": self.window_title.get_subtitle(),
            "browse_mode": self.browse_mode,
            "browse_search_results": self.browse_search_results,
            "search_placeholder": self.global_search.get_placeholder_text() or "Search",
        }

    def _set_visible_page(self, name: str, *, record: bool = True) -> None:
        current = self._visible_page_name()
        if record and current and current != name:
            state = self._capture_navigation_state()
            if state:
                self.navigation_history.append(state)
        self.expected_page = name
        player = name == "player"
        self.player_page_active = player
        self.player_expanded = player
        if player:
            self.header_background.add_css_class("player-header-background")
        else:
            self.header_background.remove_css_class("player-header-background")
        playlist = name == "playlist"
        context = name in {
            "browse-category",
            "details",
            "channel",
            "history",
            "playlist",
        }
        if name not in {"home", "browse", "library", "requests"}:
            self.active_navigation = name
            if current != name:
                self.syncing_navigation = True
                self.navigation.unselect_all()
                if not player:
                    self.syncing_navigation = False
        self.context_back.set_visible(
            context
            and (bool(self.navigation_history) or name == "browse-category")
        )
        self.home_refresh.set_visible(name == "home")
        if hasattr(self, "player_header_controls"):
            for control in self.player_header_controls:
                control.set_visible(player)
            if not player:
                self.player_comments_button.set_active(False)
                self.player_live_chat_button.set_active(False)
                self.player_comments_panel.set_visible(False)
                self.player_live_chat_panel.set_visible(False)
            self.player_live_chat_button.set_visible(
                bool(
                    player
                    and self._synctube_active()
                    and self.current_item
                    and self.current_item.source == "youtube"
                )
            )
            self.fullscreen_live_chat_button.set_visible(
                bool(
                    player
                    and self._player_is_fullscreen()
                    and self._synctube_active()
                    and self.current_item
                    and self.current_item.source == "youtube"
                )
            )
            self.fullscreen_comments_button.set_visible(
                bool(
                    player
                    and self._player_is_fullscreen()
                    and self.current_item
                    and self.current_item.source == "youtube"
                )
            )
        if hasattr(self, "playlist_header_controls"):
            for control in self.playlist_header_controls:
                control.set_visible(playlist and not self.remote_playlist_active)
        if hasattr(self, "player_comments_panel"):
            self._update_player_sidebar_layout()
        if player:
            self.header.set_title_widget(self.player_heading)
        elif (context and name not in {"browse-category", "playlist"}) or name == "requests":
            self.header.set_title_widget(self.window_title)
        else:
            self.header.set_title_widget(self.global_search_clamp)
        self.player_revealer.set_reveal_child(player)
        self.player_revealer.set_can_target(player)
        if player and self.player_revealer.get_child_revealed():
            self._player_reveal_finished(self.player_revealer, None)
        if not player:
            self.pages.set_visible_child_name(name)
        self.mini_player.set_visible(
            name != "player" and bool(self.current_item or self.queue)
        )

    def _visible_page_name(self) -> str | None:
        if hasattr(self, "player_revealer") and self.player_revealer.get_reveal_child():
            return "player"
        return self.pages.get_visible_child_name()

    def _go_back(self) -> bool:
        if not self.navigation_history:
            return False
        state = self.navigation_history.pop()
        page = str(state["page"])
        self.active_navigation = str(state["active_navigation"])
        self.browse_mode = str(state["browse_mode"])
        self.browse_search_results = bool(state["browse_search_results"])
        self.browse_category_heading.set_visible(not self.browse_search_results)
        self.global_search.set_placeholder_text(str(state["search_placeholder"]))
        self.window_title.set_title(str(state["title"]))
        self.window_title.set_subtitle(str(state["subtitle"]))
        self._select_navigation_row(self.active_navigation)
        self._set_visible_page(page, record=False)
        return True

    def _context_back_requested(self) -> None:
        if (
            self._visible_page_name() == "browse-category"
            and not self.browse_search_results
        ):
            self._browse_destination_back()
        else:
            self._go_back()

    def _visible_page_changed(self, stack: Gtk.Stack, _property: GObject.ParamSpec) -> None:
        if self.expected_page == "player":
            return
        visible = stack.get_visible_child_name()
        if visible != self.expected_page:
            # Player teardown must not unwind app navigation. Only
            # _set_visible_page is allowed to change it.
            GLib.idle_add(self._restore_expected_page)

    def _restore_expected_page(self) -> bool:
        if self.expected_page == "player":
            self.player_revealer.set_can_target(True)
            self.player_revealer.set_reveal_child(True)
        else:
            self.player_revealer.set_reveal_child(False)
            self.player_revealer.set_can_target(False)
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

    def _player_state_changed(self, position: float, duration: float, paused: bool) -> None:
        now = time.monotonic()
        self.last_playback_position = position
        self.last_playback_duration = duration
        self.last_playback_paused = paused
        if position > 0 and not paused:
            self._mark_playback_load(
                self.playback_request,
                "first_playback",
                only_once=True,
                after="file_loaded",
            )
            self._cache_current_played_video()
        if (
            self.current_item
            and self.queue
            and duration > 0
            and position + 0.5 >= duration
            and now - self.playback_started_at >= 1
            and self.queue_advance_item_id != self.current_item.id
        ):
            self.queue_advance_item_id = self.current_item.id
            GLib.idle_add(
                self._play_next_queued,
                self.player_expanded,
            )
        if hasattr(self, "mini_play"):
            self.mini_play.set_icon_name(
                "media-playback-start-symbolic"
                if paused
                else "media-playback-pause-symbolic"
            )
            self.mini_play.set_tooltip_text("Play" if paused else "Pause")
        manual_segment: SponsorSegment | None = None
        automatically_skipped = False
        if self.current_item and self.current_item.source == "youtube" and self.mpv_player:
            for segment in self.sponsor_segments:
                key = (segment.start, segment.end)
                if key in self.skipped_sponsor_segments or not (
                    segment.start <= position < segment.end
                ):
                    continue
                behavior = self.sponsorblock_categories.get(segment.category, "ignore")
                if behavior == "auto":
                    self.skipped_sponsor_segments.add(key)
                    self.mpv_player.seek_absolute(segment.end)
                    skipped = max(1, round(segment.end - position))
                    category = SPONSORBLOCK_CATEGORY_DETAILS.get(
                        segment.category, ("SponsorBlock segment", "")
                    )[0]
                    self._show_sponsor_skip_toast(skipped, category, position)
                    automatically_skipped = True
                    break
                if behavior == "button":
                    manual_segment = segment
                    break
        if automatically_skipped:
            manual_segment = None
        self._set_manual_sponsor_segment(manual_segment)
        if self.current_item and now - self.last_history_update >= 10:
            self.history.record(self.current_item, position, duration)
            self.last_history_update = now
        if (
            self.current_item
            and self.current_item.source == "jellyfin"
            and (
                now - self.last_jellyfin_update >= 10
                or paused != self.last_reported_pause
            )
        ):
            self._report_jellyfin_playback(
                self.current_item, "progress", position, paused
            )
            self.last_jellyfin_update = now
            self.last_reported_pause = paused
        if (
            self.current_item
            and self.current_item.source == "youtube"
            and self.youtube.browser
            and self.current_item.id not in self.youtube_marked_watched
            and position >= min(30, duration * 0.5 if duration else 30)
        ):
            item = self.current_item
            self.youtube_marked_watched.add(item.id)
            run_async(
                lambda: self.youtube.mark_watched(item),
                lambda _result: None,
                lambda _error: self.youtube_marked_watched.discard(item.id),
            )
        if (
            self.sync_client
            and self.sync_client.room
            and self.current_item
        ):
            with suppress(OSError, ConnectionError):
                self.sync_client.publish(
                    self.current_item.source,
                    self.current_item.id,
                    position,
                    paused,
                )
        if (
            self._jellyfin_syncplay_active()
            and self.current_item
            and self.current_item.source == "jellyfin"
            and time.monotonic() >= self.jellyfin_sync_applying_until
        ):
            self._publish_jellyfin_sync_state(position, paused)

    def _publish_jellyfin_sync_state(self, position: float, paused: bool) -> None:
        client = self.jellyfin_sync_client
        item = self.current_item
        if not client or not item:
            return
        if self.jellyfin_sync_published_item != item.id:
            jellyfin_queue = [value for value in self.queue if value.source == "jellyfin"]
            if item not in jellyfin_queue:
                jellyfin_queue.insert(0, item)
            index = next(
                (number for number, value in enumerate(jellyfin_queue) if value.id == item.id),
                0,
            )
            self.jellyfin_sync_published_item = item.id
            self.jellyfin_sync_published_position = position
            self.jellyfin_sync_published_at = time.monotonic()
            self.jellyfin_sync_published_paused = paused
            run_async(
                lambda: client.set_queue(
                    [value.id for value in jellyfin_queue], index, position
                ),
                lambda _result: None,
                self._jellyfin_sync_error,
            )
            return
        projected = self.jellyfin_sync_published_position
        if not self.jellyfin_sync_published_paused:
            projected += max(0, time.monotonic() - self.jellyfin_sync_published_at)
        pause_changed = paused != self.jellyfin_sync_published_paused
        seeked = abs(position - projected) > 2
        if pause_changed:
            operation = client.pause if paused else client.unpause
            run_async(operation, lambda _result: None, self._jellyfin_sync_error)
        elif seeked:
            run_async(
                lambda: client.seek(position),
                lambda _result: None,
                self._jellyfin_sync_error,
            )
        if pause_changed or seeked:
            self.jellyfin_sync_published_position = position
            self.jellyfin_sync_published_at = time.monotonic()
            self.jellyfin_sync_published_paused = paused

    def _synctube_active(self) -> bool:
        return bool(
            self.sync_client
            and self.sync_client.connected
            and self.sync_client.room
        )

    def _jellyfin_syncplay_active(self) -> bool:
        return bool(
            self.jellyfin_sync_client
            and self.jellyfin_sync_client.connected
            and self.jellyfin_sync_client.group_id
        )

    def _open_watch_together_choice(self, source: str) -> None:
        self.watch_together_popover.popdown()
        if source == "youtube":
            self.open_sync_room()
        else:
            self.open_jellyfin_sync_room()

    def _disconnect_watch_together(self) -> None:
        if self._jellyfin_syncplay_active():
            self._disconnect_jellyfin_syncplay()
        else:
            self._disconnect_synctube()

    def _set_synctube_mode(self, active: bool) -> None:
        if active:
            self.sync_banner.set_title(
                "Connected to SyncTube, Jellyfin streaming is unsupported"
            )
        self.sync_banner.set_revealed(active)
        if active:
            self.syncplay_button.add_css_class("suggested-action")
        else:
            self.syncplay_button.remove_css_class("suggested-action")
        live_chat_supported = bool(
            active
            and self.current_item
            and self.current_item.source == "youtube"
        )
        live_chat_available = bool(
            live_chat_supported
            and self._visible_page_name() == "player"
        )
        self.player_live_chat_button.set_visible(live_chat_available)
        self.fullscreen_live_chat_button.set_visible(
            live_chat_available and self._player_is_fullscreen()
        )
        if not live_chat_supported:
            self.player_live_chat_button.set_active(False)
            self.player_live_chat_panel.set_visible(False)
        if not active:
            self._replace_live_chat([])
        self._refresh_live_chat_controls()
        self.home_jellyfin_button.set_visible(not active)
        self.watch_jellyfin_button.set_sensitive(
            bool(self.jellyfin.session) and not active
        )
        for category in ("movies", "shows"):
            self.browse_buttons[category].set_visible(not active)
        if self._visible_page_name() == "home":
            self.window_title.set_subtitle("YouTube" if active else "YouTube + Jellyfin")
        elif self._visible_page_name() == "browse":
            self.window_title.set_subtitle(
                "YouTube channels"
                if active
                else "Movies, shows, channels, and YouTube"
            )

        was_jellyfin_page = (
            self._visible_page_name() == "browse-category"
            and self.browse_mode in {"movies", "shows"}
        ) or (
            self._visible_page_name() == "details"
            and self.detail_item is not None
            and self.detail_item.source == "jellyfin"
        )
        if active and self.current_item and self.current_item.source == "jellyfin":
            self._close_mini_player()
            was_jellyfin_page = True
        elif active and any(item.source == "jellyfin" for item in self.queue):
            current = self.current_item
            self.queue = [item for item in self.queue if item.source != "jellyfin"]
            self.queue_index = (
                next(
                    (index for index, item in enumerate(self.queue) if item is current),
                    -1,
                )
                if current
                else -1
            )
            self._refresh_queue()
        if active and was_jellyfin_page:
            self._select_page("browse", record=False)
        self._load_home_sections()
        if self._visible_page_name() == "library":
            self._load_library()
        elif self._visible_page_name() == "history":
            self._open_full_history()

    def _set_jellyfin_syncplay_mode(self, active: bool) -> None:
        if active:
            self.sync_banner.set_title(
                "Connected to a Jellyfin watch party, YouTube recommendations are unavailable"
            )
        self.sync_banner.set_revealed(active)
        if active:
            self.watch_together_button.add_css_class("suggested-action")
        else:
            self.watch_together_button.remove_css_class("suggested-action")
        self.watch_youtube_button.set_sensitive(not active)
        self.watch_jellyfin_button.set_sensitive(bool(self.jellyfin.session))
        self.browse_buttons["channels"].set_visible(not active)
        self.home_jellyfin_button.set_visible(bool(self.jellyfin.session))
        if self._visible_page_name() == "home":
            self.window_title.set_subtitle("Jellyfin" if active else "YouTube + Jellyfin")
        elif self._visible_page_name() == "browse":
            self.window_title.set_subtitle(
                "Jellyfin movies and shows"
                if active
                else "Movies, shows, channels, and YouTube"
            )

        was_youtube_page = (
            self._visible_page_name() in {"channel", "playlist"}
        ) or (
            self._visible_page_name() == "browse-category"
            and self.browse_mode in {"youtube", "channels"}
        ) or (
            self._visible_page_name() == "details"
            and self.detail_item is not None
            and self.detail_item.source.startswith("youtube")
        )
        if active and self.current_item and self.current_item.source == "youtube":
            self._close_mini_player()
            was_youtube_page = True
        elif active and any(item.source == "youtube" for item in self.queue):
            current = self.current_item
            self.queue = [item for item in self.queue if item.source != "youtube"]
            self.queue_index = (
                next(
                    (index for index, item in enumerate(self.queue) if item is current),
                    -1,
                )
                if current
                else -1
            )
            self._refresh_queue()
        if active and was_youtube_page:
            self._select_page("browse", record=False)
        self._load_home_sections()
        if self._visible_page_name() == "library":
            self._load_library()
        elif self._visible_page_name() == "history":
            self._open_full_history()

    def open_jellyfin_sync_room(self) -> None:
        if not self.jellyfin.session:
            self.toast_overlay.add_toast(
                Adw.Toast(title="Connect to Jellyfin before opening a watch party")
            )
            return
        if self._synctube_active():
            self.toast_overlay.add_toast(
                Adw.Toast(title="Leave SyncTube before joining a Jellyfin watch party")
            )
            return
        if self.jellyfin_sync_window:
            self.jellyfin_sync_window.present()
            return
        window = Adw.Window(transient_for=self, modal=True, title="Jellyfin watch party")
        window.set_default_size(540, 500)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(
            Adw.HeaderBar(
                title_widget=Adw.WindowTitle(
                    title="Jellyfin SyncPlay", subtitle="Watch Jellyfin together"
                )
            )
        )
        window.set_content(toolbar)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(content, f"set_margin_{side}")(20)
        toolbar.set_content(content)
        description = Gtk.Label(
            label=(
                "Create a room on your Jellyfin server or join one below. "
                "Playback uses Jellyfin's existing SyncPlay infrastructure."
            ),
            xalign=0,
            wrap=True,
        )
        description.add_css_class("dim-label")
        content.append(description)
        name = Gtk.Entry(placeholder_text="New room name")
        content.append(name)
        create = labeled_button("Create room", "list-add-symbolic")
        create.add_css_class("suggested-action")
        content.append(create)
        heading = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        rooms_title = Gtk.Label(label="Available rooms", xalign=0, hexpand=True)
        rooms_title.add_css_class("heading")
        heading.append(rooms_title)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh rooms")
        heading.append(refresh)
        content.append(heading)
        groups = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        groups.add_css_class("boxed-list")
        groups.set_vexpand(True)
        group_scroller = Gtk.ScrolledWindow(vexpand=True)
        group_scroller.set_child(groups)
        content.append(group_scroller)
        status = Gtk.Label(label="Loading rooms…", xalign=0, wrap=True)
        status.add_css_class("dim-label")
        content.append(status)
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        join = labeled_button("Join selected", "network-transmit-receive-symbolic")
        leave = labeled_button("Leave room", "network-offline-symbolic")
        leave.set_visible(self._jellyfin_syncplay_active())
        actions.append(join)
        actions.append(leave)
        content.append(actions)

        def selected(_box: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
            self.jellyfin_sync_selected_group = row.get_name() if row else ""
            join.set_sensitive(
                bool(self.jellyfin_sync_selected_group)
                and not self._jellyfin_syncplay_active()
            )

        def connect(create_room: bool) -> None:
            if self._jellyfin_syncplay_active():
                status.set_label("Leave the current room before joining another one.")
                return
            client = JellyfinSyncPlayClient(
                self.jellyfin,
                lambda message: GLib.idle_add(self._jellyfin_sync_message, message),
            )
            self.jellyfin_sync_client = client
            create.set_sensitive(False)
            join.set_sensitive(False)
            status.set_label("Creating room…" if create_room else "Joining room…")
            operation = (
                (lambda: client.create(name.get_text()))
                if create_room
                else (lambda: client.join(self.jellyfin_sync_selected_group))
            )
            run_async(
                operation,
                lambda group: self._jellyfin_sync_connected(group),
                self._jellyfin_sync_error,
            )

        groups.connect("row-selected", selected)
        create.connect("clicked", lambda *_: connect(True))
        join.connect("clicked", lambda *_: connect(False))
        leave.connect("clicked", lambda *_: self._disconnect_jellyfin_syncplay())
        refresh.connect("clicked", lambda *_: self._refresh_jellyfin_sync_groups())
        join.set_sensitive(False)
        self.jellyfin_sync_window = window
        self.jellyfin_sync_groups = groups
        self.jellyfin_sync_status = status
        self.jellyfin_sync_name = name
        self.jellyfin_sync_create_button = create
        self.jellyfin_sync_join_button = join
        self.jellyfin_sync_leave_button = leave
        window.connect("close-request", self._jellyfin_sync_window_closed)
        self._refresh_jellyfin_sync_groups()
        window.present()

    def _refresh_jellyfin_sync_groups(self) -> None:
        if not self.jellyfin_sync_groups:
            return
        if self.jellyfin_sync_status:
            self.jellyfin_sync_status.set_label("Loading rooms…")
        run_async(
            self.jellyfin.syncplay_groups,
            self._jellyfin_sync_groups_loaded,
            self._jellyfin_sync_error,
        )

    def _jellyfin_sync_groups_loaded(self, groups: list[dict[str, Any]]) -> bool:
        box = self.jellyfin_sync_groups
        if not box:
            return GLib.SOURCE_REMOVE
        self._clear_box(box)
        for group in groups:
            group_id = str(group.get("GroupId") or "")
            participants = [str(value) for value in group.get("Participants") or []]
            row = Adw.ActionRow(
                title=str(group.get("GroupName") or "Unnamed room"),
                subtitle=(
                    f"{len(participants)} member{'s' if len(participants) != 1 else ''}"
                    + (f" · {', '.join(participants)}" if participants else "")
                ),
            )
            row.set_name(group_id)
            if group_id == (
                self.jellyfin_sync_client.group_id
                if self.jellyfin_sync_client
                else ""
            ):
                row.add_suffix(Gtk.Image.new_from_icon_name("object-select-symbolic"))
            box.append(row)
        if not groups:
            box.append(
                Adw.ActionRow(
                    title="No watch parties yet",
                    subtitle="Create one above to start watching together.",
                    selectable=False,
                )
            )
        if self.jellyfin_sync_status:
            if self._jellyfin_syncplay_active():
                group = self.jellyfin_sync_client.group or {}
                self.jellyfin_sync_status.set_label(
                    f"Connected to {group.get('GroupName') or 'Jellyfin room'}. "
                    "Closing this window keeps the room active."
                )
            else:
                self.jellyfin_sync_status.set_label("Choose a room or create a new one.")
        return GLib.SOURCE_REMOVE

    def _jellyfin_sync_connected(self, group: dict[str, Any]) -> bool:
        self._jellyfin_sync_message({"type": "joined", "group": group})
        self._refresh_jellyfin_sync_groups()
        return GLib.SOURCE_REMOVE

    def _jellyfin_sync_error(self, error: Exception) -> bool:
        if self.jellyfin_sync_status:
            self.jellyfin_sync_status.set_label(str(error))
        if self.jellyfin_sync_client and not self.jellyfin_sync_client.group_id:
            self.jellyfin_sync_client.close()
            self.jellyfin_sync_client = None
        if self.jellyfin_sync_create_button:
            self.jellyfin_sync_create_button.set_sensitive(True)
        if self.jellyfin_sync_join_button:
            self.jellyfin_sync_join_button.set_sensitive(
                bool(self.jellyfin_sync_selected_group)
            )
        return GLib.SOURCE_REMOVE

    def _jellyfin_sync_message(self, message: dict[str, Any]) -> bool:
        kind = str(message.get("type") or "")
        if kind in {"joined", "group"}:
            group = message.get("group")
            if isinstance(group, dict) and self.jellyfin_sync_client:
                self.jellyfin_sync_client.group = group
            name = str((group or {}).get("GroupName") or "Jellyfin room")
            self.watch_together_button.set_tooltip_text(f"Connected to {name}")
            self._set_jellyfin_syncplay_mode(True)
            if self.jellyfin_sync_create_button:
                self.jellyfin_sync_create_button.set_sensitive(False)
            if self.jellyfin_sync_join_button:
                self.jellyfin_sync_join_button.set_sensitive(False)
            if self.jellyfin_sync_leave_button:
                self.jellyfin_sync_leave_button.set_visible(True)
            if self.jellyfin_sync_status:
                self.jellyfin_sync_status.set_label(
                    f"Connected to {name}. Closing this window keeps the room active."
                )
        elif kind == "queue":
            self._apply_jellyfin_sync_queue(message.get("queue"))
        elif kind == "command":
            self._apply_jellyfin_sync_command(message.get("command"))
        elif kind == "participant-joined":
            participant = self._jellyfin_participant_name(
                message.get("participant")
            )
            own_name = (
                self.jellyfin.session.username
                if self.jellyfin.session
                else ""
            )
            if not own_name or participant.casefold() != own_name.casefold():
                self._show_party_join_toast(participant, "Jellyfin")
        elif kind == "members-changed":
            self._refresh_jellyfin_sync_groups()
        elif kind in {"left", "disconnected"}:
            if self.jellyfin_sync_status:
                self.jellyfin_sync_status.set_label(
                    str(message.get("message") or "Not connected")
                )
            if self.jellyfin_sync_client:
                self.jellyfin_sync_client.close()
                self.jellyfin_sync_client = None
            self.jellyfin_sync_published_item = ""
            self.jellyfin_sync_playlist_item = ""
            self.watch_together_button.set_tooltip_text("Start or join a watch party")
            self._set_jellyfin_syncplay_mode(False)
            if self.jellyfin_sync_create_button:
                self.jellyfin_sync_create_button.set_sensitive(True)
            if self.jellyfin_sync_leave_button:
                self.jellyfin_sync_leave_button.set_visible(False)
        elif kind == "error":
            text = str(message.get("message") or "Jellyfin SyncPlay error")
            if self.jellyfin_sync_status:
                self.jellyfin_sync_status.set_label(text)
            self.toast_overlay.add_toast(Adw.Toast(title=text))
        return GLib.SOURCE_REMOVE

    @classmethod
    def _jellyfin_participant_name(cls, value: object) -> str:
        if isinstance(value, str):
            return value.strip() or "Someone"
        if isinstance(value, dict):
            for key in ("UserName", "Username", "DisplayName", "Name"):
                name = str(value.get(key) or "").strip()
                if name:
                    return name
            for key in ("User", "Session"):
                nested = value.get(key)
                if nested is not value:
                    name = cls._jellyfin_participant_name(nested)
                    if name != "Someone":
                        return name
        return "Someone"

    def _disconnect_jellyfin_syncplay(self) -> None:
        client = self.jellyfin_sync_client
        self.jellyfin_sync_client = None
        if client:
            run_async(client.leave, lambda _result: None, lambda _error: None)
        self.jellyfin_sync_published_item = ""
        self.jellyfin_sync_playlist_item = ""
        self.watch_together_button.set_tooltip_text("Start or join a watch party")
        self._set_jellyfin_syncplay_mode(False)
        if self.jellyfin_sync_status:
            self.jellyfin_sync_status.set_label("Not connected")
        if self.jellyfin_sync_create_button:
            self.jellyfin_sync_create_button.set_sensitive(True)
        if self.jellyfin_sync_join_button:
            self.jellyfin_sync_join_button.set_sensitive(
                bool(self.jellyfin_sync_selected_group)
            )
        if self.jellyfin_sync_leave_button:
            self.jellyfin_sync_leave_button.set_visible(False)
        self._refresh_jellyfin_sync_groups()

    def _jellyfin_sync_window_closed(self, *_args: object) -> bool:
        self.jellyfin_sync_window = None
        self.jellyfin_sync_groups = None
        self.jellyfin_sync_status = None
        self.jellyfin_sync_name = None
        self.jellyfin_sync_create_button = None
        self.jellyfin_sync_join_button = None
        self.jellyfin_sync_leave_button = None
        self.jellyfin_sync_selected_group = ""
        return False

    def open_sync_room(self) -> None:
        if self._jellyfin_syncplay_active():
            self.toast_overlay.add_toast(
                Adw.Toast(title="Leave the Jellyfin watch party before opening SyncTube")
            )
            return
        if self.sync_window:
            self.sync_window.present()
            return
        window = Adw.Window(transient_for=self, modal=True, title="Watch together")
        window.set_default_size(520, 340)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(
            Adw.HeaderBar(
                title_widget=Adw.WindowTitle(
                    title="SyncTube", subtitle="Watch YouTube together"
                )
            )
        )
        window.set_content(toolbar)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        content.set_margin_top(24)
        content.set_margin_bottom(24)
        content.set_margin_start(24)
        content.set_margin_end(24)
        toolbar.set_content(content)
        explanation = Gtk.Label(
            label=(
                "Create a room or paste a sync-tube.de room link. YouTube playback, pauses, "
                "and seeks stay synchronized through SyncTube."
            ),
            xalign=0,
            wrap=True,
        )
        explanation.add_css_class("dim-label")
        content.append(explanation)
        room = Gtk.Entry(placeholder_text="SyncTube room ID or URL")
        content.append(room)
        status = Gtk.Label(label="Not connected", xalign=0, wrap=True)
        content.append(status)
        members_heading = Gtk.Label(label="Members", xalign=0)
        members_heading.add_css_class("heading")
        content.append(members_heading)
        members = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        members.add_css_class("boxed-list")
        empty_member = Adw.ActionRow(
            title="Nobody here yet",
            subtitle="Create or join a room to see its members.",
        )
        members.append(empty_member)
        content.append(members)
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        create_button = Gtk.Button(
            child=icon_label("Create SyncTube room", "list-add-symbolic")
        )
        join_button = Gtk.Button(
            child=icon_label("Join room", "network-transmit-receive-symbolic")
        )
        disconnect_button = Gtk.Button(
            child=icon_label("Leave room", "network-offline-symbolic")
        )

        def connect(create: bool) -> None:
            client = SyncTubeClient(
                lambda message: GLib.idle_add(self._sync_message, message),
                lambda state, action, value: GLib.idle_add(
                    self._apply_sync_state, state, action, value
                ),
                display_name=self.sync_settings["username"],
                color=self.sync_settings["color"],
            )
            if self.sync_client:
                self.sync_client.close()
            self.sync_client = client
            self._set_sync_connecting("Creating room…" if create else "Joining room…")
            operation = client.create if create else lambda: client.join(room.get_text())
            run_async(
                operation,
                self._synctube_connected,
                self._synctube_connection_error,
            )

        create_button.add_css_class("suggested-action")
        create_button.connect("clicked", lambda *_: connect(True))
        actions.append(create_button)
        join_button.connect("clicked", lambda *_: connect(False))
        actions.append(join_button)
        disconnect_button.connect("clicked", lambda *_: self._disconnect_synctube())
        disconnect_button.set_visible(False)
        actions.append(disconnect_button)
        content.append(actions)
        self.sync_window = window
        self.sync_room_entry = room
        self.sync_status_label = status
        self.sync_members_list = members
        self.sync_create_button = create_button
        self.sync_join_button = join_button
        self.sync_disconnect_button = disconnect_button
        window.connect("close-request", self._sync_window_closed)
        if self.sync_client and self.sync_client.connected and self.sync_client.room:
            room.set_text(f"https://sync-tube.de/room/{self.sync_client.room}")
            status.set_label(
                self._sync_connection_status(
                    self.sync_client.room, self.sync_client.role
                )
            )
            self._render_sync_members(members, list(self.sync_client.members.values()))
            create_button.set_sensitive(False)
            join_button.set_sensitive(False)
            disconnect_button.set_visible(True)
        window.present()

    def _set_sync_connecting(self, message: str) -> None:
        if self.sync_status_label:
            self.sync_status_label.set_label(message)
        if self.sync_create_button:
            self.sync_create_button.set_sensitive(False)
        if self.sync_join_button:
            self.sync_join_button.set_sensitive(False)

    def _synctube_connected(self, room_id: str) -> bool:
        if self.sync_room_entry:
            self.sync_room_entry.set_text(f"https://sync-tube.de/room/{room_id}")
        return GLib.SOURCE_REMOVE

    def _synctube_connection_error(
        self,
        error: Exception,
    ) -> bool:
        if self.sync_status_label:
            self.sync_status_label.set_label(str(error))
        if self.sync_create_button:
            self.sync_create_button.set_sensitive(True)
        if self.sync_join_button:
            self.sync_join_button.set_sensitive(True)
        return GLib.SOURCE_REMOVE

    def _sync_message(self, message: dict[str, Any]) -> bool:
        kind = str(message.get("type") or "")
        if kind in {"created", "joined"}:
            room = str(message.get("room") or "")
            role = str(message.get("role") or "viewer")
            self.sync_role = role
            if self.sync_client:
                self.sync_client.role = role
                self.sync_client.room = room
            if self.sync_room_entry:
                self.sync_room_entry.set_text(f"https://sync-tube.de/room/{room}")
            if self.sync_status_label:
                self.sync_status_label.set_label(
                    self._sync_connection_status(room, role)
                )
            if self.sync_members_list:
                self._render_sync_members(
                    self.sync_members_list, message.get("members")
                )
            self._update_synctube_member_roster(
                message.get("members"), initial=True
            )
            if self.sync_disconnect_button:
                self.sync_disconnect_button.set_visible(True)
            self.syncplay_button.set_tooltip_text(
                f"Connected to SyncTube room {room} as {role}"
            )
            self._replace_live_chat(message.get("chat"))
            self._set_synctube_mode(True)
        elif kind == "members":
            self._update_synctube_member_roster(message.get("members"))
            if self.sync_members_list:
                self._render_sync_members(
                    self.sync_members_list, message.get("members")
                )
        elif (
            kind == "permission"
            and self.sync_client
            and message.get("client_id") == self.sync_client.client_id
        ):
            role = "controller" if message.get("can_control") else "viewer"
            self.sync_client.role = role
            if self.sync_status_label:
                self.sync_status_label.set_label(f"Room permission changed: {role}.")
        elif kind == "room_permission":
            permission = str(message.get("permission") or "room")
            if permission == "chat":
                self._refresh_live_chat_controls()
            if self.sync_status_label:
                state = "allowed" if message.get("allowed") else "disabled"
                self.sync_status_label.set_label(
                    f"Your {permission} permission is now {state}."
                )
        elif kind == "chat_message":
            self._append_live_chat_message(message.get("message"))
        elif kind == "chat_remove":
            self._remove_live_chat_message(message.get("message_id"))
        elif kind == "chat_clear":
            self._replace_live_chat([])
        elif kind in {"error", "disconnected", "room_closed"}:
            if self.sync_status_label:
                self.sync_status_label.set_label(
                    str(message.get("message") or kind.replace("_", " ").title())
                )
            self.syncplay_button.set_tooltip_text("Start or join a watch party")
            if kind in {"disconnected", "room_closed"}:
                self.synctube_known_members.clear()
                self.synctube_roster_initialized = False
                self._set_synctube_mode(False)
        elif kind == "participant_joined":
            client_id = str(message.get("client_id") or "")
            if (
                client_id
                and self.sync_client
                and client_id != self.sync_client.client_id
                and client_id not in self.synctube_known_members
            ):
                self.synctube_known_members.add(client_id)
                self._show_party_join_toast("Someone", "SyncTube")
            if self.sync_status_label:
                self.sync_status_label.set_label(
                    f"Participant joined: {client_id} · "
                    "host may grant control."
                )
        return GLib.SOURCE_REMOVE

    def _update_synctube_member_roster(
        self, values: object, *, initial: bool = False
    ) -> None:
        members = values if isinstance(values, list) else []
        current: dict[str, str] = {}
        for value in members:
            if not isinstance(value, dict):
                continue
            client_id = str(value.get("id") or "")
            if client_id:
                current[client_id] = str(value.get("name") or "Anonymous")
        if (
            not initial
            and self.synctube_roster_initialized
            and self.sync_client
        ):
            for client_id in current.keys() - self.synctube_known_members:
                if client_id != self.sync_client.client_id:
                    self._show_party_join_toast(
                        current[client_id], "SyncTube"
                    )
        self.synctube_known_members = set(current)
        self.synctube_roster_initialized = True

    def _show_party_join_toast(self, name: str, service: str) -> None:
        display_name = name.strip() or "Someone"
        self.toast_overlay.add_toast(
            Adw.Toast(
                title=f"{display_name} joined the {service} watch party"
            )
        )

    def _sync_connection_status(self, room: str, role: str) -> str:
        message = (
            f"Connected to SyncTube room {room} as {role}. "
            "Closing this window keeps the room active."
        )
        if self.sync_client and not self.sync_client.has_permission("play"):
            message += (
                " This room does not allow your group to control playback; "
                "an owner can make you a moderator or enable Viewer Play/Pause."
            )
        return message

    def _disconnect_synctube(self) -> None:
        if self.sync_client:
            self.sync_client.close()
            self.sync_client = None
        self.sync_role = ""
        self.synctube_known_members.clear()
        self.synctube_roster_initialized = False
        self.syncplay_button.set_tooltip_text("Start or join a watch party")
        self._set_synctube_mode(False)
        if self.sync_status_label:
            self.sync_status_label.set_label("Not connected")
        if self.sync_members_list:
            self._clear_box(self.sync_members_list)
            self.sync_members_list.append(
                Adw.ActionRow(
                    title="Nobody here yet",
                    subtitle="Create or join a room to see its members.",
                )
            )
        if self.sync_create_button:
            self.sync_create_button.set_sensitive(True)
        if self.sync_join_button:
            self.sync_join_button.set_sensitive(True)
        if self.sync_disconnect_button:
            self.sync_disconnect_button.set_visible(False)

    def _sync_window_closed(self, *_args: object) -> bool:
        self.sync_window = None
        self.sync_room_entry = None
        self.sync_status_label = None
        self.sync_members_list = None
        self.sync_create_button = None
        self.sync_join_button = None
        self.sync_disconnect_button = None
        return False

    def _render_sync_members(self, box: Gtk.ListBox, values: object) -> None:
        self._clear_box(box)
        members = values if isinstance(values, list) else []
        if not members:
            box.append(Adw.ActionRow(title="No members reported by SyncTube"))
            return
        for value in members:
            if not isinstance(value, dict):
                continue
            client_id = str(value.get("id") or "")
            name = str(value.get("name") or "Anonymous")
            group = int(value.get("group") or 0)
            role = "Owner" if group == 2 else "Moderator" if group == 1 else "Viewer"
            if self.sync_client and client_id == self.sync_client.client_id:
                role += " · You"
                if not self.sync_client.has_permission("play"):
                    role += " · Playback read-only"
            row = Adw.ActionRow(title=name, subtitle=role)
            swatch = Gtk.DrawingArea(width_request=22, height_request=22)
            rgba = Gdk.RGBA()
            if not rgba.parse(str(value.get("color") or "#8f5bd7")):
                rgba.parse("#8f5bd7")
            swatch.set_draw_func(self._draw_sync_member_color, rgba)
            row.add_prefix(swatch)
            if (
                self.sync_client
                and self.sync_client.role == "host"
                and client_id != self.sync_client.client_id
            ):
                control = Gtk.ToggleButton(
                    icon_name="security-high-symbolic",
                    tooltip_text="Allow this member to control playback",
                    valign=Gtk.Align.CENTER,
                )
                control.set_active(group > 0)
                control.connect("toggled", self._sync_member_permission_changed, client_id)
                row.add_suffix(control)
            box.append(row)

    @staticmethod
    def _draw_sync_member_color(
        _area: Gtk.DrawingArea,
        context: object,
        width: int,
        height: int,
        color: Gdk.RGBA,
    ) -> None:
        radius = min(width, height) / 2
        context.set_source_rgba(color.red, color.green, color.blue, color.alpha)
        context.arc(width / 2, height / 2, radius, 0, 2 * 3.14159265)
        context.fill()

    def _sync_member_permission_changed(
        self, button: Gtk.ToggleButton, client_id: str
    ) -> None:
        if self.sync_client:
            self.sync_client.grant(client_id, button.get_active())

    def _apply_sync_state(self, state: RoomState, _action: str, _value: float) -> bool:
        if not self.mpv_player or not state.media_id:
            return GLib.SOURCE_REMOVE
        if not self.current_item or (
            self.current_item.source,
            self.current_item.id,
        ) != (state.media_source, state.media_id):
            item = next(
                (
                    value
                    for value in self.queue
                    if (value.source, value.id) == (state.media_source, state.media_id)
                ),
                MediaItem(
                    id=state.media_id,
                    title="Room media",
                    subtitle="Synchronized playback",
                    source=state.media_source,
                ),
            )
            self._begin_playback(item)
            self.pending_sync_state = state
            return GLib.SOURCE_REMOVE
        target = state.projected_position(
            time.time() + (self.sync_client.offset if self.sync_client else 0)
        )
        drift = target - self.mpv_player.position
        if abs(drift) > 2:
            self.mpv_player.seek_absolute(target)
        elif abs(drift) > 0.15 and not state.paused:
            self.mpv_player.set_speed(1.03 if drift > 0 else 0.97)
        else:
            self.mpv_player.set_speed(1.0)
        self.mpv_player.set_paused(state.paused)
        return GLib.SOURCE_REMOVE

    def _apply_jellyfin_sync_queue(self, value: object) -> None:
        if not isinstance(value, dict):
            return
        playlist = value.get("Playlist") or []
        if not isinstance(playlist, list) or not playlist:
            return
        try:
            index = int(value.get("PlayingItemIndex") or 0)
            entry = playlist[index]
        except (IndexError, TypeError, ValueError):
            return
        if not isinstance(entry, dict):
            return
        item_id = str(entry.get("ItemId") or "")
        self.jellyfin_sync_playlist_item = str(entry.get("PlaylistItemId") or "")
        if not item_id:
            return
        position = float(value.get("StartPositionTicks") or 0) / 10_000_000
        paused = not bool(value.get("IsPlaying"))
        if (
            self.current_item
            and self.current_item.source == "jellyfin"
            and self.current_item.id == item_id
        ):
            self._apply_jellyfin_sync_playback(item_id, position, paused)
            return
        run_async(
            lambda: self.jellyfin.get_item(item_id),
            lambda item: self._jellyfin_sync_item_loaded(item, position, paused),
            self._jellyfin_sync_error,
        )

    def _jellyfin_sync_item_loaded(
        self,
        item: MediaItem,
        position: float,
        paused: bool,
    ) -> bool:
        self.jellyfin_sync_applying_until = time.monotonic() + 4
        self.queue = [item]
        self.queue_index = 0
        self._refresh_queue()
        self._begin_playback(item)
        GLib.timeout_add(
            250,
            self._apply_jellyfin_sync_playback,
            item.id,
            position,
            paused,
        )
        return GLib.SOURCE_REMOVE

    def _apply_jellyfin_sync_playback(
        self,
        item_id: str,
        position: float,
        paused: bool,
        attempts: int = 0,
    ) -> bool:
        if (
            not self.mpv_player
            or not self.current_item
            or self.current_item.id != item_id
        ):
            if attempts < 80 and self._jellyfin_syncplay_active():
                GLib.timeout_add(
                    250,
                    self._apply_jellyfin_sync_playback,
                    item_id,
                    position,
                    paused,
                    attempts + 1,
                )
            return GLib.SOURCE_REMOVE
        self.jellyfin_sync_applying_until = time.monotonic() + 2
        if abs(self.mpv_player.position - position) > 1:
            self.mpv_player.seek_absolute(max(0, position))
        self.mpv_player.set_paused(paused)
        client = self.jellyfin_sync_client
        if client and self.jellyfin_sync_playlist_item:
            run_async(
                lambda: client.ready(
                    self.jellyfin_sync_playlist_item,
                    position,
                    paused,
                ),
                lambda _result: None,
                lambda _error: None,
            )
        return GLib.SOURCE_REMOVE

    def _apply_jellyfin_sync_command(self, value: object) -> None:
        if not isinstance(value, dict):
            return
        delay = 0
        when = str(value.get("When") or "")
        if when:
            with suppress(ValueError):
                target = datetime.fromisoformat(when.replace("Z", "+00:00"))
                delay = max(
                    0,
                    int((target.astimezone(UTC) - datetime.now(UTC)).total_seconds() * 1000),
                )
        GLib.timeout_add(
            max(1, delay),
            self._execute_jellyfin_sync_command,
            value,
            0,
        )

    def _execute_jellyfin_sync_command(
        self,
        value: dict[str, Any],
        attempts: int,
    ) -> bool:
        if not self.mpv_player:
            if attempts < 80 and self._jellyfin_syncplay_active():
                GLib.timeout_add(
                    250,
                    self._execute_jellyfin_sync_command,
                    value,
                    attempts + 1,
                )
            return GLib.SOURCE_REMOVE
        command = str(value.get("Command") or "")
        position = float(value.get("PositionTicks") or 0) / 10_000_000
        self.jellyfin_sync_applying_until = time.monotonic() + 2
        if command in {"Seek", "Unpause"} and value.get("PositionTicks") is not None:
            self.mpv_player.seek_absolute(position)
        if command == "Pause":
            self.mpv_player.set_paused(True)
        elif command == "Unpause":
            self.mpv_player.set_paused(False)
        elif command == "Stop":
            self.mpv_player.set_paused(True)
        return GLib.SOURCE_REMOVE

    def open_connection(self) -> None:
        if self._synctube_active():
            self.toast_overlay.add_toast(
                Adw.Toast(title="Disconnect from SyncTube to use Jellyfin")
            )
            return
        if self.connection_window:
            self.connection_window.present()
            return
        self.connection_window = ConnectionWindow(
            self,
            self._connect_jellyfin,
            self._quick_connect_jellyfin,
        )
        self.connection_window.connect("close-request", self._connection_closed)
        self.connection_window.present()

    def open_settings(self) -> None:
        if self.settings_window:
            self.settings_window.present()
            return
        window = Adw.Window(transient_for=self, modal=True, title="Settings")
        window.set_default_size(680, 700)
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Settings", subtitle="Accounts and playback"))
        toolbar.add_top_bar(header)
        window.set_content(toolbar)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        clamp = Adw.Clamp(maximum_size=620)
        clamp.set_margin_top(24)
        clamp.set_margin_bottom(28)
        clamp.set_margin_start(18)
        clamp.set_margin_end(18)
        scroller.set_child(clamp)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        clamp.set_child(content)

        accounts = Adw.PreferencesGroup(
            title="Accounts",
            description=(
                "TubeFin opens the real service website. YouTube playback can reuse that "
                "browser's session cookies."
            ),
        )
        self.youtube_settings_status = Adw.ActionRow(
            title="YouTube",
            subtitle=self._youtube_connection_status(),
        )
        self.youtube_settings_status.add_prefix(
            Gtk.Image.new_from_icon_name("media-playback-start-symbolic")
        )
        self.youtube_sign_in_button = Gtk.Button(
            label="Open YouTube",
            icon_name="web-browser-symbolic",
            valign=Gtk.Align.CENTER,
        )
        self.youtube_sign_in_button.connect("clicked", lambda *_: self._open_youtube_sign_in())
        self.youtube_settings_status.add_suffix(self.youtube_sign_in_button)
        accounts.add(self.youtube_settings_status)

        jellyfin_status = Adw.ActionRow(
            title="Jellyfin",
            subtitle=(self.jellyfin.session.username if self.jellyfin.session else "Not connected"),
        )
        jellyfin_status.add_prefix(Gtk.Image.new_from_icon_name("network-server-symbolic"))
        jellyfin_action = Gtk.Button(
            label="Disconnect" if self.jellyfin.session else "Connect",
            icon_name=(
                "system-log-out-symbolic" if self.jellyfin.session else "web-browser-symbolic"
            ),
            valign=Gtk.Align.CENTER,
        )
        if self.jellyfin.session:
            jellyfin_action.connect("clicked", lambda *_: self._disconnect_from_settings())
        else:
            jellyfin_action.connect("clicked", lambda *_: self._connect_from_settings())
        jellyfin_status.add_suffix(jellyfin_action)
        if not self._synctube_active():
            accounts.add(jellyfin_status)
        content.append(accounts)

        requests = Adw.PreferencesGroup(
            title="Seerr requests",
            description=(
                "TubeFin checks the Jellyfin host on port 5055 automatically. "
                "Set an address for reverse proxies or a separate Seerr host."
            ),
        )
        seerr_url = Adw.EntryRow(title="Seerr address")
        seerr_url.set_text(self.seerr_settings["url"])
        seerr_url.set_input_purpose(Gtk.InputPurpose.URL)
        requests.add(seerr_url)
        seerr_key = Adw.PasswordEntryRow(title="API key (optional)")
        seerr_key.set_text(self.seerr_settings["api_key"])
        requests.add(seerr_key)
        seerr_save_row = Adw.ActionRow(
            title="Requests tab",
            subtitle="The sidebar tab appears whenever Jellyfin is connected.",
        )
        seerr_save = Gtk.Button(label="Save and check", valign=Gtk.Align.CENTER)
        seerr_save.connect(
            "clicked",
            lambda *_: self._save_seerr_settings(seerr_url, seerr_key, seerr_save),
        )
        seerr_save_row.add_suffix(seerr_save)
        requests.add(seerr_save_row)
        content.append(requests)

        playback = Adw.PreferencesGroup(title="Playback")
        sync_row = Adw.ActionRow(
            title="SyncTube watch room",
            subtitle="Create or join a sync-tube.de room for synchronized YouTube playback.",
        )
        sync = Gtk.Button(label="Open", icon_name="network-transmit-receive-symbolic")
        sync.set_valign(Gtk.Align.CENTER)
        sync.connect("clicked", lambda *_: self._open_sync_from_settings())
        sync_row.add_suffix(sync)
        playback.add(sync_row)

        sync_name = Adw.EntryRow(title="SyncTube username")
        sync_name.set_text(self.sync_settings["username"])
        playback.add(sync_name)
        sync_color_row = Adw.ActionRow(
            title="SyncTube color",
            subtitle="Shown beside your name in a watch room.",
        )
        sync_color = Gtk.ColorButton(valign=Gtk.Align.CENTER)
        rgba = Gdk.RGBA()
        rgba.parse(self.sync_settings["color"])
        sync_color.set_rgba(rgba)
        sync_color_row.add_suffix(sync_color)
        playback.add(sync_color_row)
        sync_name.connect(
            "changed", lambda *_: self._sync_identity_changed(sync_name, sync_color)
        )
        sync_color.connect(
            "color-set", lambda *_: self._sync_identity_changed(sync_name, sync_color)
        )

        captions_language = Adw.EntryRow(
            title="Default closed captions",
        )
        captions_language.set_text(self.default_caption_language)
        captions_language.set_tooltip_text(
            "Enter a language name or code. TubeFin chooses the closest available "
            "track; leave blank to keep captions off."
        )
        captions_language.connect(
            "changed", lambda entry: self._default_caption_language_changed(entry)
        )
        playback.add(captions_language)

        audio_language = Adw.EntryRow(
            title="Preferred dubbed audio",
        )
        audio_language.set_text(self.preferred_audio_language)
        audio_language.set_tooltip_text(
            "Enter a language name or code. TubeFin uses the closest available "
            "YouTube dub; leave blank for the original audio."
        )
        audio_language.connect(
            "changed", lambda entry: self._preferred_audio_language_changed(entry)
        )
        playback.add(audio_language)

        sponsorblock = Adw.SwitchRow(
            title="SponsorBlock",
            subtitle="Choose how each crowd-sourced segment category is handled.",
        )
        sponsorblock.set_active(self.sponsorblock_enabled)
        sponsorblock.connect("notify::active", self._sponsorblock_changed)
        playback.add(sponsorblock)
        self.sponsorblock_rows: dict[str, Adw.ComboRow] = {}
        for category in SPONSORBLOCK_CATEGORIES:
            title, subtitle = SPONSORBLOCK_CATEGORY_DETAILS[category]
            category_row = Adw.ComboRow(title=title, subtitle=subtitle)
            category_row.set_model(Gtk.StringList.new(SPONSORBLOCK_BEHAVIOR_LABELS))
            behavior = self.sponsorblock_categories.get(category, "ignore")
            category_row.set_selected(SPONSORBLOCK_BEHAVIOR_VALUES.index(behavior))
            category_row.set_visible(self.sponsorblock_enabled)
            category_row.connect(
                "notify::selected", self._sponsorblock_category_changed, category
            )
            playback.add(category_row)
            self.sponsorblock_rows[category] = category_row
        content.append(playback)

        self.home_order_group = Adw.PreferencesGroup(
            title="Home sections",
            description=(
                "Drag the rows to choose the shelf order. Shelves can be collapsed "
                "temporarily from Home."
            ),
        )
        self.home_order_rows: list[Adw.ActionRow] = []
        content.append(self.home_order_group)
        self._rebuild_home_order_settings()

        advanced_group = Adw.PreferencesGroup()
        advanced = Adw.ExpanderRow(title="Optional YouTube API access")
        account_page = self._build_account_page()
        advanced.add_row(account_page)
        advanced_group.add(advanced)
        content.append(advanced_group)

        data = Adw.PreferencesGroup(
            title="App data",
            description=(
                "Remove local history, subscriptions, playlists, downloads, accounts, "
                "and cached files."
            ),
        )
        clear_row = Adw.ActionRow(
            title="Clear all app data",
            subtitle="This cannot be undone. TubeFin will close when it is finished.",
        )
        clear_button = Gtk.Button(
            label="Clear…",
            icon_name="user-trash-symbolic",
            valign=Gtk.Align.CENTER,
        )
        clear_button.add_css_class("destructive-action")
        clear_button.connect("clicked", lambda *_: self._confirm_clear_all_data())
        clear_row.add_suffix(clear_button)
        data.add(clear_row)
        content.append(data)
        toolbar.set_content(scroller)
        self.settings_window = window
        window.connect("close-request", self._settings_closed)
        self._refresh_accounts()
        window.present()

    def _rebuild_home_order_settings(self) -> None:
        group = getattr(self, "home_order_group", None)
        if not group:
            return
        for row in getattr(self, "home_order_rows", []):
            group.remove(row)
        self.home_order_rows = []
        for key in self.home_section_order:
            row = Adw.ActionRow(title=HOME_SECTION_TITLES.get(key, key))
            handle = Gtk.Image.new_from_icon_name("list-drag-handle-symbolic")
            handle.add_css_class("dim-label")
            handle.set_tooltip_text("Drag to reorder")
            row.add_prefix(handle)
            drag_source = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
            drag_source.connect("prepare", self._prepare_home_section_drag, key)
            row.add_controller(drag_source)
            drop_target = Gtk.DropTarget.new(
                GObject.TYPE_STRING, Gdk.DragAction.MOVE
            )
            drop_target.set_preload(True)
            drop_target.connect("drop", self._drop_home_section, key)
            row.add_controller(drop_target)
            group.add(row)
            self.home_order_rows.append(row)

    @staticmethod
    def _prepare_home_section_drag(
        _source: Gtk.DragSource,
        _x: float,
        _y: float,
        key: str,
    ) -> Gdk.ContentProvider:
        value = GObject.Value()
        value.init(GObject.TYPE_STRING)
        value.set_string(key)
        return Gdk.ContentProvider.new_for_value(value)

    def _drop_home_section(
        self,
        _target: Gtk.DropTarget,
        source_key: str,
        _x: float,
        _y: float,
        target_key: str,
    ) -> bool:
        try:
            source = self.home_section_order.index(source_key)
            target = self.home_section_order.index(target_key)
        except ValueError:
            return False
        if source == target:
            return True
        section = self.home_section_order.pop(source)
        self.home_section_order.insert(target, section)
        self.config.save_home_section_order(self.home_section_order)
        self._rebuild_home_sections()
        self._rebuild_home_order_settings()
        return True

    def _sync_identity_changed(
        self, username: Adw.EntryRow, color_button: Gtk.ColorButton
    ) -> None:
        color = color_button.get_rgba()
        red = round(color.red * 255)
        green = round(color.green * 255)
        blue = round(color.blue * 255)
        color_value = f"#{red:02x}{green:02x}{blue:02x}"
        self.sync_settings = {
            "username": username.get_text().strip() or "TubeFin guest",
            "color": color_value,
        }
        self.config.save_sync_settings(
            self.sync_settings["username"], self.sync_settings["color"]
        )

    def _save_seerr_settings(
        self,
        url: Adw.EntryRow,
        api_key: Adw.PasswordEntryRow,
        button: Gtk.Button,
    ) -> None:
        self.seerr_settings = {
            "url": url.get_text().strip(),
            "api_key": api_key.get_text().strip(),
        }
        self.config.save_seerr_settings(
            self.seerr_settings["url"], self.seerr_settings["api_key"]
        )
        self.seerr.configure(
            self.seerr_settings["url"], self.seerr_settings["api_key"]
        )
        self.seerr_authenticated = bool(
            self.seerr_settings["api_key"] or self.seerr.has_session
        )
        self.seerr_auto_auth_attempted_url = ""
        button.set_label("Checking…")
        button.set_sensitive(False)
        self._discover_seerr()
        GLib.timeout_add(1200, self._reset_seerr_save_button, button)

    @staticmethod
    def _reset_seerr_save_button(button: Gtk.Button) -> bool:
        button.set_label("Save and check")
        button.set_sensitive(True)
        return GLib.SOURCE_REMOVE

    def _default_caption_language_changed(self, entry: Adw.EntryRow) -> None:
        self.default_caption_language = entry.get_text().strip()
        self.config.save_player_settings(
            default_caption_language=self.default_caption_language
        )
        if self.mpv_player:
            self.mpv_player.default_caption_language = self.default_caption_language

    def _preferred_audio_language_changed(self, entry: Adw.EntryRow) -> None:
        self.preferred_audio_language = entry.get_text().strip()
        self.config.save_player_settings(
            preferred_audio_language=self.preferred_audio_language
        )
        if self.mpv_player:
            self.mpv_player.set_default_audio_language(
                self.preferred_audio_language
            )

    def _sponsorblock_changed(
        self, row: Adw.SwitchRow, _property: GObject.ParamSpec
    ) -> None:
        self.sponsorblock_enabled = row.get_active()
        self.config.save_player_settings(
            sponsorblock_enabled=self.sponsorblock_enabled
        )
        for category_row in getattr(self, "sponsorblock_rows", {}).values():
            category_row.set_visible(self.sponsorblock_enabled)
        self._reload_sponsor_segments()

    def _sponsorblock_category_changed(
        self,
        row: Adw.ComboRow,
        _property: GObject.ParamSpec,
        category: str,
    ) -> None:
        selected = row.get_selected()
        if selected >= len(SPONSORBLOCK_BEHAVIOR_VALUES):
            return
        self.sponsorblock_categories[category] = SPONSORBLOCK_BEHAVIOR_VALUES[selected]
        self.config.save_player_settings(
            sponsorblock_categories=self.sponsorblock_categories
        )
        self._reload_sponsor_segments()

    def _reload_sponsor_segments(self) -> None:
        self.sponsor_segments = []
        self._set_manual_sponsor_segment(None)
        item = self.current_item
        if not self.sponsorblock_enabled or not item or item.source != "youtube":
            return
        categories = tuple(
            category
            for category in SPONSORBLOCK_CATEGORIES
            if self.sponsorblock_categories.get(category) != "ignore"
        )
        if not categories:
            return
        run_async(
            lambda: self.sponsorblock.segments(item.id, categories),
            lambda segments: self._sponsor_segments_loaded(item.id, segments),
            lambda _error: None,
        )

    def _confirm_clear_all_data(self) -> None:
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading="Clear all app data?",
            body=(
                "This permanently removes local history, subscriptions, playlists, "
                "downloads, account details, sign-in tokens, settings, and cached files."
            ),
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("clear", "Clear everything")
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("clear", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", self._clear_all_data_response)
        dialog.present()

    def _clear_all_data_response(self, _dialog: Adw.MessageDialog, response: str) -> None:
        if response != "clear":
            return
        self.clearing_all_data = True
        if self.settings_window:
            self.settings_window.close()
        self._close_player()
        self.toast_overlay.add_toast(Adw.Toast(title="Clearing all app data…"))

        data_root = self.offline.store.directory
        cache_root = self.thumbnails.directory.parent
        config_root = self.config.directory
        account_ids = [account.id for account in self.oauth_accounts]

        def clear() -> None:
            self.downloads.shutdown()
            self.thumbnails.shutdown()
            for account_id in account_ids:
                self.oauth.keyring.delete(account_id)
            for directory in (data_root, cache_root, config_root):
                self._remove_app_directory(directory)

        run_async(clear, self._all_data_cleared, self._all_data_clear_failed)

    @staticmethod
    def _remove_app_directory(directory: Path) -> None:
        # Refuse broad or accidentally substituted paths even though all roots
        # above are generated by TubeFin itself.
        if directory.name != "tubefin" or directory.parent == directory:
            raise ValueError(f"Refusing to clear unexpected path: {directory}")
        if directory.exists():
            shutil.rmtree(directory)

    def _all_data_cleared(self, _result: object) -> bool:
        application = self.get_application()
        if application:
            application.quit()
        return GLib.SOURCE_REMOVE

    def _all_data_clear_failed(self, error: Exception) -> bool:
        self.toast_overlay.add_toast(Adw.Toast(title=f"Could not clear app data: {error}"))
        return GLib.SOURCE_REMOVE

    def _connect_from_settings(self) -> None:
        if self.settings_window:
            self.settings_window.close()
        self.open_connection()

    def _open_youtube_sign_in(self) -> None:
        browser = self.youtube.browser or self._detect_cookie_browser()
        if browser:
            self.youtube.browser = browser
            self.downloads.browser = browser
            self.config.save_youtube_browser(browser)
            self.youtube_browser_session = None
            self.youtube_browser_error = ""
            self.youtube_browser_checking = True
            if hasattr(self, "comment_composer"):
                self._refresh_comment_composer()
            self.youtube_settings_status.set_subtitle(
                f"Waiting for YouTube sign-in in {browser.title()}…"
            )
            self.youtube_sign_in_button.set_sensitive(False)
            self.youtube_sign_in_button.set_label("Checking…")
        else:
            self.youtube_settings_status.set_subtitle(
                "Browser opened; no supported local browser profile was found"
            )
        self._launch_youtube_browser()
        if browser:
            self._verify_youtube_browser_session(wait=True)

    def _verify_youtube_browser_session(self, *, wait: bool = False) -> None:
        self.youtube_browser_checking = True
        operation = (
            self.youtube.wait_for_browser_session if wait else self.youtube.browser_session
        )
        run_async(
            operation,
            self._youtube_browser_connected,
            lambda error: self._youtube_browser_failed(error, notify=wait),
        )

    def _youtube_browser_connected(self, session: YouTubeBrowserSession) -> bool:
        self.youtube_browser_session = session
        self.youtube_browser_checking = False
        self.youtube_browser_error = ""
        if hasattr(self, "youtube_settings_status"):
            self.youtube_settings_status.set_subtitle(self._youtube_connection_status())
        if hasattr(self, "youtube_sign_in_button"):
            self.youtube_sign_in_button.set_sensitive(True)
            self.youtube_sign_in_button.set_label("Open YouTube")
        if hasattr(self, "comment_composer"):
            self._refresh_comment_composer()
        self.toast_overlay.add_toast(
            Adw.Toast(title=f"Signed in to YouTube as {session.display_name}")
        )
        # Preserve the already-rendered Home shelves and scroll position. The
        # newly available personalized feed is appended to that cached view.
        self.home_signed_out.set_visible(False)
        if not self.active_oauth_account:
            self._load_more_recommendations()
        self._sync_online_subscriptions()
        self._sync_online_history()
        return GLib.SOURCE_REMOVE

    def _youtube_browser_failed(self, error: Exception, *, notify: bool) -> bool:
        self.youtube_browser_session = None
        self.youtube_browser_checking = False
        self.youtube_browser_error = str(error)
        if hasattr(self, "youtube_settings_status"):
            self.youtube_settings_status.set_subtitle(self._youtube_connection_status())
        if hasattr(self, "youtube_sign_in_button"):
            self.youtube_sign_in_button.set_sensitive(True)
            self.youtube_sign_in_button.set_label("Try again")
        if hasattr(self, "comment_composer"):
            self._refresh_comment_composer()
        if notify:
            self.toast_overlay.add_toast(Adw.Toast(title=str(error)))
        self.home_signed_out.set_visible(
            not bool(
                self.active_oauth_account
                or (self.jellyfin.session and not self._synctube_active())
            )
        )
        return GLib.SOURCE_REMOVE

    def _youtube_connection_status(self) -> str:
        if self.youtube_browser_session:
            browser = self.youtube_browser_session.browser.title()
            api = " + API access" if self.active_oauth_account else ""
            return f"{self.youtube_browser_session.display_name} · Signed in through {browser}{api}"
        if self.youtube_browser_checking and self.youtube.browser:
            return f"Checking the YouTube session in {self.youtube.browser.title()}…"
        if self.active_oauth_account:
            return f"{self.active_oauth_account.display_name} · API access connected"
        if self.youtube_browser_error and self.youtube.browser:
            return f"Not signed in through {self.youtube.browser.title()}"
        return "Open YouTube and sign in there"

    @staticmethod
    def _detect_cookie_browser() -> str:
        candidates = (
            ("firefox", Path.home() / ".mozilla/firefox"),
            ("chromium", Path.home() / ".config/chromium"),
            ("chrome", Path.home() / ".config/google-chrome"),
            ("brave", Path.home() / ".config/BraveSoftware/Brave-Browser"),
        )
        return next((name for name, path in candidates if path.exists()), "")

    @staticmethod
    def _launch_youtube_browser() -> None:
        # The browser whose cookies yt-dlp can read is not necessarily the
        # user's preferred browser. Let GIO/xdg-desktop-portal resolve the
        # registered HTTPS handler instead of launching the cookie source.
        with suppress(GLib.Error):
            TubeFinWindow._launch_default_uri("https://www.youtube.com/")

    @staticmethod
    def _launch_default_uri(uri: str) -> None:
        Gio.AppInfo.launch_default_for_uri(uri, None)

    def _disconnect_from_settings(self) -> None:
        self.disconnect_jellyfin()
        if self.settings_window:
            self.settings_window.close()

    def _open_sync_from_settings(self) -> None:
        if self.settings_window:
            self.settings_window.close()
        self.open_sync_room()

    def _settings_closed(self, *_args: object) -> bool:
        self.settings_window = None
        return False

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
        self.pending_seerr_credentials = (username, password)
        run_async(
            lambda: self.jellyfin.authenticate(server, username, password),
            self._jellyfin_connected,
            self._jellyfin_connection_error,
        )

    def _jellyfin_connection_error(self, error: Exception) -> None:
        self.pending_seerr_credentials = None
        if self.connection_window:
            self.connection_window.show_error(str(error))

    def _quick_connect_jellyfin(self, server: str, _button: Gtk.Button) -> None:
        self.pending_seerr_credentials = None
        run_async(
            lambda: self.jellyfin.initiate_quick_connect(server),
            self._jellyfin_quick_code,
            lambda error: (
                self.connection_window.show_error(str(error)) if self.connection_window else None
            ),
        )

    def _jellyfin_quick_code(self, result: tuple[str, str, str]) -> bool:
        server, secret, code = result
        if not self.connection_window:
            return GLib.SOURCE_REMOVE
        self.connection_window.show_quick_code(code)
        with suppress(GLib.Error):
            Gio.AppInfo.launch_default_for_uri(f"{server.rstrip('/')}/web/", None)
        run_async(
            lambda: self.jellyfin.complete_quick_connect(server, secret),
            self._jellyfin_connected,
            lambda error: (
                self.connection_window.show_error(str(error)) if self.connection_window else None
            ),
        )
        return GLib.SOURCE_REMOVE

    def _jellyfin_connected(self, session: JellyfinSession) -> bool:
        self.config.save_session(session)
        self.jellyfin_loaded = False
        self._set_account(session)
        self._discover_seerr()
        if self.connection_window:
            self.connection_window.close()
        self.toast_overlay.add_toast(Adw.Toast(title=f"Connected as {session.username}"))
        self.active_navigation = "browse"
        self._select_navigation_row("browse")
        self._open_browse_category("movies")
        return GLib.SOURCE_REMOVE

    def _set_account(self, session: JellyfinSession) -> None:
        self.account_row.set_title(session.username)
        self.account_row.set_subtitle(session.server_url)
        self.disconnect_action.set_enabled(True)
        self.watch_jellyfin_button.set_sensitive(not self._synctube_active())
        self.requests_navigation_row.set_visible(True)

    def disconnect_jellyfin(self) -> None:
        if self._jellyfin_syncplay_active():
            self._disconnect_jellyfin_syncplay()
        self.config.clear_session()
        self.jellyfin.session = None
        self.seerr.clear_session()
        self.seerr_authenticated = bool(self.seerr_settings["api_key"])
        self.seerr_authenticating = False
        self.seerr_auto_auth_attempted_url = ""
        self.pending_seerr_credentials = None
        self._seerr_discovered("")
        if self._visible_page_name() == "requests":
            self._select_page("library")
        self.jellyfin_loaded = False
        self.browse_cache = {
            key: value
            for key, value in self.browse_cache.items()
            if not key.startswith("jellyfin:")
        }
        self.disconnect_action.set_enabled(False)
        self.watch_jellyfin_button.set_sensitive(False)
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
        page = self._visible_page_name()
        control = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        alt = bool(state & Gdk.ModifierType.ALT_MASK)
        if (
            control
            and shift
            and alt
            and keyval in (Gdk.KEY_d, Gdk.KEY_D)
        ):
            self._show_playback_load_window()
            return True
        if (
            keyval == Gdk.KEY_Escape
            and page == "player"
            and (
                self.player_comments_panel.get_visible()
                or self.player_live_chat_panel.get_visible()
            )
        ):
            self._close_player_sidebar()
            return True
        focused = self.get_focus()
        while focused:
            if isinstance(focused, (Gtk.Editable, Gtk.TextView)):
                if (
                    keyval == Gdk.KEY_Escape
                    and page == "player"
                    and self.mpv_player
                ):
                    self.mpv_player.grab_focus()
                    return True
                return False
            focused = focused.get_parent()

        if control and keyval in (Gdk.KEY_f, Gdk.KEY_F, Gdk.KEY_l, Gdk.KEY_L):
            self._select_page("browse")
            self.global_search.grab_focus()
            return True
        if keyval == Gdk.KEY_Escape and page == "player":
            if self._player_is_fullscreen():
                self._toggle_fullscreen()
                return True
            self._leave_player()
            return True
        if page == "player" and self.mpv_player:
            modifiers = state & Gtk.accelerator_get_default_mod_mask()
            if keyval in (Gdk.KEY_c, Gdk.KEY_C) and not modifiers:
                live_chat_available = bool(
                    self._synctube_active()
                    and self.current_item
                    and self.current_item.source == "youtube"
                )
                if live_chat_available:
                    self.player_live_chat_button.set_active(True)
                    self.live_chat_entry.grab_focus()
                    return True
            if keyval in (Gdk.KEY_f, Gdk.KEY_F) and not modifiers:
                self._toggle_fullscreen()
                return True
            if keyval == Gdk.KEY_space:
                self.mpv_player.toggle_pause()
                return True
            if keyval in (Gdk.KEY_Left, Gdk.KEY_h, Gdk.KEY_H):
                if shift:
                    self.mpv_player.seek_chapter(-1)
                elif control:
                    self._play_previous_queued(reveal_player=True)
                else:
                    self.mpv_player.seek_relative(-10)
                return True
            if keyval in (Gdk.KEY_Right, Gdk.KEY_l, Gdk.KEY_L):
                if shift:
                    self.mpv_player.seek_chapter(1)
                elif control:
                    self._play_next_queued(reveal_player=True)
                else:
                    self.mpv_player.seek_relative(10)
                return True
        return False

    def show_shortcuts(self) -> None:
        shortcuts = Gtk.ShortcutsWindow(transient_for=self, modal=True)
        section = Gtk.ShortcutsSection(section_name="general", title="General")
        group = Gtk.ShortcutsGroup(title="Navigation")
        group.add_shortcut(Gtk.ShortcutsShortcut(title="Focus search", accelerator="<Control>F"))
        group.add_shortcut(Gtk.ShortcutsShortcut(title="Leave player", accelerator="Escape"))
        group.add_shortcut(Gtk.ShortcutsShortcut(title="Fullscreen", accelerator="f"))
        group.add_shortcut(Gtk.ShortcutsShortcut(title="Focus live chat", accelerator="c"))
        group.add_shortcut(Gtk.ShortcutsShortcut(title="Seek backward", accelerator="h"))
        group.add_shortcut(Gtk.ShortcutsShortcut(title="Seek forward", accelerator="l"))
        group.add_shortcut(
            Gtk.ShortcutsShortcut(title="Previous video section", accelerator="<Shift>h")
        )
        group.add_shortcut(
            Gtk.ShortcutsShortcut(title="Next video section", accelerator="<Shift>l")
        )
        group.add_shortcut(Gtk.ShortcutsShortcut(title="Play or pause", accelerator="space"))
        group.add_shortcut(Gtk.ShortcutsShortcut(title="Quit", accelerator="<Control>Q"))
        section.add_group(group)
        shortcuts.add_section(section)
        shortcuts.present()


class TubeFinApplication(Adw.Application):
    def __init__(self, application_id: str = APP_ID) -> None:
        super().__init__(application_id=application_id, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.window: TubeFinWindow | None = None

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        self._load_css()
        self._add_action("about", self._show_about)
        self._add_action("quit", lambda *_: self.quit())
        self.set_accels_for_action("app.quit", ["<Control>q"])

    def do_activate(self) -> None:
        display = Gdk.Display.get_default()
        if display:
            icon_path = resources.files("tubefin").joinpath("icons")
            Gtk.IconTheme.get_for_display(display).add_search_path(str(icon_path))
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
