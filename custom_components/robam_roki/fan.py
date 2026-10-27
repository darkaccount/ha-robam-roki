"""Fan platform for the ROBAM / ROKI integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import RokiAuthError, RokiError
from .const import (
    CONF_LEVELS,
    DEFAULT_MAX_LEVEL,
    known_levels_for_model,
    levels_source_for_model,
)
from .coordinator import RokiCoordinator
from .entity import RokiEntity

_LOGGER = logging.getLogger(__name__)


def _parse_levels(value: Any) -> list[int]:
    """Parse a comma separated ladder such as ``1,2,3,6``.

    Full-width commas (``，``) are accepted because Chinese IMEs produce them.
    Returns an empty list for anything that is not a clean ascending ladder of
    positive integers, so a bad option falls through to the model table.
    """
    if value is None:
        return []
    if isinstance(value, (int, float)):
        value = [value]
    if isinstance(value, str):
        chunks: list[Any] = value.replace("，", ",").split(",")
    elif isinstance(value, (list, tuple)):
        chunks = list(value)
    else:
        return []
    levels: list[int] = []
    for chunk in chunks:
        text = str(chunk).strip()
        if not text:
            continue
        try:
            number = int(text)
        except ValueError:
            return []
        if number <= 0 or number in levels:
            return []
        levels.append(number)
    return sorted(levels)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: RokiCoordinator = entry.runtime_data.coordinator
    async_add_entities(
        RokiHoodFan(coordinator, device)
        for device in coordinator.devices
        if str(device.get("dc", "")).upper() in ("", "RYYJ")
    )


class RokiHoodFan(RokiEntity, FanEntity):
    """A range hood: power plus fan level.

    The gear ladder comes from a static per-model table (``const.MODEL_LEVELS``),
    an option override, or a linear ``1..max_level`` fallback -- never from the
    device.  Percentage maps onto that ladder, so a R8229 exposes 0 / 33 / 67 /
    100 % for its 1 / 2 / 3 / 6 gears instead of a fake linear 1..6 scale.
    """

    _attr_name = None
    _attr_supported_features = (
        FanEntityFeature.TURN_ON
        | FanEntityFeature.TURN_OFF
        | FanEntityFeature.SET_SPEED
    )

    def __init__(
        self,
        coordinator: RokiCoordinator,
        device: dict[str, Any],
        max_level: int | None = None,
    ) -> None:
        super().__init__(coordinator, device)
        # Kept for backwards compatibility with the previous constructor; the
        # option now only feeds the fallback ladder for unknown models.
        self._max_level = max(2, int(max_level or DEFAULT_MAX_LEVEL))
        # Static ladder for this model, empty when the table does not know it.
        self._model_levels = known_levels_for_model(self.model_code)
        self._attr_unique_id = f"{self._device_id}_fan"

    # -- gear ladder ----------------------------------------------------

    @property
    def levels(self) -> list[int]:
        """Levels this hood supports, ascending.

        Precedence: an explicit option override, then the static model table,
        then a linear ``1..max_level`` ladder.  Nothing here asks the device
        for anything -- sending an unsupported level would start the motor.
        """
        override = _parse_levels(self._levels_option)
        if override:
            return override
        if self._model_levels:
            return list(self._model_levels)
        return list(range(1, self._max_level + 1))

    @property
    def _levels_option(self) -> Any:
        entry = getattr(self.coordinator, "entry", None)
        if entry is None:
            return None
        return entry.options.get(CONF_LEVELS)

    @property
    def speed_count(self) -> int:
        return len(self.levels)

    # -- state ----------------------------------------------------------

    @property
    def is_on(self) -> bool:
        return self.prop_int("workStatus", "working_status") != 0

    @property
    def percentage(self) -> int | None:
        if not self.is_on:
            return 0
        level = self.prop_int("level")
        levels = self.levels
        if level <= 0 or not levels:
            return None
        if level not in levels:
            # The hood reports a level our ladder does not list; clamp so the
            # UI still shows something sane rather than 0 %.
            level = min(levels, key=lambda item: abs(item - level))
        # Divide by the number of gears, not by ``len - 1``.  With ``-1`` the
        # lowest gear maps to 0 %, which Home Assistant reads as "off", and
        # two card positions collapse onto the same level.
        return round(100 * (levels.index(level) + 1) / len(levels))

    # -- commands -------------------------------------------------------

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        try:
            if not self.is_on:
                await self.coordinator.async_set_power(self._device_id, True)
                self.coordinator.async_apply(self._device_id, {"workStatus": 1})
            if percentage is not None:
                await self.async_set_percentage(percentage)
        except RokiAuthError as err:
            raise HomeAssistantError(f"ROKI session expired: {err}") from err
        except RokiError as err:
            raise HomeAssistantError(f"Could not turn on the hood: {err}") from err
        self.coordinator.async_schedule_refresh(self._device_id)

    async def async_turn_off(self, **kwargs: Any) -> None:
        try:
            await self.coordinator.async_set_power(self._device_id, False)
        except RokiAuthError as err:
            raise HomeAssistantError(f"ROKI session expired: {err}") from err
        except RokiError as err:
            raise HomeAssistantError(f"Could not turn off the hood: {err}") from err
        self.coordinator.async_apply(
            self._device_id, {"workStatus": 0, "working_status": 0}
        )
        self.coordinator.async_schedule_refresh(self._device_id)

    async def async_set_percentage(self, percentage: int) -> None:
        if percentage <= 0:
            await self.async_turn_off()
            return

        levels = self.levels
        if not levels:
            raise HomeAssistantError(
                "No fan levels are configured for this hood; "
                "set them in the integration options."
            )
        # Inverse of the percentage property: the card's positions are
        # 100/n, 2·100/n, ..., 100 %, so dividing by the gear count keeps the
        # round trip exact and leaves every gear reachable from the card.
        index = min(len(levels) - 1, max(0, round(percentage * len(levels) / 100) - 1))
        level = levels[index]

        try:
            if not self.is_on:
                await self.coordinator.async_set_power(self._device_id, True)
            await self.coordinator.async_set_level(self._device_id, level)
        except RokiAuthError as err:
            raise HomeAssistantError(f"ROKI session expired: {err}") from err
        except RokiError as err:
            raise HomeAssistantError(f"Could not set the fan level: {err}") from err

        self.coordinator.async_apply(
            self._device_id, {"workStatus": 1, "level": level}
        )
        self.coordinator.async_schedule_refresh(self._device_id)

    @property
    def levels_source(self) -> str:
        """Where :attr:`levels` came from, so users can judge its trust.

        ``option`` beats ``hardware``: an explicit override is what the user
        asked for, whatever the table says.
        """
        if _parse_levels(self._levels_option):
            return "option"
        return levels_source_for_model(self.model_code)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "level": self.prop_int("level"),
            "levels": self.levels,
            "levels_source": self.levels_source,
            "max_level": self.levels[-1] if self.levels else None,
            "work_status": self.prop_int("workStatus", "working_status"),
            "fault_code": self.prop("faultCode", "fault_code"),
            "device_id": self._device_id,
        }
