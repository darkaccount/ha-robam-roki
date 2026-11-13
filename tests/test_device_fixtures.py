"""Regression fixtures for real-device property payloads.

A fixture whose source is ``user`` is a capture from real hardware: the R8229
hood baseline and the C98A-D3P (RZKY / 98AD3) capture from PR #3 are the only
credible ones so far.

P3-F moved the stove (RRQZ), dishwasher (RXWJ) and water purifier (RJSQ) to
built-in YAML profiles, plus a model override for RZQL.  No capture exists for
them, so their fixtures are marked ``source: "hand"`` and say so in a note:
they are reviewed regression aids for the migrated profiles, never evidence
about a real appliance.  tests/fixtures/behavior/<dc>.json holds the
pre-migration golden those profiles are compared against.

Drivers are resolved exactly the way the integration resolves them -- with the
built-in profiles loaded -- so a fixture also exercises the profile path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from custom_components.robam_roki import profiles as profiles_module
from custom_components.robam_roki.devices import get_driver_for_device

COMPONENT = (
    Path(__file__).resolve().parents[1] / "custom_components" / "robam_roki"
)
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "devices"
FIXTURES = sorted(FIXTURE_DIR.glob("*.json"))

#: Captures from real hardware; adding one here is a deliberate claim.
REAL_DEVICE_FIXTURES = {"RYYJ-R8229", "RZKY-98AD3"}

#: Synthetic fixtures for categories that have no capture yet.
HAND_FIXTURES = {"RRQZ", "RXWJ", "RJSQ", "RZQL"}

#: Every source a fixture may declare (see CONTRIBUTING.md, maintainer chapter).
FIXTURE_SOURCES = {"user", "bundle", "auto", "hand"}


def _fixture(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _stems_with_source(source: str) -> set[str]:
    return {path.stem for path in FIXTURES if _fixture(path)["source"] == source}


def _builtin_profiles() -> list[Any]:
    loaded = profiles_module.load_profile_dir(
        COMPONENT / "profiles", profiles_module.SOURCE_BUILTIN
    )
    return profiles_module.resolve_extends(loaded)


class _FakeCoordinator:
    """Just enough coordinator for get_driver_for_device."""

    def __init__(self, guid: str, properties: dict[str, Any]) -> None:
        self.devices: list[dict[str, Any]] = []
        self.data = {guid: properties}
        self.profiles = _builtin_profiles()

    def is_rest_device(self, guid: str) -> bool:
        return False


def _entity_values(driver: Any, properties: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for spec in driver.sensors():
        if spec.value_fn is not None:
            values[spec.key] = spec.value_fn(properties)
        else:
            values[spec.key] = properties.get(spec.key)
    for spec in driver.binary_sensors():
        if spec.is_on_fn is not None:
            values[spec.key] = spec.is_on_fn(properties)
        else:
            values[spec.key] = properties.get(spec.key)
    return values


def test_the_fixture_list_is_the_reviewed_real_device_set() -> None:
    assert _stems_with_source("user") == REAL_DEVICE_FIXTURES


def test_the_hand_written_fixtures_say_they_are_not_captures() -> None:
    assert _stems_with_source("hand") == HAND_FIXTURES
    for path in FIXTURES:
        fixture = _fixture(path)
        if fixture["source"] != "hand":
            continue
        # A reviewer must be able to tell a synthetic payload from evidence.
        assert "not a real device capture" in fixture["note"].lower(), path.name


@pytest.mark.parametrize("path", FIXTURES, ids=lambda path: path.stem)
def test_fixture_matches_the_current_driver(path: Path) -> None:
    fixture = _fixture(path)
    assert fixture["schema"] == "roki-fixture/1"
    assert fixture["source"] in FIXTURE_SOURCES
    assert fixture["match"]["dc"]
    assert fixture["properties"]

    device = {
        "guid": "FIXTUREDEVICE0001",
        "dc": fixture["match"]["dc"],
        "dt": fixture["match"].get("dt"),
    }
    coordinator = _FakeCoordinator(device["guid"], fixture["properties"])
    driver = get_driver_for_device(coordinator, device)

    if fixture["expect"]:
        values = _entity_values(driver, fixture["properties"])
        for key, expected in fixture["expect"].items():
            assert values.get(key) == expected, key
        assert set(fixture["expect"]) <= set(values)
    else:  # pragma: no cover - current fixtures all carry expect
        assert driver.sensors() or driver.binary_sensors()


@pytest.mark.parametrize("path", FIXTURES, ids=lambda path: path.stem)
def test_fixture_tells_where_it_came_from(path: Path) -> None:
    fixture = _fixture(path)
    assert isinstance(fixture["expect"], dict) and fixture["expect"]
    if fixture["source"] == "user":
        # source=user means the payload was captured on a real appliance; the PR
        # description records which test/backup it came from.
        assert fixture["match"].get("dt")
    else:
        # A hand-written fixture must carry the note explaining that.
        assert fixture["note"]
