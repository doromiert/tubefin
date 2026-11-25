from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from nacl.public import Box, PrivateKey, PublicKey

from tubefin.keepassxc import KeePassXC, KeePassXCError, encoded


class KeePassXCTests(unittest.TestCase):
    def test_encrypted_pairing_and_lookup_keep_password_out_of_saved_identity(self):
        with tempfile.TemporaryDirectory(prefix="tubefin-keepassxc-") as root:
            destination = Path(root) / "association.json"
            client = KeePassXC(destination, executable="test-proxy")
            host = PrivateKey.generate()
            box = None
            actions = []

            def exchange(_process, request):
                nonlocal box
                action = request["action"]
                actions.append(action)
                if action == "change-public-keys":
                    box = Box(host, PublicKey(base64.b64decode(request["publicKey"])))
                    return {
                        "action": action,
                        "success": "true",
                        "publicKey": encoded(bytes(host.public_key)),
                    }
                nonce = base64.b64decode(request["nonce"])
                plaintext = json.loads(box.decrypt(base64.b64decode(request["message"]), nonce))
                self.assertEqual(plaintext["action"], action)
                response_nonce = (int.from_bytes(nonce, "little") + 1).to_bytes(24, "little")
                result = {"success": "true", "nonce": encoded(response_nonce), "id": "TubeFin Test"}
                if action == "get-logins":
                    self.assertEqual(plaintext["url"], "https://accounts.google.com/")
                    self.assertEqual(plaintext["keys"][0]["id"], "TubeFin Test")
                    result["entries"] = [{"login": "test-user", "password": "test-secret"}]
                return {
                    "action": action,
                    "nonce": encoded(response_nonce),
                    "message": encoded(
                        box.encrypt(json.dumps(result).encode(), response_nonce).ciphertext
                    ),
                }

            client._exchange = exchange
            with patch("tubefin.keepassxc.subprocess.Popen", return_value=MagicMock()):
                for _ in range(2):
                    self.assertEqual(
                        client.logins("https://accounts.google.com/v3/signin")[0]["login"],
                        "test-user",
                    )
            self.assertEqual(actions.count("associate"), 1)
            self.assertEqual(actions.count("test-associate"), 1)
            self.assertNotIn("test-secret", destination.read_text())
            self.assertEqual(set(json.loads(destination.read_text())), {"id", "key"})
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)

    def test_replayed_nonce_is_rejected(self):
        client = KeePassXC(Path("unused"), executable="test-proxy")
        box = Box(PrivateKey.generate(), PrivateKey.generate().public_key)
        client._exchange = lambda _process, request: {
            "message": "",
            "nonce": request["nonce"],
        }
        with self.assertRaises(KeePassXCError):
            client._request(None, box, "test", "get-logins")

    def test_only_exact_secure_google_origin_can_request_credentials(self):
        client = KeePassXC(Path("unused"), executable="test-proxy")
        with patch("tubefin.keepassxc.subprocess.Popen") as process:
            for url in (
                "http://accounts.google.com/",
                "https://accounts.google.com.attacker.test/",
                "https://accounts.google.com:8443/",
                "https://youtube.com/",
            ):
                with self.subTest(url=url), self.assertRaises(KeePassXCError):
                    client.logins(url)
            process.assert_not_called()


if __name__ == "__main__":
    unittest.main()
