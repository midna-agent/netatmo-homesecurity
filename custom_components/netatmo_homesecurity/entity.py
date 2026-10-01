"""Shared entity base for Netatmo Home + Security."""

from __future__ import annotations

from homeassistant.helpers.device_info import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import NetatmoHSCoordinator


class NetatmoHSEntity(CoordinatorEntity[NetatmoHSCoordinator]):
    """Base entity bound to a Netatmo module."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: NetatmoHSCoordinator, module_id: str) -> None:
        super().__init__(coordinator)
        self._module_id = module_id

    @property
    def _module(self):
        return self.coordinator.data["modules"].get(self._module_id)

    @property
    def device_info(self) -> DeviceInfo:
        module = self._module
        name = module.name if module else self._module_id
        return DeviceInfo(
            identifiers={(DOMAIN, self._module_id)},
            name=name,
            manufacturer="Netatmo / Legrand-BTicino",
            model=(module.type if module else None),
            via_device=(DOMAIN, self.coordinator.home_id),
        )

    @property
    def available(self) -> bool:
        module = self._module
        reachable = bool(module.raw.get("reachable", True)) if module else False
        return super().available and reachable
