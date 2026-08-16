from __future__ import annotations

import argparse
from contextlib import suppress

from tubefin.sync import ThreadingRoomServer


def main() -> None:
    parser = argparse.ArgumentParser(description="TubeFin synchronized playback room server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    arguments = parser.parse_args()
    with ThreadingRoomServer((arguments.host, arguments.port)) as server:
        print(f"TubeFin room server listening on {arguments.host}:{server.server_address[1]}")
        with suppress(KeyboardInterrupt):
            server.serve_forever()


if __name__ == "__main__":
    main()
