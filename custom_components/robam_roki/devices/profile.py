"""Runtime driver generated from a YAML profile.

A ProfileDriver layers one resolved profile (custom_components/robam_roki/
profiles.py) over an optional Python base driver:

* the base driver's specs come first and keep their order;
* a profile entry for an existing key overrides presentation fields (name,
  icon, unit, device_class, state_class, entity_category) and, when it has
  keys, the value extraction as well;
* a profile entry for a new key becomes a fresh read-only entity;
* commands() is empty in P1, so no control platform generates anything for a
  profile-driven device -- fan/light/number/select all gate on capabilities.

Nothing here touches the appliance: entities are pure reads of coordinator data.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import EntityCategory

from ..profiles import TYPE_CONVERTERS, Profile
from .base import BinarySensorSpec, CommandSpec, RokiDeviceDriver, SensorSpec

_LOGGER = logging.getLogger(__name__)


class _Missing:
    """Sentinel telling a value pipeline that nothing usable was reported."""


MISSING = _Missing()


def _first_value(properties: dict[str, Any], keys: list[str]) -> Any:
    """The first non-empty property among keys, or MISSING."""
    for key in keys:
        if key not in properties:
            continue
        value = properties[key]
        if value is None or value == "":
            continue
        return value
    return MISSING


def _convert(value: Any, type_name: str | None) -> Any:
    """Apply the profile's type conversion; MISSING when it cannot be done."""
    if type_name is None:
        return value
    if type_name == "bool":
        # bool("0") is True, so strings get an explicit truth table.
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"1", "true", "on", "yes"}:
                return True
            if lowered in {"0", "false", "off", "no", ""}:
                return False
        return bool(value)
    converter = TYPE_CONVERTERS.get(type_name)
    if converter is None:
        return value
    try:
        return converter(value)
    except (TypeError, ValueError):
        return MISSING


def _sensor_value_fn(entry: dict[str, Any]) -> Callable[[dict[str, Any]], Any]:
    """Build the value closure for one profile sensor entry.

    Pipeline: first non-empty key -> type conversion -> scale -> map, with the
    entry's default used when no key matched, the conversion failed or the map
    missed.
    """
    keys = list(entry.get("keys") or [])
    type_name = entry.get("type")
    scale = entry.get("scale")
    mapping = entry.get("map")
    has_default = "default" in entry
    default = entry.get("default")

    def value_fn(properties: dict[str, Any]) -> Any:
        raw = _first_value(properties, keys)
        if raw is MISSING:
            return default if has_default else None
        value = _convert(raw, type_name)
        if value is MISSING:
            return default if has_default else raw
        if (
            scale is not None
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        ):
            value = value * scale
        if mapping is not None:
            try:
                return mapping[value]
            except (KeyError, TypeError):
                return default if has_default else value
        return value

    return value_fn


def _binary_value_fn(entry: dict[str, Any]) -> Callable[[dict[str, Any]], bool | None]:
    """Build the on/off closure for one profile binary sensor entry."""
    keys = list(entry.get("keys") or [])
    on_values = entry.get("on_values")

    def is_on_fn(properties: dict[str, Any]) -> bool | None:
        raw = _first_value(properties, keys)
        if raw is MISSING:
            return None
        if on_values is not None:
            return raw in on_values
        return bool(raw)

    return is_on_fn


def _enum_value(enum_cls: Any, value: Any, where: str, label: str) -> Any:
    """Convert a profile string to a Home Assistant enum, warning on typos."""
    if not value:
        return None
    try:
        return enum_cls(value)
    except (TypeError, ValueError):
        _LOGGER.warning("%s: unknown %s %r, ignored", where, label, value)
        return None


def _entity_category(value: Any, where: str) -> Any:
    if not value:
        return None
    try:
        return EntityCategory(value)
    except (TypeError, ValueError):
        _LOGGER.warning("%s: unknown entity_category %r, ignored", where, value)
        return None


class ProfileDriver(RokiDeviceDriver):
    """Read-only driver generated from one resolved YAML profile."""

    def __init__(
        self,
        coordinator: Any,
        device: dict[str, Any],
        profile: Profile,
        base: RokiDeviceDriver | None = None,
    ) -> None:
        super().__init__(coordinator, device)
        self.profile = profile
        self.base = base
        self.category_code = profile.dc
        self.category_name = profile.name or "Profile"

    def commands(self) -> dict[str, CommandSpec]:
        """P1 exposes no controls: a YAML profile is read-only telemetry only."""
        return {}

    # -- sensors ----------------------------------------------------------

    def sensors(self) -> list[SensorSpec]:
        """Base specs first, profile entries layered on top."""
        specs: dict[str, SensorSpec] = {}
        order: list[str] = []
        if self.base is not None:
            for spec in self.base.sensors():
                specs[spec.key] = spec
                order.append(spec.key)
        for key, entry in self.profile.sensors.items():
            base_spec = specs.get(key)
            if base_spec is None:
                if not entry.get("keys"):
                    _LOGGER.warning(
                        "profile %s: sensor %r has no keys and no base spec, "
                        "skipped",
                        self.profile.id,
                        key,
                    )
                    continue
                specs[key] = self._new_sensor(key, entry)
                order.append(key)
            else:
                specs[key] = self._override_sensor(key, base_spec, entry)
        return [specs[key] for key in order]

    def _new_sensor(self, key: str, entry: dict[str, Any]) -> SensorSpec:
        where = f"profile {self.profile.id}: sensor {key}"
        return SensorSpec(
            key=key,
            translation_key=key,
            name=entry.get("name"),
            icon=entry.get("icon"),
            device_class=_enum_value(
                SensorDeviceClass,
                entry.get("device_class"),
                where,
                "device_class",
            ),
            state_class=_enum_value(
                SensorStateClass,
                entry.get("state_class"),
                where,
                "state_class",
            ),
            unit_of_measurement=entry.get("unit"),
            entity_category=_entity_category(entry.get("entity_category"), where),
            value_fn=_sensor_value_fn(entry),
        )

    def _override_sensor(
        self, key: str, spec: SensorSpec, entry: dict[str, Any]
    ) -> SensorSpec:
        where = f"profile {self.profile.id}: sensor {key}"
        changes: dict[str, Any] = {}
        if "name" in entry:
            changes["name"] = entry["name"]
        if "icon" in entry:
            changes["icon"] = entry["icon"]
        if "unit" in entry:
            changes["unit_of_measurement"] = entry["unit"]
        if "device_class" in entry:
            device_class = _enum_value(
                SensorDeviceClass,
                entry["device_class"],
                where,
                "device_class",
            )
            if device_class is not None:
                changes["device_class"] = device_class
        if "state_class" in entry:
            state_class = _enum_value(
                SensorStateClass,
                entry["state_class"],
                where,
                "state_class",
            )
            if state_class is not None:
                changes["state_class"] = state_class
        if "entity_category" in entry:
            category = _entity_category(entry["entity_category"], where)
            if category is not None:
                changes["entity_category"] = category
        if entry.get("keys"):
            changes["value_fn"] = _sensor_value_fn(entry)
        return replace(spec, **changes)

    # -- binary sensors ---------------------------------------------------

    def binary_sensors(self) -> list[BinarySensorSpec]:
        """Base specs first, profile entries layered on top."""
        specs: dict[str, BinarySensorSpec] = {}
        order: list[str] = []
        if self.base is not None:
            for spec in self.base.binary_sensors():
                specs[spec.key] = spec
                order.append(spec.key)
        for key, entry in self.profile.binary_sensors.items():
            base_spec = specs.get(key)
            if base_spec is None:
                if not entry.get("keys"):
                    _LOGGER.warning(
                        "profile %s: binary sensor %r has no keys and no base "
                        "spec, skipped",
                        self.profile.id,
                        key,
                    )
                    continue
                specs[key] = self._new_binary_sensor(key, entry)
                order.append(key)
            else:
                specs[key] = self._override_binary_sensor(key, base_spec, entry)
        return [specs[key] for key in order]

    def _new_binary_sensor(
        self, key: str, entry: dict[str, Any]
    ) -> BinarySensorSpec:
        where = f"profile {self.profile.id}: binary_sensor {key}"
        return BinarySensorSpec(
            key=key,
            translation_key=key,
            name=entry.get("name"),
            icon=entry.get("icon"),
            device_class=_enum_value(
                BinarySensorDeviceClass,
                entry.get("device_class"),
                where,
                "device_class",
            ),
            entity_category=_entity_category(entry.get("entity_category"), where),
            is_on_fn=_binary_value_fn(entry),
        )

    def _override_binary_sensor(
        self, key: str, spec: BinarySensorSpec, entry: dict[str, Any]
    ) -> BinarySensorSpec:
        where = f"profile {self.profile.id}: binary_sensor {key}"
        changes: dict[str, Any] = {}
        if "name" in entry:
            changes["name"] = entry["name"]
        if "icon" in entry:
            changes["icon"] = entry["icon"]
        if "device_class" in entry:
            device_class = _enum_value(
                BinarySensorDeviceClass,
                entry["device_class"],
                where,
                "device_class",
            )
            if device_class is not None:
                changes["device_class"] = device_class
        if "entity_category" in entry:
            category = _entity_category(entry["entity_category"], where)
            if category is not None:
                changes["entity_category"] = category
        if entry.get("keys"):
            changes["is_on_fn"] = _binary_value_fn(entry)
        return replace(spec, **changes)
