"""Countdown (倒计时) number entity for the ROBAM / ROKI integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import RokiAuthError, RokiError
from .const import MAX_COUNTDOWN_MINUTES
from .coordinator import RokiCoordinator
from .entity import RokiEntity

_LOGGER = logging.getLogger(__name__)

# The countdown range.  The protocol carries FanTime as a single unsigned
# byte, so 255 is the hard ceiling.  The app's hour+minute picker tops out at
# 3 h 59 min (239), which is a UI choice rather than a protocol limit; 240 is
# used here so the slider lands on a round four hours.  Sending 0 cancels the
# countdown, exactly like the app's confirm dialog does.
#
# Note that FanCtr8229View.onStartClock passes 0-60 to its NumberPicker, but
# that is only the range of the hood panel's own entry point -- it is not the
# app-wide limit.
MIN_MINUTES = 0
MAX_MINUTES = MAX_COUNTDOWN_MINUTES
STEP = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: RokiCoordinator = entry.runtime_data.coordinator
    async_add_entities(
        RokiHoodCountdown(coordinator, device)
        for device in coordinator.devices
        if str(device.get("dc", "")).upper() in ("", "RYYJ")
    )


class RokiHoodCountdown(RokiEntity, NumberEntity):
    """Countdown in minutes, as the app's countdown picker offers it."""

    _attr_translation_key = "countdown"
    _attr_has_entity_name = True
    _attr_icon = "mdi:timer-outline"
    _attr_native_min_value = MIN_MINUTES
    _attr_native_max_value = MAX_MINUTES
    _attr_native_step = STEP
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: RokiCoordinator, device: dict[str, Any]) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{self._device_id}_countdown"

    @property
    def available(self) -> bool:
        """Only offer the countdown while the hood is actually running.

        Command 144 carries the level to run on, and the app always sends the
        current one.  With the hood off there is no level to send, so the
        command would stop the fan instead of arming a timer.
        """
        return super().available and self.prop_int("level") > 0

    @property
    def native_value(self) -> float | None:
        value = self.prop("timing")
        if value is None:
            return None
        return float(self.prop_int("timing"))

    async def async_set_native_value(self, value: float) -> None:
        minutes = round(value)
        # Pass the level we are showing.  The hood reports gear and countdown
        # in separate frames, so a level read back from the coordinator right
        # after a gear change can still be the previous one, and command 144
        # would drag the fan back to it.
        level = self.prop_int("level")
        try:
            await self.coordinator.async_set_timing(
                self._device_id, minutes, level=level or None
            )
        except RokiAuthError as err:
            raise HomeAssistantError(f"ROKI session expired: {err}") from err
        except RokiError as err:
            raise HomeAssistantError(f"Could not set the countdown: {err}") from err
        self.coordinator.async_apply(self._device_id, {"timing": minutes})
        self.coordinator.async_schedule_refresh(self._device_id)
