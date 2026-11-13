"""draft --merge: captured controls join an existing profile.

The merge lives in draft.py (HA-free) so it can be tested directly; the CLI
tests below drive the subcommand and check the files it writes.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[1]
CLI_PATH = REPO / "tools" / "roki_cli.py"
DRAFT_PATH = REPO / "custom_components" / "robam_roki" / "draft.py"


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cli = _load_module("roki_cli_merge_test", CLI_PATH)
draft = _load_module("roki_draft_merge_test", DRAFT_PATH)

PROFILE = """# 我自己写的配置，别动注释
schema: roki-profile/1
extends: python:hood
match:
  dc: RYYJ
  dt: R8229
sensors:
  custom:
    name: 自定义传感器
    # 手工加的说明
binary_sensors:
  dry:
    key: clean_required
    device_class: problem
controls:
  action_1:  # 命令 132，body 01
    name: 开机
    kind: mqtt
    command: 132
    body_hex: "01"
    verified: false
  power:
    kind: property
    on: {working_status: 1}
    off: {working_status: 0}
    verified: true
"""

BARE_PROFILE = """schema: roki-profile/1
match:
  dc: RYYJ
  dt: R8229
sensors:
  custom:
    name: 自定义传感器
# controls: 以后手写
"""

LEARNED = [
    {
        "index": 0,
        "command": 132,
        "body_hex": "0b3132333435363738393001",
        "observed": {"workStatus": {"from": 0, "to": 1}},
    },
    {
        "index": 1,
        "command": 134,
        "body_hex": "02",
        "observed": {"level": {"from": 1, "to": 3}},
    },
]

BUNDLE = {
    "schema": "roki-bundle/1",
    "device": {"dc": "RYYJ", "dt": "R8229", "transport": "mqtt"},
    "properties": {"workStatus": 1, "level": 2},
    "frames": [],
    "observations": [],
    "commands": LEARNED,
    "truncated": False,
}


# -- draft.py: the merge itself -----------------------------------------


def test_merge_keeps_everything_it_did_not_come_for() -> None:
    result = draft.merge_controls(PROFILE, LEARNED)
    document = yaml.safe_load(result.profile_yaml)
    assert document["extends"] == "python:hood"
    assert list(document["sensors"]) == ["custom"]
    assert list(document["binary_sensors"]) == ["dry"]
    assert "power" in document["controls"]
    # Comments and hand edits survive: the file is edited, never re-rendered.
    assert "# 我自己写的配置，别动注释" in result.profile_yaml
    assert "# 手工加的说明" in result.profile_yaml
    assert "name: 开机" in result.profile_yaml


def test_merge_appends_new_actions_and_skips_duplicates() -> None:
    result = draft.merge_controls(PROFILE, LEARNED)
    document = yaml.safe_load(result.profile_yaml)
    # 132:01 already exists (the capture carries the same body plus envelope).
    assert result.skipped == [(132, "01")]
    assert result.added == ["action_2"]
    assert list(document["controls"]) == ["action_1", "power", "action_2"]
    assert document["controls"]["action_2"]["command"] == 134
    assert document["controls"]["action_2"]["body_hex"] == "02"
    assert document["controls"]["action_2"]["verified"] is False


def test_merge_into_a_profile_without_controls_creates_the_block() -> None:
    result = draft.merge_controls(BARE_PROFILE, LEARNED)
    document = yaml.safe_load(result.profile_yaml)
    assert list(document["sensors"]) == ["custom"]
    assert result.added == ["action_1", "action_2"]
    # The hand-written tail comment is still there.
    assert "# controls: 以后手写" in result.profile_yaml
    assert "extends" not in document


def test_an_empty_learning_session_leaves_the_profile_alone() -> None:
    result = draft.merge_controls(PROFILE, [])
    assert result.profile_yaml == PROFILE
    assert result.added == []
    assert result.skipped == []


def test_invalid_yaml_is_reported_and_not_touched() -> None:
    result = draft.merge_controls("controls: [unclosed", LEARNED)
    assert result.profile_yaml == "controls: [unclosed"
    assert result.warnings
    assert "not valid YAML" in result.warnings[0]


def test_merge_controls_into_profile_returns_text() -> None:
    merged = draft.merge_controls_into_profile(PROFILE, LEARNED)
    assert isinstance(merged, str)
    assert "action_2" in merged


# -- the CLI subcommand --------------------------------------------------


def _write_bundle(tmp_path: Path) -> Path:
    target = tmp_path / "bundle.json"
    target.write_text(json.dumps(BUNDLE), encoding="utf-8")
    return target


def _draft_args(tmp_path: Path, bundle: Path, profile: Path) -> Any:
    return cli.build_parser().parse_args(
        [
            "draft",
            str(bundle),
            "--out",
            str(tmp_path / "out"),
            "--merge",
            str(profile),
        ]
    )


def test_the_cli_merges_into_an_existing_profile(
    tmp_path: Path, capsys: Any
) -> None:
    bundle = _write_bundle(tmp_path)
    profile = tmp_path / "profiles" / "RYYJ-R8229.yaml"
    profile.parent.mkdir()
    profile.write_text(PROFILE, encoding="utf-8")

    assert cli.cmd_draft(None, _draft_args(tmp_path, bundle, profile)) == 0
    output = capsys.readouterr().out
    assert "kept    : 2 条已有 controls" in output
    assert "added   : 1 条（action_2）" in output
    assert "skipped : 1 条重复（132:01）" in output

    document = yaml.safe_load(profile.read_text(encoding="utf-8"))
    assert list(document["controls"]) == ["action_1", "power", "action_2"]
    # No stray fixture: merging only touches the profile.
    assert not (tmp_path / "out").exists()


def test_a_missing_profile_is_created_instead(tmp_path: Path, capsys: Any) -> None:
    bundle = _write_bundle(tmp_path)
    profile = tmp_path / "profiles" / "RYYJ-R8229.yaml"

    assert cli.cmd_draft(None, _draft_args(tmp_path, bundle, profile)) == 0
    assert profile.is_file()
    document = yaml.safe_load(profile.read_text(encoding="utf-8"))
    assert document["extends"] == "python:hood"
    assert list(document["controls"]) == ["action_1", "action_2"]
    # A fresh config gets its fixture next to it.
    fixture = tmp_path / "profiles" / "RYYJ-R8229.json"
    assert fixture.is_file()
    assert json.loads(fixture.read_text(encoding="utf-8"))["source"] == "bundle"
    assert "新建" in capsys.readouterr().out


def test_a_capture_without_commands_reports_nothing_to_merge(
    tmp_path: Path, capsys: Any
) -> None:
    bundle = tmp_path / "bundle.json"
    bundle.write_text(
        json.dumps({"schema": "roki-bundle/1", "device": {"dc": "RYYJ", "dt": "R8229"},
                    "properties": {}, "frames": [], "observations": []}),
        encoding="utf-8",
    )
    profile = tmp_path / "profiles" / "RYYJ-R8229.yaml"
    profile.parent.mkdir()
    profile.write_text(PROFILE, encoding="utf-8")

    assert cli.cmd_draft(None, _draft_args(tmp_path, bundle, profile)) == 0
    output = capsys.readouterr().out
    assert "nothing to merge" in output
    assert profile.read_text(encoding="utf-8") == PROFILE
