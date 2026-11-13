"""The CLI draft must produce the same profile the integration does.

The HA service drafts from an explicit driver coverage; the CLI drafts from a
captured bundle through tools/roki_cli.py, which loads the same helper
(builtin.py) stand-alone.  This module pins the agreement: identical extends
line, identical sensors and identical fixture expectations for three device
shapes -- a Python driver (hood), a built-in YAML profile (stove) and an unknown
category (generic fallback).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from custom_components.robam_roki import builtin as builtin_module
from custom_components.robam_roki import draft as draft_module

REPO = Path(__file__).resolve().parents[1]
CLI_PATH = REPO / "tools" / "roki_cli.py"


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cli = _load_module("roki_cli_builtin_test", CLI_PATH)

HOOD = {
    "guid": "R8229000000000000",
    "dc": "RYYJ",
    "dt": "R8229",
    "deviceSource": 0,
}
HOOD_PROPERTIES = {
    "workStatus": 1,
    "level": 2,
    "lightStatus": 0,
    "timing": 15,
    "custom": 7,
}
STOVE = {
    "guid": "RRQZ0000000000000",
    "dc": "RRQZ",
    "dt": "9B39",
    "deviceSource": 0,
}
STOVE_PROPERTIES = {"stoveHeadLeftPower": 5, "phone": "13800000000"}
UNKNOWN = {
    "guid": "RXXX0000000000000",
    "dc": "RXXX",
    "dt": "X1",
    "deviceSource": 0,
}
UNKNOWN_PROPERTIES = {"custom": 7, "phone": "13800000000"}

CASES = [
    ("hood", HOOD, HOOD_PROPERTIES),
    ("stove", STOVE, STOVE_PROPERTIES),
    ("unknown", UNKNOWN, UNKNOWN_PROPERTIES),
]


def _bundle(device: dict[str, Any], properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "roki-bundle/1",
        "created": "2026-11-13T00:00:00Z",
        "device": {
            "dc": device["dc"],
            "dt": device["dt"],
            "transport": "mqtt",
        },
        "properties": dict(properties),
        "frames": [],
        "observations": [],
        "truncated": False,
    }


def _cli_draft(
    tmp_path: Path, device: dict[str, Any], properties: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """What the CLI writes for one capture."""
    source = tmp_path / "bundle.json"
    source.write_text(
        json.dumps(_bundle(device, properties)), encoding="utf-8"
    )
    out = tmp_path / "out"
    args = cli.build_parser().parse_args(
        ["draft", str(source), "--out", str(out)]
    )
    assert cli.cmd_draft(None, args) == 0
    profile = yaml.safe_load(
        (out / f"{device['dc']}-{device['dt']}.yaml").read_text(encoding="utf-8")
    )
    fixture = json.loads(
        (out / f"{device['dc']}-{device['dt']}.json").read_text(encoding="utf-8")
    )
    return profile, fixture


def _ha_draft(
    device: dict[str, Any], properties: dict[str, Any]
) -> Any:
    """What the integration service would draft for the same capture."""
    name, keys, expect_fn = builtin_module.coverage(dict(device))
    return draft_module.draft_from_properties(
        dict(device),
        dict(properties),
        expect_fn=expect_fn,
        builtin_driver=name,
        builtin_keys=keys,
    )


@pytest.mark.parametrize("label,device,properties", CASES, ids=[case[0] for case in CASES])
def test_the_cli_and_the_ha_draft_produce_the_same_profile(
    tmp_path: Path, label: str, device: dict[str, Any], properties: dict[str, Any]
) -> None:
    cli_profile, _ = _cli_draft(tmp_path, device, properties)
    reference = _ha_draft(device, properties)
    assert reference.profile_yaml
    assert cli_profile == yaml.safe_load(reference.profile_yaml)


@pytest.mark.parametrize("label,device,properties", CASES, ids=[case[0] for case in CASES])
def test_the_cli_and_the_ha_fixture_agree(
    tmp_path: Path, label: str, device: dict[str, Any], properties: dict[str, Any]
) -> None:
    _, cli_fixture = _cli_draft(tmp_path, device, properties)
    reference = _ha_draft(device, properties)
    assert cli_fixture["expect"] == reference.fixture["expect"]
    assert cli_fixture["properties"] == reference.fixture["properties"]
    assert cli_fixture["match"] == reference.fixture["match"]


def test_the_hood_draft_inherits_the_hood_driver(tmp_path: Path) -> None:
    profile, _ = _cli_draft(tmp_path, HOOD, HOOD_PROPERTIES)
    assert profile["extends"] == "python:hood"
    # workStatus/level/lightStatus/timing back the hood controls, so they are
    # skipped; only the genuinely new property becomes a sensor.
    assert list(profile["sensors"]) == ["custom"]


def test_an_unknown_category_falls_back_to_the_generic_driver(
    tmp_path: Path,
) -> None:
    profile, _ = _cli_draft(tmp_path, UNKNOWN, UNKNOWN_PROPERTIES)
    assert profile["extends"] == "python:generic"


def test_a_category_served_by_a_builtin_profile_gets_no_extends(
    tmp_path: Path,
) -> None:
    profile, _ = _cli_draft(tmp_path, STOVE, STOVE_PROPERTIES)
    # The built-in YAML profile is the driver; the draft replaces nothing.
    assert "extends" not in profile
    assert profile["match"] == {"dc": "RRQZ", "dt": "9B39"}


def test_the_hood_fixture_carries_the_real_expectations(tmp_path: Path) -> None:
    _, fixture = _cli_draft(tmp_path, HOOD, HOOD_PROPERTIES)
    assert fixture["expect"]["work_status"] in {"开机", "延时关机", "关机"}
    assert "fault_code" in fixture["expect"]
    assert "clean_required" in fixture["expect"]
    # Privacy: the phone-like value is still redacted by the drafter.
    assert fixture["properties"].get("phone") in (None, "**REDACTED**")
