"""Start the real GTK application briefly on Broadway and fail on startup errors."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time


def main() -> int:
    broadway = shutil.which("gtk4-broadwayd")
    if not broadway:
        print("gtk4-broadwayd is unavailable; skipped")
        return 0
    display = f":{os.getpid() % 10000 + 100}"
    server = subprocess.Popen(
        [broadway, display],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    runtime_directory = tempfile.mkdtemp(prefix="tubefin-startup-smoke-")
    try:
        time.sleep(0.5)
        environment = {
            **os.environ,
            "BROADWAY_DISPLAY": display,
            "GDK_BACKEND": "broadway",
            "XDG_CACHE_HOME": f"{runtime_directory}/cache",
            "XDG_CONFIG_HOME": f"{runtime_directory}/config",
            "XDG_DATA_HOME": f"{runtime_directory}/data",
        }
        executable = sys.argv[1] if len(sys.argv) > 1 else None
        command = (
            [executable]
            if executable
            else [
                sys.executable,
                "-c",
                (
                    "import os; from tubefin.application import TubeFinApplication; "
                    "app = TubeFinApplication("
                    "f'io.github.doromiert.TubeFin.StartupSmoke{os.getpid()}'); "
                    "raise SystemExit(app.run([]))"
                ),
            ]
        )
        application = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        try:
            stdout, stderr = application.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            application.terminate()
            stdout, stderr = application.communicate(timeout=5)
            if application.returncode not in {-15, 0}:
                print(stdout)
                print(stderr, file=sys.stderr)
                return 1
            print("GTK startup smoke test passed")
            return 0
        print(stdout)
        print(stderr, file=sys.stderr)
        return application.returncode or 1
    finally:
        server.terminate()
        server.wait(timeout=5)
        shutil.rmtree(runtime_directory, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
