"""Base driver and entity specifications for ROBAM / ROKI appliances."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import EntityCategory

from ..coordinator import RokiCoordinator

# The capability contract every driver speaks.  A platform asks a driver what
# it can do and only generates entities for the matching capabilities; nothing
# filters on a device's category code any more.
Capability = Literal["power", "level", "light", "timing"]


@dataclass(frozen=True)
class CommandSpec:
    """Declarative description of one command an appliance accepts.

    kind="property" writes the on/off payload through the cloud property API;
    kind="invoke" calls a vendor function identified by function and passes
    param/params.  verified records whether the command has been confirmed on
    real hardware: a spec stays in commands() once declared, but an unverified
    one must not be wired to a user-facing control yet.
    """

    kind: Literal["property", "invoke"]
    on: dict[str, Any] | None = None
    off: dict[str, Any] | None = None
    function: str | None = None
    param: str | None = None
    params: dict[str, Any] | None = None
    verified: bool = True


@dataclass
class SensorSpec:
    """Specification for a telemetry/diagnostic sensor entity."""

    key: str
    translation_key: str
    name: str | None = None
    icon: str | None = None
    device_class: SensorDeviceClass | None = None
    state_class: SensorStateClass | None = None
    unit_of_measurement: str | None = None
    entity_category: EntityCategory | None = None
    value_fn: Callable[[dict[str, Any]], Any] | None = None
    attrs_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None


@dataclass
class BinarySensorSpec:
    """Specification for a boolean/diagnostic binary sensor entity."""

    key: str
    translation_key: str
    name: str | None = None
    icon: str | None = None
    device_class: BinarySensorDeviceClass | None = None
    entity_category: EntityCategory | None = None
    is_on_fn: Callable[[dict[str, Any]], bool | None] | None = None
    attrs_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None


class RokiDeviceDriver:
    """Base driver representing a category of ROBAM kitchen appliances."""

    category_code: str = ""
    category_name: str = "Device"

    def __init__(self, coordinator: RokiCoordinator, device: dict[str, Any]) -> None:
        self.coordinator = coordinator
        self.device = device
        self.device_id = str(device.get("guid", ""))
        self.dt = str(device.get("dt", "")).strip()
        self.dc = str(device.get("dc", "")).strip()

    @property
    def properties(self) -> dict[str, Any]:
        """Return the current device state properties from coordinator."""
        return (self.coordinator.data or {}).get(self.device_id, {})

    def prop(self, *keys: str, default: Any = None) -> Any:
        """Read the first matching property in current device state."""
        props = self.properties
        for k in keys:
            if k in props:
                return props[k]
        return default

    def prop_int(self, *keys: str, default: int = 0) -> int:
        val = self.prop(*keys, default=default)
        try:
            return int(val)
        except (TypeError, ValueError):
            return default

    def commands(self) -> dict[str, CommandSpec]:
        """Commands this appliance accepts, keyed by capability name.

        An instance method on purpose: one driver class serves devices on both
        cloud transports, and what a single device can do depends on the
        device, not just on its category (see HoodDriver).
        """
        return {}

    @property
    def capabilities(self) -> frozenset[str]:
        """What this device can do, derived from commands().

        Derived rather than declared so the two can never disagree: the command
        table is the single source of truth for platform gating.
        """
        return frozenset(self.commands())

    def sensors(self) -> list[SensorSpec]:
        """Return sensor specifications for this device category."""
        return []

    def binary_sensors(self) -> list[BinarySensorSpec]:
        """Return binary sensor specifications for this device category."""
        return []
