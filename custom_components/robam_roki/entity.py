"""Shared entity helpers for the ROBAM / ROKI integration."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import as_int
from .const import DOMAIN
from .coordinator import RokiCoordinator
from .devices import RokiDeviceDriver, get_driver_for_device


class RokiEntity(CoordinatorEntity[RokiCoordinator]):
    """Base class binding an entity to one appliance."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: RokiCoordinator, device: dict[str, Any]) -> None:
        super().__init__(coordinator)
        self._device = device
        self._device_id = str(device["guid"])
        self.driver: RokiDeviceDriver = get_driver_for_device(coordinator, device)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            name=device.get("name") or self._device_id,
            manufacturer="ROBAM",
            model=str(device.get("dt") or device.get("dc") or "ROKI"),
            serial_number=self._device_id,
        )

    @property
    def model_code(self) -> str:
        """The ``dt`` code the app uses to pick a control view."""
        return str(self._device.get("dt") or "").strip()

    @property
    def properties(self) -> dict[str, Any]:
        return (self.coordinator.data or {}).get(self._device_id, {})

    @property
    def available(self) -> bool:
        return super().available and bool(self.properties)

    def prop(self, *keys: str, default: Any = None) -> Any:
        """Read the first matching property (read API uses camelCase)."""
        for key in keys:
            if key in self.properties:
                return self.properties[key]
        return default

    def prop_int(self, *keys: str, default: int = 0) -> int:
        return as_int(self.prop(*keys, default=default), default)
