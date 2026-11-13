"""Tests for the hood gear (档位) select entity.

Same offline harness as ``test_fan_levels.py``: the component is loaded under
a fake package with just enough Home Assistant stubbed out to import, and the
fake coordinator records every command so tests can prove what was sent.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from homeassistant.exceptions import HomeAssistantError

COMPONENT = (
    Path(__file__).resolve().parents[1] / "custom_components" / "robam_roki"
)


def _install_ha_stubs() -> None:
    """Add the select component symbols on top of the shared stub tree."""
    components = sys.modules["homeassistant.components"]
    select_mod = sys.modules.get("homeassistant.components.select")
    if select_mod is None:
        select_mod = types.ModuleType("homeassistant.components.select")
        sys.modules["homeassistant.components.select"] = select_mod
    components.select = select_mod

    class SelectEntity:
        pass

    select_mod.SelectEntity = SelectEntity


def _load_package_module(name: str, relative: str) -> types.ModuleType:
    """Load a component module under a real package so relative imports work."""
    package_name = "roki_select_pkg_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(COMPONENT)]
    sys.modules[package_name] = package

    path = COMPONENT / relative
    spec = importlib.util.spec_from_file_location(f"{package_name}.{name}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"{package_name}.{name}"] = module
    spec.loader.exec_module(module)
    return module


def _stub_coordinator() -> None:
    package_name = "roki_select_pkg_test"
    module = types.ModuleType(f"{package_name}.coordinator")

    class RokiCoordinator:
        pass

    module.RokiCoordinator = RokiCoordinator
    sys.modules[f"{package_name}.coordinator"] = module


def _stub_entity() -> None:
    """Replace entity.py with a minimal CoordinatorEntity stand-in."""
    package_name = "roki_select_pkg_test"
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
select = _load_package_module("select", "select.py")


class _FakeCoordinator:
    def __init__(self, entry: Any = None) -> None:
        self.entry = entry
        self.data: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, Any]] = []

    async def async_set_power(self, device_id: str, on: bool) -> None:
        self.calls.append(("power", on))

    async def async_set_level(self, device_id: str, level: int) -> None:
        self.calls.append(("level", level))

    async def async_execute(
        self, device_id: str, capability: str, **kwargs: Any
    ) -> None:
        if capability == "power":
            await self.async_set_power(device_id, kwargs["on"])
        elif capability == "level":
            await self.async_set_level(device_id, kwargs["level"])
        else:
            raise AssertionError(f"unexpected capability {capability}")

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
) -> Any:
    coordinator = _FakeCoordinator(_FakeEntry(options or {}))
    device = dict(DEVICE)
    if model is None:
        device.pop("dt")
    else:
        device["dt"] = model
    guid = device["guid"]
    coordinator.data[guid] = properties or {}
    return select.RokiHoodGearSelect(coordinator, device)


# -- options -------------------------------------------------------------


def test_options_off_plus_the_ladder_labels():
    entity = _make_entity()
    assert entity.options == ["关档", "弱档", "中档", "强档", "爆炒档"]


def test_82_i1_options_match_the_huawei_select():
    # select.you_yan_ji_82_i1_dang_wei on the Huawei side offers exactly
    # 关档/弱档/强档/爆炒档; automations migrating over must see the same list.
    entity = _make_entity("82-i1")
    assert entity.options == ["关档", "弱档", "强档", "爆炒档"]


def test_options_follow_the_levels_option():
    entity = _make_entity(options={"levels": "1,6"})
    assert entity.options == ["关档", "弱档", "爆炒档"]


# -- state ---------------------------------------------------------------


def test_current_option_follows_the_running_gear():
    entity = _make_entity("82-i1", properties={"workStatus": 1, "level": 3})
    assert entity.current_option == "强档"


def test_current_option_reads_off_when_stopped():
    entity = _make_entity(properties={"workStatus": 0, "level": 0})
    assert entity.current_option == "关档"


def test_current_option_reads_off_even_with_a_residual_level():
    entity = _make_entity(properties={"workStatus": 0, "level": 6})
    assert entity.current_option == "关档"


def test_current_option_is_none_for_an_unknown_gear():
    entity = _make_entity("82-i1", properties={"workStatus": 1, "level": 2})
    assert entity.current_option is None


# -- commands ------------------------------------------------------------


def test_select_gear_sets_the_level():
    entity = _make_entity("82-i1", properties={"workStatus": 1, "level": 1})
    asyncio.run(entity.async_select_option("爆炒档"))
    assert entity.coordinator.calls == [("level", 6)]
    assert entity.properties["level"] == 6


def test_select_gear_while_off_powers_up_first():
    entity = _make_entity("82-i1", properties={"workStatus": 0, "level": 0})
    asyncio.run(entity.async_select_option("强档"))
    assert entity.coordinator.calls == [("power", True), ("level", 3)]


def test_select_off_turns_the_hood_off():
    entity = _make_entity(properties={"workStatus": 1, "level": 6})
    asyncio.run(entity.async_select_option("关档"))
    assert entity.coordinator.calls == [("power", False)]
    assert entity.properties["workStatus"] == 0


def test_select_off_while_already_off_sends_nothing_new():
    entity = _make_entity(properties={"workStatus": 0, "level": 0})
    asyncio.run(entity.async_select_option("关档"))
    # The power command is still issued: 关档 is an explicit user intent.
    assert entity.coordinator.calls == [("power", False)]


def test_select_rejects_unknown_labels():
    entity = _make_entity(properties={"workStatus": 1, "level": 1})
    with pytest.raises(HomeAssistantError):
        asyncio.run(entity.async_select_option("涡轮档"))
    assert entity.coordinator.calls == []


def test_setup_sends_no_commands():
    coordinator = _FakeCoordinator(_FakeEntry({}))
    for model in (*const.MODEL_LEVELS, "R9999"):
        select.RokiHoodGearSelect(coordinator, {**DEVICE, "dt": model})
    assert coordinator.calls == []
