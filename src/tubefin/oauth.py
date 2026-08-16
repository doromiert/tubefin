from __future__ import annotations

import base64
import hashlib
import json
import secrets
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from tubefin.models import OAuthAccount
from tubefin.services import ServiceError

READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
MANAGE_SCOPE = "https://www.googleapis.com/auth/youtube"
COMMENT_SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"
PROFILE_SCOPES = ["openid", "email", "profile"]


class KeyringStore:
    """Store refresh tokens in the desktop Secret Service via libsecret's CLI."""

    def __init__(self, executable: str | None = None) -> None:
        self.executable = executable or shutil.which("secret-tool")

    @property
    def available(self) -> bool:
        return bool(self.executable)

    def save(self, account_id: str, refresh_token: str) -> None:
        if not self.executable:
            raise ServiceError("A Secret Service keyring is required for YouTube sign-in.")
        result = subprocess.run(
            [
                self.executable,
                "store",
                "--label=TubeFin YouTube account",
                "application",
                "tubefin",
                "account",
                account_id,
            ],
            input=refresh_token,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if result.returncode:
            raise ServiceError(result.stderr.strip() or "Could not unlock the system keyring.")

    def load(self, account_id: str) -> str | None:
        if not self.executable:
            return None
        result = subprocess.run(
            [
                self.executable,
                "lookup",
                "application",
                "tubefin",
                "account",
                account_id,
            ],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None

    def delete(self, account_id: str) -> None:
        if self.executable:
            subprocess.run(
                [
                    self.executable,
                    "clear",
                    "application",
                    "tubefin",
                    "account",
                    account_id,
                ],
                capture_output=True,
                timeout=30,
                check=False,
            )


class OAuthClient:
    AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    TOKEN_URL = "https://oauth2.googleapis.com/token"
    REVOKE_URL = "https://oauth2.googleapis.com/revoke"
    USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

    def __init__(self, client_id: str, keyring: KeyringStore | None = None) -> None:
        self.client_id = client_id.strip()
        self.keyring = keyring or KeyringStore()
        self._access: dict[str, tuple[str, float]] = {}

    def authorize(
        self,
        *,
        manage_playlists: bool = False,
        open_browser: bool = True,
        url_opener: Callable[[str], None] | None = None,
        timeout: int = 180,
    ) -> OAuthAccount:
        if not self.client_id:
            raise ServiceError("Set a Google OAuth desktop client ID in TubeFin preferences.")
        if not self.keyring.available:
            raise ServiceError("Install libsecret and start a Secret Service keyring first.")
        verifier = secrets.token_urlsafe(64)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        state = secrets.token_urlsafe(24)
        result: dict[str, str] = {}
        ready = threading.Event()

        class Callback(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                values = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                result.update({key: value[0] for key, value in values.items() if value})
                body = b"TubeFin sign-in is complete. You can close this tab."
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                ready.set()

            def log_message(self, _format: str, *_args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Callback)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        redirect_uri = f"http://127.0.0.1:{server.server_address[1]}/callback"
        scopes = [*PROFILE_SCOPES, READONLY_SCOPE, COMMENT_SCOPE]
        if manage_playlists:
            scopes.append(MANAGE_SCOPE)
        authorize_query = urllib.parse.urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": " ".join(scopes),
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "access_type": "offline",
                "prompt": "consent select_account",
            }
        )
        url = f"{self.AUTHORIZE_URL}?{authorize_query}"
        if open_browser:
            if url_opener is None:
                raise ServiceError("No system browser launcher is available.")
            url_opener(url)
        if not ready.wait(timeout):
            server.shutdown()
            raise ServiceError("Google sign-in timed out.")
        server.shutdown()
        if result.get("state") != state:
            raise ServiceError("Google sign-in returned an invalid state value.")
        if error := result.get("error"):
            raise ServiceError(f"Google sign-in failed: {error.replace('_', ' ')}.")
        tokens = self._post_form(
            self.TOKEN_URL,
            {
                "client_id": self.client_id,
                "code": result.get("code", ""),
                "code_verifier": verifier,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
        )
        access_token = str(tokens.get("access_token") or "")
        refresh_token = str(tokens.get("refresh_token") or "")
        if not access_token or not refresh_token:
            raise ServiceError("Google did not return a reusable account session.")
        profile = self._json_request(self.USERINFO_URL, access_token)
        account_id = str(profile.get("sub") or profile.get("email") or "")
        if not account_id:
            raise ServiceError("Google did not return an account identity.")
        self.keyring.save(account_id, refresh_token)
        self._access[account_id] = (
            access_token,
            time.time() + int(tokens.get("expires_in", 3600)) - 60,
        )
        return OAuthAccount(
            account_id,
            str(profile.get("email") or "YouTube account"),
            str(profile.get("name") or profile.get("email") or "YouTube account"),
            scopes,
        )

    def access_token(self, account: OAuthAccount) -> str:
        cached = self._access.get(account.id)
        if cached and cached[1] > time.time():
            return cached[0]
        refresh_token = self.keyring.load(account.id)
        if not refresh_token:
            raise ServiceError("This account is no longer available in the system keyring.")
        tokens = self._post_form(
            self.TOKEN_URL,
            {
                "client_id": self.client_id,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        token = str(tokens.get("access_token") or "")
        if not token:
            raise ServiceError("Google could not refresh this account session.")
        self._access[account.id] = (token, time.time() + int(tokens.get("expires_in", 3600)) - 60)
        return token

    def sign_out(self, account: OAuthAccount) -> None:
        refresh_token = self.keyring.load(account.id)
        if refresh_token:
            with suppress(ServiceError):
                self._post_form(self.REVOKE_URL, {"token": refresh_token})
        self.keyring.delete(account.id)
        self._access.pop(account.id, None)

    @staticmethod
    def account_dict(account: OAuthAccount) -> dict[str, Any]:
        return asdict(account)

    @staticmethod
    def _post_form(url: str, values: dict[str, str]) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=urllib.parse.urlencode(values).encode(),
            headers={"Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            try:
                detail = json.load(error)
                message = detail.get("error_description") or detail.get("error")
            except (ValueError, AttributeError):
                message = f"HTTP {error.code}"
            raise ServiceError(f"Google authorization failed: {message}.") from error
        except (OSError, urllib.error.URLError, TimeoutError) as error:
            raise ServiceError("Could not reach Google authorization services.") from error

    @staticmethod
    def _json_request(url: str, access_token: str) -> dict[str, Any]:
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except (OSError, ValueError, urllib.error.URLError, TimeoutError) as error:
            raise ServiceError("Could not load the Google account profile.") from error
