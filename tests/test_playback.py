from __future__ import annotations

import unittest
from pathlib import Path


class PlaybackTests(unittest.TestCase):
    def test_application_uses_mpv_instead_of_gtk_media_file(self) -> None:
        application = Path("src/tubefin/application.py").read_text(encoding="utf-8")
        self.assertIn("MpvPlayer", application)
        self.assertNotIn("Gtk.MediaFile", application)
        self.assertNotIn("Gtk.Video(", application)


if __name__ == "__main__":
    unittest.main()
