"""Platform gating: entity generation follows driver capabilities.

Every platform used to filter devices on a hard-coded category check
(dc in ('', 'RYYJ')).  The platform now asks the driver what a device can do
and generates an entity only for the matching capabilities.  A fake coordinator
serves a mixed fleet here -- MQTT hood, REST hood, cooking appliance, unknown
category -- and each platform is asserted to pick exactly the capable devices.

The integration is not importable outside Home Assistant, so the platforms are
loaded in an isolated package with the HA symbols stubbed out, same as the
sibling entity tests.
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
PACKAGE = "roki_gating_pkg_test"


def _component_module(name: str) -> types.ModuleType:
    """Register (or reuse) one homeassistant.components submodule."""
    components = sys.modules["homeassistant.components"]
    module = sys.modules.get(f"homeassistant.components.{name}")
    if module is None:
        module = types.ModuleType(f"homeassistant.components.{name}")
        sys.modules[f"homeassistant.components.{name}"] = module
    setattr(components, name, module)
    return module


def _install_ha_stubs() -> None:
    """Add the platform symbols on top of the shared stub tree."""
    sys.modules["homeassistant"].components = sys.modules[
        "homeassistant.components"
    ]

    fan_mod = _component_module("fan")

    class FanEntity:
        pass

    class FanEntityFeature:
        TURN_ON = 1
        TURN_OFF = 2
        SET_SPEED = 4
        PRESET_MODE = 8

    fan_mod.FanEntity = FanEntity
    fan_mod.FanEntityFeature = FanEntityFeature

    light_mod = _component_module("light")

    class ColorMode:
        ONOFF = "onoff"

    class LightEntity:
        pass

    light_mod.ColorMode = ColorMode
    light_mod.LightEntity = LightEntity

    number_mod = _component_module("number")

    class NumberEntity:
        pass

    class NumberMode:
        SLIDER = "slider"
        BOX = "box"

    number_mod.NumberEntity = NumberEntity
    number_mod.NumberMode = NumberMode

    select_mod = _component_module("select")

    class SelectEntity:
        pass

    select_mod.SelectEntity = SelectEntity


def _load(name: str, relative: str) -> types.ModuleType:
    """Load a component module under a real package so imports resolve."""
    package = sys.modules.get(PACKAGE)
    if package is None:
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


def _stub_coordinator() -> None:
    """The platform modules only need the coordinator for type hints."""
    module = types.ModuleType(f"{PACKAGE}.coordinator")

    class RokiCoordinator:
        pass

    module.RokiCoordinator = RokiCoordinator
    sys.modules[f"{PACKAGE}.coordinator"] = module


def _stub_entity() -> None:
    """Replace entity.py with a minimal CoordinatorEntity stand-in."""
    module = types.ModuleType(f"{PACKAGE}.entity")

    class RokiEntity:
        _attr_has_entity_name = True

        def __init__(self, coordinator: Any, device: dict[str, Any]) -> None:
            self.coordinator = coordinator
            self._device = device
            self._device_id = str(device["guid"])

        @property
        def model_code(self) -> str:
            return str(self._device.get("dt") or "").strip()

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

    module.RokiEntity = RokiEntity
    sys.modules[f"{PACKAGE}.entity"] = module


_install_ha_stubs()
_stub_coordinator()
_stub_entity()
const = _load("const", "const.py")
fan = _load("fan", "fan.py")
light = _load("light", "light.py")
number = _load("number", "number.py")
select = _load("select", "select.py")

PLATFORMS = {
    "fan": fan,
    "light": light,
    "number": number,
    "select": select,
}

REQUIRED = {
    "fan": {"power", "level"},
    "light": {"light"},
    "number": {"timing"},
    "select": {"level"},
}


# -- the fleet -----------------------------------------------------------

MQTT_HOOD = {
    "guid": "H0000000000000001",
    "name": "hood",
    "dc": "RYYJ",
    "dt": "R8229",
    "deviceSource": 0,
}
REST_HOOD = {
    "guid": "H0000000000000002",
    "name": "hood-rest",
    "dc": "RYYJ",
    "dt": "R8229",
    "deviceSource": 1,
}
# No dc: the factory falls back to the hood driver, so this device used to pass
# the old dc in ('', 'RYYJ') filter and must keep its entities.
NO_DC_HOOD = {"guid": "H0000000000000003", "name": "hood-nodc", "dt": "82-i1"}
THERMAL = {
    "guid": "T0000000000000001",
    "name": "steam-oven",
    "dc": "RZQL",
    "dt": "C98A",
    "deviceSource": 0,
}
GENERIC = {
    "guid": "G0000000000000001",
    "name": "robot",
    "dc": "SPECIAL_ROBOT",
    "deviceSource": 0,
}

FLEET = [MQTT_HOOD, REST_HOOD, NO_DC_HOOD, THERMAL, GENERIC]
HOOD_GUIDS = {MQTT_HOOD["guid"], REST_HOOD["guid"], NO_DC_HOOD["guid"]}


class _FakeCoordinator:
    """A mixed fleet plus the one coordinator method drivers need."""

    def __init__(self, devices: list[dict[str, Any]]) -> None:
        self.devices = [dict(device) for device in devices]
        self.data = {str(device["guid"]): {} for device in self.devices}
        self.calls: list[tuple[str, Any]] = []

    def is_rest_device(self, guid: str) -> bool:
        for device in self.devices:
            if str(device.get("guid")) == str(guid):
                return str(device.get("deviceSource")) == "1"
        return False

    async def async_set_power(self, device_id: str, on: bool) -> None:
        self.calls.append(("power", on))

    async def async_set_level(self, device_id: str, level: int) -> None:
        self.calls.append(("level", level))

    async def async_set_light(self, device_id: str, on: bool) -> None:
        self.calls.append(("light", on))

    async def async_set_timing(
        self, device_id: str, minutes: int, *, level: int | None = None
    ) -> None:
        self.calls.append(("timing", minutes, level))

    def async_apply(self, device_id: str, properties: dict[str, Any]) -> None:
        self.data.setdefault(device_id, {}).update(properties)

    def async_schedule_refresh(
        self, device_id: str | None = None, delay: float = 2.0
    ) -> None:
        pass


class _FakeEntry:
    def __init__(self, coordinator: _FakeCoordinator) -> None:
        self.runtime_data = types.SimpleNamespace(coordinator=coordinator)
        self.options: dict[str, Any] = {}


class _CapabilityDriver:
    """Minimal driver stand-in used to prove the gate reads capabilities."""

    def __init__(self, capabilities: set[str]) -> None:
        self.capabilities = frozenset(capabilities)


def _setup(platform: types.ModuleType, coordinator: _FakeCoordinator) -> list[Any]:
    added: list[Any] = []
    entry = _FakeEntry(coordinator)
    asyncio.run(platform.async_setup_entry(None, entry, added.extend))
    return added


# -- gating --------------------------------------------------------------


@pytest.mark.parametrize(("name", "required"), sorted(REQUIRED.items()))
def test_matcher_covers_every_platform(name: str, required: set[str]) -> None:
    # Guards against a platform drifting from its documented capability set.
    assert PLATFORMS[name]._REQUIRED_CAPABILITIES == frozenset(required)


@pytest.mark.parametrize("name", sorted(PLATFORMS))
def test_only_capable_devices_get_entities(name: str) -> None:
    coordinator = _FakeCoordinator(FLEET)
    entities = _setup(PLATFORMS[name], coordinator)
    assert {entity._device_id for entity in entities} == HOOD_GUIDS


@pytest.mark.parametrize("name", sorted(PLATFORMS))
def test_thermal_and_generic_devices_get_no_entities(name: str) -> None:
    coordinator = _FakeCoordinator([THERMAL, GENERIC])
    assert _setup(PLATFORMS[name], coordinator) == []


@pytest.mark.parametrize("name", sorted(PLATFORMS))
def test_gating_follows_capabilities_not_category(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    platform = PLATFORMS[name]
    required = set(REQUIRED[name])
    # The cooking appliance is outside the old hood whitelist, so the old dc
    # filter would never generate an entity for it.  With a driver advertising
    # the required capabilities it must be picked.
    coordinator = _FakeCoordinator([THERMAL])
    monkeypatch.setattr(
        platform, "get_driver_for_device", lambda *_: _CapabilityDriver(required)
    )
    assert len(_setup(platform, coordinator)) == 1

    # One capability short is enough to be left out, hood or not.
    partial = set(required)
    partial.discard(min(required))
    monkeypatch.setattr(
        platform, "get_driver_for_device", lambda *_: _CapabilityDriver(partial)
    )
    assert _setup(platform, coordinator) == []


# -- the hood fleet keeps every entity -----------------------------------


def test_hood_gets_exactly_four_entity_types() -> None:
    coordinator = _FakeCoordinator([MQTT_HOOD])
    unique_ids: set[str] = set()
    for platform in PLATFORMS.values():
        unique_ids.update(
            entity._attr_unique_id
            for entity in _setup(platform, coordinator)
        )
    guid = MQTT_HOOD["guid"]
    assert unique_ids == {
        f"{guid}_fan",
        f"{guid}_light",
        f"{guid}_countdown",
        f"{guid}_gear",
    }


@pytest.mark.parametrize("device", [MQTT_HOOD, REST_HOOD, NO_DC_HOOD])
def test_each_hood_gets_one_entity_per_platform(device: dict[str, Any]) -> None:
    coordinator = _FakeCoordinator([device])
    counts = {
        name: len(_setup(platform, coordinator))
        for name, platform in PLATFORMS.items()
    }
    assert counts == {"fan": 1, "light": 1, "number": 1, "select": 1}


def test_setup_sends_no_commands() -> None:
    # Assembling the platforms must never touch the appliance: no power, no
    # gear, no probe.
    coordinator = _FakeCoordinator(FLEET)
    for platform in PLATFORMS.values():
        _setup(platform, coordinator)
    assert coordinator.calls == []


# -- the select's 关档 entry needs the power capability ---------------------


def test_hood_select_offers_off_with_its_real_driver() -> None:
    coordinator = _FakeCoordinator([MQTT_HOOD])
    entity = select.RokiHoodGearSelect(coordinator, dict(MQTT_HOOD))
    entity.driver = fan.get_driver_for_device(coordinator, dict(MQTT_HOOD))
    assert entity.options == ["关档", "弱档", "中档", "强档", "爆炒档"]


def test_off_option_requires_the_power_capability() -> None:
    coordinator = _FakeCoordinator([MQTT_HOOD])
    entity = select.RokiHoodGearSelect(coordinator, dict(MQTT_HOOD))

    entity.driver = _CapabilityDriver({"level", "light", "timing"})
    assert "关档" not in entity.options
    assert entity.options == ["弱档", "中档", "强档", "爆炒档"]

    entity.driver = _CapabilityDriver({"power", "level"})
    assert entity.options[0] == "关档"
