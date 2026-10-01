"""Data coordinator: owns the NetatmoClient, keeps the session alive, and
polls home status + the latest event snapshot."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_HOME_ID,
    CONF_TOKEN,
    DOMAIN,
    UPDATE_INTERVAL_SECONDS,
)
from .lib import NetatmoClient, NetatmoError, Token

_LOGGER = logging.getLogger(__name__)


class NetatmoHSCoordinator(DataUpdateCoordinator):
    """Coordinates one Netatmo home."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL_SECONDS),
        )
        self.entry = entry
        self.home_id: str = entry.data[CONF_HOME_ID]
        self.client = NetatmoClient(
            client_id=entry.data.get(CONF_CLIENT_ID),
            client_secret=entry.data.get(CONF_CLIENT_SECRET),
        )
        self.client.api.set_token(Token.from_dict(entry.data[CONF_TOKEN]))
        # Persist rotated tokens back into the config entry (keeps the session
        # alive across HA restarts).
        self.client.api.on_token_change = self._on_token_change
        self._latest_snapshot: bytes | None = None

    async def async_setup(self) -> None:
        """Discover the home and bring up the realtime keep-alive session."""
        await self.hass.async_add_executor_job(self._discover)
        # Start token auto-refresh + persistent websocket (door relay needs it).
        await self.hass.async_add_executor_job(
            lambda: self.client.start_keepalive(realtime=True)
        )

    def _discover(self) -> None:
        self.client.discover()
        if self.home_id not in self.client.homes:
            self.client.add_home(self.home_id)

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            return await self.hass.async_add_executor_job(self._poll)
        except NetatmoError as err:
            raise UpdateFailed(str(err)) from err

    def _poll(self) -> dict[str, Any]:
        home = self.client.refresh_home_status(self.home_id)
        # Grab the most recent event snapshot for the camera entity.
        try:
            events = self.client.api.getevents(self.home_id, size=5)
            body = events.get("body", {}) or {}
            ev_list = body.get("events") or body.get("home", {}).get("events") or []
            for ev in ev_list:
                for url, _label in self.client._iter_snapshot_urls(ev):
                    self._latest_snapshot = self.client.api.download_blob(url)
                    break
                if self._latest_snapshot is not None:
                    break
        except NetatmoError as err:
            _LOGGER.debug("snapshot poll failed: %s", err)
        return {
            "home": home,
            "modules": {m.id: m for m in home.modules.values()},
        }

    @property
    def latest_snapshot(self) -> bytes | None:
        return self._latest_snapshot

    @callback
    def _on_token_change(self, token: Token) -> None:
        # Called from a worker thread; hop to the event loop to update the entry.
        self.hass.loop.call_soon_threadsafe(self._persist_token, token)

    def _persist_token(self, token: Token) -> None:
        data = {**self.entry.data, CONF_TOKEN: token.to_dict()}
        self.hass.config_entries.async_update_entry(self.entry, data=data)

    async def async_shutdown(self) -> None:
        await self.hass.async_add_executor_job(self.client.close)
