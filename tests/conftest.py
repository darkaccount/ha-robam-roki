"""Shared Home Assistant stubs for the offline test suite.

The integration is only importable inside Home Assistant, so every test module
that loads a component file stubs the symbols it needs.  Those stubs used to
fight each other: whichever module ran first decided the shape of the
``homeassistant`` package tree, and a later module could no longer register
``homeassistant.components.fan`` or ``homeassistant.components.number``.

This conftest lays down a single, permissive ``homeassistant`` package that
every module extends.  The ``__path__`` entries are empty, so nothing here can
ever shadow a real Home Assistant install.

The Home Assistant enums that the driver layer stores on every entity spec
(device_class / state_class / entity_category) are *not* permissive stubs:
they are faithful ``StrEnum`` stand-ins with the member names and values Home
Assistant uses.  A permissive stub made every value look valid, so a typo in a
YAML profile was silently accepted, and the two ways of building an enum
(``SensorDeviceClass.DURATION`` versus the profile layer's
``SensorDeviceClass("duration")``) could not be compared at all -- which is
exactly what the behaviour golden files in ``fixtures/behavior/`` are for.
"""

from __future__ import annotations

import sys
import types
from enum import StrEnum
from typing import Any


class _Stub:
    """Attribute sink that answers any access with another stub."""

    def __init__(self, name: str = "stub") -> None:
        self._name = name

    def __getattr__(self, item: str) -> Any:
        value = _Stub(f"{self._name}.{item}")
        setattr(self, item, value)
        return value

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return _Stub(f"{self._name}()")

    def __or__(self, other: Any) -> _Stub:
        return self

    def __ror__(self, other: Any) -> _Stub:
        return self


# -- faithful Home Assistant enums ---------------------------------------
#
# These mirror the StrEnum classes Home Assistant ships.  Only the members this
# integration (or a YAML profile in the built-in directory) can name are
# declared: an unknown value has to fail here rather than silently pass.


class BinarySensorDeviceClass(StrEnum):
    """homeassistant.components.binary_sensor.BinarySensorDeviceClass."""

    BATTERY = "battery"
    CONNECTIVITY = "connectivity"
    DOOR = "door"
    MOISTURE = "moisture"
    MOVING = "moving"
    OCCUPANCY = "occupancy"
    OPENING = "opening"
    PLUG = "plug"
    POWER = "power"
    PROBLEM = "problem"
    RUNNING = "running"
    SAFETY = "safety"
    SMOKE = "smoke"
    WINDOW = "window"


class SensorDeviceClass(StrEnum):
    """homeassistant.components.sensor.SensorDeviceClass."""

    BATTERY = "battery"
    CURRENT = "current"
    DURATION = "duration"
    ENERGY = "energy"
    HUMIDITY = "humidity"
    POWER = "power"
    SIGNAL_STRENGTH = "signal_strength"
    TEMPERATURE = "temperature"
    VOLTAGE = "voltage"
    WATER = "water"


class SensorStateClass(StrEnum):
    """homeassistant.components.sensor.SensorStateClass."""

    MEASUREMENT = "measurement"
    TOTAL = "total"
    TOTAL_INCREASING = "total_increasing"


class EntityCategory(StrEnum):
    """homeassistant.const.EntityCategory."""

    CONFIG = "config"
    DIAGNOSTIC = "diagnostic"


def _ensure_package(name: str) -> types.ModuleType:
    module = sys.modules.get(name)
    if module is None:
        module = types.ModuleType(name)
        sys.modules[name] = module
    module.__path__ = []
    return module


def _ensure(name: str) -> types.ModuleType:
    module = sys.modules.get(name)
    if module is None:
        module = types.ModuleType(name)
        sys.modules[name] = module
    return module


def _flow_base() -> type:
    """A ConfigFlow/OptionsFlow base that swallows the class keyword arguments.

    It also implements the two response shapes a menu flow needs: a form
    description (with its description placeholders) and a menu.  hass and
    config_entry are plain attributes so a test can hand the flow a double; the
    real Home Assistant assigns both before the first step runs.
    """

    class _ConfigFlowBase:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.hass = _Stub("hass")
            self.config_entry = None
            self.context: dict[str, object] = {}

        def __init_subclass__(cls, **kwargs: object) -> None:
            super().__init_subclass__()

        def async_show_form(
            self,
            *,
            step_id: str,
            data_schema: object = None,
            errors: dict[str, str] | None = None,
            description_placeholders: dict[str, str] | None = None,
            **kwargs: object,
        ) -> dict[str, Any]:
            return {
                "type": "form",
                "step_id": step_id,
                "data_schema": data_schema,
                "errors": errors or {},
                "description_placeholders": description_placeholders or {},
            }

        def async_show_menu(
            self,
            *,
            step_id: str,
            menu_options: object,
            description_placeholders: object = None,
            menu_option_descriptions: object = None,
            **kwargs: object,
        ) -> dict[str, Any]:
            return {
                "type": "menu",
                "step_id": step_id,
                "menu_options": list(menu_options),  # type: ignore[call-overload]
                "description_placeholders": description_placeholders or {},
            }

        def async_create_entry(self, **kwargs: object) -> dict[str, Any]:
            return {"type": "create_entry", **kwargs}

        def async_abort(self, **kwargs: object) -> dict[str, Any]:
            return {"type": "abort", **kwargs}

        async def async_set_unique_id(self, unique_id: str) -> None:
            self._unique_id = unique_id

        def _abort_if_unique_id_configured(self) -> None:
            return None

    return _ConfigFlowBase


def install_ha_stubs() -> None:
    """Lay down the shared HA package tree once per test session."""
    ha = _ensure_package("homeassistant")

    components = _ensure_package("homeassistant.components")
    ha.components = components

    core = _ensure("homeassistant.core")
    core.HomeAssistant = _Stub("HomeAssistant")
    core.callback = lambda func: func
    ha.core = core

    exceptions = _ensure("homeassistant.exceptions")
    exceptions.HomeAssistantError = type("HomeAssistantError", (Exception,), {})
    exceptions.ConfigEntryAuthFailed = type(
        "ConfigEntryAuthFailed", (Exception,), {}
    )
    exceptions.ConfigEntryNotReady = type(
        "ConfigEntryNotReady", (Exception,), {}
    )
    exceptions.ServiceValidationError = type(
        "ServiceValidationError", (Exception,), {}
    )
    ha.exceptions = exceptions

    diagnostics = _ensure_package("homeassistant.components.diagnostics")
    def async_redact_data(data: Any, to_redact: Any) -> Any:
        if isinstance(data, dict):
            return {
                k: "**REDACTED**" if k in to_redact else async_redact_data(v, to_redact)
                for k, v in data.items()
            }
        if isinstance(data, list):
            return [async_redact_data(item, to_redact) for item in data]
        return data
    diagnostics.async_redact_data = async_redact_data
    ha.components.diagnostics = diagnostics

    binary_sensor = _ensure_package("homeassistant.components.binary_sensor")
    binary_sensor.BinarySensorDeviceClass = BinarySensorDeviceClass
    binary_sensor.BinarySensorEntity = type("BinarySensorEntity", (), {})
    ha.components.binary_sensor = binary_sensor

    sensor = _ensure_package("homeassistant.components.sensor")
    sensor.SensorDeviceClass = SensorDeviceClass
    sensor.SensorStateClass = SensorStateClass
    sensor.SensorEntity = type("SensorEntity", (), {})
    ha.components.sensor = sensor

    config_entries = _ensure_package("homeassistant.config_entries")
    config_entries.ConfigEntry = _Stub("ConfigEntry")
    config_entries.ConfigFlow = _flow_base()
    config_entries.ConfigFlowResult = type("ConfigFlowResult", (), {})
    config_entries.OptionsFlow = _flow_base()
    ha.config_entries = config_entries

    helpers = _ensure_package("homeassistant.helpers")
    ha.helpers = helpers

    update_coordinator = _ensure("homeassistant.helpers.update_coordinator")

    class DataUpdateCoordinator:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.data = None
            self.update_interval = kwargs.get("update_interval")

        def __class_getitem__(cls, item: object) -> type:
            return cls

        def async_set_updated_data(self, data: object) -> None:
            self.data = data
    update_coordinator.DataUpdateCoordinator = DataUpdateCoordinator
    update_coordinator.UpdateFailed = type("UpdateFailed", (Exception,), {})
    helpers.update_coordinator = update_coordinator

    entity_platform = _ensure("homeassistant.helpers.entity_platform")
    entity_platform.AddEntitiesCallback = _Stub("AddEntitiesCallback")
    helpers.entity_platform = entity_platform

    config_validation = _ensure("homeassistant.helpers.config_validation")

    class _Invalid(Exception):
        pass

    class multi_select:
        """Copied from homeassistant/helpers/config_validation.py."""

        def __init__(self, options: dict | list) -> None:
            self.options = options

        def __call__(self, selected: list) -> list:
            if not isinstance(selected, list):
                raise _Invalid("Not a list")
            for value in selected:
                if value not in self.options:
                    raise _Invalid(f"{value} is not a valid option")
            return selected

    config_validation.multi_select = multi_select

    def config_entry_only_config_schema(domain: str) -> dict:
        """Minimal stand-in: the real one logs when YAML setup is attempted."""
        return {}

    config_validation.config_entry_only_config_schema = config_entry_only_config_schema
    helpers.config_validation = config_validation

    selector = _ensure_package("homeassistant.helpers.selector")

    class DeviceSelectorConfig(dict):
        """TypedDict stand-in: the real one is called with keyword arguments."""

    class DeviceSelector:
        """The only selector this integration uses so far."""

        def __init__(self, config: Any = None) -> None:
            self.config = config

    selector.DeviceSelectorConfig = DeviceSelectorConfig
    selector.DeviceSelector = DeviceSelector
    helpers.selector = selector

    aiohttp_client = _ensure("homeassistant.helpers.aiohttp_client")
    aiohttp_client.async_get_clientsession = lambda hass: None
    helpers.aiohttp_client = aiohttp_client

    device_registry = _ensure("homeassistant.helpers.device_registry")
    device_registry.DeviceInfo = _Stub("DeviceInfo")
    device_registry.DeviceEntry = _Stub("DeviceEntry")
    helpers.device_registry = device_registry

    const = _ensure("homeassistant.const")
    const.Platform = _Stub("Platform")
    const.EntityCategory = EntityCategory

    class UnitOfTime:
        MINUTES = "min"
        SECONDS = "s"
        HOURS = "h"

    class UnitOfTemperature:
        CELSIUS = "°C"
        FAHRENHEIT = "°F"

    const.UnitOfTime = UnitOfTime
    const.UnitOfTemperature = UnitOfTemperature
    ha.const = const


install_ha_stubs()
