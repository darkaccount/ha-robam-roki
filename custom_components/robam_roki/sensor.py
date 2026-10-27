"""Diagnostic sensors for the ROBAM / ROKI integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import RokiCoordinator
from .devices import SensorSpec, get_driver_for_device
from .entity import RokiEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: RokiCoordinator = entry.runtime_data.coordinator
    entities: list[SensorEntity] = []

    for device in coordinator.devices:
        driver = get_driver_for_device(coordinator, device)
        for spec in driver.sensors():
            entities.append(RokiDriverSensor(coordinator, device, spec))

    async_add_entities(entities)


class RokiDriverSensor(RokiEntity, SensorEntity):
    """Dynamic sensor generated from Driver SensorSpec."""

    def __init__(
        self,
        coordinator: RokiCoordinator,
        device: dict[str, Any],
        spec: SensorSpec,
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
        if spec.state_class:
            self._attr_state_class = spec.state_class
        if spec.unit_of_measurement:
            self._attr_native_unit_of_measurement = spec.unit_of_measurement
        if spec.entity_category:
            self._attr_entity_category = spec.entity_category

    @property
    def native_value(self) -> Any:
        if self.spec.value_fn:
            return self.spec.value_fn(self.properties)
        return self.prop(self.spec.key)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self.spec.attrs_fn:
            return self.spec.attrs_fn(self.properties)
        return {}


# Backward compatibility alias
RokiFaultSensor = RokiDriverSensor
