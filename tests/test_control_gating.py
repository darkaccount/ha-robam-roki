"""Unverified controls: hidden by default, experiment-opt-in, marked in the UI.

The gate is double: the platforms do not generate the entity unless the option
is on, and the executor refuses an unverified command even if some older entity
still tries to send it.  This module exercises the platform half with a real
platform (light) and a driver supplying one declarative command.
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
PACKAGE = "roki_control_gate_pkg_test"
GUID = "R8229000000000000"
DEVICE = {
    "guid": GUID,
    "name": "hood",
    "dc": "RYYJ",
    "dt": "R8229",
    "deviceSource": 0,
}


def _module(name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__path__ = []
    return module


def _install_ha_stubs() -> None:
    ha = sys.modules["homeassistant"]
    components = sys.modules["homeassistant.components"]
    ha.components = components
    light_mod = sys.modules.get("homeassistant.components.light")
    if light_mod is None:
        light_mod = types.ModuleType("homeassistant.components.light")
        sys.modules["homeassistant.components.light"] = light_mod
    components.light = light_mod

    class LightEntity:
        pass

    class ColorMode:
        ONOFF = "onoff"

    light_mod.LightEntity = LightEntity
    light_mod.ColorMode = ColorMode


def _stub_package() -> None:
    package = _module(PACKAGE)
    package.__path__ = [str(COMPONENT)]
    sys.modules[PACKAGE] = package

    coordinator = _module(f"{PACKAGE}.coordinator")

    class RokiCoordinator:
        pass

    coordinator.RokiCoordinator = RokiCoordinator
    sys.modules[f"{PACKAGE}.coordinator"] = coordinator

    entity = _module(f"{PACKAGE}.entity")

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

    devices = _module(f"{PACKAGE}.devices")
    devices.__path__ = [str(COMPONENT / "devices")]
    devices.get_driver_for_device = lambda *_args, **_kwargs: None
    sys.modules[f"{PACKAGE}.devices"] = devices


def _load(name: str, relative: str) -> types.ModuleType:
    path = COMPONENT / relative
    spec = importlib.util.spec_from_file_location(f"{PACKAGE}.{name}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"{PACKAGE}.{name}"] = module
    spec.loader.exec_module(module)
    return module


_install_ha_stubs()
_stub_package()
base = _load("devices.base", "devices/base.py")
sys.modules[f"{PACKAGE}.devices"].base = base
light = _load("light", "light.py")


class _SpecDriver(base.RokiDeviceDriver):
    """A driver exposing one declarative light command."""

    def __init__(self, spec: Any) -> None:
        self._spec = spec
        self.coordinator = None
        self.device = dict(DEVICE)
        self.device_id = GUID
        self.dt = "R8229"
        self.dc = "RYYJ"

    def commands(self) -> dict[str, Any]:
        return {"light": self._spec}

    def sensors(self) -> list[Any]:
        return []

    def binary_sensors(self) -> list[Any]:
        return []


class _FakeCoordinator:
    def __init__(self, allow_unverified: bool) -> None:
        self.devices = [dict(DEVICE)]
        self.data = {GUID: {}}
        self.allow = allow_unverified

    def allow_unverified_controls(self) -> bool:
        return self.allow

    def is_rest_device(self, guid: str) -> bool:
        return False


class _FakeEntry:
    def __init__(self, coordinator: _FakeCoordinator) -> None:
        self.runtime_data = types.SimpleNamespace(coordinator=coordinator)


def _light_spec(verified: bool) -> Any:
    return base.CommandSpec(
        kind="property",
        on={"light_status": 1},
        off={"light_status": 0},
        verified=verified,
    )


def _setup(
    coordinator: _FakeCoordinator,
    driver: _SpecDriver,
    monkeypatch: pytest.MonkeyPatch,
) -> list[Any]:
    monkeypatch.setattr(light, "get_driver_for_device", lambda *_args: driver)
    added: list[Any] = []
    asyncio.run(
        light.async_setup_entry(None, _FakeEntry(coordinator), added.extend)
    )
    return added


@pytest.mark.parametrize(
    ("verified", "allow", "expected"),
    [
        (True, False, 1),
        (True, True, 1),
        (False, False, 0),
        (False, True, 1),
    ],
)
def test_platform_generation_honours_both_gates(
    verified: bool,
    allow: bool,
    expected: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _SpecDriver(_light_spec(verified))
    coordinator = _FakeCoordinator(allow)
    entities = _setup(coordinator, driver, monkeypatch)
    assert len(entities) == expected


def test_an_allowed_unverified_entity_is_marked_experimental(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _SpecDriver(_light_spec(False))
    coordinator = _FakeCoordinator(True)
    entity = _setup(coordinator, driver, monkeypatch)[0]
    entity.driver = driver
    assert entity.extra_state_attributes == {"experimental": True}


def test_a_verified_entity_is_not_marked_experimental(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _SpecDriver(_light_spec(True))
    coordinator = _FakeCoordinator(False)
    entity = _setup(coordinator, driver, monkeypatch)[0]
    entity.driver = driver
    assert entity.extra_state_attributes == {}


def test_the_default_option_is_off() -> None:
    # Has to be imported from the real component file, not a stub.
    const = _load("const", "const.py")
    assert const.DEFAULT_ALLOW_UNVERIFIED_CONTROLS is False
    assert const.CONF_ALLOW_UNVERIFIED_CONTROLS == "allow_unverified_controls"
