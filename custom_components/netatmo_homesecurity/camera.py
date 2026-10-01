"""Camera entity exposing the latest event snapshot from the panel/camera."""

from __future__ import annotations

from homeassistant.components.camera import Camera
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
    cameras = [
        NetatmoSnapshotCamera(coordinator, m.id)
        for m in coordinator.data["modules"].values()
        if m.is_camera
    ]
    async_add_entities(cameras)


class NetatmoSnapshotCamera(NetatmoHSEntity, Camera):
    """Shows the most recent event snapshot captured for the home."""

    _attr_name = None

    def __init__(self, coordinator: NetatmoHSCoordinator, module_id: str) -> None:
        NetatmoHSEntity.__init__(self, coordinator, module_id)
        Camera.__init__(self)
        self._attr_unique_id = f"{module_id}_snapshot"

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        return self.coordinator.latest_snapshot
