"""KeePassXC's authenticated, encrypted native-messaging browser protocol.

Protocol: https://github.com/keepassxreboot/keepassxc-browser/blob/develop/keepassxc-protocol.md
Only the public association identifier is persisted; passwords stay in memory.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import select
import shutil
import struct
import subprocess
import tempfile
import time
from pathlib import Path

from nacl.exceptions import CryptoError
from nacl.public import Box, PrivateKey, PublicKey


class KeePassXCError(Exception):
    pass


def encoded(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


class KeePassXC:
    def __init__(self, association_file: Path, executable: str | None = None):
        self.association_file = association_file
        self.executable = executable or shutil.which("keepassxc-proxy")

    @staticmethod
    def _read(pipe, count: int, deadline: float) -> bytes:
        value = bytearray()
        while len(value) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([pipe], [], [], remaining)[0]:
                raise KeePassXCError("KeePassXC timed out. Unlock the database and try again.")
            chunk = os.read(pipe.fileno(), count - len(value))
            if not chunk:
                raise KeePassXCError(
                    "Open KeePassXC and enable Browser Integration, then try again."
                )
            value.extend(chunk)
        return bytes(value)

    def _exchange(self, process, request: dict) -> dict:
        payload = json.dumps(request).encode("utf-8")
        process.stdin.write(struct.pack("=I", len(payload)) + payload)
        process.stdin.flush()
        deadline = time.monotonic() + 90
        while True:
            length = struct.unpack("=I", self._read(process.stdout, 4, deadline))[0]
            if length > 4 * 1024 * 1024:
                raise KeePassXCError("KeePassXC returned an oversized response.")
            response = json.loads(self._read(process.stdout, length, deadline))
            if response.get("action") == request["action"]:
                return response

    def _request(self, process, box: Box, client_id: str, action: str, **fields) -> dict:
        nonce = secrets.token_bytes(Box.NONCE_SIZE)
        message = {"action": action, **fields}
        response = self._exchange(
            process,
            {
                "action": action,
                "clientID": client_id,
                "nonce": encoded(nonce),
                "message": encoded(box.encrypt(json.dumps(message).encode(), nonce).ciphertext),
            },
        )
        if "message" not in response:
            raise KeePassXCError(
                "KeePassXC did not grant access. Unlock and approve TubeFin there."
            )
        try:
            response_nonce = base64.b64decode(response["nonce"], validate=True)
            expected = ((int.from_bytes(nonce, "little") + 1) % (1 << 192)).to_bytes(24, "little")
            if response_nonce != expected:
                raise KeePassXCError("KeePassXC returned an unexpected response nonce.")
            result = json.loads(
                box.decrypt(
                    base64.b64decode(response["message"], validate=True),
                    response_nonce,
                )
            )
        except (CryptoError, ValueError, KeyError) as error:
            raise KeePassXCError("Could not authenticate the KeePassXC response.") from error
        if result.get("nonce") != encoded(response_nonce) or result.get("success") != "true":
            raise KeePassXCError("KeePassXC did not grant access to this login.")
        return result

    def logins(self, url: str) -> list[dict]:
        # The embedded login never asks for entries for arbitrary navigated sites.
        from urllib.parse import urlsplit

        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "accounts.google.com"
            or parsed.port not in (None, 443)
        ):
            raise KeePassXCError(
                "KeePassXC autofill is available only on Google's secure sign-in page."
            )
        if not self.executable:
            raise KeePassXCError(
                "Install KeePassXC and enable Browser Integration to use autofill."
            )
        with subprocess.Popen(
            [self.executable],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        ) as process:
            try:
                key = PrivateKey.generate()
                client_id = encoded(secrets.token_bytes(24))
                response = self._exchange(
                    process,
                    {
                        "action": "change-public-keys",
                        "publicKey": encoded(bytes(key.public_key)),
                        "nonce": encoded(secrets.token_bytes(24)),
                        "clientID": client_id,
                    },
                )
                if response.get("success") != "true":
                    raise KeePassXCError(
                        "Could not connect to KeePassXC. Enable Browser Integration."
                    )
                box = Box(key, PublicKey(base64.b64decode(response["publicKey"], validate=True)))
                try:
                    association = json.loads(self.association_file.read_text())
                except (OSError, ValueError):
                    association = None
                if association:
                    self._request(process, box, client_id, "test-associate", **association)
                else:
                    identification = encoded(bytes(PrivateKey.generate().public_key))
                    result = self._request(
                        process,
                        box,
                        client_id,
                        "associate",
                        key=encoded(bytes(key.public_key)),
                        idKey=identification,
                    )
                    association = {"id": result["id"], "key": identification}
                    self.association_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    fd, name = tempfile.mkstemp(
                        prefix=".keepassxc-", dir=self.association_file.parent
                    )
                    try:
                        with os.fdopen(fd, "w") as output:
                            json.dump(association, output)
                        os.replace(name, self.association_file)
                    finally:
                        Path(name).unlink(missing_ok=True)
                result = self._request(
                    process,
                    box,
                    client_id,
                    "get-logins",
                    url="https://accounts.google.com/",
                    keys=[association],
                )
                return [
                    entry
                    for entry in result.get("entries", [])
                    if isinstance(entry.get("login"), str)
                    and isinstance(entry.get("password"), str)
                    and entry.get("expired") != "true"
                ]
            except (OSError, ValueError, KeyError) as error:
                raise KeePassXCError(
                    "Could not communicate with KeePassXC. Reconnect and try again."
                ) from error
            finally:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
