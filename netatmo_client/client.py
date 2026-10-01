"""
High-level Netatmo "Home + Security" client.

Responsibilities:
  * login / token persistence (survive restarts via the refresh token)
  * first-connect entity discovery (homes, bridges, cameras, door locks, ...)
  * actions (open a door)
  * stream/snapshot capture (timeline event snapshots)
  * keep the session alive like the real app: a background token refresher and
    a persistent realtime websocket with auto-reconnect.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .api import NetatmoAPI, NetatmoError, Token, WS_HOST
from .ws import WebSocketChannel


@dataclass
class Module:
    id: str
    name: str
    type: str
    bridge: Optional[str] = None
    category: Optional[str] = None
    raw: dict = field(default_factory=dict)

    # Heuristics over Netatmo/BTicino module types + capabilities.
    DOORLOCK_TYPES = {"NDL", "BNDL"}
    CAMERA_TYPES = {"NACamera", "NOC", "NOC2", "NDB", "NPC", "BNC1", "BNC3",
                    "BNCX", "BNMH"}

    @property
    def is_doorlock(self) -> bool:
        return self.type in self.DOORLOCK_TYPES or "lock" in self.raw \
            or "door lock" in (self.name or "").lower()

    @property
    def is_camera(self) -> bool:
        return self.type in self.CAMERA_TYPES or "vpn_url" in self.raw \
            or "entrance panel" in (self.name or "").lower()


@dataclass
class Home:
    id: str
    name: str
    timezone: str = "Europe/Madrid"
    modules: dict[str, Module] = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    @property
    def doorlocks(self) -> list[Module]:
        return [m for m in self.modules.values() if m.is_doorlock]

    @property
    def cameras(self) -> list[Module]:
        return [m for m in self.modules.values() if m.is_camera]


class NetatmoClient:
    def __init__(self, token_store: str | os.PathLike | None = None,
                 timeout: float = 30.0,
                 client_id: str | None = None,
                 client_secret: str | None = None):
        self.api = NetatmoAPI(timeout=timeout, client_id=client_id,
                             client_secret=client_secret)
        self.token_store = Path(token_store) if token_store else None
        self.homes: dict[str, Home] = {}
        # Persist the token on every rotation (login, manual or auto refresh).
        self.api.on_token_change = lambda _tok: self._save_token()

        self._keepalive_stop = threading.Event()
        self._keepalive_thread: Optional[threading.Thread] = None
        self._ws: Optional[WebSocketChannel] = None
        self._event_handlers: list[Callable[[Any], None]] = []

    # -- authentication & persistence ---------------------------------------

    def login(self, username: str, password: str) -> None:
        self.api.login(username, password)
        self._save_token()

    def load_saved_session(self) -> bool:
        """Restore a previous session from disk; refresh if needed.

        Returns True if a usable session was restored.
        """
        if not self.token_store or not self.token_store.exists():
            return False
        try:
            data = json.loads(self.token_store.read_text())
            self.api.set_token(Token.from_dict(data))
            self.api.valid_token()   # refreshes if expired
            self._save_token()
            return True
        except (json.JSONDecodeError, KeyError, NetatmoError):
            return False

    def _save_token(self) -> None:
        if self.token_store and self.api.token:
            self.token_store.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.token_store.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.api.token.to_dict()))
            os.replace(tmp, self.token_store)
            try:
                os.chmod(self.token_store, 0o600)
            except OSError:
                pass

    # -- discovery ----------------------------------------------------------

    def discover(self) -> dict[str, Home]:
        """Discover all homes and their modules on first connect.

        Merges topology (homesdata) with live state (homestatus), exactly like
        the app does when it builds its home screen.
        """
        self.homes = {}
        topo = self.api.homesdata()
        homes_topo = (topo.get("body", {}) or {}).get("homes", []) or []

        # Some accounts return only feature flags from homesdata; in that case
        # we still discover modules from homestatus below.
        for h in homes_topo:
            home = Home(
                id=h["id"],
                name=h.get("name", h["id"]),
                timezone=h.get("timezone", "Europe/Madrid"),
                raw=h,
            )
            for m in h.get("modules", []) or []:
                home.modules[m["id"]] = Module(
                    id=m["id"], name=m.get("name", m["id"]),
                    type=m.get("type", ""), bridge=m.get("bridge"),
                    category=m.get("category"), raw=m,
                )
            self.homes[home.id] = home

        # If homesdata gave us no homes, we need at least one home_id. The app
        # also learns it from homestatus; but homestatus requires a home_id.
        # Fall back to gethomeusers/homesdata "homes" ids if present, else the
        # caller can pass a known home_id to refresh_home_status().
        for home_id, home in list(self.homes.items()):
            self._merge_homestatus(home)

        return self.homes

    def add_home(self, home_id: str, name: str | None = None,
                 timezone: str = "Europe/Madrid") -> Home:
        """Register a home by id (for accounts whose homesdata omits topology)
        and populate it from homestatus."""
        home = self.homes.get(home_id) or Home(id=home_id, name=name or home_id,
                                               timezone=timezone)
        self.homes[home_id] = home
        self._merge_homestatus(home)
        return home

    def _merge_homestatus(self, home: Home) -> None:
        status = self.api.homestatus(home.id)
        body = status.get("body", {}) or {}
        sh = body.get("home", {}) or {}
        if sh.get("name"):
            home.name = sh["name"]
        for m in sh.get("modules", []) or []:
            mid = m.get("id")
            if not mid:
                continue
            existing = home.modules.get(mid)
            if existing:
                existing.raw.update(m)
                existing.bridge = existing.bridge or m.get("bridge")
                if not existing.type:
                    existing.type = m.get("type", "")
            else:
                home.modules[mid] = Module(
                    id=mid, name=m.get("name", mid), type=m.get("type", ""),
                    bridge=m.get("bridge"), category=m.get("category"), raw=m,
                )

    def refresh_home_status(self, home_id: str) -> Home:
        home = self.homes.get(home_id)
        if not home:
            return self.add_home(home_id)
        self._merge_homestatus(home)
        return home

    # -- actions ------------------------------------------------------------

    def open_door(self, home_id: str, module_id: str | None = None) -> dict:
        """Open/unlock a door lock. If module_id is omitted, the first
        discovered door lock in the home is used."""
        home = self.homes.get(home_id)
        if home is None:
            raise NetatmoError(f"unknown home {home_id}; run discover() first")
        if module_id is None:
            locks = home.doorlocks
            if not locks:
                raise NetatmoError("no door-lock module discovered in this home")
            module = locks[0]
        else:
            module = home.modules.get(module_id)
            if module is None:
                raise NetatmoError(f"unknown module {module_id}")
        bridge = module.bridge
        if not bridge:
            raise NetatmoError(f"module {module.id} has no bridge; "
                               "run discover()/refresh_home_status() first")
        return self.api.open_door(home.id, module.id, bridge,
                                 timezone=home.timezone)

    # -- stream / snapshot capture ------------------------------------------

    def capture_snapshots(self, home_id: str, out_dir: str | os.PathLike,
                          size: int = 30) -> list[str]:
        """Fetch recent timeline events and download their snapshot frames.

        Returns the list of written file paths. This is the app's snapshot
        pipeline: getevents -> pre-signed blob URLs -> JPEG download.
        """
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        events = self.api.getevents(home_id, size=size)
        body = events.get("body", {}) or {}
        ev_list = body.get("events") or body.get("home", {}).get("events") or []
        written: list[str] = []
        for ev in ev_list:
            for url, label in self._iter_snapshot_urls(ev):
                try:
                    data = self.api.download_blob(url)
                except NetatmoError:
                    continue
                ts = ev.get("time") or ev.get("timestamp") or int(time.time())
                fname = out / f"{ts}_{label}.jpg"
                fname.write_bytes(data)
                written.append(str(fname))
        return written

    @staticmethod
    def _iter_snapshot_urls(event: dict):
        """Yield (url, label) snapshot references found inside an event."""
        eid = event.get("id", "evt")
        for key in ("snapshot", "vignette"):
            obj = event.get(key)
            if isinstance(obj, dict) and obj.get("url"):
                yield obj["url"], f"{eid}_{key}"
        # Subevents (person/vehicle/animal detections) carry their own frames.
        for sub in event.get("subevents", []) or []:
            for key in ("snapshot", "vignette"):
                obj = sub.get(key)
                if isinstance(obj, dict) and obj.get("url"):
                    yield obj["url"], f"{eid}_{sub.get('id','sub')}_{key}"

    def live_snapshot(self, home_id: str, module_id: str,
                      out_file: str | os.PathLike,
                      resolution: int = 720) -> str:
        """Best-effort single live frame from a camera's local vpn_url
        (GET {vpn_url}/live/snapshot_{res}.jpg), as used for Netatmo cameras.
        Requires the module's vpn_url to be present in homestatus.
        """
        home = self.homes.get(home_id)
        module = home.modules.get(module_id) if home else None
        if not module:
            raise NetatmoError(f"unknown module {module_id}; discover() first")
        vpn = module.raw.get("vpn_url")
        if not vpn:
            raise NetatmoError(
                f"module {module_id} exposes no vpn_url; this device streams "
                "over the realtime/UDP channel (e.g. a BTicino entrance panel). "
                "Use capture_snapshots() for its event frames instead.")
        data = self.api.download_blob(vpn.rstrip("/") +
                                     f"/live/snapshot_{resolution}.jpg")
        Path(out_file).write_bytes(data)
        return str(out_file)

    # -- keep-alive: token refresher + realtime websocket -------------------

    def on_event(self, handler: Callable[[Any], None]) -> None:
        self._event_handlers.append(handler)

    def start_keepalive(self, realtime: bool = True,
                        refresh_check: float = 60.0) -> None:
        """Keep the session open like the real app.

        * A daemon thread refreshes the access token before it expires and
          re-persists it.
        * If realtime=True, a persistent websocket is opened to the events
          channel with automatic reconnect.
        """
        if self._keepalive_thread and self._keepalive_thread.is_alive():
            return
        self._keepalive_stop.clear()

        def _loop():
            while not self._keepalive_stop.wait(refresh_check):
                try:
                    tok = self.api.token
                    if tok and tok.expired:
                        self.api.refresh()
                        self._save_token()
                except NetatmoError:
                    pass

        self._keepalive_thread = threading.Thread(target=_loop, daemon=True,
                                                  name="netatmo-keepalive")
        self._keepalive_thread.start()

        if realtime:
            self._start_ws()

    def _start_ws(self) -> None:
        def on_message(msg):
            try:
                payload = json.loads(msg) if isinstance(msg, str) else msg
            except json.JSONDecodeError:
                payload = msg
            for h in self._event_handlers:
                try:
                    h(payload)
                except Exception:  # noqa: BLE001
                    pass

        # The app authenticates the socket by sending its bearer token right
        # after the upgrade; we mirror that on every (re)connect.
        def on_state(state):
            if state == "connected":
                try:
                    tok = self.api.valid_token()
                    self._ws.send_text(json.dumps({
                        "action": "Authorization",
                        "access_token": tok.access_token,
                    }))
                except Exception:  # noqa: BLE001
                    pass

        self._ws = WebSocketChannel(WS_HOST, on_message=on_message,
                                   on_state=on_state)
        self._ws.start()

    def stop_keepalive(self) -> None:
        self._keepalive_stop.set()
        if self._ws:
            self._ws.stop()
            self._ws = None

    def close(self) -> None:
        self.stop_keepalive()
