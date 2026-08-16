"""Exercise the main navigation and contextual surfaces on a virtual display."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import traceback


def main() -> int:
    broadway = shutil.which("gtk4-broadwayd")
    if not broadway:
        print("gtk4-broadwayd is unavailable; skipped")
        return 0
    display = f":{os.getpid() % 10000 + 100}"
    server = subprocess.Popen(
        [broadway, display],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    runtime_directory = tempfile.mkdtemp(prefix="tubefin-flow-smoke-")
    try:
        time.sleep(0.5)
        os.environ.update(
            {
                "BROADWAY_DISPLAY": display,
                "GDK_BACKEND": "broadway",
                "XDG_CACHE_HOME": f"{runtime_directory}/cache",
                "XDG_CONFIG_HOME": f"{runtime_directory}/config",
                "XDG_DATA_HOME": f"{runtime_directory}/data",
            }
        )

        import gi

        gi.require_version("Gdk", "4.0")
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gdk, GLib, Gtk

        from tubefin.application import TubeFinApplication
        from tubefin.models import (
            ChannelSubscription,
            Comment,
            CommentPage,
            LocalPlaylist,
            MediaItem,
        )
        from tubefin.sync import SyncTubeClient

        application = TubeFinApplication(f"io.github.doromiert.TubeFin.FlowSmoke{os.getpid()}")
        failures: list[BaseException] = []

        def verify() -> bool:
            window = application.window
            assert window
            assert window.get_size_request() == (360, 300)
            assert window.global_search_clamp.get_maximum_size() == 400
            window.youtube_grid.set_loading("Loading while resizing…")
            window.maximize()
            while GLib.MainContext.default().pending():
                GLib.MainContext.default().iteration(False)
            window.unmaximize()
            assert window.home_refresh
            old_subscriptions_syncing = window.subscriptions_syncing
            old_history_syncing = window.history_syncing
            window.subscriptions_syncing = True
            window.history_syncing = True
            window._update_account_sync_status()
            assert window.account_sync_status.get_visible()
            assert (
                window.account_sync_status.get_tooltip_text()
                == "Syncing subscriptions and history…"
            )
            assert window.account_sync_spinner.get_next_sibling() is None
            window.subscriptions_syncing = old_subscriptions_syncing
            window.history_syncing = old_history_syncing
            window._update_account_sync_status()
            display_backend = Gdk.Display.get_default()
            assert display_backend
            assert Gtk.IconTheme.get_for_display(display_backend).has_icon("compass2-symbolic")
            assert Gtk.IconTheme.get_for_display(display_backend).has_icon("list-add-symbolic")
            assert Gtk.IconTheme.get_for_display(display_backend).has_icon(
                "network-transmit-receive-symbolic"
            )
            assert Gtk.IconTheme.get_for_display(display_backend).has_icon(
                "network-offline-symbolic"
            )
            assert set(window.browse_buttons) == {"movies", "shows", "channels"}
            assert window.pages.get_child_by_name("browse-category") is not None
            assert window.pages.get_child_by_name("downloads") is not None
            assert window.pages.get_child_by_name("playlist") is not None
            navigation_names: set[str] = set()
            navigation_row = window.navigation.get_first_child()
            while navigation_row:
                navigation_names.add(navigation_row.get_name())
                navigation_row = navigation_row.get_next_sibling()
            assert navigation_names == {"home", "browse", "library"}
            assert window.sidebar_download.get_visible()
            assert not window.channel_grid.show_channel
            assert window.channel_subscribe.has_css_class("pill")
            assert window.channel_notifications.get_size_request() == (40, 40)
            enriched_history_item = window._history_display_item(
                MediaItem(
                    "history-video",
                    "History video",
                    source="youtube",
                    payload={"channel_id": "creator"},
                ),
                {
                    "id:creator": ChannelSubscription(
                        "creator",
                        "Creator",
                        "https://youtube.example/@creator",
                        "https://example/avatar.jpg",
                    )
                },
            )
            assert enriched_history_item.subtitle == "Creator"
            assert enriched_history_item.payload["channel_avatar_url"]
            assert (
                window.library_playlist_heading.get_next_sibling()
                == window.playlists_box
            )
            assert (
                window.playlists_box.get_next_sibling()
                == window.library_subscriptions_title
            )
            for button in (
                window.manage_playlists_button,
                window.playlist_options_button,
            ):
                assert button.get_size_request() == (40, 40)
            window._clear_box(window.home_sections)
            window._append_home_section(
                "Responsive cards",
                [MediaItem("responsive", "Responsive", source="youtube")],
            )
            shelf = window.home_sections.get_first_child()
            assert shelf.flow.get_halign() == Gtk.Align.FILL
            shelf_card = shelf.flow.get_first_child().get_child()
            assert shelf_card.get_hexpand()
            window._show_details(
                MediaItem(
                    "from-home",
                    "Opened from Home",
                    source="jellyfin",
                    payload={"Overview": "Details"},
                )
            )
            assert window.pages.get_visible_child_name() == "details"
            assert window.navigation.get_selected_row() is None
            window._context_back_requested()
            assert window.pages.get_visible_child_name() == "home"
            generation = window.recommendation_generation
            window.navigation_history.clear()
            window._set_visible_page("player")
            assert window.active_navigation == "player"
            assert window.navigation.get_selected_row() is None
            assert window.player_revealer.get_transition_duration() == 220
            fullscreen_calls: list[bool] = []
            original_toggle_fullscreen = window._toggle_fullscreen
            window._toggle_fullscreen = lambda: fullscreen_calls.append(True)
            assert window._key_pressed(
                None, Gdk.KEY_f, 0, Gdk.ModifierType(0)
            )
            assert fullscreen_calls == [True]
            key_seeks: list[int] = []
            pause_calls: list[bool] = []
            previous_calls: list[bool] = []
            next_calls: list[bool] = []
            original_seek_relative = window.mpv_player.seek_relative
            original_toggle_pause = window.mpv_player.toggle_pause
            original_previous = window._play_previous_queued
            original_next = window._skip_queued
            window.mpv_player.seek_relative = lambda seconds: key_seeks.append(seconds)
            window.mpv_player.toggle_pause = lambda: pause_calls.append(True)
            window._play_previous_queued = lambda: previous_calls.append(True)
            window._skip_queued = lambda: next_calls.append(True)
            for key in (Gdk.KEY_h, Gdk.KEY_Left, Gdk.KEY_l, Gdk.KEY_Right):
                assert window._key_pressed(None, key, 0, Gdk.ModifierType(0))
            assert key_seeks == [-10, -10, 10, 10]
            assert window._key_pressed(None, Gdk.KEY_space, 0, Gdk.ModifierType(0))
            assert pause_calls == [True]
            assert window._key_pressed(
                None, Gdk.KEY_Left, 0, Gdk.ModifierType.CONTROL_MASK
            )
            assert window._key_pressed(
                None, Gdk.KEY_Right, 0, Gdk.ModifierType.CONTROL_MASK
            )
            assert previous_calls == [True]
            assert next_calls == [True]
            window.mpv_player.seek_relative = original_seek_relative
            window.mpv_player.toggle_pause = original_toggle_pause
            window._play_previous_queued = original_previous
            window._skip_queued = original_next

            class Device:
                def __init__(self, source: Gdk.InputSource) -> None:
                    self.source = source

                def get_source(self) -> Gdk.InputSource:
                    return self.source

            class Gesture:
                def __init__(self, source: Gdk.InputSource) -> None:
                    self.device = Device(source)

                def get_current_event_device(self) -> Device:
                    return self.device

            seeks: list[int] = []
            window.mpv_player.seek_relative = lambda seconds: seeks.append(seconds)
            window.mpv_player.on_fullscreen = lambda: fullscreen_calls.append(True)
            window.mpv_player._video_clicked(
                Gesture(Gdk.InputSource.TOUCHSCREEN), 2, 0, 0
            )
            window.mpv_player._video_clicked(
                Gesture(Gdk.InputSource.TOUCHSCREEN), 2, 1_000_000, 0
            )
            assert window.seek_feedback.get_visible()
            assert window.seek_feedback_label.get_label() == "10 seconds forward"
            window.mpv_player._video_clicked(
                Gesture(Gdk.InputSource.MOUSE), 2, 0, 0
            )
            assert seeks == [-10, 10]
            assert fullscreen_calls == [True, True]
            window._toggle_fullscreen = original_toggle_fullscreen
            assert window._go_back()
            assert window.pages.get_visible_child_name() == "home"
            assert window.navigation.get_selected_row().get_name() == "home"
            assert window._key_pressed(
                None, Gdk.KEY_f, 0, Gdk.ModifierType.CONTROL_MASK
            )
            assert window._visible_page_name() == "browse"
            window._select_page("home")
            window._select_page("browse")
            window._select_page("home")
            assert window.recommendation_generation == generation
            window.youtube_grid.on_preview = None
            window._youtube_results(
                [
                    MediaItem(
                        "video",
                        "A very long title that must be clipped inside the fixed video card " * 4,
                        subtitle="Creator",
                        source="youtube",
                    )
                ]
            )
            flow_child = window.youtube_grid.flow.get_first_child()
            card = flow_child.get_child()
            self_width = card.measure(Gtk.Orientation.HORIZONTAL, -1)
            self_height = card.measure(Gtk.Orientation.VERTICAL, -1)
            assert self_width[:2] == (271, 271), self_width
            assert self_height[:2] == (235, 235), self_height
            card_actions = card.get_first_child().get_last_child().get_last_child()
            assert isinstance(card_actions.get_popover().get_child(), Gtk.Stack)
            window._select_page("browse")
            window._select_page("downloads")
            assert window.pages.get_visible_child_name() == "downloads"
            window._select_page("library")
            window.subscriptions.list = lambda: [
                ChannelSubscription(
                    "channel-id",
                    "Subscribed channel",
                    "https://youtube.example/channel-id",
                )
            ]
            channel_loads: list[int] = []
            original_load_channel = window._load_channel
            window._load_channel = lambda page: channel_loads.append(page)
            window._load_subscriptions()
            window._open_subscription_channel(
                window.subscription_lookup["channel-id"].url
            )
            assert window.channel_url == "https://youtube.example/channel-id"
            assert channel_loads == [1]
            window._load_channel = original_load_channel
            window.manage_playlists_button.set_active(True)
            assert window.new_playlist_name.grab_focus()
            window.manage_playlists_button.set_active(False)
            playlist = LocalPlaylist(
                "playlist",
                "Drag test",
                [MediaItem("video", "Playlist item", source="youtube")],
            )
            window.playlists.list = lambda: [playlist]
            window._load_playlists()
            playlist_button = window.playlists_box.get_first_child().get_child()
            playlist_card = playlist_button.get_child()
            playlist_aspect = playlist_card.get_first_child()
            assert playlist_aspect.get_size_request() == (270, 152)
            window._show_playlist("playlist")
            playlist_row = window.playlist_items.get_first_child().get_child()
            playlist_thumbnail = playlist_row.get_first_child().get_next_sibling()
            playlist_thumbnail = playlist_thumbnail.get_next_sibling()
            assert playlist_thumbnail.get_size_request() == (270, 152)
            assert window.header.get_title_widget() != window.playlist_title
            assert window.context_back.get_visible()
            window._save_item(MediaItem("save", "Choose a playlist", source="youtube"))
            assert window.playlist_picker
            window.playlist_picker.close()
            assert window._prepare_playlist_drag(None, 0, 0, "playlist", 0)
            window.playlist_options_button.set_active(True)
            assert window.playlist_name_entry.grab_focus()
            window.playlist_options_button.set_active(False)
            window._select_page("library")
            window._show_details(
                MediaItem(
                    "movie",
                    "Jellyfin details test",
                    source="jellyfin",
                    payload={"Overview": "Details"},
                )
            )
            assert window.header.get_title_widget() == window.window_title
            assert window.context_back.get_visible()
            assert len(window.player_header_controls) == 3
            assert window.player_comments_panel
            window._clear_box(window.comments_box)
            window._comments_loaded(
                CommentPage(
                    [
                        Comment(
                            "comment",
                            "Viewer",
                            "Top-level comment",
                            replies=[Comment("reply", "Creator", "A reply")],
                        )
                    ]
                )
            )
            comment_card = window.comments_box.get_first_child()
            comment_button = comment_card.get_first_child()
            replies = comment_button.get_next_sibling()
            assert not replies.get_visible()
            comment_button.emit("clicked")
            assert replies.get_visible()
            window._clear_box(window.comments_box)
            window._comments_loaded(
                CommentPage([Comment("no-replies", "Viewer", "Another comment")])
            )
            comment_card = window.comments_box.get_first_child()
            comment_button = comment_card.get_first_child()
            replies = comment_button.get_next_sibling()
            comment_button.emit("clicked")
            assert replies.get_visible()
            assert replies.get_first_child().get_label() == "No replies to this comment yet."
            assert comment_button.get_child().get_last_child().get_first_child().get_label() == (
                "Hide replies"
            )
            comment_loads: list[bool] = []
            original_load_comments = window._load_comments
            window._load_comments = lambda: comment_loads.append(True)
            window.comment_cursor = "20"
            window.player_comments_panel.set_visible(True)

            class NearBottom:
                @staticmethod
                def get_value() -> float:
                    return 800

                @staticmethod
                def get_page_size() -> float:
                    return 400

                @staticmethod
                def get_upper() -> float:
                    return 1250

            window._comments_scroll_changed(NearBottom())
            assert comment_loads == [True]
            window._load_comments = original_load_comments
            window.history.list = lambda _limit=50: []
            window._open_full_history()
            assert window.pages.get_visible_child_name() == "history"
            assert window.context_back.get_visible()
            previous = MediaItem("previous", "Previous", source="youtube")
            queued = MediaItem("queued", "Queued", source="youtube")
            window.current_item = previous
            window.queue = []
            window.queue_index = -1
            window._begin_playback = lambda _item: None
            window._play_selected(queued)
            assert [item.id for item in window.queue] == ["previous", "queued"]
            assert window.queue_index == 1
            window._set_visible_page("channel")
            assert window.mini_player.get_visible()
            assert window.mini_previous.get_visible()
            assert not window.mini_next.get_visible()
            assert window.pages.get_visible_child_name() == "channel"
            home_row = window.navigation.get_row_at_index(0)
            for destination in ("home", "library", "downloads"):
                window._select_page(destination, record=False)
                window._show_player()
                assert window._visible_page_name() == "player"
                assert window.syncing_navigation
                # GTK may report the formerly focused row again while the
                # player reveal changes the layout. It must not navigate.
                window._navigation_changed(window.navigation, home_row)
                assert window._visible_page_name() == "player"
            window._set_visible_page("channel", record=False)
            window._show_player()
            assert window._visible_page_name() == "player"
            assert window.player_revealer.get_reveal_child()
            assert window.player_revealer.get_can_target()
            assert window.pages.get_visible_child_name() == "channel"
            assert not window.mini_player.get_visible()
            assert window.mpv_player.previous_button.get_visible()
            assert not window.mpv_player.next_button.get_visible()
            window._set_visible_page("channel")
            assert not window.player_revealer.get_can_target()
            window._close_mini_player()
            assert not window.queue
            assert window.current_item is None
            assert not window.mini_player.get_visible()
            window.open_settings()
            assert window.settings_window
            window.settings_window.close()
            sync_client = SyncTubeClient(lambda _message: None, lambda *_args: None)
            sync_client.connected = True
            sync_client.room = "persistent-room"
            sync_client.role = "controller"
            sync_client.group = 1
            window.sync_client = sync_client
            window.open_sync_room()
            assert window.sync_window
            window._sync_message(
                {
                    "type": "joined",
                    "room": "persistent-room",
                    "role": "controller",
                    "members": [],
                }
            )
            assert window.sync_banner.get_revealed()
            original_is_fullscreen = window.is_fullscreen
            window.is_fullscreen = lambda: False
            window._toggle_fullscreen()
            assert not window.sync_banner.get_visible()
            window.is_fullscreen = lambda: True
            window._toggle_fullscreen()
            assert window.sync_banner.get_visible()
            window.is_fullscreen = original_is_fullscreen
            assert window.syncplay_button.has_css_class("suggested-action")
            assert not window.home_jellyfin_button.get_visible()
            assert not window.browse_buttons["movies"].get_visible()
            assert not window.browse_buttons["shows"].get_visible()
            window.sync_window.close()
            assert window.sync_client is sync_client
            assert sync_client.connected
            window._disconnect_synctube()
            assert not window.sync_banner.get_revealed()
            assert not window.syncplay_button.has_css_class("suggested-action")
            assert window.home_jellyfin_button.get_visible()
            application.quit()
            return GLib.SOURCE_REMOVE

        def exercise() -> bool:
            try:
                return verify()
            except BaseException as error:
                failures.append(error)
                traceback.print_exc()
                application.quit()
                return GLib.SOURCE_REMOVE

        GLib.timeout_add(250, exercise)
        result = application.run([])
        if failures:
            return 1
        print("GTK flow smoke test passed")
        return result
    finally:
        server.terminate()
        server.wait(timeout=5)
        shutil.rmtree(runtime_directory, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
