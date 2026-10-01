"""Door-lock entity: opens/unlocks a Netatmo-BTicino door module."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.lock import LockEntity, LockEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import NetatmoHSCoordinator
from .entity import NetatmoHSEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: NetatmoHSCoordinator = hass.data[DOMAIN][entry.entry_id]
    locks = [
        NetatmoDoorLock(coordinator, m.id)
        for m in coordinator.data["modules"].values()
        if m.is_doorlock
    ]
    async_add_entities(locks)


class NetatmoDoorLock(NetatmoHSEntity, LockEntity):
    """A momentary door opener exposed as a lock with an OPEN action."""

    _attr_name = None  # use the device name
    _attr_supported_features = LockEntityFeature.OPEN

    def __init__(self, coordinator: NetatmoHSCoordinator, module_id: str) -> None:
        super().__init__(coordinator, module_id)
        self._attr_unique_id = f"{module_id}_lock"

    @property
    def is_locked(self) -> bool:
        # Door openers are momentary; report locked at rest.
        module = self._module
        if module and "lock" in module.raw:
            return bool(module.raw.get("lock"))
        return True

    async def async_open(self, **kwargs: Any) -> None:
        """Open the door (pulse the relay), as the app's 'Slide to unlock'."""
        await self.hass.async_add_executor_job(
            self.coordinator.client.open_door,
            self.coordinator.home_id,
            self._module_id,
        )
        await self.coordinator.async_request_refresh()

    async def async_unlock(self, **kwargs: Any) -> None:
        # For a momentary opener, unlock == open.
        await self.async_open(**kwargs)

    async def async_lock(self, **kwargs: Any) -> None:
        _LOGGER.debug("lock() is a no-op for a momentary door opener")
