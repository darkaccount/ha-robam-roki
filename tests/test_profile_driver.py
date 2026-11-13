"""Tests for the profile-driven runtime (devices/profile.py + factory wiring).

The integration cannot be imported inside Home Assistant here, so one isolated
package loads the real devices package (which brings the real profiles.py in)
plus the sensor and binary_sensor platforms with the Home Assistant symbols
stubbed out.  A second section uses the real integration package to exercise
the profile loading that runs during async_setup_entry.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import logging
import os
import shutil
import sys
import types
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

COMPONENT = (
    Path(__file__).resolve().parents[1] / "custom_components" / "robam_roki"
)
PACKAGE = "roki_profile_driver_pkg_test"


@pytest.fixture
def tmp_path() -> Iterator[Path]:
    """A temporary directory that can actually be written to.

    The sandbox on this machine denies writes to the user Temp directory, so
    the builtin tmp_path fixture falls back to the repository root and leaves
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
    components = sys.modules["homeassistant.components"]
    module = sys.modules.get(f"homeassistant.components.{name}")
    if module is None:
        module = types.ModuleType(f"homeassistant.components.{name}")
        sys.modules[f"homeassistant.components.{name}"] = module
        setattr(components, name, module)
    return module


def _install_ha_stubs() -> None:
    ha = sys.modules["homeassistant"]
    ha.components = sys.modules["homeassistant.components"]
    for name in ("sensor", "binary_sensor"):
        _component(name)

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

    number_mod = _component("number")

    class NumberEntity:
        pass

    class NumberMode:
        SLIDER = "slider"
        BOX = "box"

    number_mod.NumberEntity = NumberEntity
    number_mod.NumberMode = NumberMode

    select_mod = _component("select")

    class SelectEntity:
        pass

    select_mod.SelectEntity = SelectEntity


def _stub_coordinator() -> None:
    module = types.ModuleType(f"{PACKAGE}.coordinator")

    class RokiCoordinator:
        pass

    module.RokiCoordinator = RokiCoordinator
    sys.modules[f"{PACKAGE}.coordinator"] = module


def _stub_entity() -> None:
    module = types.ModuleType(f"{PACKAGE}.entity")

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
sensor = _load("sensor", "sensor.py")
binary_sensor = _load("binary_sensor", "binary_sensor.py")
fan = _load("fan", "fan.py")
light = _load("light", "light.py")
number = _load("number", "number.py")
select = _load("select", "select.py")

ProfileDriver = devices.ProfileDriver

CONTROL_PLATFORMS = {
    "fan": fan,
    "light": light,
    "number": number,
    "select": select,
}


# -- fake fleet ----------------------------------------------------------


class _FakeCoordinator:
    """Just enough coordinator for driver construction."""

    def __init__(
        self,
        devices: list[dict[str, Any]] | None = None,
        profiles: list[Any] | None = None,
    ) -> None:
        self.devices = [dict(device) for device in devices or []]
        self.data = {str(device["guid"]): {} for device in self.devices}
        self.profiles = profiles

    def is_rest_device(self, guid: str) -> bool:
        for device in self.devices:
            if str(device.get("guid")) == str(guid):
                return str(device.get("deviceSource")) == "1"
        return False


def _device(dc: str = "RCOF", guid: str = "G000000000000001") -> dict[str, Any]:
    return {"guid": guid, "name": "appliance", "dc": dc, "deviceSource": 0}


def _full(**overrides: Any) -> Any:
    values: dict[str, Any] = {
        "id": "test-profile",
        "source": "user",
        "path": None,
        "dc": "RCOF",
        "dt": None,
        "name": None,
        "sensors": {},
        "binary_sensors": {},
        "controls": {},
        "extends": None,
        "python_base": None,
    }
    values.update(overrides)
    return profiles_mod.FullProfile(**values)


def _driver_for(device: dict[str, Any], profile: Any | None = None) -> Any:
    profiles = [_full(**profile)] if isinstance(profile, dict) else (
        [profile] if profile is not None else []
    )
    coordinator = _FakeCoordinator([device], profiles)
    return devices.get_driver_for_device(coordinator, dict(device))


def _new_spec(key: str, **fields: Any) -> Any:
    return devices.SensorSpec(key=key, translation_key=key, **fields)


# -- no profile: existing behaviour --------------------------------------


def test_without_profiles_the_builtin_driver_is_used() -> None:
    device = {"guid": "T000000000000001", "dc": "RZQL", "dt": "C98A"}
    coordinator = _FakeCoordinator([device], profiles=[])
    driver = devices.get_driver_for_device(coordinator, device)
    assert type(driver) is devices.SteamOvenDriver

    reference = devices.SteamOvenDriver(coordinator, device)
    assert [spec.key for spec in driver.sensors()] == [
        spec.key for spec in reference.sensors()
    ]
    assert [spec.key for spec in driver.binary_sensors()] == [
        spec.key for spec in reference.binary_sensors()
    ]


def test_coordinator_without_profiles_attribute_still_works() -> None:
    coordinator = types.SimpleNamespace(data={})
    device = {"guid": "H000000000000001", "dc": "RYYJ", "dt": "R8229"}
    driver = devices.get_driver_for_device(coordinator, device)
    assert isinstance(driver, devices.HoodDriver)


def test_non_matching_profile_falls_back_to_the_builtin_driver() -> None:
    device = {"guid": "T000000000000002", "dc": "RZQL", "dt": "C98A"}
    profile = _full(dc="RCOF")  # different category code
    driver = _driver_for(device, profile)
    assert type(driver) is devices.SteamOvenDriver


# -- profile-driver basics -----------------------------------------------


def test_matching_profile_builds_a_profile_driver() -> None:
    device = _device()
    profile = _full(
        sensors={
            "water_level": {
                "keys": ["waterLevel", "water"],
                "type": "int",
                "unit": "%",
                "name": "水箱水位",
            }
        }
    )
    driver = _driver_for(device, profile)
    assert isinstance(driver, ProfileDriver)
    assert driver.profile is profile

    specs = driver.sensors()
    assert [spec.key for spec in specs] == ["water_level"]
    spec = specs[0]
    assert spec.translation_key == "water_level"
    assert spec.name == "水箱水位"
    assert spec.unit_of_measurement == "%"


def test_profile_driver_commands_are_empty_in_p1() -> None:
    driver = _driver_for(_device(), _full(sensors={"a": {"keys": ["a"]}}))
    assert driver.commands() == {}
    assert driver.capabilities == frozenset()


def test_profile_entries_become_binary_sensors() -> None:
    device = _device()
    profile = _full(
        binary_sensors={
            "tank_missing": {
                "keys": ["tankMissing"],
                "on_values": [1, "open"],
                "device_class": "problem",
                "name": "水箱未安装",
            }
        }
    )
    specs = _driver_for(device, profile).binary_sensors()
    assert [spec.key for spec in specs] == ["tank_missing"]
    spec = specs[0]
    assert spec.name == "水箱未安装"
    assert spec.is_on_fn({"tankMissing": 1}) is True
    assert spec.is_on_fn({"tankMissing": 0}) is False
    assert spec.is_on_fn({}) is None


# -- value pipeline ------------------------------------------------------


def test_multi_key_fallback_skips_empty_values() -> None:
    spec = _driver_for(
        _device(),
        _full(sensors={"temp": {"keys": ["b", "a"], "type": "int"}}),
    ).sensors()[0]
    value_fn = spec.value_fn
    assert value_fn({"a": 5}) == 5
    # 0 is a real reading, not "empty".
    assert value_fn({"b": 0, "a": 5}) == 0
    assert value_fn({"b": "", "a": 7}) == 7
    assert value_fn({"b": None, "a": 8}) == 8
    assert value_fn({}) is None


def test_map_hit_miss_and_default() -> None:
    spec = _driver_for(
        _device(),
        _full(
            sensors={
                "state": {
                    "keys": ["workState"],
                    "type": "int",
                    "map": {2: "预热中"},
                    "default": "未知",
                }
            }
        ),
    ).sensors()[0]
    assert spec.value_fn({"workState": 2}) == "预热中"
    assert spec.value_fn({"workState": 9}) == "未知"
    assert spec.value_fn({}) == "未知"


def test_map_miss_without_default_keeps_the_value() -> None:
    spec = _driver_for(
        _device(),
        _full(sensors={"state": {"keys": ["workState"], "type": "int", "map": {2: "预热"}}}),
    ).sensors()[0]
    assert spec.value_fn({"workState": 9}) == 9


def test_scale_is_applied_after_the_type_conversion() -> None:
    spec = _driver_for(
        _device(),
        _full(
            sensors={
                "remain": {"keys": ["seconds"], "type": "int", "scale": 0.01}
            }
        ),
    ).sensors()[0]
    assert spec.value_fn({"seconds": 1800}) == 18.0


def test_type_conversion_handles_strings_and_failures() -> None:
    float_spec = _driver_for(
        _device(), _full(sensors={"temp": {"keys": ["temp"], "type": "float"}})
    ).sensors()[0]
    assert float_spec.value_fn({"temp": "21.5"}) == 21.5

    bool_spec = _driver_for(
        _device(), _full(sensors={"flag": {"keys": ["flag"], "type": "bool"}})
    ).sensors()[0]
    assert bool_spec.value_fn({"flag": "1"}) is True
    assert bool_spec.value_fn({"flag": "0"}) is False
    assert bool_spec.value_fn({"flag": "off"}) is False

    default_spec = _driver_for(
        _device(),
        _full(sensors={"n": {"keys": ["n"], "type": "int", "default": 0}}),
    ).sensors()[0]
    assert default_spec.value_fn({"n": "not-a-number"}) == 0


def test_on_values_and_truthiness_cover_binary_sensors() -> None:
    driver = _driver_for(
        _device(),
        _full(
            binary_sensors={
                "door": {"keys": ["doorState"], "on_values": [1, "open"]},
                "alarm": {"keys": ["fault"]},
            }
        ),
    )
    specs = {spec.key: spec for spec in driver.binary_sensors()}
    assert specs["door"].is_on_fn({"doorState": 1}) is True
    assert specs["door"].is_on_fn({"doorState": "open"}) is True
    assert specs["door"].is_on_fn({"doorState": 0}) is False
    assert specs["alarm"].is_on_fn({"fault": 2}) is True
    assert specs["alarm"].is_on_fn({"fault": 0}) is False


# -- a profile never creates control entities ----------------------------


def test_profile_driven_device_gets_no_control_entities() -> None:
    device = _device()
    profile = _full(
        sensors={"level": {"keys": ["level"], "type": "int", "name": "水位"}}
    )
    coordinator = _FakeCoordinator([device], [profile])
    entry = types.SimpleNamespace(
        runtime_data=types.SimpleNamespace(coordinator=coordinator),
        options={},
    )
    for name, platform in CONTROL_PLATFORMS.items():
        added: list[Any] = []
        asyncio.run(platform.async_setup_entry(None, entry, added.extend))
        assert added == [], name

    # The read-only telemetry entity is still generated for the same device.
    sensors: list[Any] = []
    asyncio.run(sensor.async_setup_entry(None, entry, sensors.extend))
    assert [entity.spec.key for entity in sensors] == ["level"]
    assert sensors[0].spec.name == "水位"


# -- extends: python:<driver> --------------------------------------------


def test_extends_python_keeps_base_entities_and_overrides_presentation() -> None:
    device = {"guid": "T000000000000003", "dc": "RZQL", "dt": "C98A"}
    profile = _full(
        dc="RZQL",
        python_base="thermal",
        sensors={"top_temperature": {"name": "上管温度"}},
    )
    coordinator = _FakeCoordinator([device], [profile])
    driver = devices.get_driver_for_device(coordinator, device)
    assert isinstance(driver, ProfileDriver)

    assert driver.base is not None
    base_sensors = {spec.key: spec for spec in driver.base.sensors()}
    specs = {spec.key: spec for spec in driver.sensors()}
    assert list(specs) == list(base_sensors)  # no entity added or removed

    overridden = specs["top_temperature"]
    base_spec = base_sensors["top_temperature"]
    assert overridden.name == "上管温度"
    # Presentation-only override: the value still comes from the base driver,
    # because the entry declares no keys of its own.
    assert overridden.value_fn is not None
    assert overridden.value_fn({"curTopTemp": 140}) == 140.0
    assert overridden.device_class is base_spec.device_class
    assert overridden.unit_of_measurement == base_spec.unit_of_measurement
    assert overridden.icon == base_spec.icon


def test_extends_python_keys_replace_the_value_function_only() -> None:
    device = {"guid": "T000000000000004", "dc": "RZQL", "dt": "C98A"}
    profile = _full(
        dc="RZQL",
        python_base="thermal",
        sensors={
            "top_temperature": {
                "keys": ["curTopTemp"],
                "type": "int",
                "scale": 2,
                "name": "顶部探针",
            }
        },
    )
    coordinator = _FakeCoordinator([device], [profile])
    driver = devices.get_driver_for_device(coordinator, device)
    assert driver.base is not None
    specs = {spec.key: spec for spec in driver.sensors()}
    base_specs = {spec.key: spec for spec in driver.base.sensors()}
    overridden = specs["top_temperature"]
    assert overridden.name == "顶部探针"
    # A keys entry replaces the value function: the profile's scale applies.
    assert overridden.value_fn({"curTopTemp": 140}) == 280
    assert base_specs["top_temperature"].value_fn({"curTopTemp": 140}) == 140.0
    # The base presentation fields survive the override.
    assert overridden.icon == base_specs["top_temperature"].icon
    assert overridden.state_class is base_specs["top_temperature"].state_class


def test_extends_python_unknown_driver_falls_back_to_profile_only() -> None:
    profile = _full(python_base="does-not-exist", sensors={"a": {"keys": ["a"]}})
    driver = _driver_for(_device(), profile)
    assert isinstance(driver, ProfileDriver)
    assert driver.base is None
    assert [spec.key for spec in driver.sensors()] == ["a"]


def test_profile_entry_without_base_spec_and_without_keys_is_skipped() -> None:
    profile = _full(sensors={"ghost": {"name": "幽灵"}})
    driver = _driver_for(_device(), profile)
    assert driver.sensors() == []


def test_builtin_example_profile_loads_and_inherits_thermal() -> None:
    loaded = profiles_mod.load_profile_dir(
        COMPONENT / "profiles", profiles_mod.SOURCE_BUILTIN
    )
    resolved = profiles_mod.resolve_extends(loaded)
    by_id = {profile.id: profile for profile in resolved}
    assert "thermal-override" in by_id
    thermal = by_id["thermal-override"]
    assert thermal.python_base == "thermal"
    assert thermal.dc == "RZQL"
    assert thermal.sensors["top_temperature"]["name"] == "上管温度"


# -- entities: unique_id and name priority -------------------------------


def test_unique_id_stays_guid_plus_key() -> None:
    coordinator = _FakeCoordinator([_device()], [])
    device = _device()
    named = sensor.RokiDriverSensor(
        coordinator, device, _new_spec("cavity_temp", name="腔体温度")
    )
    assert named._attr_unique_id == "G000000000000001_cavity_temp"
    plain = sensor.RokiDriverSensor(
        coordinator, device, _new_spec("cavity_temp")
    )
    assert plain._attr_unique_id == "G000000000000001_cavity_temp"

    binary = binary_sensor.RokiDriverBinarySensor(
        coordinator, device, devices.BinarySensorSpec(key="door", translation_key="door", name="门")
    )
    assert binary._attr_unique_id == "G000000000000001_door"


def test_name_wins_over_translation_key() -> None:
    coordinator = _FakeCoordinator([_device()], [])
    device = _device()
    named = sensor.RokiDriverSensor(
        coordinator, device, _new_spec("k", name="自定义名称")
    )
    assert named._attr_name == "自定义名称"
    assert getattr(named, "_attr_translation_key", None) is None

    plain = sensor.RokiDriverSensor(coordinator, device, _new_spec("k"))
    assert plain._attr_translation_key == "k"
    assert getattr(plain, "_attr_name", None) is None

    binary_named = binary_sensor.RokiDriverBinarySensor(
        coordinator,
        device,
        devices.BinarySensorSpec(key="k", translation_key="k", name="自定义"),
    )
    assert binary_named._attr_name == "自定义"
    assert getattr(binary_named, "_attr_translation_key", None) is None


def test_profile_sensor_entity_reads_through_its_value_function() -> None:
    device = _device()
    coordinator = _FakeCoordinator([device], [])
    coordinator.data[device["guid"]] = {"waterLevel": "60"}
    profile = _full(
        sensors={"water_level": {"keys": ["waterLevel"], "type": "int", "unit": "%"}}
    )
    driver = _driver_for(device, profile)
    spec = driver.sensors()[0]
    entity = sensor.RokiDriverSensor(coordinator, device, spec)
    assert entity.native_value == 60
    assert entity._attr_native_unit_of_measurement == "%"


# -- integration profile loading -----------------------------------------


class _FakeConfig:
    def __init__(self, base: Path) -> None:
        self._base = base

    def path(self, *parts: str) -> str:
        return str(self._base.joinpath(*parts))


class _FakeHass:
    def __init__(self, base: Path) -> None:
        self.config = _FakeConfig(base)


def test_load_profiles_reports_builtin_and_missing_user_dir(
    tmp_path: Path, caplog: Any
) -> None:
    from custom_components.robam_roki import _load_profiles

    with caplog.at_level(logging.INFO):
        resolved = _load_profiles(_FakeHass(tmp_path))
    by_id = {profile.id: profile for profile in resolved}
    assert "thermal-override" in by_id
    assert by_id["thermal-override"].python_base == "thermal"
    messages = "\n".join(record.getMessage() for record in caplog.records)
    builtin_count = len(list((COMPONENT / "profiles").glob("*.yaml")))
    assert f"profiles: builtin={builtin_count} user=0" in messages
    assert "(missing)" in messages


def test_load_profiles_picks_up_the_user_directory(
    tmp_path: Path, caplog: Any
) -> None:
    from custom_components.robam_roki import _load_profiles

    user_dir = tmp_path / "robam_roki" / "profiles"
    user_dir.mkdir(parents=True)
    (user_dir / "mine.yaml").write_text(
        textwrap_dedent(
            """
            schema: roki-profile/1
            match:
              dc: RCOF
            sensors:
              level:
                keys: [level]
                type: int
                name: 水位
            """
        ),
        encoding="utf-8",
    )
    with caplog.at_level(logging.INFO):
        resolved = _load_profiles(_FakeHass(tmp_path))
    by_id = {profile.id: profile for profile in resolved}
    assert "mine" in by_id
    assert "thermal-override" in by_id
    messages = "\n".join(record.getMessage() for record in caplog.records)
    builtin_count = len(list((COMPONENT / "profiles").glob("*.yaml")))
    assert f"profiles: builtin={builtin_count} user=1" in messages
    assert "(missing)" not in messages


def textwrap_dedent(text: str) -> str:
    import textwrap

    return textwrap.dedent(text).strip() + "\n"
