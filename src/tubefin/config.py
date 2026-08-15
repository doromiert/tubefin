from __future__ import annotations

import json
import os
import tempfile
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path

from tubefin.models import JellyfinSession


class ConfigStore:
    def __init__(self) -> None:
        config_root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        self.directory = config_root / "tubefin"
        self.path = self.directory / "config.json"

    def load_session(self) -> JellyfinSession | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            session = data.get("jellyfin")
            if not session:
                return None
            return JellyfinSession(**session)
        except (OSError, ValueError, TypeError):
            return None

    def save_session(self, session: JellyfinSession) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = json.dumps({"jellyfin": asdict(session)}, indent=2)
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
        with suppress(FileNotFoundError):
            self.path.unlink()
