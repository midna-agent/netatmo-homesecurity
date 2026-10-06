"""Netatmo / Legrand-BTicino Home + Security integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import DOMAIN, PLATFORMS
from .coordinator import NetatmoHSCoordinator
from .lib import NetatmoError

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Netatmo Home + Security from a config entry."""
    coordinator = NetatmoHSCoordinator(hass, entry)
    try:
        await coordinator.async_setup()
    except NetatmoError as err:
        # Transient backend problems (rate limit 429, network, temporary auth
        # refresh failure) must not park the entry in setup_error forever.
        # Raising ConfigEntryNotReady makes Home Assistant retry with backoff.
        await coordinator.async_shutdown()
        raise ConfigEntryNotReady(
            f"Netatmo backend not ready: {err}"
        ) from err
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: NetatmoHSCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
