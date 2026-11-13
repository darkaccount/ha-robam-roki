"""End-to-end CLI draft from a recorded collect bundle.

The bundle in tests/fixtures/bundles/ is what `collect` writes after it
recorded the App's command frames: properties (redacted), compressed status
frames, observations and the commands section.  This test drives the real CLI
draft command over that file -- no Home Assistant, no network -- and checks the
profile it produces, including the envelope strip the drafter applies.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[1]
CLI_PATH = REPO / "tools" / "roki_cli.py"
PROFILES_PATH = REPO / "custom_components" / "robam_roki" / "profiles.py"
BUNDLE = REPO / "tests" / "fixtures" / "bundles" / "collected-RYYJ-R8229.json"


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cli = _load_module("roki_cli_e2e_test", CLI_PATH)
profiles = _load_module("roki_profiles_e2e_test", PROFILES_PATH)


def _draft(tmp_path: Path) -> Path:
    args = cli.build_parser().parse_args(
        ["draft", str(BUNDLE), "--out", str(tmp_path / "out")]
    )
    assert cli.cmd_draft(None, args) == 0
    return tmp_path / "out" / "RYYJ-R8229.yaml"


def test_the_bundle_fixture_has_commands_and_loads(tmp_path: Path) -> None:
    import json

    bundle = json.loads(BUNDLE.read_text(encoding="utf-8"))
    assert bundle["schema"] == "roki-bundle/1"
    assert bundle["device"] == {"dc": "RYYJ", "dt": "R8229", "transport": "mqtt"}
    assert len(bundle["commands"]) == 2


def test_cli_draft_turns_the_bundle_commands_into_controls(tmp_path: Path) -> None:
    profile_path = _draft(tmp_path)
    document = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    assert document["schema"] == "roki-profile/1"
    assert document["match"] == {"dc": "RYYJ", "dt": "R8229"}

    controls = document["controls"]
    assert list(controls) == ["action_1", "action_2"]
    # The captured envelope (terminal type + App user id) is stripped: only the
    # payload reaches the profile, exactly like the HA learning session.
    assert controls["action_1"]["body_hex"] == "02"
    assert controls["action_2"]["body_hex"] == "01"
    assert controls["action_1"]["kind"] == "mqtt"
    assert controls["action_1"]["command"] == 134
    assert controls["action_1"]["verified"] is False
    # The names come from the observed effects, with the guess comment above.
    assert controls["action_1"]["name"] == "设置档位 3"
    assert controls["action_2"]["name"] == "开机"
    text = profile_path.read_text(encoding="utf-8")
    assert text.count("# 名称是按设备变化推测的") == 2


def test_the_drafted_profile_matches_the_device_and_exposes_controls(
    tmp_path: Path,
) -> None:
    profile_path = _draft(tmp_path)
    profile = profiles.load_profile_file(profile_path, profiles.SOURCE_USER)
    assert profile is not None
    matched = profiles.match_profile({"dc": "RYYJ", "dt": "R8229"}, [profile])
    assert matched is not None
    assert set(matched.controls) == {"action_1", "action_2"}
    assert matched.controls["action_1"]["body_hex"] == "02"


def test_the_fixture_next_to_the_profile_keeps_the_capture(tmp_path: Path) -> None:
    import json

    _draft(tmp_path)
    fixture = json.loads(
        (tmp_path / "out" / "RYYJ-R8229.json").read_text(encoding="utf-8")
    )
    assert fixture["schema"] == "roki-fixture/1"
    assert fixture["source"] == "bundle"
    assert fixture["properties"]["workStatus"] == 1
