"""Offline tests for draft.py (the profile + fixture drafter).

Both modules are loaded straight from their files, which also proves they do
not need Home Assistant or the integration package to run.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import yaml

MODULE_DIR = (
    Path(__file__).resolve().parents[1] / "custom_components" / "robam_roki"
)
DRAFT_PATH = MODULE_DIR / "draft.py"
PROFILES_PATH = MODULE_DIR / "profiles.py"


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


draft = _load_module("roki_draft_test", DRAFT_PATH)
profiles = _load_module("roki_profiles_draft_test", PROFILES_PATH)

DEVICE = {"guid": "RYYJ000000000001", "dc": "RYYJ", "dt": "R8229", "deviceSource": 0, "name": "厨房油烟机"}

PROPERTIES: dict[str, Any] = {
    "workStatus": 1,
    "level": 2,
    "timing": None,
    "temp": 38.5,
    "mode": "自动",
    "clean": True,
}


def _loaded(result: Any) -> dict[str, Any]:
    return yaml.safe_load(result.profile_yaml)


# -- from properties -----------------------------------------------------


def test_profile_yaml_has_schema_match_and_sorted_sensors() -> None:
    result = draft.draft_from_properties(DEVICE, PROPERTIES)
    document = _loaded(result)
    assert document["schema"] == "roki-profile/1"
    assert document["match"] == {"dc": "RYYJ", "dt": "R8229"}
    assert list(document["sensors"]) == [
        "clean",
        "level",
        "mode",
        "temp",
        "timing",
        "workStatus",
    ]
    assert result.warnings  # expect is empty without expect_fn
    assert any("expect" in warning for warning in result.warnings)


def test_scalar_types_are_inferred() -> None:
    sensors = _loaded(draft.draft_from_properties(DEVICE, PROPERTIES))["sensors"]
    assert sensors["clean"] == {"keys": ["clean"], "type": "bool"}
    assert sensors["level"] == {"keys": ["level"], "type": "int"}
    assert sensors["temp"] == {"keys": ["temp"], "type": "float"}
    assert sensors["mode"] == {"keys": ["mode"], "type": "str"}
    # A null value cannot be typed and stays a plain key list.
    assert sensors["timing"] == {"keys": ["timing"]}


def test_semantic_hints_stay_comments() -> None:
    result = draft.draft_from_properties(DEVICE, PROPERTIES)
    document = _loaded(result)
    sensors = document["sensors"]
    assert "device_class" not in sensors["temp"]
    assert "unit" not in sensors["temp"]
    assert "# device_class: temperature" in result.profile_yaml
    assert "# unit: °C" in result.profile_yaml
    # The small integer value only suggests an enum map as a comment.
    assert "map" not in sensors["level"]
    assert "# map: {0: 含义0, 1: 含义1, 2: 含义2}" in result.profile_yaml
    # A non-semantic key gets no guesses at all.
    assert "device_class" not in sensors["workStatus"]


def test_controls_block_is_entirely_commented() -> None:
    result = draft.draft_from_properties(DEVICE, PROPERTIES)
    document = _loaded(result)
    assert "controls" not in document
    comment_lines = [
        line for line in result.profile_yaml.splitlines() if line.lstrip().startswith("#")
    ]
    commented = "\n".join(comment_lines)
    assert "# controls:" in commented
    for field in ("kind", "on", "off", "function", "param", "params", "verified"):
        assert f"{field}:" in commented, field


def test_dt_is_omitted_from_match_when_device_has_none() -> None:
    result = draft.draft_from_properties({"dc": "RCOF"}, {"level": 1})
    document = _loaded(result)
    assert document["match"] == {"dc": "RCOF"}
    assert result.fixture["match"] == {"dc": "RCOF"}


def test_non_scalar_properties_warn_and_stay_untyped() -> None:
    result = draft.draft_from_properties(
        DEVICE, {"level": 1, "nested": {"a": 1}, "items": [1, 2]}
    )
    sensors = _loaded(result)["sensors"]
    assert sensors["nested"] == {"keys": ["nested"]}
    assert sensors["items"] == {"keys": ["items"]}
    assert sum("not a scalar" in warning for warning in result.warnings) == 2


def test_generated_profile_loads_and_matches(tmp_path: Path) -> None:
    result = draft.draft_from_properties(DEVICE, PROPERTIES)
    assert result.profile_yaml
    # The file is validated through the very loader the integration uses.
    profile_path = tmp_path / "draft-RYYJ-R8229.yaml"
    profile_path.write_text(result.profile_yaml, encoding="utf-8")
    profile = profiles.load_profile_file(profile_path, profiles.SOURCE_USER)
    assert profile is not None
    assert profile.dc == "RYYJ"
    assert profile.dt == "R8229"
    assert list(profile.sensors) == list(_loaded(result)["sensors"])
    matched = profiles.match_profile({"dc": "RYYJ", "dt": "R8229"}, [profile])
    assert matched is not None


# -- fixture -------------------------------------------------------------


def test_fixture_shape_and_redacted_properties() -> None:
    sensitive = {
        "level": 2,
        "phone": "13800000000",
        "guid": "0123456789abcdef0123456789abcdef",
    }
    result = draft.draft_from_properties(DEVICE, sensitive)
    fixture = result.fixture
    assert fixture["schema"] == "roki-fixture/1"
    assert fixture["source"] == "auto"
    assert fixture["match"] == {"dc": "RYYJ", "dt": "R8229"}
    assert fixture["properties"]["level"] == 2
    assert fixture["properties"]["phone"] == "**REDACTED**"
    assert fixture["properties"]["guid"] == "**REDACTED**"
    assert fixture["expect"] == {}
    assert any("expect" in warning for warning in result.warnings)
    # The fixture must survive a JSON round trip (the CLI writes it as JSON).
    assert json.loads(json.dumps(fixture)) == fixture


def test_expect_fn_populates_the_fixture() -> None:
    def expect_fn(properties: dict[str, Any]) -> dict[str, Any]:
        return {"level": properties.get("level")}

    result = draft.draft_from_properties(DEVICE, PROPERTIES, expect_fn=expect_fn)
    assert result.fixture["expect"] == {"level": 2}
    assert not any("expect" in warning for warning in result.warnings)


def test_expect_fn_errors_degrade_to_a_warning() -> None:
    def broken(properties: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("driver exploded")

    result = draft.draft_from_properties(DEVICE, PROPERTIES, expect_fn=broken)
    assert result.fixture["expect"] == {}
    assert any("expect_fn failed" in warning for warning in result.warnings)


def test_expect_fn_returning_a_non_mapping_warns() -> None:
    result = draft.draft_from_properties(
        DEVICE, PROPERTIES, expect_fn=lambda properties: ["nope"]
    )
    assert result.fixture["expect"] == {}
    assert any("not a mapping" in warning for warning in result.warnings)


# -- from a bundle -------------------------------------------------------


BUNDLE: dict[str, Any] = {
    "schema": "roki-bundle/1",
    "created": "2026-11-12T00:00:00Z",
    "device": {"dc": "RYYJ", "dt": "R8229", "transport": "mqtt"},
    "properties": {"level": 2, "workStatus": 1},
    "frames": [],
    "observations": [],
    "truncated": False,
}


def test_bundle_draft_marks_its_source() -> None:
    result = draft.draft_from_bundle(BUNDLE)
    assert result.fixture["source"] == "bundle"
    assert "# source: bundle" in result.profile_yaml
    assert "# transport: mqtt" in result.profile_yaml
    assert _loaded(result)["match"] == {"dc": "RYYJ", "dt": "R8229"}
    assert list(_loaded(result)["sensors"]) == ["level", "workStatus"]


def test_bundle_with_unexpected_schema_only_warns() -> None:
    bundle = dict(BUNDLE)
    bundle["schema"] = "roki-bundle/99"
    result = draft.draft_from_bundle(bundle)
    assert result.profile_yaml
    assert any("unexpected bundle schema" in warning for warning in result.warnings)


def test_bundle_invalid_input_is_a_warning() -> None:
    result = draft.draft_from_bundle(None)  # type: ignore[arg-type]
    assert result.profile_yaml == ""
    assert result.fixture == {}
    assert result.warnings

    missing = draft.draft_from_bundle({"schema": "roki-bundle/1"})
    assert missing.profile_yaml == ""
    assert missing.warnings


# -- from diagnostics ----------------------------------------------------


DIAGNOSTICS: dict[str, Any] = {
    "entry": {"title": "ROKI", "data": {}, "options": {}},
    "coordinator": {
        "sse_connected": False,
        "mqtt_connected": True,
        "enable_sse": True,
        "data": {
            "RYYJ...0001": {"level": 2, "workStatus": 1},
            "RZKY...0002": {"workState": 3, "curTopTemp": 140},
        },
    },
    "devices": [
        {
            "guid_masked": "RYYJ...0001",
            "name": "RYYJ-R8229",
            "dc": "RYYJ",
            "dt": "R8229",
            "deviceSource": 0,
        },
        {
            "guid_masked": "RZKY...0002",
            "name": "RZKY-98AD3",
            "dc": "RZKY",
            "dt": "98AD3",
            "deviceSource": 1,
        },
    ],
}


def test_diagnostics_multi_device_needs_a_choice() -> None:
    result = draft.draft_from_diagnostics(DIAGNOSTICS)
    assert result.profile_yaml == ""
    assert result.fixture == {}
    assert any("pass dc/dt" in warning for warning in result.warnings)


def test_diagnostics_selects_by_dc() -> None:
    result = draft.draft_from_diagnostics(DIAGNOSTICS, dc="RZKY")
    document = _loaded(result)
    assert document["match"] == {"dc": "RZKY", "dt": "98AD3"}
    assert list(document["sensors"]) == ["curTopTemp", "workState"]


def test_diagnostics_selects_by_dt() -> None:
    result = draft.draft_from_diagnostics(DIAGNOSTICS, dt="R8229")
    assert _loaded(result)["match"] == {"dc": "RYYJ", "dt": "R8229"}


def test_diagnostics_single_device_is_selected_automatically() -> None:
    single = dict(DIAGNOSTICS)
    single["devices"] = [DIAGNOSTICS["devices"][0]]
    result = draft.draft_from_diagnostics(single, expect_fn=lambda p: {"level": p.get("level")})
    assert _loaded(result)["match"]["dc"] == "RYYJ"
    assert result.fixture["expect"] == {"level": 2}
    assert not any("matching devices" in warning for warning in result.warnings)


def test_diagnostics_without_matching_device_warns() -> None:
    result = draft.draft_from_diagnostics(DIAGNOSTICS, dc="RYYJ", dt="UNKNOWN")
    assert result.profile_yaml == ""
    assert any("no diagnostics device matches" in warning for warning in result.warnings)


def test_diagnostics_without_cached_properties_still_pins_match() -> None:
    missing = dict(DIAGNOSTICS)
    missing["coordinator"] = {"data": {}}
    missing["devices"] = [DIAGNOSTICS["devices"][0]]
    result = draft.draft_from_diagnostics(missing)
    assert result.profile_yaml
    assert _loaded(result)["match"] == {"dc": "RYYJ", "dt": "R8229"}
    assert any("no cached properties" in warning for warning in result.warnings)


def test_diagnostics_without_devices_warns() -> None:
    result = draft.draft_from_diagnostics({"devices": [], "coordinator": {"data": {}}})
    assert result.profile_yaml == ""
    assert any("no devices" in warning for warning in result.warnings)


def test_diagnostics_ignores_redacted_credentials_in_properties() -> None:
    diagnostics = dict(DIAGNOSTICS)
    diagnostics["devices"] = [DIAGNOSTICS["devices"][0]]
    diagnostics["coordinator"] = {
        "data": {"RYYJ...0001": {"level": 2, "phone": "13800000000"}}
    }
    result = draft.draft_from_diagnostics(diagnostics)
    assert result.fixture["properties"]["phone"] == "**REDACTED**"


# -- input robustness ----------------------------------------------------


def test_invalid_inputs_only_produce_warnings() -> None:
    result = draft.draft_from_properties(None, None)  # type: ignore[arg-type]
    assert result.profile_yaml == ""
    assert result.warnings

    no_dc = draft.draft_from_properties({"dt": "R8229"}, {"level": 1})
    assert no_dc.profile_yaml == ""
    assert any("dc" in warning for warning in no_dc.warnings)

    bad_props = draft.draft_from_properties(DEVICE, ["nope"])  # type: ignore[arg-type]
    assert bad_props.profile_yaml == ""
    assert bad_props.warnings

    empty = draft.draft_from_properties(DEVICE, {})
    assert empty.profile_yaml
    assert any("no properties" in warning for warning in empty.warnings)


def test_draft_is_deterministic() -> None:
    first = draft.draft_from_properties(DEVICE, PROPERTIES)
    second = draft.draft_from_properties(DEVICE, PROPERTIES)
    assert first.profile_yaml == second.profile_yaml
    assert first.fixture == second.fixture


# -- the module itself ---------------------------------------------------


def _imports_homeassistant(source: str) -> bool:
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import homeassistant", "from homeassistant")):
            return True
    return False


def test_draft_module_has_no_homeassistant_import() -> None:
    assert not _imports_homeassistant(DRAFT_PATH.read_text(encoding="utf-8"))


def test_public_interface_is_intact() -> None:
    assert draft.DraftResult is not None
    assert callable(draft.draft_from_properties)
    assert callable(draft.draft_from_bundle)
    assert callable(draft.draft_from_diagnostics)
    assert draft.PROFILE_SCHEMA == "roki-profile/1"
    assert draft.FIXTURE_SCHEMA == "roki-fixture/1"
    assert draft.BUNDLE_SCHEMA == "roki-bundle/1"
