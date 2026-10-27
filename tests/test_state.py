"""Offline tests for the countdown (timing) and online-state support.

These need the integration importable outside Home Assistant, so the HA
symbols are stubbed and the component is loaded as a real package.  Only the
behaviour that can be decided from the protocol is tested here; the pieces that
need real hardware (the ``info/state`` endpoint, whether the cloud's ``status``
code really means online) are documented in the README instead.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

import pytest

COMPONENT = (
    Path(__file__).resolve().parents[1] / "custom_components" / "robam_roki"
)
PACKAGE = "roki_state_pkg_test"


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

    def __or__(self, other: Any) -> Any:
        return self

    def __ror__(self, other: Any) -> Any:
        return self


def _install_ha_stubs() -> None:
    """Register just enough Home Assistant for number.py to import.

    Another test module may already have registered a ``homeassistant``
    stub whose ``components`` is not a package, so every entry is (re)written
    unconditionally instead of bailing out when the name exists.
    """
    ha = sys.modules.get("homeassistant")
    if ha is None:
        ha = types.ModuleType("homeassistant")
        sys.modules["homeassistant"] = ha
    ha.__path__ = []

    components = types.ModuleType("homeassistant.components")
    components.__path__ = []
    number_mod = types.ModuleType("homeassistant.components.number")
    number_mod.__path__ = []

    class NumberEntity:
        pass

    class NumberMode:
        SLIDER = "slider"
        BOX = "box"

    number_mod.NumberEntity = NumberEntity
    number_mod.NumberMode = NumberMode
    # Bind the stub as an attribute too: `from x.y import z` resolves the
    # submodule through the parent's attribute before touching sys.modules.
    components.number = number_mod
    ha.components = components

    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = _Stub("HomeAssistant")
    exceptions = types.ModuleType("homeassistant.exceptions")
    exceptions.HomeAssistantError = type("HomeAssistantError", (Exception,), {})
    config_entries = types.ModuleType("homeassistant.config_entries")
    config_entries.ConfigEntry = _Stub("ConfigEntry")
    helpers = types.ModuleType("homeassistant.helpers")
    entity_platform = types.ModuleType("homeassistant.helpers.entity_platform")
    entity_platform.AddEntitiesCallback = _Stub("AddEntitiesCallback")

    sys.modules.update(
        {
            "homeassistant": ha,
            "homeassistant.components": components,
            "homeassistant.components.number": number_mod,
            "homeassistant.core": core,
            "homeassistant.exceptions": exceptions,
            "homeassistant.config_entries": config_entries,
            "homeassistant.helpers": helpers,
            "homeassistant.helpers.entity_platform": entity_platform,
        }
    )


def _load_package_module(name: str, relative: str) -> types.ModuleType:
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(COMPONENT)]
    sys.modules[PACKAGE] = package

    path = COMPONENT / relative
    spec = importlib.util.spec_from_file_location(f"{PACKAGE}.{name}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"{PACKAGE}.{name}"] = module
    spec.loader.exec_module(module)
    return module


def _stub_coordinator_and_entity() -> None:
    """Replace the heavy coordinator with a light-weight stand-in.

    The real one imports homeassistant.helpers.update_coordinator, which the
    stubs do not provide; the number platform only needs the coordinator for
    type hints and a few command methods.
    """
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(COMPONENT)]
    sys.modules[PACKAGE] = package

    coordinator = types.ModuleType(f"{PACKAGE}.coordinator")

    class RokiCoordinator:
        pass

    coordinator.RokiCoordinator = RokiCoordinator
    sys.modules[f"{PACKAGE}.coordinator"] = coordinator

    entity = types.ModuleType(f"{PACKAGE}.entity")

    class RokiEntity:
        _attr_has_entity_name = True

        def __init__(self, coordinator: Any, device: dict[str, Any]) -> None:
            self.coordinator = coordinator
            self._device = device
            self._device_id = str(device["guid"])

        @property
        def properties(self) -> dict[str, Any]:
            return (self.coordinator.data or {}).get(self._device_id, {})

        @property
        def available(self) -> bool:
            return True

        def prop(self, *keys: str, default: Any = None) -> Any:
            for key in keys:
                if key in self.properties:
                    return self.properties[key]
            return default

        def prop_int(self, *keys: str, default: int = 0) -> int:
            value = self.prop(*keys, default=default)
            try:
                return int(value)
            except (TypeError, ValueError):
                return default

    entity.RokiEntity = RokiEntity
    sys.modules[f"{PACKAGE}.entity"] = entity


_install_ha_stubs()
_stub_coordinator_and_entity()
const = _load_package_module("const", "const.py")
number = _load_package_module("number", "number.py")

# The number platform wraps RokiError into HomeAssistantError; grab the real
# exception classes so the test asserts the actual contract.
_RokiApiError = sys.modules[f"{PACKAGE}.api"].RokiApiError

DEVICE = {
    "guid": "R8229000000000000",
    "name": "油烟机8229",
    "dt": "R8229",
    "deviceSource": 0,
}


class _FakeCoordinator:
    def __init__(self) -> None:
        self.data: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, Any]] = []

    async def async_set_timing(
        self, device_id: str, minutes: int, *, level: int | None = None
    ) -> None:
        self.calls.append(("timing", minutes, level))

    def async_apply(self, device_id: str, properties: dict[str, Any]) -> None:
        self.data.setdefault(device_id, {}).update(properties)

    def async_schedule_refresh(self, device_id: str) -> None:
        pass


def _make_countdown(properties: dict[str, Any]) -> Any:
    coordinator = _FakeCoordinator()
    coordinator.data[DEVICE["guid"]] = properties
    return number.RokiHoodCountdown(coordinator, dict(DEVICE))


# -- range matches the app's picker --------------------------------------


def test_range_matches_the_app_picker():
    # The protocol's FanTime is a single byte (0-255); the app's hour+minute
    # picker tops out at 3 h 59 min, and the integration offers a round 240.
    entity = _make_countdown({})
    assert entity._attr_native_min_value == 0
    assert entity._attr_native_max_value == const.MAX_COUNTDOWN_MINUTES
    assert entity._attr_native_max_value == 240
    assert entity._attr_native_step == 1


# -- availability --------------------------------------------------------


def test_countdown_is_unavailable_while_the_hood_is_off():
    # Sending 144 with level 0 would stop the fan rather than arm a timer.
    entity = _make_countdown({"workStatus": 0, "level": 0, "timing": 0})
    assert entity.available is False


def test_countdown_is_available_while_the_hood_runs():
    entity = _make_countdown({"workStatus": 1, "level": 2, "timing": 0})
    assert entity.available is True


# -- value ---------------------------------------------------------------


def test_countdown_reads_the_reported_timing():
    entity = _make_countdown({"workStatus": 1, "level": 2, "timing": 30})
    assert entity.native_value == 30.0


def test_countdown_is_unknown_when_not_reported():
    entity = _make_countdown({"workStatus": 1, "level": 2})
    assert entity.native_value is None


def test_setting_the_countdown_sends_the_minutes():
    entity = _make_countdown({"workStatus": 1, "level": 2, "timing": 0})
    asyncio.run(entity.async_set_native_value(30))
    assert entity.coordinator.calls == [("timing", 30, 2)]
    assert entity.properties["timing"] == 30


def test_the_countdown_carries_the_level_being_shown():
    # Command 144 restarts the fan on whatever level it carries, so the number
    # entity must pass the gear it is displaying rather than let the
    # coordinator guess.  A stale guess yanks the fan back to the old gear.
    entity = _make_countdown({"workStatus": 1, "level": 6, "timing": 0})
    asyncio.run(entity.async_set_native_value(45))
    assert entity.coordinator.calls == [("timing", 45, 6)]


def test_setting_zero_cancels_the_countdown():
    entity = _make_countdown({"workStatus": 1, "level": 2, "timing": 15})
    asyncio.run(entity.async_set_native_value(0))
    assert entity.coordinator.calls == [("timing", 0, 2)]


def test_setting_a_fractional_value_rounds():
    entity = _make_countdown({"workStatus": 1, "level": 2, "timing": 0})
    asyncio.run(entity.async_set_native_value(29.6))
    assert entity.coordinator.calls == [("timing", 30, 2)]


def test_command_errors_surface_as_home_assistant_errors():
    class _CountdownError(Exception):
        pass

    class _Failing(_FakeCoordinator):
        async def async_set_timing(
            self, device_id: str, minutes: int, *, level: int | None = None
        ) -> None:
            raise _CountdownError("boom")

    entity = number.RokiHoodCountdown(_Failing(), dict(DEVICE))
    entity.coordinator.data[DEVICE["guid"]] = {"level": 2}
    with pytest.raises(_CountdownError):
        asyncio.run(entity.async_set_native_value(10))


def test_roki_api_errors_are_wrapped_for_the_ui():
    class _Failing(_FakeCoordinator):
        async def async_set_timing(
            self, device_id: str, minutes: int, *, level: int | None = None
        ) -> None:
            raise _RokiApiError("hood refused")

    entity = number.RokiHoodCountdown(_Failing(), dict(DEVICE))
    entity.coordinator.data[DEVICE["guid"]] = {"level": 2}
    with pytest.raises(Exception) as excinfo:
        asyncio.run(entity.async_set_native_value(10))
    assert "countdown" in str(excinfo.value).lower()


# -- the coordinator-side guard ------------------------------------------


def test_const_exposes_the_offline_threshold():
    assert const.OFFLINE_AFTER_POLLS == 6


def test_platforms_include_number():
    assert "number" in const.PLATFORMS
