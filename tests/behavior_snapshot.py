"""Behaviour snapshot ("golden") for the built-in device categories.

Why this exists
---------------
P3-F moves a category's telemetry from a hand-written Python driver to a YAML
profile.  Moving code is only safe if the entities it produces stay exactly the
same: same keys, in the same order, with the same names, icons, units, device
classes, entity categories and computed values.  This module records that
contract per device category as a JSON file under ``tests/fixtures/behavior/``.

Provenance of a golden file
---------------------------
The files were captured from the *pre-migration* Python drivers by running this
module's ``__main__`` before any profile existed for the category.  They are
never regenerated to make a failing comparison pass: a snapshot is the
contract, not a snapshot of the current code.  ``baseline`` records which
driver produced it, so the diff between "captured" and "now" stays readable.

The payloads come from the existing test data (``tests/test_drivers.py`` and
the real captures in ``tests/fixtures/devices/``); each case names its source.

Regenerate (only for a deliberate behaviour change):
    python tests/behavior_snapshot.py
    python tests/behavior_snapshot.py --out /tmp/behavior   # compare elsewhere
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
FIXTURE_DIR = HERE / "fixtures" / "behavior"
PROFILES_DIR = REPO_ROOT / "custom_components" / "robam_roki" / "profiles"

SCHEMA_ID = "roki-behavior/1"

#: One fixed guid per snapshot: entity keys never depend on it, but the
#: coordinator needs one to look the device's properties up.
GUID = "BEHAVIOURDEVICE001"

#: The payloads each category is recorded with.  "source" says where the
#: properties came from so a reviewer can trace them back to a test or capture.
CASES: dict[str, list[dict[str, Any]]] = {
    "RRQZ": [
        {
            "id": "stove_telemetry",
            "source": "tests/test_drivers.py::test_stove_driver_telemetry",
            "device": {"dc": "RRQZ", "dt": "9B39"},
            "properties": {
                "stoveHeadLeftPower": 5,
                "stoveHeadRightPower": 9,
                "stoveHeadLeftRemainingTime": 15,
                "stoveHeadRightRemainingTime": 0,
                "stoveHeadLeftWorkStatus": 2,
                "stoveHeadRightWorkStatus": 3,
                "stoveHeadLeftAlarmStatus": 0,
                "stoveHeadRightAlarmStatus": 1,
            },
        },
        {
            "id": "stove_legacy_keys",
            "source": "the legacy key spelling the former driver also accepted",
            "device": {"dc": "RRQZ", "dt": "9B39"},
            "properties": {
                "leftHeadLevel": 4,
                "rightHeadLevel": 7,
                "leftHeadTime": 20,
                "rightHeadTime": 5,
                "leftHeadStatus": 1,
                "rightHeadStatus": 0,
                "leftHeadAlarm": 0,
                "rightHeadAlarm": 0,
            },
        },
        {
            "id": "stove_unknown_status",
            "source": "a status code outside the documented map",
            "device": {"dc": "RRQZ", "dt": "9B39"},
            "properties": {
                "stoveHeadLeftPower": 0,
                "stoveHeadRightPower": 0,
                "stoveHeadLeftRemainingTime": 0,
                "stoveHeadRightRemainingTime": 0,
                "stoveHeadLeftWorkStatus": 9,
                "stoveHeadRightWorkStatus": 7,
                "stoveHeadLeftAlarmStatus": 0,
                "stoveHeadRightAlarmStatus": 0,
            },
        },
    ],
    "RXWJ": [
        {
            "id": "dishwasher_telemetry",
            "source": "tests/test_drivers.py::test_dishwasher_driver_telemetry",
            "device": {"dc": "RXWJ"},
            "properties": {
                "workStatus": 2,
                "remainTime": 45,
                "workMode": "强力洗",
                "doorOpen": 0,
                "lackSalt": 1,
                "lackRinse": 0,
            },
        },
        {
            "id": "dishwasher_legacy_keys",
            "source": "the DishWasher* key spelling the former driver also accepted",
            "device": {"dc": "RXWJ"},
            "properties": {
                "DishWasherStatus": 4,
                "DishWasherRemainingWorkingTime": 12,
                "DishWasherWorkMode": "eco",
                "DishWasherDoorValue": 1,
                "lackSalt": 0,
                "lackRinse": 1,
            },
        },
        {
            "id": "dishwasher_unknown_status",
            "source": "a status code outside the documented map",
            "device": {"dc": "RXWJ"},
            "properties": {
                "workStatus": 9,
                "remainTime": 0,
                "workMode": "unknown",
                "doorOpen": 0,
                "lackSalt": 0,
                "lackRinse": 0,
            },
        },
    ],
    "RJSQ": [
        {
            "id": "water_purifier_telemetry",
            "source": "tests/test_drivers.py::test_water_purifier_telemetry",
            "device": {"dc": "RJSQ"},
            "properties": {
                "input_tds": 250,
                "output_tds": 12,
                "filter1": 95,
                "filter2": 78,
                "purifying": 1,
                "leakAlarm": 0,
            },
        },
        {
            "id": "water_purifier_legacy_keys",
            "source": "the inTds / currentTDSValue spelling the former driver "
            "also accepted",
            "device": {"dc": "RJSQ"},
            "properties": {
                "inTds": 320,
                "currentTDSValue": 18,
                "filterLife1": 88,
                "filterLife2": 64,
                "workStatus": 1,
                "leakAlarm": 1,
            },
        },
        {
            "id": "water_purifier_filters_exhausted",
            "source": "a synthetic payload with both filters nearly used up",
            "device": {"dc": "RJSQ"},
            "properties": {
                "input_tds": 300,
                "output_tds": 20,
                "filter1": 10,
                "filter2": 5,
                "purifying": 0,
                "leakAlarm": 1,
            },
        },
    ],
}

#: Payloads shared by the whole thermal family (one Python base, YAML overrides).
THERMAL_CASES: list[dict[str, Any]] = [
    {
        "id": "c98a_working",
        "source": "tests/fixtures/devices/RZKY-98AD3.json (real capture, PR #3)",
        "properties": {
            "workState": 2,
            "powerState": 2,
            "curTopTemp": 140,
            "curButtomTemp": 96,
            "curStageNum": 1,
            "stageTotalNum": 0,
            "stageOneSetTopTemp": 160,
            "stageOneSetButtomTemp": 160,
            "totalRemainSeonds": 1800,
            "doorState": 0,
            "faultCode": 0,
            "lightSwitch": 1,
        },
    },
    {
        "id": "legacy_working",
        "source": "tests/test_drivers.py::test_thermal_cooker_telemetry",
        "properties": {
            "workStatus": 3,
            "temp": 100,
            "setTemp": 100,
            "remainTime": 25,
            "doorOpen": 0,
            "alarm": 0,
        },
    },
    {
        "id": "multi_stage",
        "source": "a synthetic payload with a two-stage programme",
        "properties": {
            "workState": 3,
            "powerState": 2,
            "curTopTemp": 175,
            "curButtomTemp": 170,
            "curStageNum": 2,
            "stageTotalNum": 3,
            "stageTwoSetTopTemp": 180,
            "stageTwoSetButtomTemp": 175,
            "remainTime": 40,
            "doorState": 1,
            "faultCode": 2,
            "lightSwitch": 0,
        },
    },
    {
        "id": "unplugged",
        "source": "tests/test_drivers.py::test_thermal_idle_is_not_always_off",
        "properties": {"workState": 0, "powerState": 0, "curTopTemp": 30},
    },
    {
        "id": "cooling_down",
        "source": "tests/test_drivers.py::test_thermal_idle_is_not_always_off",
        "properties": {"workState": 0, "powerState": 2, "curTopTemp": 130},
    },
]

#: dc -> model code used for the thermal cases (the profile layer matches on it).
THERMAL_DEVICES: dict[str, dict[str, Any]] = {
    "RZQL": {"dc": "RZQL", "dt": "C98A"},
    "RZKY": {"dc": "RZKY", "dt": "98AD3"},
    "RDKX": {"dc": "RDKX", "dt": "K1"},
    "RWBL": {"dc": "RWBL", "dt": "W1"},
    "RXDG": {"dc": "RXDG", "dt": "X1"},
}


def cases_for(dc: str) -> list[dict[str, Any]]:
    """The payload cases a snapshot for this category is recorded with."""
    if dc in CASES:
        return CASES[dc]
    if dc in THERMAL_DEVICES:
        device = THERMAL_DEVICES[dc]
        return [
            {**case, "device": dict(device)} for case in THERMAL_CASES
        ]
    raise KeyError(f"no behaviour cases for {dc}")


def categories() -> list[str]:
    """Every category a behaviour snapshot exists for."""
    return [*CASES, *THERMAL_DEVICES]


# -- capturing -----------------------------------------------------------


def _label(value: Any) -> str | None:
    """A stable string for an enum/unit/icon, or None.

    Home Assistant stores StrEnum members, whose value is the wire label; the
    offline test stubs mirror that, so one ``str()`` covers both.
    """
    return None if value is None else str(value)


def _safe(call: Any, *args: Any) -> Any:
    """Call a driver hook and record a raised exception instead of failing."""
    try:
        return call(*args)
    except Exception as err:  # noqa: BLE001 - a snapshot must never crash
        return {"error": f"{type(err).__name__}: {err}"}


def _sensor_state(spec: Any, properties: dict[str, Any]) -> dict[str, Any]:
    if spec.value_fn is not None:
        value = _safe(spec.value_fn, properties)
    else:
        value = properties.get(spec.key)
    return {
        "translation_key": spec.translation_key,
        "name": spec.name,
        "icon": spec.icon,
        "device_class": _label(spec.device_class),
        "state_class": _label(spec.state_class),
        "unit": _label(spec.unit_of_measurement),
        "entity_category": _label(spec.entity_category),
        "value": value,
        "attrs": _safe(spec.attrs_fn, properties) if spec.attrs_fn else {},
    }


def _binary_sensor_state(
    spec: Any, properties: dict[str, Any]
) -> dict[str, Any]:
    if spec.is_on_fn is not None:
        value = _safe(spec.is_on_fn, properties)
    else:
        value = properties.get(spec.key)
    return {
        "translation_key": spec.translation_key,
        "name": spec.name,
        "icon": spec.icon,
        "device_class": _label(spec.device_class),
        "entity_category": _label(spec.entity_category),
        "value": value,
        "attrs": _safe(spec.attrs_fn, properties) if spec.attrs_fn else {},
    }


def describe(driver: Any, properties: dict[str, Any]) -> dict[str, Any]:
    """Everything a driver exposes for one payload, as plain JSON data."""
    return {
        "sensors": {
            spec.key: _sensor_state(spec, properties)
            for spec in driver.sensors()
        },
        "binary_sensors": {
            spec.key: _binary_sensor_state(spec, properties)
            for spec in driver.binary_sensors()
        },
    }


def load_builtin_profiles() -> list[Any]:
    """The built-in profiles exactly as the integration loads them."""
    from custom_components.robam_roki import profiles as profiles_module

    profiles = profiles_module.load_profile_dir(
        PROFILES_DIR, profiles_module.SOURCE_BUILTIN
    )
    return profiles_module.resolve_extends(profiles)


class SnapshotCoordinator:
    """The corner of RokiCoordinator the driver factory reads."""

    def __init__(self, properties: dict[str, Any], profiles: list[Any]) -> None:
        self.devices: list[dict[str, Any]] = []
        self.data = {GUID: dict(properties)}
        self.profiles = profiles

    def is_rest_device(self, guid: str) -> bool:
        return False


def build_driver(device: dict[str, Any], properties: dict[str, Any]) -> Any:
    """The driver the integration builds for this device and payload."""
    from custom_components.robam_roki.devices import get_driver_for_device

    coordinator = SnapshotCoordinator(properties, load_builtin_profiles())
    return get_driver_for_device(
        coordinator, {"guid": GUID, **device}
    )


def baseline_of(driver: Any) -> dict[str, Any]:
    """Which driver produced the recorded entities."""
    profile = getattr(driver, "profile", None)
    return {
        "captured_from": (
            "yaml profile" if profile is not None else "python driver"
        ),
        "driver": type(driver).__name__,
        "profile_id": getattr(profile, "id", None),
        "python_base": getattr(profile, "python_base", None),
        "builtin_profiles": sorted(
            str(profile.path.name) for profile in load_builtin_profiles()
            if profile.path is not None
        ),
    }


def snapshot(dc: str) -> dict[str, Any]:
    """Build the golden document for one device category."""
    cases: list[dict[str, Any]] = []
    first_driver: Any = None
    for case in cases_for(dc):
        driver = build_driver(case["device"], case["properties"])
        if first_driver is None:
            first_driver = driver
        cases.append(
            {
                "id": case["id"],
                "source": case["source"],
                "device": case["device"],
                "properties": case["properties"],
                **describe(driver, case["properties"]),
            }
        )
    return {
        "schema": SCHEMA_ID,
        "dc": dc,
        "note": (
            "Golden behaviour baseline: the entities the pre-migration driver "
            "produced for these payloads. Regenerate only for a deliberate "
            "behaviour change (python tests/behavior_snapshot.py)."
        ),
        "baseline": baseline_of(first_driver),
        "cases": cases,
    }


def write_snapshots(out_dir: Path = FIXTURE_DIR, *, quiet: bool = False) -> list[Path]:
    """Write one ``<dc>.json`` per category and return the paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for dc in categories():
        path = out_dir / f"{dc}.json"
        path.write_text(
            json.dumps(snapshot(dc), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        written.append(path)
        if not quiet:
            print(f"wrote {path.relative_to(REPO_ROOT) if out_dir == FIXTURE_DIR else path}")
    return written


def _bootstrap() -> None:
    """Make the integration importable outside pytest (shared HA stubs)."""
    for entry in (str(REPO_ROOT), str(HERE)):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    import conftest  # noqa: F401  installs the Home Assistant stubs on import


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Capture the driver behaviour golden files."
    )
    parser.add_argument(
        "--out",
        default=str(FIXTURE_DIR),
        help="output directory (default: tests/fixtures/behavior)",
    )
    args = parser.parse_args(argv)
    _bootstrap()
    write_snapshots(Path(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
