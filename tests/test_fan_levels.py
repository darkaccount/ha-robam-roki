"""Entity level tests for the ROBAM / ROKI fan platform.

The integration is not importable outside Home Assistant, so the fan module is
loaded in isolation with the Home Assistant symbols stubbed out.  What matters
here is the percentage <-> level mapping and that nothing ever probes the
device during setup.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

COMPONENT = (
    Path(__file__).resolve().parents[1] / "custom_components" / "robam_roki"
)


def _install_ha_stubs() -> None:
    """Register just enough Home Assistant for fan.py to import.

    ``tests/conftest.py`` already laid down a shared ``homeassistant`` package
    tree; this fills in the fan component symbols on top of it instead of
    replacing the tree (replacing it broke the sibling test modules).
    """
    components = sys.modules["homeassistant.components"]
    fan_mod = sys.modules.get("homeassistant.components.fan")
    if fan_mod is None:
        fan_mod = types.ModuleType("homeassistant.components.fan")
        sys.modules["homeassistant.components.fan"] = fan_mod
    components.fan = fan_mod

    class FanEntity:
        pass

    class FanEntityFeature:
        TURN_ON = 1
        TURN_OFF = 2
        SET_SPEED = 4

    fan_mod.FanEntity = FanEntity
    fan_mod.FanEntityFeature = FanEntityFeature


def _load_package_module(name: str, relative: str) -> types.ModuleType:
    """Load a component module under a real package so relative imports work."""
    package_name = "roki_fan_pkg_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(COMPONENT)]
    sys.modules[package_name] = package

    path = COMPONENT / relative
    spec = importlib.util.spec_from_file_location(
        f"{package_name}.{name}", path
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"{package_name}.{name}"] = module
    spec.loader.exec_module(module)
    return module


def _stub_coordinator() -> None:
    """fan.py only needs the coordinator for type hints."""
    package_name = "roki_fan_pkg_test"
    module = types.ModuleType(f"{package_name}.coordinator")

    class RokiCoordinator:
        pass

    module.RokiCoordinator = RokiCoordinator
    sys.modules[f"{package_name}.coordinator"] = module


def _stub_entity() -> None:
    """Replace entity.py with a minimal CoordinatorEntity stand-in."""
    package_name = "roki_fan_pkg_test"
    module = types.ModuleType(f"{package_name}.entity")

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
    sys.modules[f"{package_name}.entity"] = module


_install_ha_stubs()
_stub_coordinator()
_stub_entity()
const = _load_package_module("const", "const.py")
fan = _load_package_module("fan", "fan.py")


class _FakeCoordinator:
    """Records every command so tests can prove nothing was sent."""

    def __init__(self, entry: Any = None) -> None:
        self.entry = entry
        self.data: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, Any]] = []

    async def async_set_power(self, device_id: str, on: bool) -> None:
        self.calls.append(("power", on))

    async def async_set_level(self, device_id: str, level: int) -> None:
        self.calls.append(("level", level))

    async def async_set_light(self, device_id: str, on: bool) -> None:
        self.calls.append(("light", on))

    def async_apply(self, device_id: str, properties: dict[str, Any]) -> None:
        self.data.setdefault(device_id, {}).update(properties)

    def async_schedule_refresh(self, device_id: str) -> None:
        pass


class _FakeEntry:
    def __init__(self, options: dict[str, Any]) -> None:
        self.options = options


DEVICE = {
    "guid": "R8229000000000000",
    "name": "油烟机8229",
    "dt": "R8229",
    "deviceSource": 0,
}


def _make_entity(
    model: str | None = "R8229",
    properties: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
    max_level: int | None = None,
) -> Any:
    coordinator = _FakeCoordinator(_FakeEntry(options or {}))
    device = dict(DEVICE)
    if model is None:
        device.pop("dt")
    else:
        device["dt"] = model
    guid = device["guid"]
    coordinator.data[guid] = properties or {}
    return fan.RokiHoodFan(coordinator, device, max_level)


# -- ladder resolution ---------------------------------------------------


def test_r8229_uses_the_four_gear_ladder():
    entity = _make_entity()
    assert entity.levels == [1, 2, 3, 6]
    assert entity.speed_count == 4


def test_two_gear_model_has_two_steps():
    entity = _make_entity("R66A2")
    assert entity.levels == [2, 6]
    assert entity.speed_count == 2


def test_unknown_model_falls_back_to_max_level():
    entity = _make_entity("R9999", max_level=4)
    assert entity.levels == [1, 2, 3, 4]


def test_option_overrides_the_model_table():
    entity = _make_entity("R8229", options={"levels": "2,4"})
    assert entity.levels == [2, 4]


def test_option_overrides_even_a_known_model():
    entity = _make_entity("R66A2", options={"levels": "1,2,3,4,5,6"})
    assert entity.levels == [1, 2, 3, 4, 5, 6]


def test_bad_option_falls_back_to_the_table():
    entity = _make_entity("R8229", options={"levels": "3,1,oops"})
    assert entity.levels == [1, 2, 3, 6]


def test_blank_option_falls_back_to_the_table():
    entity = _make_entity("R8229", options={"levels": "  "})
    assert entity.levels == [1, 2, 3, 6]


def test_parse_levels_accepts_full_width_commas():
    assert fan._parse_levels("1，2，3") == [1, 2, 3]
    assert fan._parse_levels("6,3,1") == [1, 3, 6]
    assert fan._parse_levels("") == []
    assert fan._parse_levels(None) == []
    assert fan._parse_levels("1,1") == []
    assert fan._parse_levels("0,2") == []
    assert fan._parse_levels("a") == []

# -- percentage mapping --------------------------------------------------


def test_off_state_reports_zero_percent():
    entity = _make_entity(properties={"workStatus": 0, "level": 0})
    assert entity.is_on is False
    assert entity.percentage == 0


def test_r8229_levels_map_to_the_slider_steps():
    for level, expected in ((1, 25), (2, 50), (3, 75), (6, 100)):
        entity = _make_entity(properties={"workStatus": 1, "level": level})
        assert entity.percentage == expected, level


def test_r8229_slider_steps_map_back_to_the_same_levels():
    for percentage, expected in ((1, 1), (25, 1), (50, 2), (75, 3), (100, 6)):
        entity = _make_entity(properties={"workStatus": 1, "level": 1})
        asyncio.run(entity.async_set_percentage(percentage))
        assert entity.coordinator.calls[-1] == ("level", expected), percentage


def test_every_gear_is_reachable_from_the_card():
    # The regression: with ``len - 1`` as the divisor two card positions
    # collapsed onto level 3 and level 1 was unreachable, so a four-gear hood
    # only ever received three distinct commands.
    entity = _make_entity(properties={"workStatus": 1, "level": 1})
    for percentage, expected in ((25, 1), (50, 2), (75, 3), (100, 6)):
        asyncio.run(entity.async_set_percentage(percentage))
        assert entity.coordinator.calls[-1] == ("level", expected), percentage


def test_two_gear_mapping():
    entity = _make_entity("R66A2", properties={"workStatus": 1, "level": 2})
    assert entity.percentage == 50
    asyncio.run(entity.async_set_percentage(100))
    assert entity.coordinator.calls[-1] == ("level", 6)
    asyncio.run(entity.async_set_percentage(50))
    assert entity.coordinator.calls[-1] == ("level", 2)


def test_unknown_reported_level_is_clamped():
    # A level outside the ladder must not crash or report 0 %.
    entity = _make_entity(properties={"workStatus": 1, "level": 4})
    assert entity.percentage == 75


def test_the_lowest_gear_is_not_reported_as_off():
    # 0 % is how Home Assistant reads "off"; the lowest gear must not land on
    # it or the card shows a running hood as stopped.
    entity = _make_entity(properties={"workStatus": 1, "level": 1})
    assert entity.percentage > 0


def test_percentage_round_trip_is_stable_for_every_known_model():
    for model in const.MODEL_LEVELS:
        entity = _make_entity(model, properties={"workStatus": 1, "level": 0})
        for level in entity.levels:
            entity.coordinator.async_apply(entity._device_id, {"level": level})
            percentage = entity.percentage
            assert percentage > 0, (model, level)
            asyncio.run(entity.async_set_percentage(percentage))
            assert entity.coordinator.calls[-1] == ("level", level), (model, level)


# -- commands ------------------------------------------------------------


def test_turn_on_powers_up_first():
    entity = _make_entity(properties={"workStatus": 0, "level": 0})
    asyncio.run(entity.async_turn_on())
    assert entity.coordinator.calls == [("power", True)]


def test_turn_on_with_percentage_also_sets_the_level():
    entity = _make_entity(properties={"workStatus": 0, "level": 0})
    asyncio.run(entity.async_turn_on(percentage=100))
    assert entity.coordinator.calls == [("power", True), ("level", 6)]


def test_turn_on_while_running_does_not_repeat_the_power_command():
    entity = _make_entity(properties={"workStatus": 1, "level": 2})
    asyncio.run(entity.async_turn_on())
    assert entity.coordinator.calls == []


def test_set_percentage_while_off_powers_up_first():
    entity = _make_entity(properties={"workStatus": 0, "level": 0})
    asyncio.run(entity.async_set_percentage(50))
    assert entity.coordinator.calls == [("power", True), ("level", 2)]


def test_percentage_zero_turns_the_hood_off():
    entity = _make_entity(properties={"workStatus": 1, "level": 3})
    asyncio.run(entity.async_set_percentage(0))
    assert entity.coordinator.calls == [("power", False)]


def test_turn_off_sends_only_the_power_command():
    entity = _make_entity(properties={"workStatus": 1, "level": 3})
    asyncio.run(entity.async_turn_off())
    assert entity.coordinator.calls == [("power", False)]


def test_extra_attributes_expose_the_ladder():
    entity = _make_entity(properties={"workStatus": 1, "level": 6})
    attributes = entity.extra_state_attributes
    assert attributes["levels"] == [1, 2, 3, 6]
    assert attributes["max_level"] == 6
    assert attributes["level"] == 6
    assert attributes["device_id"] == "R8229000000000000"


def test_setup_sends_no_commands():
    # The regression that matters: constructing the entity must not touch the
    # device, otherwise adding the integration would start the motor.
    coordinator = _FakeCoordinator(_FakeEntry({}))
    for model in (*const.MODEL_LEVELS, "R9999"):
        fan.RokiHoodFan(coordinator, {**DEVICE, "dt": model})
    assert coordinator.calls == []
