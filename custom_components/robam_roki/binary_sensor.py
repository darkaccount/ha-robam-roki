"""Binary sensors for the ROBAM / ROKI integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import RokiCoordinator
from .devices import BinarySensorSpec, get_driver_for_device
from .entity import RokiEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: RokiCoordinator = entry.runtime_data.coordinator
    entities: list[BinarySensorEntity] = []

    for device in coordinator.devices:
        driver = get_driver_for_device(coordinator, device)
        for spec in driver.binary_sensors():
            entities.append(RokiDriverBinarySensor(coordinator, device, spec))
        # Global connectivity diagnostic sensor per device
        entities.append(RokiOnlineSensor(coordinator, device))

    async_add_entities(entities)


class RokiDriverBinarySensor(RokiEntity, BinarySensorEntity):
    """Dynamic binary sensor generated from Driver BinarySensorSpec."""

    def __init__(
        self,
        coordinator: RokiCoordinator,
        device: dict[str, Any],
        spec: BinarySensorSpec,
    ) -> None:
        super().__init__(coordinator, device)
        self.spec = spec
        self._attr_translation_key = spec.translation_key
        self._attr_has_entity_name = True
        self._attr_unique_id = f"{self._device_id}_{spec.key}"
        if spec.icon:
            self._attr_icon = spec.icon
        if spec.device_class:
            self._attr_device_class = spec.device_class
        if spec.entity_category:
            self._attr_entity_category = spec.entity_category

    @property
    def is_on(self) -> bool | None:
        if self.spec.is_on_fn:
            return self.spec.is_on_fn(self.properties)
        val = self.prop(self.spec.key)
        return bool(val) if val is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self.spec.attrs_fn:
            return self.spec.attrs_fn(self.properties)
        return {}


# Backward compatibility alias
RokiCleanRequiredSensor = RokiDriverBinarySensor


class RokiOnlineSensor(RokiEntity, BinarySensorEntity):
    """Whether the hood itself is reachable.

    This is a diagnostic entity on purpose: it deliberately does not feed
    ``available`` on the fan or light entities.  A wrong guess here would grey
    out every control, which is worse than having no online indicator at all.
    """

    _attr_translation_key = "online"
    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: RokiCoordinator, device: dict[str, Any]) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{self._device_id}_online"

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.online_state(self._device_id)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.coordinator.online_details(self._device_id)
