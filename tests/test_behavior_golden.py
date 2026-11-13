"""Golden behaviour comparison for the P3-F YAML migration.

Every category that used to be served by a hand-written Python driver was
captured into ``tests/fixtures/behavior/<dc>.json`` *before* the migration:
the entity keys, their order, the names, icons, units, device classes, entity
categories and the values computed from a fixed set of payloads.  These tests
replay that golden against the driver the integration builds today.

The golden files are a contract, not a snapshot of the current code: a failure
means the migration changed what Home Assistant sees, and the fix belongs in
the profile -- not in the fixture.

Covered categories:
  * stove (RRQZ), dishwasher (RXWJ), water purifier (RJSQ): Python driver ->
    built-in YAML profile, so the driver becomes a ProfileDriver with no base;
  * the thermal family (RZQL / RZKY / RDKX / RWBL / RXDG): the Python base
    stays and the YAML overrides are presentation-only, so the driver is a
    ProfileDriver over ThermalCookerDriver (or still the Python driver where no
    profile matches).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

import custom_components.robam_roki.devices as devices_module
from custom_components.robam_roki.devices import (
    DRIVER_REGISTRY,
    MicrowaveDriver,
    OvenDriver,
    ProfileDriver,
    SterilizerDriver,
    get_driver_for_device,
)
from custom_components.robam_roki.profiles import (
    SOURCE_BUILTIN,
    load_profile_dir,
    match_profile,
    resolve_extends,
)

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
COMPONENT = REPO_ROOT / "custom_components" / "robam_roki"
BEHAVIOR_DIR = HERE / "fixtures" / "behavior"
GOLDEN_FILES = sorted(BEHAVIOR_DIR.glob("*.json"))

#: The pre-migration Python drivers this module is about: dc -> module, class.
#: Both must be gone after the migration.
REMOVED_DRIVERS: dict[str, tuple[str, str]] = {
    "RRQZ": ("stove", "StoveDriver"),
    "RXWJ": ("dishwasher", "DishwasherDriver"),
    "RJSQ": ("water_purifier", "WaterPurifierDriver"),
}

#: The driver each golden file was captured from, before the migration.  A
#: golden that claims the wrong provenance proves nothing, so it is pinned.
BASELINE: dict[str, tuple[str, str]] = {
    "RRQZ": ("python driver", "StoveDriver"),
    "RXWJ": ("python driver", "DishwasherDriver"),
    "RJSQ": ("python driver", "WaterPurifierDriver"),
    "RZQL": ("yaml profile", "ProfileDriver"),
    "RZKY": ("python driver", "SteamOvenDriver"),
    "RDKX": ("python driver", "OvenDriver"),
    "RWBL": ("python driver", "MicrowaveDriver"),
    "RXDG": ("python driver", "SterilizerDriver"),
}

#: How the factory must resolve each category after the migration.  A mapping
#: of dc -> (profile id or None, Python base or None, driver class).
RESOLUTION: dict[str, tuple[str | None, str | None, type]] = {
    "RRQZ": ("stove", None, ProfileDriver),
    "RXWJ": ("dishwasher", None, ProfileDriver),
    "RJSQ": ("water-purifier", None, ProfileDriver),
    "RZQL": ("thermal-override", "thermal", ProfileDriver),
    "RZKY": ("thermal-c98a", "thermal", ProfileDriver),
    "RDKX": (None, None, OvenDriver),
    "RWBL": (None, None, MicrowaveDriver),
    "RXDG": (None, None, SterilizerDriver),
}

GUID = "BEHAVIOURDEVICE001"


def _snapshot_module() -> Any:
    """Load tests/behavior_snapshot.py, the module that captured the golden."""
    name = "roki_behavior_snapshot_test"
    spec = importlib.util.spec_from_file_location(
        name, HERE / "behavior_snapshot.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


snapshot = _snapshot_module()


def _golden(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _builtin_profiles() -> list[Any]:
    loaded = load_profile_dir(COMPONENT / "profiles", SOURCE_BUILTIN)
    return resolve_extends(loaded)


def _driver_for(device: dict[str, Any], properties: dict[str, Any]) -> Any:
    coordinator = snapshot.SnapshotCoordinator(properties, _builtin_profiles())
    return get_driver_for_device(coordinator, {"guid": GUID, **device})


# -- the golden contract -------------------------------------------------


def test_the_golden_set_covers_every_category_of_the_migration() -> None:
    assert {path.stem for path in GOLDEN_FILES} == set(RESOLUTION)
    assert {path.stem for path in GOLDEN_FILES} == set(snapshot.categories())


@pytest.mark.parametrize("path", GOLDEN_FILES, ids=lambda path: path.stem)
def test_golden_file_is_a_complete_snapshot(path: Path) -> None:
    golden = _golden(path)
    assert golden["schema"] == snapshot.SCHEMA_ID
    assert golden["dc"] == path.stem
    # Provenance: which driver produced the baseline and which profiles existed.
    assert golden["baseline"]["driver"]
    assert golden["baseline"]["builtin_profiles"]
    assert golden["cases"]
    for case in golden["cases"]:
        assert case["device"]["dc"] == path.stem
        assert case["source"]
        assert case["properties"]
        assert case["sensors"] or case["binary_sensors"]


@pytest.mark.parametrize("path", GOLDEN_FILES, ids=lambda path: path.stem)
def test_the_entities_match_the_pre_migration_golden(path: Path) -> None:
    golden = _golden(path)
    for case in golden["cases"]:
        driver = _driver_for(case["device"], case["properties"])
        actual = snapshot.describe(driver, case["properties"])
        # Compare section by section so a failure names the entity and field.
        for section in ("sensors", "binary_sensors"):
            expected_entities = case[section]
            actual_entities = actual[section]
            assert list(actual_entities) == list(expected_entities), (
                f"{path.stem}.{case['id']}.{section}: entity set/order changed"
            )
            for key, expected in expected_entities.items():
                got = actual_entities[key]
                for field, value in expected.items():
                    assert got.get(field) == value, (
                        f"{path.stem}.{case['id']}.{section}.{key}.{field}"
                    )


@pytest.mark.parametrize("path", GOLDEN_FILES, ids=lambda path: path.stem)
def test_the_baseline_records_the_driver_that_was_migrated(path: Path) -> None:
    """The provenance in every golden file must stay truthful."""
    baseline = _golden(path)["baseline"]
    captured_from, driver_name = BASELINE[path.stem]
    assert baseline["captured_from"] == captured_from
    assert baseline["driver"] == driver_name
    if captured_from == "yaml profile":
        assert baseline["profile_id"] == "thermal-override"
        assert baseline["python_base"] == "thermal"
    else:
        assert baseline["profile_id"] is None
        assert baseline["python_base"] is None
    # P3-F added four profiles after these files were captured; the recorded
    # list is the evidence that the baseline predates them.
    assert baseline["builtin_profiles"] == ["thermal-override.yaml"]


# -- factory resolution --------------------------------------------------


def test_the_migrated_python_drivers_are_gone() -> None:
    for dc, (module_name, class_name) in REMOVED_DRIVERS.items():
        assert dc not in DRIVER_REGISTRY, dc
        # Neither the class nor its module may come back by accident.
        assert not hasattr(devices_module, class_name), class_name
        assert importlib.util.find_spec(
            f"custom_components.robam_roki.devices.{module_name}"
        ) is None


@pytest.mark.parametrize("dc", sorted(RESOLUTION))
def test_the_factory_resolves_each_category_as_recorded(dc: str) -> None:
    profile_id, python_base, driver_cls = RESOLUTION[dc]
    device = dict(
        snapshot.THERMAL_DEVICES.get(dc) or snapshot.cases_for(dc)[0]["device"]
    )
    driver = _driver_for(device, {})

    assert type(driver) is driver_cls, dc
    profile = getattr(driver, "profile", None)
    assert getattr(profile, "id", None) == profile_id, dc
    assert getattr(profile, "python_base", None) == python_base, dc


@pytest.mark.parametrize("dc", sorted(REMOVED_DRIVERS))
def test_the_matching_profile_exposes_the_same_entity_set(dc: str) -> None:
    """match_profile must yield the entity set the golden recorded."""
    golden = _golden(BEHAVIOR_DIR / f"{dc}.json")
    profiles = _builtin_profiles()
    profile = match_profile(golden["cases"][0]["device"], profiles)
    assert profile is not None
    assert profile.source == SOURCE_BUILTIN
    assert profile.dc == dc

    driver = _driver_for(golden["cases"][0]["device"], {})
    assert isinstance(driver, ProfileDriver)
    assert driver.commands() == {}
    assert driver.capabilities == frozenset()

    recorded = set(golden["cases"][0]["sensors"])
    recorded |= set(golden["cases"][0]["binary_sensors"])
    exposed = {spec.key for spec in driver.sensors()}
    exposed |= {spec.key for spec in driver.binary_sensors()}
    assert exposed == recorded


# -- the one deliberate behaviour difference -----------------------------


def test_a_missing_status_property_is_unknown_not_off() -> None:
    """The only deliberate difference against the Python drivers.

    The Python drivers computed the status word from a property that defaulted
    to 0, so a payload without the status property at all read 关火 (stove) or
    关机 (dishwasher) -- an assertion that the appliance is off, made on no
    evidence.  The profiles use one honest fallback for "no property" and
    "unknown code" alike (未知).  Recognised and unrecognised *codes* render
    exactly as before; the golden files pin both.
    """
    stove = _driver_for({"dc": "RRQZ", "dt": "9B39"}, {"stoveHeadLeftPower": 0})
    sensors = {spec.key: spec.value_fn for spec in stove.sensors()}
    assert sensors["left_head_status"]({"stoveHeadLeftPower": 0}) == "未知"
    assert sensors["right_head_status"]({"stoveHeadLeftPower": 0}) == "未知"

    dishwasher = _driver_for({"dc": "RXWJ"}, {"remainTime": 10})
    work_status = {spec.key: spec.value_fn for spec in dishwasher.sensors()}[
        "work_status"
    ]
    assert work_status({"remainTime": 10}) == "未知"


# -- profile hygiene -----------------------------------------------------


def test_every_presentation_value_in_a_builtin_profile_is_a_real_enum() -> None:
    """A typo in a profile would otherwise degrade silently to "no class"."""
    from homeassistant.components.binary_sensor import BinarySensorDeviceClass
    from homeassistant.components.sensor import (
        SensorDeviceClass,
        SensorStateClass,
    )
    from homeassistant.const import EntityCategory

    checked = 0
    for profile in load_profile_dir(COMPONENT / "profiles", SOURCE_BUILTIN):
        for entry in profile.sensors.values():
            if "device_class" in entry:
                SensorDeviceClass(entry["device_class"])
                checked += 1
            if "state_class" in entry:
                SensorStateClass(entry["state_class"])
                checked += 1
            if "entity_category" in entry:
                EntityCategory(entry["entity_category"])
                checked += 1
        for entry in profile.binary_sensors.values():
            if "device_class" in entry:
                BinarySensorDeviceClass(entry["device_class"])
                checked += 1
            if "entity_category" in entry:
                EntityCategory(entry["entity_category"])
                checked += 1
    assert checked > 0


def test_every_builtin_device_profile_has_a_device_fixture() -> None:
    """The rule of tools/check_fixtures.py, applied to the whole directory."""
    name = "roki_check_fixtures_golden_test"
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "tools" / "check_fixtures.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)

    changed = [
        str(path.relative_to(REPO_ROOT)).replace("\\", "/")
        for path in sorted((COMPONENT / "profiles").glob("*.yaml"))
    ]
    result = module.check_paths(changed, REPO_ROOT)
    assert result.missing == []
    assert result.uncovered == []
