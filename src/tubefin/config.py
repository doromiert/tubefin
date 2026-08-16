from __future__ import annotations

import json
import os
import tempfile
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path

from tubefin.models import JellyfinSession, OAuthAccount

SPONSORBLOCK_CATEGORIES = (
    "sponsor",
    "selfpromo",
    "interaction",
    "intro",
    "outro",
    "preview",
    "hook",
    "music_offtopic",
    "filler",
)
SPONSORBLOCK_BEHAVIORS = {"auto", "button", "ignore"}
SPONSORBLOCK_DEFAULTS = {
    category: "auto" if category == "sponsor" else "ignore"
    for category in SPONSORBLOCK_CATEGORIES
}
HOME_SECTION_ORDER = (
    "local_history",
    "offline",
    "jellyfin_continue",
    "jellyfin_recent",
    "youtube_activity",
    "recommendations",
    "watched_channels",
)


class ConfigStore:
    def __init__(self) -> None:
        config_root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        self.directory = config_root / "tubefin"
        self.path = self.directory / "config.json"

    def load_session(self) -> JellyfinSession | None:
        try:
            data = self._load()
            session = data.get("jellyfin")
            if not session:
                return None
            return JellyfinSession(**session)
        except (OSError, ValueError, TypeError):
            return None

    def save_session(self, session: JellyfinSession) -> None:
        payload = self._load()
        payload["jellyfin"] = asdict(session)
        self._save(payload)

    def load_home_section_order(self) -> list[str]:
        home = self._load().get("home") or {}
        saved = home.get("section_order") if isinstance(home, dict) else None
        order: list[str] = []
        if isinstance(saved, list):
            order.extend(
                key for key in saved if key in HOME_SECTION_ORDER and key not in order
            )
        order.extend(key for key in HOME_SECTION_ORDER if key not in order)
        return order

    def save_home_section_order(self, order: list[str]) -> None:
        payload = self._load()
        home = payload.setdefault("home", {})
        if not isinstance(home, dict):
            home = {}
            payload["home"] = home
        normalized = [
            key for key in order if key in HOME_SECTION_ORDER
        ]
        normalized.extend(key for key in HOME_SECTION_ORDER if key not in normalized)
        home["section_order"] = normalized
        self._save(payload)

    def load_player_settings(self) -> dict[str, object]:
        player = self._load().get("player") or {}
        try:
            buffer_seconds = max(5, min(300, int(player.get("buffer_seconds", 20))))
        except (TypeError, ValueError):
            buffer_seconds = 20
        try:
            saved_prefetch = max(
                0, min(1024, int(player.get("home_prefetch_mib", 160)))
            )
            home_prefetch_mib = (
                max(32, saved_prefetch) if saved_prefetch else 0
            )
        except (TypeError, ValueError):
            home_prefetch_mib = 160
        saved_categories = player.get("sponsorblock_categories")
        category_behaviors = dict(SPONSORBLOCK_DEFAULTS)
        if isinstance(saved_categories, dict):
            for category in SPONSORBLOCK_CATEGORIES:
                behavior = str(saved_categories.get(category) or "")
                if behavior in SPONSORBLOCK_BEHAVIORS:
                    category_behaviors[category] = behavior
        return {
            "buffer_seconds": buffer_seconds,
            "home_prefetch_mib": home_prefetch_mib,
            "default_caption_language": str(
                player.get("default_caption_language") or ""
            ).strip(),
            "preferred_audio_language": str(
                player.get("preferred_audio_language") or ""
            ).strip(),
            "sponsorblock_enabled": bool(player.get("sponsorblock_enabled", True)),
            "sponsorblock_categories": category_behaviors,
        }

    def save_player_settings(
        self,
        *,
        buffer_seconds: int | None = None,
        home_prefetch_mib: int | None = None,
        default_caption_language: str | None = None,
        preferred_audio_language: str | None = None,
        sponsorblock_enabled: bool | None = None,
        sponsorblock_categories: dict[str, str] | None = None,
    ) -> None:
        payload = self._load()
        player = payload.setdefault("player", {})
        if not isinstance(player, dict):
            player = {}
            payload["player"] = player
        if buffer_seconds is not None:
            player["buffer_seconds"] = max(5, min(300, buffer_seconds))
        if home_prefetch_mib is not None:
            normalized = max(0, min(1024, home_prefetch_mib))
            player["home_prefetch_mib"] = (
                max(32, normalized) if normalized else 0
            )
        if default_caption_language is not None:
            player["default_caption_language"] = default_caption_language.strip()
        if preferred_audio_language is not None:
            player["preferred_audio_language"] = preferred_audio_language.strip()
        if sponsorblock_enabled is not None:
            player["sponsorblock_enabled"] = sponsorblock_enabled
        if sponsorblock_categories is not None:
            player["sponsorblock_categories"] = {
                category: behavior
                for category, behavior in sponsorblock_categories.items()
                if category in SPONSORBLOCK_CATEGORIES
                and behavior in SPONSORBLOCK_BEHAVIORS
            }
        self._save(payload)

    def load_sync_settings(self) -> dict[str, str]:
        sync = self._load().get("synctube") or {}
        if not isinstance(sync, dict):
            sync = {}
        username = str(sync.get("username") or "").strip() or "TubeFin guest"
        color = str(sync.get("color") or "").strip()
        if not self._valid_color(color):
            color = "#8f5bd7"
        return {"username": username, "color": color.lower()}

    def save_sync_settings(self, username: str, color: str) -> None:
        username = username.strip() or "TubeFin guest"
        color = color.strip()
        if not self._valid_color(color):
            color = "#8f5bd7"
        payload = self._load()
        payload["synctube"] = {"username": username, "color": color.lower()}
        self._save(payload)

    def load_seerr_settings(self) -> dict[str, str]:
        value = self._load().get("seerr") or {}
        if not isinstance(value, dict):
            value = {}
        return {
            "url": str(value.get("url") or "").strip(),
            "api_key": str(value.get("api_key") or "").strip(),
        }

    def save_seerr_settings(self, url: str, api_key: str) -> None:
        payload = self._load()
        payload["seerr"] = {
            "url": url.strip().rstrip("/"),
            "api_key": api_key.strip(),
        }
        self._save(payload)

    @staticmethod
    def _valid_color(color: str) -> bool:
        if len(color) != 7 or not color.startswith("#"):
            return False
        try:
            int(color[1:], 16)
        except ValueError:
            return False
        return True

    def clear_all(self) -> None:
        with suppress(FileNotFoundError):
            self.path.unlink()
        with suppress(OSError):
            self.directory.rmdir()

    def load_oauth_settings(self) -> dict[str, object]:
        value = self._load().get("youtube") or {}
        if not isinstance(value, dict):
            value = {}
        accounts: list[OAuthAccount] = []
        for account in value.get("accounts") or []:
            try:
                accounts.append(OAuthAccount(**account))
            except (TypeError, ValueError):
                continue
        active_id = str(value.get("active_account_id") or "")
        return {
            "client_id": str(value.get("client_id") or ""),
            "browser": str(value.get("browser") or ""),
            "accounts": accounts,
            "active_account_id": active_id,
        }

    def save_youtube_browser(self, browser: str) -> None:
        payload = self._load()
        youtube = payload.setdefault("youtube", {})
        if not isinstance(youtube, dict):
            youtube = {}
            payload["youtube"] = youtube
        youtube["browser"] = browser.strip()
        self._save(payload)

    def save_oauth_client_id(self, client_id: str) -> None:
        payload = self._load()
        youtube = payload.setdefault("youtube", {})
        if not isinstance(youtube, dict):
            youtube = {}
            payload["youtube"] = youtube
        youtube["client_id"] = client_id.strip()
        self._save(payload)

    def save_oauth_account(self, account: OAuthAccount, *, active: bool = True) -> None:
        payload = self._load()
        youtube = payload.setdefault("youtube", {})
        if not isinstance(youtube, dict):
            youtube = {}
            payload["youtube"] = youtube
        accounts = [
            value
            for value in youtube.get("accounts") or []
            if isinstance(value, dict) and value.get("id") != account.id
        ]
        accounts.append(asdict(account))
        youtube["accounts"] = accounts
        if active:
            youtube["active_account_id"] = account.id
        self._save(payload)

    def set_active_oauth_account(self, account_id: str) -> None:
        payload = self._load()
        youtube = payload.get("youtube")
        if isinstance(youtube, dict):
            youtube["active_account_id"] = account_id
            self._save(payload)

    def remove_oauth_account(self, account_id: str) -> None:
        payload = self._load()
        youtube = payload.get("youtube")
        if not isinstance(youtube, dict):
            return
        youtube["accounts"] = [
            value
            for value in youtube.get("accounts") or []
            if isinstance(value, dict) and value.get("id") != account_id
        ]
        if youtube.get("active_account_id") == account_id:
            youtube["active_account_id"] = ""
        self._save(payload)

    def _load(self) -> dict[str, object]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict[str, object]) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = json.dumps(data, indent=2)
        descriptor, temp_name = tempfile.mkstemp(prefix="config-", dir=self.directory)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.write("\n")
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def clear_session(self) -> None:
        payload = self._load()
        payload.pop("jellyfin", None)
        if payload:
            self._save(payload)
        else:
            with suppress(FileNotFoundError):
                self.path.unlink()
