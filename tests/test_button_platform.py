"""Button platform + profile command merge: learned controls become entities.

A matching YAML profile used to make a device uncontrollable: ProfileDriver
returned no commands, so capabilities were empty and fan/light/number/select
stopped generating entities, while the learned controls had no platform at
all.  This module pins the fix from both sides: the driver merges its base
driver's commands with the profile's controls, and the button platform renders
exactly the controls no rich platform consumes.

The integration is not importable outside Home Assistant, so an isolated
package loads the real devices package (and with it the real profile layer)
plus the real button/number/fan/light/select platforms with the HA symbols
stubbed out, same as the sibling platform tests.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import logging
import os
import shutil
import sys
import textwrap
import types
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

COMPONENT = (
    Path(__file__).resolve().parents[1] / "custom_components" / "robam_roki"
)
PACKAGE = "roki_button_pkg_test"
GUID = "R8229000000000000"
HOOD = {"guid": GUID, "name": "hood", "dc": "RYYJ", "dt": "R8229", "deviceSource": 0}


@pytest.fixture
def tmp_path() -> Iterator[Path]:
    """A temporary directory that can actually be written to.

    The sandbox on this machine denies writes to the user Temp directory, so
    the builtin fixture falls back to the repository root and leaves
    pytest-of-* directories in the working tree.  This override keeps the
    directory inside the workspace and always removes it.
    """
    base = Path(__file__).resolve().parents[1] / ".pytest-profile-tmp"
    path = base / f"{os.getpid()}-{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
        try:
            base.rmdir()
        except OSError:
            pass


# -- isolated package harness -------------------------------------------


def _component(name: str) -> types.ModuleType:
    """Register (or reuse) one homeassistant.components submodule."""
    components = sys.modules["homeassistant.components"]
    module = sys.modules.get(f"homeassistant.components.{name}")
    if module is None:
        module = types.ModuleType(f"homeassistant.components.{name}")
        sys.modules[f"homeassistant.components.{name}"] = module
    setattr(components, name, module)
    return module


def _install_ha_stubs() -> None:
    sys.modules["homeassistant"].components = sys.modules[
        "homeassistant.components"
    ]

    button_mod = _component("button")

    class ButtonEntity:
        pass

    button_mod.ButtonEntity = ButtonEntity

    number_mod = _component("number")

    class NumberEntity:
        pass

    class NumberMode:
        SLIDER = "slider"
        BOX = "box"

    number_mod.NumberEntity = NumberEntity
    number_mod.NumberMode = NumberMode

    fan_mod = _component("fan")

    class FanEntity:
        pass

    class FanEntityFeature:
        TURN_ON = 1
        TURN_OFF = 2
        SET_SPEED = 4
        PRESET_MODE = 8

    fan_mod.FanEntity = FanEntity
    fan_mod.FanEntityFeature = FanEntityFeature

    light_mod = _component("light")

    class ColorMode:
        ONOFF = "onoff"

    class LightEntity:
        pass

    light_mod.ColorMode = ColorMode
    light_mod.LightEntity = LightEntity

    select_mod = _component("select")

    class SelectEntity:
        pass

    select_mod.SelectEntity = SelectEntity


def _stub_coordinator() -> None:
    """The platform modules only need the coordinator for type hints."""
    module = types.ModuleType(f"{PACKAGE}.coordinator")

    class RokiCoordinator:
        pass

    module.RokiCoordinator = RokiCoordinator
    sys.modules[f"{PACKAGE}.coordinator"] = module


def _stub_entity() -> None:
    """Replace entity.py, resolving the driver like the real base does.

    Binding the real driver matters here: the button platform reads the
    CommandSpec name off it, so a stand-in without one would test nothing.
    """
    module = types.ModuleType(f"{PACKAGE}.entity")

    class RokiEntity:
        _attr_has_entity_name = True

        def __init__(self, coordinator: Any, device: dict[str, Any]) -> None:
            self.coordinator = coordinator
            self._device = device
            self._device_id = str(device["guid"])
            factory = sys.modules[f"{PACKAGE}.devices"].get_driver_for_device
            self.driver = factory(coordinator, device)

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


def _load(name: str, relative: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        f"{PACKAGE}.{name}", COMPONENT / relative
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"{PACKAGE}.{name}"] = module
    spec.loader.exec_module(module)
    return module


_install_ha_stubs()
_stub_coordinator()
_stub_entity()
package = types.ModuleType(PACKAGE)
package.__path__ = [str(COMPONENT)]
sys.modules[PACKAGE] = package

profiles_mod = importlib.import_module(f"{PACKAGE}.profiles")
devices = importlib.import_module(f"{PACKAGE}.devices")
const = importlib.import_module(f"{PACKAGE}.const")
button = _load("button", "button.py")
number = _load("number", "number.py")
fan = _load("fan", "fan.py")
light = _load("light", "light.py")
select = _load("select", "select.py")
api_mod = sys.modules[f"{PACKAGE}.api"]
RokiApiError = api_mod.RokiApiError
HomeAssistantError = sys.modules["homeassistant.exceptions"].HomeAssistantError

#: The platforms that must keep generating a hood entity once a profile
#: matches it; a profile must never empty the capability table again.
STANDARD_PLATFORMS = {
    "fan": fan,
    "light": light,
    "number": number,
    "select": select,
}


# -- fixtures ------------------------------------------------------------


def _profile(tmp_path: Path, text: str) -> Any:
    """Load one profile through the real loader and resolve its extends."""
    path = tmp_path / "learned.yaml"
    path.write_text(textwrap.dedent(text).strip() + "\n", encoding="utf-8")
    resolved = profiles_mod.resolve_extends(
        profiles_mod.load_profile_dir(tmp_path, profiles_mod.SOURCE_USER)
    )
    assert len(resolved) == 1, resolved
    return resolved[0]


FOUR_LEARNED = """
schema: roki-profile/1
extends: python:hood
match:
  dc: RYYJ
controls:
  action_1:
    kind: mqtt
    command: 132
    body_hex: "01"
    verified: false
  action_2:
    kind: mqtt
    command: 136
    body_hex: "02"
    verified: false
  action_3:
    kind: mqtt
    command: 138
    body_hex: "03"
    verified: false
  action_4:
    kind: mqtt
    command: 140
    body_hex: ""
    verified: false
"""

ONE_LEARNED = """
schema: roki-profile/1
extends: python:hood
match:
  dc: RYYJ
controls:
  action_1:
    kind: mqtt
    command: 132
    body_hex: "01"
    verified: false
"""

NAMED_CONTROL = """
schema: roki-profile/1
extends: python:hood
match:
  dc: RYYJ
controls:
  lamp:
    kind: mqtt
    command: 132
    body_hex: "01"
    name: 开灯
    verified: true
"""

UNSUPPORTED_KINDS = """
schema: roki-profile/1
extends: python:hood
match:
  dc: RYYJ
controls:
  lamp_switch:
    kind: property
    on: {light_status: 1}
    off: {light_status: 0}
  gear:
    kind: invoke
    function: "134"
    param: level
  beep:
    kind: invoke
    function: "137"
"""

READ_ONLY = """
schema: roki-profile/1
match:
  dc: RCOF
sensors:
  water_level:
    keys: [waterLevel]
    type: int
"""


# -- fake fleet ----------------------------------------------------------


class _FakeCoordinator:
    """A fleet plus the coordinator surface the platforms touch."""

    def __init__(
        self,
        devices: list[dict[str, Any]],
        profiles: list[Any] | None = None,
        *,
        allow_unverified: bool = False,
    ) -> None:
        self.devices = [dict(device) for device in devices]
        self.data = {
            str(device["guid"]): {"level": 2, "workStatus": 1}
            for device in self.devices
        }
        self.profiles = profiles
        self.allow = allow_unverified
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.refreshes: list[str] = []

    def allow_unverified_controls(self) -> bool:
        return self.allow

    def is_rest_device(self, guid: str) -> bool:
        for device in self.devices:
            if str(device.get("guid")) == str(guid):
                return str(device.get("deviceSource")) == "1"
        return False

    async def async_execute(
        self, device_id: str, capability: str, **kwargs: Any
    ) -> None:
        self.calls.append((device_id, capability, dict(kwargs)))

    def async_apply(self, device_id: str, properties: dict[str, Any]) -> None:
        self.data.setdefault(device_id, {}).update(properties)

    def async_schedule_refresh(
        self, device_id: str | None = None, delay: float = 2.0
    ) -> None:
        self.refreshes.append(str(device_id))


class _FailingCoordinator(_FakeCoordinator):
    """A coordinator whose writes fail the way the cloud API does."""

    async def async_execute(
        self, device_id: str, capability: str, **kwargs: Any
    ) -> None:
        raise RokiApiError("the appliance is offline")


def _setup(platform: types.ModuleType, coordinator: Any) -> list[Any]:
    added: list[Any] = []
    entry = types.SimpleNamespace(
        runtime_data=types.SimpleNamespace(coordinator=coordinator),
        options={},
    )
    asyncio.run(platform.async_setup_entry(None, entry, added.extend))
    return added


def _buttons(tmp_path: Path, text: str, *, allow_unverified: bool = True) -> list[Any]:
    profile = _profile(tmp_path, text)
    coordinator = _FakeCoordinator([HOOD], [profile], allow_unverified=allow_unverified)
    return _setup(button, coordinator)


# -- generation ----------------------------------------------------------


def test_the_button_platform_is_registered() -> None:
    assert "button" in const.PLATFORMS
    assert (COMPONENT / "button.py").is_file()


def test_four_learned_controls_become_four_buttons(tmp_path: Path) -> None:
    entities = _buttons(tmp_path, FOUR_LEARNED)
    assert [entity.control_key for entity in entities] == [
        "action_1",
        "action_2",
        "action_3",
        "action_4",
    ]
    assert [entity._attr_unique_id for entity in entities] == [
        f"{GUID}_control_action_1",
        f"{GUID}_control_action_2",
        f"{GUID}_control_action_3",
        f"{GUID}_control_action_4",
    ]
    # No name was declared, so the key is the label.
    assert [entity._attr_name for entity in entities] == [
        "action_1",
        "action_2",
        "action_3",
        "action_4",
    ]


def test_standard_capabilities_are_never_duplicated_as_buttons(
    tmp_path: Path,
) -> None:
    # The hood still advertises power/level/light/timing; those belong to the
    # fan, light, number and select platforms, so only action_1 is a button.
    entities = _buttons(tmp_path, ONE_LEARNED)
    assert [entity.control_key for entity in entities] == ["action_1"]
    assert button.pressable_capabilities(
        entities[0].driver, entities[0].coordinator
    ) == ["action_1"]


def test_a_declared_control_name_becomes_the_button_name(
    tmp_path: Path,
) -> None:
    entities = _buttons(tmp_path, NAMED_CONTROL)
    assert len(entities) == 1
    entity = entities[0]
    assert entity._attr_name == "开灯"
    assert entity._attr_unique_id == f"{GUID}_control_lamp"


def test_a_read_only_profile_gets_no_buttons(tmp_path: Path) -> None:
    profile = _profile(tmp_path, READ_ONLY)
    appliance = {"guid": "C0000000000000001", "name": "purifier", "dc": "RCOF"}
    coordinator = _FakeCoordinator([appliance], [profile], allow_unverified=True)
    assert _setup(button, coordinator) == []


def test_property_and_parameterised_controls_get_no_button(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    profile = _profile(tmp_path, UNSUPPORTED_KINDS)
    coordinator = _FakeCoordinator([HOOD], [profile])
    with caplog.at_level(logging.DEBUG):
        entities = _setup(button, coordinator)
    assert [entity.control_key for entity in entities] == ["beep"]
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "kind=property" in messages
    assert "needs a value for 'level'" in messages


def _load_file_module(name: str, relative: str) -> types.ModuleType:
    """Load a Home-Assistant-free module straight from its file."""
    spec = importlib.util.spec_from_file_location(name, COMPONENT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


draft_mod = _load_file_module("roki_button_draft_test", "draft.py")


def test_the_drafted_suggested_name_reaches_the_button(tmp_path: Path) -> None:
    """End to end: a learned action -> draft YAML -> profile -> button name."""
    learned = [
        {
            "frames": [{"command": 132, "body_hex": "01"}],
            "observed": {"workStatus": {"from": 0, "to": 1}},
        }
    ]
    result = draft_mod.draft_from_properties(
        {"dc": "RYYJ", "dt": "R8229"},
        {"workStatus": 1},
        learned_controls=learned,
    )
    assert "name:" in result.profile_yaml
    profile = _profile(tmp_path, result.profile_yaml)
    coordinator = _FakeCoordinator([HOOD], [profile], allow_unverified=True)
    entities = _setup(button, coordinator)
    assert [entity.control_key for entity in entities] == ["action_1"]
    assert entities[0]._attr_name == "开机"


# -- the experimental-controls gate --------------------------------------


@pytest.mark.parametrize(("allow_unverified", "expected"), [(False, 0), (True, 4)])
def test_unverified_controls_need_the_experimental_option(
    tmp_path: Path, allow_unverified: bool, expected: int
) -> None:
    entities = _buttons(
        tmp_path, FOUR_LEARNED, allow_unverified=allow_unverified
    )
    assert len(entities) == expected


def test_an_allowed_unverified_button_is_marked_experimental(
    tmp_path: Path,
) -> None:
    entities = _buttons(tmp_path, ONE_LEARNED, allow_unverified=True)
    assert entities[0].extra_state_attributes == {"experimental": True}


def test_a_verified_control_is_not_marked_experimental(tmp_path: Path) -> None:
    entities = _buttons(tmp_path, NAMED_CONTROL, allow_unverified=False)
    assert len(entities) == 1
    assert entities[0].extra_state_attributes == {}


# -- pressing ------------------------------------------------------------


def test_pressing_sends_the_control_capability(tmp_path: Path) -> None:
    entities = _buttons(tmp_path, ONE_LEARNED)
    coordinator = entities[0].coordinator
    asyncio.run(entities[0].async_press())
    assert coordinator.calls == [(GUID, "action_1", {})]
    # A raw frame reports its effect later, so a refresh is requested.
    assert coordinator.refreshes == [GUID]


def test_a_failed_press_becomes_a_home_assistant_error(
    tmp_path: Path,
) -> None:
    profile = _profile(tmp_path, ONE_LEARNED)
    coordinator = _FailingCoordinator([HOOD], [profile], allow_unverified=True)
    entity = _setup(button, coordinator)[0]
    with pytest.raises(HomeAssistantError) as err:
        asyncio.run(entity.async_press())
    assert "Could not press" in str(err.value)
    assert "offline" in str(err.value)


# -- regression: a matched profile keeps the standard platforms ----------


def test_a_learned_profile_keeps_all_four_standard_platforms(
    tmp_path: Path,
) -> None:
    """The real-device bug: the draft matched and every entity vanished."""
    profile = _profile(tmp_path, FOUR_LEARNED)
    coordinator = _FakeCoordinator([HOOD], [profile])
    counts = {
        name: len(_setup(platform, coordinator))
        for name, platform in STANDARD_PLATFORMS.items()
    }
    assert counts == {"fan": 1, "light": 1, "number": 1, "select": 1}


def test_the_countdown_entity_survives_a_learned_profile(tmp_path: Path) -> None:
    profile = _profile(tmp_path, FOUR_LEARNED)
    coordinator = _FakeCoordinator([HOOD], [profile])
    countdowns = _setup(number, coordinator)
    assert [entity._attr_unique_id for entity in countdowns] == [
        f"{GUID}_countdown"
    ]


def test_the_driver_merges_base_commands_and_profile_controls(tmp_path: Path) -> None:
    profile = _profile(tmp_path, FOUR_LEARNED)
    coordinator = _FakeCoordinator([HOOD], [profile])
    driver = devices.get_driver_for_device(coordinator, dict(HOOD))
    commands = driver.commands()
    assert set(commands) == {
        "power",
        "level",
        "light",
        "timing",
        "action_1",
        "action_2",
        "action_3",
        "action_4",
    }
    assert driver.capabilities == frozenset(commands)
    assert commands["action_1"].kind == "mqtt"
    assert commands["action_1"].command == 132
    assert commands["level"].function == "134"
