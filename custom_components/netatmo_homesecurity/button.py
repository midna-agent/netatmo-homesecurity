"""Open-door button entity."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import NetatmoHSCoordinator
from .entity import NetatmoHSEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: NetatmoHSCoordinator = hass.data[DOMAIN][entry.entry_id]
    buttons = [
        NetatmoOpenDoorButton(coordinator, m.id)
        for m in coordinator.data["modules"].values()
        if m.is_doorlock
    ]
    async_add_entities(buttons)


class NetatmoOpenDoorButton(NetatmoHSEntity, ButtonEntity):
    _attr_translation_key = "open_door"
    _attr_icon = "mdi:door-open"

    def __init__(self, coordinator: NetatmoHSCoordinator, module_id: str) -> None:
        super().__init__(coordinator, module_id)
        self._attr_unique_id = f"{module_id}_open"
        self._attr_name = "Open"

    async def async_press(self) -> None:
        await self.hass.async_add_executor_job(
            self.coordinator.client.open_door,
            self.coordinator.home_id,
            self._module_id,
        )
