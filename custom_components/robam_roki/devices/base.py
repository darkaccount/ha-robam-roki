"""Base driver and entity specifications for ROBAM / ROKI appliances."""

from __future__ import annotations

import re
from collections.abc import Iterable
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

#: The capabilities a dedicated platform already renders as a rich entity
#: (fan, light, number, select).  The button platform consumes whatever is left
#: over, so a learned/declared command can never appear twice.
STANDARD_CAPABILITIES = frozenset({"power", "level", "light", "timing"})

#: The largest raw MQTT payload a declarative command may carry.
MAX_MQTT_BODY_BYTES = 64

#: body_hex must be an even-length run of hex digits (empty allowed).
_HEX_BODY = re.compile(r"(?:[0-9a-fA-F]{2})*")


@dataclass(frozen=True)
class CommandSpec:
    """Declarative description of one command an appliance accepts.

    kind="property" writes the on/off payload through the cloud property API;
    kind="invoke" calls a vendor function identified by function and passes
    param/params; kind="mqtt" sends one raw frame (command + body_hex).
    name is the display label a declarative source (a YAML profile) declared
    for the command; the button platform uses it, falling back to the key.
    verified records whether the command has been confirmed on real hardware:
    a spec stays in commands() once declared, but an unverified one is hidden
    from the platforms unless the allow_unverified_controls option is on.
    """

    kind: Literal["property", "invoke", "mqtt"]
    name: str | None = None
    on: dict[str, Any] | None = None
    off: dict[str, Any] | None = None
    function: str | None = None
    param: str | None = None
    params: dict[str, Any] | None = None
    command: int | None = None
    body_hex: str | None = None
    verified: bool = True

    def __post_init__(self) -> None:
        """Validate the raw-frame fields at construction time."""
        if self.kind == "mqtt":
            if self.command is None:
                raise ValueError("mqtt commands need a command number")
            if self.body_hex is None:
                raise ValueError("mqtt commands need a body_hex payload")
        if self.body_hex is not None:
            body = str(self.body_hex).strip()
            if not _HEX_BODY.fullmatch(body):
                raise ValueError(
                    f"body_hex must be an even-length hex string: "
                    f"{self.body_hex!r}"
                )
            if len(body) // 2 > MAX_MQTT_BODY_BYTES:
                raise ValueError(
                    f"body_hex must be at most {MAX_MQTT_BODY_BYTES} bytes"
                )


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

    def available_capabilities(
        self, *, allow_unverified: bool = False
    ) -> frozenset[str]:
        """Capabilities a platform may expose right now.

        An unverified command stays declared in commands() but is hidden from
        the UI unless the user explicitly enables experimental controls.
        """
        if allow_unverified:
            return self.capabilities
        return frozenset(
            name for name, spec in self.commands().items() if spec.verified
        )

    def sensors(self) -> list[SensorSpec]:
        """Return sensor specifications for this device category."""
        return []

    def binary_sensors(self) -> list[BinarySensorSpec]:
        """Return binary sensor specifications for this device category."""
        return []


def allowed_capabilities(
    driver: RokiDeviceDriver, coordinator: Any
) -> frozenset[str]:
    """Capabilities a platform may generate, honouring the unverified gate.

    The coordinator owns the option (it knows the config entry); test doubles
    that predate it simply expose the driver's verified capabilities.
    """
    allow = getattr(coordinator, "allow_unverified_controls", None)
    allowed = bool(allow()) if callable(allow) else False
    available = getattr(driver, "available_capabilities", None)
    if not callable(available):
        return frozenset(driver.capabilities)
    return available(allow_unverified=allowed)


def experimental_attributes(
    driver: Any, capabilities: Iterable[str]
) -> dict[str, Any]:
    """Mark an entity as experimental when one of its commands is unverified."""
    commands = getattr(driver, "commands", None)
    if not callable(commands):
        return {}
    specs = commands()
    if any(
        name in specs and not specs[name].verified
        for name in capabilities
    ):
        return {"experimental": True}
    return {}
