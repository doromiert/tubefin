from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

from tubefin.models import MediaItem, ResolvedStream
from tubefin.streaming import PrebufferedStream, PrebufferManager


class PrebufferTests(unittest.TestCase):
    def test_prebuffer_is_capped_at_ten_seconds(self) -> None:
        stream = PrebufferedStream(ResolvedStream("https://media.example/video"), seconds=60)

        self.assertEqual(stream.seconds, 10)
        self.assertLessEqual(stream.max_bytes, 8 << 20)

    def test_manager_bounds_concurrency_and_capacity(self) -> None:
        manager = PrebufferManager(concurrency=2, capacity=3)
        active = 0
        maximum = 0
        lock = threading.Lock()

        def resolve(_item: MediaItem) -> ResolvedStream:
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.03)
            with lock:
                active -= 1
            return ResolvedStream("https://media.example/video")

        try:
            with patch.object(PrebufferedStream, "warm"):
                for index in range(8):
                    manager.offer(MediaItem(str(index), str(index), source="youtube"), resolve)
                for _created, future in list(manager.entries.values()):
                    future.result(timeout=2)

            self.assertLessEqual(maximum, 2)
            self.assertLessEqual(len(manager.entries), 3)
        finally:
            manager.close()


if __name__ == "__main__":
    unittest.main()
