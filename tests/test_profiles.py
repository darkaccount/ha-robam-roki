"""Offline tests for the YAML profile layer (profiles.py).

The module is loaded straight from its file so these tests prove it has no
Home Assistant dependency: nothing from the integration package is imported,
and every profile is parsed with PyYAML only.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import shutil
import sys
import textwrap
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "robam_roki"
    / "profiles.py"
)


def _load_profiles_module() -> Any:
    name = "roki_profiles_test"
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Register before executing: dataclasses resolves string annotations through
    # sys.modules on Python 3.13.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


profiles = _load_profiles_module()


@pytest.fixture
def tmp_path() -> Iterator[Path]:
    """A temporary directory that can actually be written to.

    This machine's sandbox denies writes to the user Temp directory, so the
    builtin tmp_path fixture silently falls back to the repository root and
    leaves pytest-of-* directories in the working tree.  This override keeps the
    directory inside the workspace and always removes it, also on failure.
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


MINIMAL = """
schema: roki-profile/1
match:
  dc: RZQL
"""

COMPLETE = """
schema: roki-profile/1
id: complete-oven
extends: python:thermal
match:
  dc: RZQL
  dt: [C98A, C98B]
name: 蒸烤箱
sensors:
  cavity_temp:
    keys: [curTopTemp, topTemp]
    type: int
    scale: 1
    map: {0: 关闭}
    default: 0
    name: 腔体温度
    icon: mdi:thermometer
    unit: °C
    device_class: temperature
    state_class: measurement
    entity_category: diagnostic
binary_sensors:
  door:
    keys: [doorState, doorOpen]
    on_values: [1, "open"]
    name: 门
    icon: mdi:door
    device_class: door
    entity_category: diagnostic
controls:
  power:
    kind: property
    on:
      working_status: 1
    off:
      working_status: 0
    verified: true
  level:
    kind: invoke
    function: 134
    param: level
    params:
      gear: 2
    verified: false
"""


def _write(directory: Path, name: str, text: str) -> Path:
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).strip() + "\n", encoding="utf-8")
    return path


def _load(tmp_path: Path, text: str, name: str = "profile.yaml") -> Any:
    return profiles.load_profile_file(_write(tmp_path, name, text))


# -- minimal / complete documents ----------------------------------------


def test_minimal_profile_loads(tmp_path: Path) -> None:
    profile = _load(tmp_path, MINIMAL)
    assert profile is not None
    assert profile.id == "profile"  # defaults to the file name stem
    assert profile.dc == "RZQL"
    assert profile.dt is None
    assert profile.source == profiles.SOURCE_USER
    assert profile.sensors == {}
    assert profile.binary_sensors == {}
    assert profile.controls == {}
    assert profile.extends is None


def test_complete_profile_round_trips(tmp_path: Path) -> None:
    profile = _load(tmp_path, COMPLETE)
    assert profile is not None
    assert profile.id == "complete-oven"
    assert profile.dc == "RZQL"
    assert profile.dt == ["C98A", "C98B"]
    assert profile.name == "蒸烤箱"
    assert profile.extends == "python:thermal"

    sensor = profile.sensors["cavity_temp"]
    assert sensor["keys"] == ["curTopTemp", "topTemp"]
    assert sensor["type"] == "int"
    assert sensor["scale"] == 1
    assert sensor["map"] == {0: "关闭"}
    assert sensor["default"] == 0
    assert sensor["name"] == "腔体温度"
    assert sensor["icon"] == "mdi:thermometer"
    assert sensor["unit"] == "°C"
    assert sensor["device_class"] == "temperature"
    assert sensor["state_class"] == "measurement"
    assert sensor["entity_category"] == "diagnostic"

    binary = profile.binary_sensors["door"]
    assert binary["keys"] == ["doorState", "doorOpen"]
    assert binary["on_values"] == [1, "open"]
    assert binary["name"] == "门"
    assert binary["icon"] == "mdi:door"
    assert binary["device_class"] == "door"
    assert binary["entity_category"] == "diagnostic"

    # controls mirror the CommandSpec fields from devices/base.py.
    assert profile.controls["power"] == {
        "kind": "property",
        "on": {"working_status": 1},
        "off": {"working_status": 0},
        "verified": True,
    }
    assert profile.controls["level"] == {
        "kind": "invoke",
        "function": "134",
        "param": "level",
        "params": {"gear": 2},
        "verified": False,
    }


def test_explicit_id_wins_over_the_file_name(tmp_path: Path) -> None:
    profile = _load(tmp_path, MINIMAL + "id: custom-id\n")
    assert profile is not None
    assert profile.id == "custom-id"


# -- validation failures -------------------------------------------------


def test_unknown_fields_are_ignored_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    text = """
    schema: roki-profile/1
    match:
      dc: RZQL
    mystery: 1
    sensors:
      temp:
        keys: [temp]
        bogus: 2
    """
    with caplog.at_level(logging.WARNING):
        profile = _load(tmp_path, text)
    assert profile is not None
    assert "bogus" not in profile.sensors["temp"]
    assert not hasattr(profile, "mystery")
    messages = [record.getMessage() for record in caplog.records]
    assert any("unknown field 'mystery'" in message for message in messages)
    assert any("unknown field 'bogus'" in message for message in messages)


def test_wrong_field_types_are_dropped_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    text = """
    schema: roki-profile/1
    match:
      dc: RZQL
    sensors:
      temp:
        keys: [temp]
        type: integer
        scale: big
        map: nope
        name: 7
      broken:
        keys: 5
      empty:
        unknown_field: 1
    binary_sensors:
      door:
        keys: doorState
        on_values: 1
    """
    with caplog.at_level(logging.WARNING):
        profile = _load(tmp_path, text)
    assert profile is not None
    temp = profile.sensors["temp"]
    assert temp == {"keys": ["temp"]}  # every bad field was dropped
    assert "broken" not in profile.sensors  # unusable keys -> whole entry
    assert "empty" not in profile.sensors  # nothing usable -> whole entry
    assert "on_values" not in profile.binary_sensors["door"]
    warnings = "\n".join(record.getMessage() for record in caplog.records)
    assert ".type" in warnings
    assert ".scale" in warnings


def test_missing_dc_is_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    text = """
    schema: roki-profile/1
    match:
      dt: R8229
    """
    with caplog.at_level(logging.WARNING):
        assert _load(tmp_path, text) is None
    assert any(
        "match.dc" in record.getMessage() for record in caplog.records
    )


def test_wrong_schema_is_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    text = """
    schema: roki-profile/99
    match:
      dc: RZQL
    """
    with caplog.at_level(logging.WARNING):
        assert _load(tmp_path, text) is None
    assert any(
        "unsupported schema" in record.getMessage() for record in caplog.records
    )


def test_missing_schema_is_skipped(tmp_path: Path) -> None:
    text = """
    match:
      dc: RZQL
    """
    assert _load(tmp_path, text) is None


def test_empty_file_is_skipped(tmp_path: Path) -> None:
    assert _load(tmp_path, "") is None
    assert _load(tmp_path, "# only a comment\n") is None


def test_invalid_yaml_never_raises(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        assert _load(tmp_path, "schema: [unclosed") is None
    assert any(
        "invalid YAML" in record.getMessage() for record in caplog.records
    )


def test_non_mapping_document_is_skipped(tmp_path: Path) -> None:
    assert _load(tmp_path, "- just\n- a list\n") is None


def test_unreadable_file_returns_none(tmp_path: Path) -> None:
    assert profiles.load_profile_file(tmp_path / "missing.yaml") is None


# -- directories ---------------------------------------------------------


def test_load_profile_dir_missing_and_empty(tmp_path: Path) -> None:
    assert profiles.load_profile_dir(tmp_path / "nope", "builtin") == []
    (tmp_path / "empty").mkdir()
    assert profiles.load_profile_dir(tmp_path / "empty", "builtin") == []


def test_load_profile_dir_picks_up_yaml_only(tmp_path: Path) -> None:
    directory = tmp_path / "profiles"
    _write(directory, "one.yaml", MINIMAL)
    _write(directory, "two.yml", MINIMAL)
    _write(directory, "notes.txt", MINIMAL)
    loaded = profiles.load_profile_dir(directory, "builtin")
    assert [profile.path.name for profile in loaded] == ["one.yaml"]
    assert loaded[0].source == "builtin"


def test_device_profiles_need_a_match(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    directory = tmp_path / "profiles"
    _write(directory, "fragment.yaml", "schema: roki-profile/1\nsensors: {}\n")
    _write(directory, "good.yaml", MINIMAL)
    with caplog.at_level(logging.WARNING):
        loaded = profiles.load_profile_dir(directory, "user")
    assert [profile.id for profile in loaded] == ["good"]
    assert any(
        "needs match.dc" in record.getMessage() for record in caplog.records
    )


def test_broken_file_does_not_stop_other_files(tmp_path: Path) -> None:
    directory = tmp_path / "profiles"
    _write(directory, "a-broken.yaml", "schema: [unclosed")
    _write(directory, "b-good.yaml", MINIMAL)
    loaded = profiles.load_profile_dir(directory, "user")
    assert [profile.path.name for profile in loaded] == ["b-good.yaml"]


def test_shared_fragments_may_omit_match(tmp_path: Path) -> None:
    directory = tmp_path / "profiles"
    _write(
        directory,
        "_shared/base.yaml",
        """
        schema: roki-profile/1
        id: base
        sensors:
          temp:
            keys: [temp]
            type: int
        """,
    )
    _write(
        directory,
        "device.yaml",
        """
        schema: roki-profile/1
        extends: base
        match:
          dc: RZQL
        """,
    )
    loaded = profiles.load_profile_dir(directory, "user")
    assert [profile.id for profile in loaded] == ["base", "device"]


# -- extends resolution --------------------------------------------------


def test_extends_fragment_merges_sections(tmp_path: Path) -> None:
    directory = tmp_path / "profiles"
    _write(
        directory,
        "_shared/base.yaml",
        """
        schema: roki-profile/1
        id: base
        name: 基础
        sensors:
          temp:
            keys: [temp]
            type: int
          keep:
            keys: [keepKey]
        binary_sensors:
          door:
            keys: [door]
        """,
    )
    _write(
        directory,
        "device.yaml",
        """
        schema: roki-profile/1
        extends: base
        match:
          dc: RZQL
        sensors:
          temp:
            keys: [curTopTemp]
        """,
    )
    full = profiles.resolve_extends(
        profiles.load_profile_dir(directory, "user")
    )
    assert len(full) == 1
    assert full[0].id == "device"
    assert full[0].name == "基础"  # inherited from the fragment
    assert full[0].sensors["keep"] == {"keys": ["keepKey"]}
    # Shallow merge: the child entry replaces the whole parent entry, so the
    # parent's type must be gone rather than merged field by field.
    assert full[0].sensors["temp"] == {"keys": ["curTopTemp"]}
    assert full[0].binary_sensors["door"] == {"keys": ["door"]}


def test_extends_python_driver_sets_the_base(tmp_path: Path) -> None:
    directory = tmp_path / "profiles"
    _write(
        directory,
        "device.yaml",
        """
        schema: roki-profile/1
        extends: python:thermal
        match:
          dc: RZQL
        sensors:
          temp:
            keys: [curTopTemp]
        """,
    )
    full = profiles.resolve_extends(
        profiles.load_profile_dir(directory, "user")
    )
    assert len(full) == 1
    assert full[0].python_base == "thermal"
    assert full[0].sensors == {"temp": {"keys": ["curTopTemp"]}}


def test_extends_missing_target_is_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    directory = tmp_path / "profiles"
    _write(
        directory,
        "device.yaml",
        """
        schema: roki-profile/1
        extends: does-not-exist
        match:
          dc: RZQL
        """,
    )
    with caplog.at_level(logging.WARNING):
        full = profiles.resolve_extends(
            profiles.load_profile_dir(directory, "user")
        )
    assert full == []
    assert any(
        "not found" in record.getMessage() for record in caplog.records
    )


def test_extends_cycle_is_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    directory = tmp_path / "profiles"
    _write(
        directory,
        "_shared/one.yaml",
        """
        schema: roki-profile/1
        id: one
        extends: two
        """,
    )
    _write(
        directory,
        "_shared/two.yaml",
        """
        schema: roki-profile/1
        id: two
        extends: one
        """,
    )
    _write(
        directory,
        "device.yaml",
        """
        schema: roki-profile/1
        extends: one
        match:
          dc: RZQL
        """,
    )
    with caplog.at_level(logging.WARNING):
        full = profiles.resolve_extends(
            profiles.load_profile_dir(directory, "user")
        )
    assert full == []
    assert any(
        "loops back" in record.getMessage() for record in caplog.records
    )


def test_fragments_are_not_device_profiles(tmp_path: Path) -> None:
    directory = tmp_path / "profiles"
    _write(
        directory,
        "_shared/base.yaml",
        """
        schema: roki-profile/1
        id: base
        sensors:
          temp:
            keys: [temp]
        """,
    )
    loaded = profiles.load_profile_dir(directory, "user")
    assert len(loaded) == 1
    assert profiles.resolve_extends(loaded) == []
    device = {"dc": "RZQL", "dt": "C98A"}
    assert profiles.match_profile(device, loaded) is None


# -- matching and priority ----------------------------------------------


def _builtin(tmp_path: Path, text: str, name: str) -> Any:
    directory = tmp_path / "builtin"
    _write(directory, name, text)
    return profiles.load_profile_dir(directory, "builtin")


def test_user_profile_beats_builtin(tmp_path: Path) -> None:
    builtin = _builtin(tmp_path, MINIMAL, "builtin.yaml")
    user_dir = tmp_path / "user"
    _write(user_dir, "user.yaml", MINIMAL)
    user = profiles.load_profile_dir(user_dir, "user")
    resolved = profiles.resolve_extends([*builtin, *user])
    matched = profiles.match_profile({"dc": "RZQL"}, resolved)
    assert matched is not None
    assert matched.source == "user"


def test_dt_exact_beats_list_beats_dc_only(tmp_path: Path) -> None:
    directory = tmp_path / "profiles"
    _write(directory, "dc-only.yaml", MINIMAL)
    _write(
        directory,
        "dt-list.yaml",
        """
        schema: roki-profile/1
        match:
          dc: RZQL
          dt: [C98A, C98B]
        """,
    )
    _write(
        directory,
        "dt-exact.yaml",
        """
        schema: roki-profile/1
        match:
          dc: RZQL
          dt: C98A
        """,
    )
    resolved = profiles.resolve_extends(
        profiles.load_profile_dir(directory, "builtin")
    )

    exact = profiles.match_profile({"dc": "RZQL", "dt": "C98A"}, resolved)
    assert exact is not None and exact.id == "dt-exact"

    listed = profiles.match_profile({"dc": "RZQL", "dt": "C98B"}, resolved)
    assert listed is not None and listed.id == "dt-list"

    fallback = profiles.match_profile({"dc": "RZQL", "dt": "UNKNOWN"}, resolved)
    assert fallback is not None and fallback.id == "dc-only"

    # No dt on the device: only the dc-only profile can match.
    no_dt = profiles.match_profile({"dc": "RZQL"}, resolved)
    assert no_dt is not None and no_dt.id == "dc-only"


def test_same_precision_conflict_picks_first_by_file_name(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    directory = tmp_path / "profiles"
    _write(directory, "b-second.yaml", MINIMAL)
    _write(directory, "a-first.yaml", MINIMAL)
    resolved = profiles.resolve_extends(
        profiles.load_profile_dir(directory, "builtin")
    )
    with caplog.at_level(logging.WARNING):
        matched = profiles.match_profile({"dc": "RZQL"}, resolved)
    assert matched is not None
    assert matched.path is not None and matched.path.name == "a-first.yaml"
    assert any(
        "profile conflict" in record.getMessage() for record in caplog.records
    )


def test_conflict_warning_is_emitted_only_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    profiles.reset_conflict_warnings()
    directory = tmp_path / "profiles"
    _write(directory, "b-first.yaml", MINIMAL)
    _write(directory, "c-second.yaml", MINIMAL)
    resolved = profiles.resolve_extends(
        profiles.load_profile_dir(directory, "builtin")
    )

    with caplog.at_level(logging.WARNING):
        profiles.match_profile({"dc": "RZQL"}, resolved)
        profiles.match_profile({"dc": "RZQL"}, resolved)
    warnings = [
        record for record in caplog.records
        if "profile conflict" in record.getMessage()
    ]
    assert len(warnings) == 1

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        profiles.match_profile({"dc": "RZQL"}, resolved)
    assert not [
        record for record in caplog.records
        if "profile conflict" in record.getMessage()
    ]

    # The test hook makes the next conflict reportable again.
    profiles.reset_conflict_warnings()
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        profiles.match_profile({"dc": "RZQL"}, resolved)
    assert len(
        [
            record for record in caplog.records
            if "profile conflict" in record.getMessage()
        ]
    ) == 1


def test_match_requires_the_same_dc_and_a_dc(tmp_path: Path) -> None:
    resolved = profiles.resolve_extends(_builtin(tmp_path, MINIMAL, "p.yaml"))
    assert profiles.match_profile({"dc": "RYYJ"}, resolved) is None
    assert profiles.match_profile({}, resolved) is None
    assert profiles.match_profile({"dc": "RZQL"}, resolved) is not None


def test_match_is_case_insensitive(tmp_path: Path) -> None:
    resolved = profiles.resolve_extends(_builtin(tmp_path, MINIMAL, "p.yaml"))
    matched = profiles.match_profile({"dc": " rzql "}, resolved)
    assert matched is not None


def test_dt_list_matching_is_case_insensitive(tmp_path: Path) -> None:
    directory = tmp_path / "profiles"
    _write(
        directory,
        "p.yaml",
        """
        schema: roki-profile/1
        match:
          dc: RZQL
          dt: [c98a]
        """,
    )
    resolved = profiles.resolve_extends(
        profiles.load_profile_dir(directory, "user")
    )
    matched = profiles.match_profile({"dc": "rzql", "dt": "C98A"}, resolved)
    assert matched is not None


def test_duplicate_ids_keep_the_first_of_the_same_source(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    directory = tmp_path / "profiles"
    _write(directory, "a.yaml", MINIMAL + "id: same\n")
    _write(directory, "b.yaml", MINIMAL + "id: same\n")
    with caplog.at_level(logging.WARNING):
        loaded = profiles.load_profile_dir(directory, "builtin")
    resolved = profiles.resolve_extends(loaded)
    assert [profile.id for profile in resolved] == ["same"]
    assert any(
        "duplicate profile id" in record.getMessage()
        for record in caplog.records
    )


# -- presentation fields (schema fix) ------------------------------------


def test_invalid_entity_category_and_presentation_types_are_dropped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    text = """
    schema: roki-profile/1
    match:
      dc: RZQL
    sensors:
      bad_category:
        keys: [temp]
        entity_category: warning
      config_category:
        keys: [temp]
        entity_category: config
      bad_types:
        keys: [temp]
        icon: 7
        unit: 8
        device_class: []
        state_class: false
    binary_sensors:
      bad_door:
        keys: [door]
        entity_category: diagnostic
    """
    with caplog.at_level(logging.WARNING):
        profile = _load(tmp_path, text)
    assert profile is not None
    assert "entity_category" not in profile.sensors["bad_category"]
    assert profile.sensors["config_category"]["entity_category"] == "config"
    assert profile.sensors["bad_types"] == {"keys": ["temp"]}
    assert (
        profile.binary_sensors["bad_door"]["entity_category"] == "diagnostic"
    )
    warnings = "\n".join(record.getMessage() for record in caplog.records)
    assert "entity_category" in warnings


def test_keyless_entries_are_presentation_overrides(tmp_path: Path) -> None:
    # A profile may override only the presentation of an existing driver spec;
    # the driver then keeps the base entity's value function.
    text = """
    schema: roki-profile/1
    match:
      dc: RZQL
    sensors:
      top_temperature:
        name: 上管温度
        icon: mdi:thermometer-high
    binary_sensors:
      door:
        device_class: door
    """
    profile = _load(tmp_path, text)
    assert profile is not None
    assert profile.sensors["top_temperature"] == {
        "name": "上管温度",
        "icon": "mdi:thermometer-high",
    }
    assert profile.binary_sensors["door"] == {"device_class": "door"}


# -- controls mirror CommandSpec -----------------------------------------


def test_control_requires_a_valid_kind(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    text = """
    schema: roki-profile/1
    match:
      dc: RZQL
    controls:
      missing_kind:
        on: {working_status: 1}
      bad_kind:
        kind: write
        on: {working_status: 1}
    """
    with caplog.at_level(logging.WARNING):
        profile = _load(tmp_path, text)
    assert profile is not None
    assert profile.controls == {}
    warnings = "\n".join(record.getMessage() for record in caplog.records)
    assert ".kind:" in warnings


def test_invalid_control_field_types_are_dropped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    text = """
    schema: roki-profile/1
    match:
      dc: RZQL
    controls:
      power:
        kind: property
        on: [1, 0]
        off: nope
        params: 5
        function: true
        verified: yes_please
    """
    with caplog.at_level(logging.WARNING):
        profile = _load(tmp_path, text)
    assert profile is not None
    assert profile.controls["power"] == {"kind": "property"}
    warnings = "\n".join(record.getMessage() for record in caplog.records)
    assert ".verified:" in warnings


def test_yaml_boolean_keys_and_scalars_are_normalised(tmp_path: Path) -> None:
    # PyYAML turns bare on/off into booleans.  Users should not have to quote
    # them: both the boolean and the quoted spellings must load identically.
    text = """
    schema: roki-profile/1
    match:
      dc: RZQL
    controls:
      unquoted:
        kind: property
        on: {working_status: 1}
        off: {working_status: 0}
        param: on
      quoted:
        kind: property
        "on": {working_status: 1}
        "off": {working_status: 0}
        param: "on"
    """
    profile = _load(tmp_path, text)
    assert profile is not None
    unquoted = profile.controls["unquoted"]
    assert unquoted["on"] == {"working_status": 1}
    assert unquoted["off"] == {"working_status": 0}
    assert unquoted["param"] == "on"
    quoted = profile.controls["quoted"]
    assert quoted["on"] == {"working_status": 1}
    assert quoted["off"] == {"working_status": 0}
    assert quoted["param"] == "on"


def test_boolean_control_name_is_normalised(tmp_path: Path) -> None:
    text = """
    schema: roki-profile/1
    match:
      dc: RZQL
    controls:
      off:
        kind: property
        on: {working_status: 0}
    """
    profile = _load(tmp_path, text)
    assert profile is not None
    assert list(profile.controls) == ["off"]


# -- mqtt controls (P3-B) ------------------------------------------------


def test_mqtt_control_carries_command_and_body(tmp_path: Path) -> None:
    text = """
    schema: roki-profile/1
    match:
      dc: RZQL
    controls:
      lamp:
        kind: mqtt
        command: 140
        body_hex: "0A0B"
        verified: false
    """
    profile = _load(tmp_path, text)
    assert profile is not None
    assert profile.controls["lamp"] == {
        "kind": "mqtt",
        "command": 140,
        "body_hex": "0a0b",
        "verified": False,
    }


def test_mqtt_control_accepts_a_numeric_command_string(tmp_path: Path) -> None:
    text = """
    schema: roki-profile/1
    match:
      dc: RZQL
    controls:
      lamp:
        kind: mqtt
        command: "141"
        body_hex: ""
    """
    profile = _load(tmp_path, text)
    assert profile is not None
    assert profile.controls["lamp"]["command"] == 141
    assert profile.controls["lamp"]["body_hex"] == ""


def test_mqtt_controls_missing_or_broken_fields_are_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    too_long = "00" * 65
    text = f"""
    schema: roki-profile/1
    match:
      dc: RZQL
    controls:
      no_command:
        kind: mqtt
        body_hex: "01"
      no_body:
        kind: mqtt
        command: 140
      bad_hex:
        kind: mqtt
        command: 140
        body_hex: "zz"
      odd_hex:
        kind: mqtt
        command: 140
        body_hex: "abc"
      too_long:
        kind: mqtt
        command: 140
        body_hex: "{too_long}"
      bad_command:
        kind: mqtt
        command: 999
        body_hex: "01"
      non_mqtt_extra:
        kind: property
        on: {{working_status: 1}}
        command: 140
    """
    with caplog.at_level(logging.WARNING):
        profile = _load(tmp_path, text)
    assert profile is not None
    assert list(profile.controls) == ["non_mqtt_extra"]
    assert profile.controls["non_mqtt_extra"] == {
        "kind": "property",
        "on": {"working_status": 1},
    }
    warnings = "\n".join(record.getMessage() for record in caplog.records)
    assert ".command:" in warnings
    assert ".body_hex:" in warnings
    assert "command/body_hex only apply to kind=mqtt" in warnings


# -- the module itself ---------------------------------------------------


def test_profiles_module_has_no_homeassistant_import() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "homeassistant" not in source


def test_constants_match_the_public_contract() -> None:
    assert profiles.SCHEMA_ID == "roki-profile/1"
    assert set(profiles.TYPE_CONVERTERS) == {"int", "float", "str", "bool"}
    assert profiles.SOURCE_BUILTIN == "builtin"
    assert profiles.SOURCE_USER == "user"
