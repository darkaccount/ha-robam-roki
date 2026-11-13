"""Gear (档位) select entity for the ROBAM / ROKI integration.

Mirrors the select the Huawei SmartHome integration exposes for the same
hoods: a flat 关档/弱档/强档/爆炒档 list that automations can target without
doing percentage arithmetic.  Picking 关档 turns the hood off; picking a gear
turns it on at that gear, exactly like the app's gear buttons.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import RokiAuthError, RokiError
from .const import (
    CONF_LEVELS,
    CONF_MAX_LEVEL,
    level_name,
    level_names_for,
    resolve_levels,
)
from .coordinator import RokiCoordinator
from .devices import get_driver_for_device
from .entity import RokiEntity

_LOGGER = logging.getLogger(__name__)

# Capability contract for this platform: a gear list needs selectable gears.
# The 关档 entry additionally needs the power command (see "options").
_REQUIRED_CAPABILITIES = frozenset({"level"})

OFF_OPTION = "关档"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: RokiCoordinator = entry.runtime_data.coordinator
    async_add_entities(
        RokiHoodGearSelect(coordinator, device)
        for device in coordinator.devices
        if _REQUIRED_CAPABILITIES
        <= get_driver_for_device(coordinator, device).capabilities
    )


class RokiHoodGearSelect(RokiEntity, SelectEntity):
    """The hood's gear as a select: 关档 plus every rung of the ladder."""

    _attr_translation_key = "gear"
    _attr_icon = "mdi:tune-vertical"

    def __init__(self, coordinator: RokiCoordinator, device: dict[str, Any]) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{self._device_id}_gear"

    # -- ladder -----------------------------------------------------------

    @property
    def levels(self) -> list[int]:
        """Same ladder the fan resolves: option override, table, fallback."""
        entry = getattr(self.coordinator, "entry", None)
        option = entry.options.get(CONF_LEVELS) if entry is not None else None
        max_level = entry.options.get(CONF_MAX_LEVEL) if entry is not None else None
        return resolve_levels(self.model_code, option, max_level)

    @property
    def _supports_power(self) -> bool:
        """Whether the driver behind this entity can switch the hood off.

        The base entity already resolved the driver, so the capability is
        read from it rather than guessed.  Without capability data (the
        offline test harness swaps the base class for a lighter stand-in
        that carries no driver) the historical 关档 entry is kept, which is
        what every real hood offers.
        """
        driver = getattr(self, "driver", None)
        capabilities = getattr(driver, "capabilities", None)
        if capabilities is None:
            return True
        return "power" in capabilities

    @property
    def options(self) -> list[str]:
        # 关档 maps onto the power command, so it is only offered when the
        # device advertises that capability; a hood always does.
        gears = level_names_for(self.levels)
        if self._supports_power:
            return [OFF_OPTION, *gears]
        return gears

    # -- state ------------------------------------------------------------

    @property
    def is_on(self) -> bool:
        return self.prop_int("workStatus", "working_status") != 0

    @property
    def current_option(self) -> str | None:
        """关档 when the hood is off, else the running gear's label.

        A reported level outside the ladder yields ``None`` rather than a
        guess, same as the fan's preset_mode.
        """
        if not self.is_on:
            return OFF_OPTION
        level = self.prop_int("level")
        if level <= 0:
            return OFF_OPTION
        if level not in self.levels:
            return None
        return level_name(level, self.levels)

    # -- commands ---------------------------------------------------------

    async def async_select_option(self, option: str) -> None:
        if option == OFF_OPTION:
            try:
                await self.coordinator.async_execute(
                    self._device_id, "power", on=False
                )
            except RokiAuthError as err:
                raise HomeAssistantError(f"ROKI session expired: {err}") from err
            except RokiError as err:
                raise HomeAssistantError(f"Could not turn off the hood: {err}") from err
            self.coordinator.async_apply(
                self._device_id, {"workStatus": 0, "working_status": 0}
            )
            self.coordinator.async_schedule_refresh(self._device_id)
            return

        levels = self.levels
        names = level_names_for(levels)
        if option not in names:
            raise HomeAssistantError(
                f"Unknown hood gear {option!r}; "
                f"this hood offers: {', '.join(self.options)}"
            )
        level = levels[names.index(option)]
        try:
            if not self.is_on:
                await self.coordinator.async_execute(
                    self._device_id, "power", on=True
                )
            await self.coordinator.async_execute(
                self._device_id, "level", level=level
            )
        except RokiAuthError as err:
            raise HomeAssistantError(f"ROKI session expired: {err}") from err
        except RokiError as err:
            raise HomeAssistantError(f"Could not set the fan gear: {err}") from err
        self.coordinator.async_apply(
            self._device_id, {"workStatus": 1, "level": level}
        )
        self.coordinator.async_schedule_refresh(self._device_id)
