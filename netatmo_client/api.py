"""
Low-level HTTP client for the Netatmo Security / Legrand-Netatmo-BTicino
"Home + Security" backend (app package com.netatmo.camera).

Every request here reproduces, byte-for-byte where it matters, what the
official Android app (Security v26.9.1.0 / build 500000026) sends on the wire:
same hosts, paths, OAuth client credentials, User-Agent and JSON body shapes.
The goal is wire-transparency: the server cannot distinguish this client from
the official app.

Reverse-engineered from decrypted TLS captured on an authorized test device.
"""

from __future__ import annotations

import gzip
import json
import os
import time
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import requests


# --- Constants lifted verbatim from the official app's traffic -------------

API_HOST = "https://app.netatmo.net"
WS_HOST = "wss://app-ws.netatmo.net/ws/"           # realtime events channel
NEWS_HOST = "https://api-news.netatmo.net"
BLOB_HOST = "https://netatmocameraimage.blob.core.windows.net"

APP_VERSION = "26.9.1.0"
APP_BUILD = "500000026"
APP_TYPE = "app_camera"

# The app identifies itself to the API with this User-Agent. BTicino/Legrand
# "Home + Security" is the same binary family, hence "Security".
API_USER_AGENT = (
    f"NetatmoApp(Security/v{APP_VERSION}/{APP_BUILD}) Android(16/Google/Pixel 7)"
)
# okhttp is used for the websocket, the news host and the Azure image blobs.
OKHTTP_USER_AGENT = "okhttp/5.3.2"

# OAuth client identity for the password-grant flow.
#
# These identify the *application* (not the user) and are a fixed input to the
# login request — they are NOT issued/generated during login. The app ships
# them baked into its binary; they are identical across every install and
# extractable from the public APK (and already published in community Netatmo
# libraries). They are therefore hard-coded here as defaults, while still being
# overridable via constructor args or the NETATMO_CLIENT_ID /
# NETATMO_CLIENT_SECRET environment variables.
CLIENT_ID = os.environ.get("NETATMO_CLIENT_ID", "na_client_android_welcome")
CLIENT_SECRET = os.environ.get("NETATMO_CLIENT_SECRET",
                              "8ab584d62ca2a77e37ccc6b2c7e4f29e")
SCOPE = "security_scopes"

# Device types the app asks for in homesdata / homestatus. Kept identical so
# the backend returns the same module set it returns to the real app.
DEVICE_TYPES = [
    "BNMH", "BNCX", "BNC3", "BFII", "BPAC", "BPVC", "BNC1", "BDIY",
    "BNHY", "BCEPA", "NACamera", "NPC", "NOC2", "NOC", "NDB", "NSD",
    "NCO", "NDL",
]


class NetatmoError(RuntimeError):
    """Raised when the backend returns an error payload or an HTTP failure."""

    def __init__(self, message: str, *, status: int | None = None,
                 payload: Any = None):
        super().__init__(message)
        self.status = status
        self.payload = payload


@dataclass
class Token:
    access_token: str
    refresh_token: str
    expires_at: float               # epoch seconds
    scope: list[str] = field(default_factory=list)

    @property
    def expired(self) -> bool:
        # Refresh a little early, exactly as a real client would.
        return time.time() >= (self.expires_at - 120)

    def to_dict(self) -> dict:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "scope": self.scope,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Token":
        return cls(
            access_token=d["access_token"],
            refresh_token=d["refresh_token"],
            expires_at=d["expires_at"],
            scope=d.get("scope", []),
        )

    @classmethod
    def from_response(cls, body: dict) -> "Token":
        expires_in = int(body.get("expires_in") or body.get("expire_in") or 10800)
        return cls(
            access_token=body["access_token"],
            refresh_token=body["refresh_token"],
            expires_at=time.time() + expires_in,
            scope=body.get("scope", []),
        )


class NetatmoAPI:
    """Thin, faithful wrapper over the raw HTTP endpoints."""

    def __init__(self, timeout: float = 30.0,
                 client_id: str | None = None,
                 client_secret: str | None = None):
        self.timeout = timeout
        self.client_id = client_id or CLIENT_ID
        self.client_secret = client_secret or CLIENT_SECRET
        self.token: Optional[Token] = None
        self._lock = threading.RLock()
        # Invoked whenever the token is created/rotated, so callers can persist
        # it immediately — essential for a long-lived session.
        self.on_token_change: Optional[Callable[["Token"], None]] = None

        # One pooled session with keep-alive, like okhttp's connection pool.
        self._s = requests.Session()
        # Match the app: no "Accept" header, gzip accepted, our UA.
        self._s.headers.clear()
        self._s.headers["User-Agent"] = API_USER_AGENT
        self._s.headers["Accept-Encoding"] = "gzip"

    # -- internal helpers ---------------------------------------------------

    def _decode(self, resp: requests.Response) -> Any:
        raw = resp.content
        if resp.headers.get("content-encoding", "").lower() == "gzip":
            # requests usually auto-decodes, but be defensive for raw gzip.
            try:
                raw = gzip.decompress(raw)
            except (OSError, EOFError):
                raw = resp.content
        text = raw.decode("utf-8", "replace") if raw else ""
        ctype = resp.headers.get("content-type", "")
        if "application/json" in ctype and text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
        return text

    def _check(self, resp: requests.Response) -> Any:
        data = self._decode(resp)
        if resp.status_code >= 400:
            raise NetatmoError(
                f"HTTP {resp.status_code} on {resp.request.method} "
                f"{resp.request.url}: {data}",
                status=resp.status_code, payload=data,
            )
        if isinstance(data, dict) and data.get("error"):
            raise NetatmoError(f"API error: {data['error']}",
                               status=resp.status_code, payload=data)
        return data

    def _auth_headers(self) -> dict:
        tok = self.valid_token()
        return {"Authorization": f"Bearer {tok.access_token}"}

    def _post_json(self, path: str, body: dict, *, auth: bool = True) -> Any:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if auth:
            headers.update(self._auth_headers())
        resp = self._s.post(API_HOST + path, data=json.dumps(body),
                            headers=headers, timeout=self.timeout)
        return self._check(resp)

    # -- authentication -----------------------------------------------------

    def _require_secret(self) -> None:
        if not self.client_secret:
            raise NetatmoError(
                "No OAuth client_secret configured. Provide it via the "
                "NetatmoAPI(client_secret=...) argument or the "
                "NETATMO_CLIENT_SECRET environment variable. This is the "
                "application secret the app sends on login; it is not issued "
                "by the server.")

    def login(self, username: str, password: str) -> Token:
        """Password-grant login — identical to the app's POST /oauth2/token."""
        self._require_secret()
        form = {
            "password": password,
            "app_version": APP_VERSION,
            "grant_type": "password",
            "scope": SCOPE,
            "client_secret": self.client_secret,
            "client_id": self.client_id,
            "username": username,
        }
        resp = self._s.post(
            API_HOST + "/oauth2/token", data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=self.timeout,
        )
        body = self._check(resp)
        with self._lock:
            self.token = Token.from_response(body)
        self._notify_token()
        return self.token

    def refresh(self) -> Token:
        """Renew the access token via grant_type=refresh_token."""
        self._require_secret()
        with self._lock:
            if not self.token:
                raise NetatmoError("no token to refresh; call login() first")
            rt = self.token.refresh_token
        form = {
            "grant_type": "refresh_token",
            "refresh_token": rt,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": SCOPE,
            "app_version": APP_VERSION,
        }
        resp = self._s.post(
            API_HOST + "/oauth2/token", data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=self.timeout,
        )
        body = self._check(resp)
        with self._lock:
            self.token = Token.from_response(body)
        self._notify_token()
        return self.token

    def _notify_token(self) -> None:
        cb = self.on_token_change
        if cb and self.token:
            try:
                cb(self.token)
            except Exception:  # noqa: BLE001 - persistence must not break auth
                pass

    def valid_token(self) -> Token:
        """Return a non-expired token, refreshing transparently if needed."""
        with self._lock:
            if not self.token:
                raise NetatmoError("not authenticated; call login() first")
            if self.token.expired:
                self.refresh()
            return self.token

    def set_token(self, token: Token) -> None:
        with self._lock:
            self.token = token

    # -- discovery ----------------------------------------------------------

    def homesdata(self) -> dict:
        """Topology: homes, modules, schedules. POST /api/homesdata."""
        return self._post_json("/api/homesdata", {
            "app_type": APP_TYPE,
            "app_version": APP_VERSION,
            "with_schedules": True,
            "device_types": DEVICE_TYPES,
            "sync_measurements": False,
        })

    def homestatus(self, home_id: str) -> dict:
        """Live state of every module in a home. POST /syncapi/v1/homestatus."""
        return self._post_json("/syncapi/v1/homestatus", {
            "app_type": APP_TYPE,
            "app_version": APP_VERSION,
            "home_id": home_id,
            "device_types": DEVICE_TYPES,
        })

    def gethomeusers(self, home_id: str) -> dict:
        return self._post_json("/api/gethomeusers", {
            "app_type": APP_TYPE,
            "app_version": APP_VERSION,
            "home_id": home_id,
            "version": 2,
        })

    # -- actions ------------------------------------------------------------

    def setstate(self, home_id: str, modules: list[dict],
                 timezone: str = "Europe/Madrid") -> dict:
        """Generic module command. POST /syncapi/v1/setstate."""
        return self._post_json("/syncapi/v1/setstate", {
            "app_type": APP_TYPE,
            "app_version": APP_VERSION,
            "home": {"timezone": timezone, "id": home_id, "modules": modules},
        })

    def open_door(self, home_id: str, module_id: str, bridge: str,
                  timezone: str = "Europe/Madrid") -> dict:
        """
        Unlock / open a door-lock module. This is exactly the request the app
        issues when you use "Slide to unlock": lock=false.
        """
        return self.setstate(home_id, [
            {"bridge": bridge, "lock": False, "id": module_id},
        ], timezone=timezone)

    # -- events / snapshots -------------------------------------------------

    def getevents(self, home_id: str, size: int = 30) -> dict:
        """Timeline events (each carries snapshot references). POST /api/getevents."""
        return self._post_json("//api/getevents", {
            "app_type": APP_TYPE,
            "size": size,
            "app_version": APP_VERSION,
            "home_id": home_id,
        })

    def download_blob(self, url: str) -> bytes:
        """
        Download a snapshot/vignette. These are pre-signed Azure blob URLs
        (SAS token in the query string), fetched by the app with okhttp and
        NO Authorization header.
        """
        resp = requests.get(url, headers={
            "User-Agent": OKHTTP_USER_AGENT,
            "Accept-Encoding": "gzip",
            "Cache-Control": "no-cache",
        }, timeout=self.timeout)
        if resp.status_code >= 400:
            raise NetatmoError(f"blob HTTP {resp.status_code}", status=resp.status_code)
        return resp.content
