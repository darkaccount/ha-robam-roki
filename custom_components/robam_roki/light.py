"""Light platform for the ROBAM / ROKI integration."""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from homeassistant.components.light import ColorMode, LightEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import RokiAuthError, RokiError
from .coordinator import RokiCoordinator
from .entity import RokiEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: RokiCoordinator = entry.runtime_data.coordinator
    async_add_entities(
        RokiHoodLight(coordinator, device)
        for device in coordinator.devices
        if str(device.get("dc", "")).upper() in ("", "RYYJ")
    )


class RokiHoodLight(RokiEntity, LightEntity):
    """The hood lamp."""

    _attr_translation_key = "light"
    _attr_has_entity_name = True
    _attr_color_mode = ColorMode.ONOFF
    _attr_supported_color_modes: ClassVar[set[ColorMode]] = {ColorMode.ONOFF}

    def __init__(self, coordinator: RokiCoordinator, device: dict[str, Any]) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{self._device_id}_light"

    @property
    def is_on(self) -> bool | None:
        value = self.prop("lightStatus", "light_status")
        if value is None:
            return None
        return self.prop_int("lightStatus", "light_status") == 1

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set_light(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set_light(False)

    async def _async_set_light(self, on: bool) -> None:
        try:
            await self.coordinator.async_set_light(self._device_id, on)
        except RokiAuthError as err:
            raise HomeAssistantError(f"ROKI session expired: {err}") from err
        except RokiError as err:
            raise HomeAssistantError(f"Could not switch the light: {err}") from err
        self.coordinator.async_apply(
            self._device_id, {"lightStatus": 1 if on else 0}
        )
        self.coordinator.async_schedule_refresh(self._device_id)
