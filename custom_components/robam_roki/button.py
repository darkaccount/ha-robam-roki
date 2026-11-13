"""Button platform: one pressable entity per declarative control.

A profile's ``controls`` section describes commands the appliance accepts but
that no dedicated platform renders -- typically the frames a learning session
captured from the official App (``kind: mqtt``) or a named vendor function
without parameters (``kind: invoke``).  Each of those becomes a button, so a
learned feature shows up in Home Assistant instead of silently widening the
driver's capability table.

Two kinds stay out on purpose:

* the capabilities a rich platform already renders (power/level/light/timing:
  fan, light, number, select) are never duplicated as buttons;
* ``kind: property`` writes through the REST property API, which is a P4 item,
  so it is skipped with a debug log instead of pretending to work.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import RokiAuthError, RokiError
from .coordinator import RokiCoordinator
from .devices import get_driver_for_device
from .devices.base import (
    STANDARD_CAPABILITIES,
    CommandSpec,
    allowed_capabilities,
    experimental_attributes,
)
from .entity import RokiEntity

_LOGGER = logging.getLogger(__name__)

# This platform consumes whatever the standard capabilities left over, so its
# "required capability" is the empty set (the per-control gate lives in
# pressable_capabilities).
_REQUIRED_CAPABILITIES: frozenset[str] = frozenset()


def pressable_capabilities(driver: Any, coordinator: Any) -> list[str]:
    """The control keys of a device that deserve a button, in driver order.

    A key qualifies when the platform gate allows it (declared and, unless the
    experimental-controls option is on, verified) and its spec can be sent as a
    single press: a raw MQTT frame, or a vendor function call with no
    parameter to fill in.
    """
    commands = getattr(driver, "commands", None)
    if not callable(commands):
        return []
    specs: dict[str, CommandSpec] = commands() or {}
    allowed = allowed_capabilities(driver, coordinator)
    keys: list[str] = []
    for key, spec in specs.items():
        if key in STANDARD_CAPABILITIES or key not in allowed:
            continue
        if spec.kind == "mqtt":
            keys.append(key)
        elif spec.kind == "invoke":
            if spec.param:
                _LOGGER.debug(
                    "control %r needs a value for %r, so it needs its own "
                    "entity, not a button",
                    key,
                    spec.param,
                )
                continue
            keys.append(key)
        else:
            _LOGGER.debug(
                "control %r is kind=%s: property writes land with the REST "
                "write path (P4), no button generated",
                key,
                spec.kind,
            )
    return keys


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: RokiCoordinator = entry.runtime_data.coordinator
    entities: list[ButtonEntity] = []
    for device in coordinator.devices:
        driver = get_driver_for_device(coordinator, device)
        for key in pressable_capabilities(driver, coordinator):
            entities.append(RokiControlButton(coordinator, device, key))
    async_add_entities(entities)


class RokiControlButton(RokiEntity, ButtonEntity):
    """One declarative control as a stateless button."""

    _attr_icon = "mdi:gesture-tap-button"

    def __init__(
        self,
        coordinator: RokiCoordinator,
        device: dict[str, Any],
        key: str,
    ) -> None:
        super().__init__(coordinator, device)
        self.control_key = key
        self._attr_unique_id = f"{self._device_id}_control_{key}"
        # A name declared on the control wins over the key, exactly like a
        # profile sensor's name wins over its translation key.
        self._attr_name = self._spec_name() or key
        self._attr_has_entity_name = True

    def _spec(self) -> CommandSpec | None:
        commands = getattr(self.driver, "commands", None)
        if not callable(commands):
            return None
        return (commands() or {}).get(self.control_key)

    def _spec_name(self) -> str | None:
        name = getattr(self._spec(), "name", None)
        return str(name) if name else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return experimental_attributes(
            getattr(self, "driver", None), (self.control_key,)
        )

    async def async_press(self) -> None:
        """Send the control this button stands for."""
        try:
            await self.coordinator.async_execute(self._device_id, self.control_key)
        except RokiAuthError as err:
            raise HomeAssistantError(f"ROKI session expired: {err}") from err
        except RokiError as err:
            raise HomeAssistantError(
                f"Could not press {self._attr_name!r}: {err}"
            ) from err
        # A raw frame's effect is only visible in the next state report, so ask
        # for one instead of guessing an optimistic state.
        self.coordinator.async_schedule_refresh(self._device_id)
