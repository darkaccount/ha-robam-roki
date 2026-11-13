"""The export document: five sections, two marker blocks, no raw secrets.

The export module is loaded straight from its file, which also proves it has
no Home Assistant dependency (it only borrows the shared redaction helpers).
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "robam_roki"
    / "export.py"
)


def _load_export_module() -> Any:
    name = "roki_export_test"
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


export = _load_export_module()

GENERATED_AT = datetime(2026, 1, 2, 3, 4)

PROFILE_YAML = """schema: roki-profile/1
extends: python:hood
match:
  dc: RYYJ
  dt: R8229
controls:
  action_1:
    name: 开机
    kind: mqtt
    command: 132
    body_hex: "01"
    verified: false
"""

FIXTURE = {
    "schema": "roki-fixture/1",
    "source": "auto",
    "match": {"dc": "RYYJ", "dt": "R8229"},
    "properties": {
        "workStatus": 1,
        "phone": "13800000000",
        "appGuid": "SOME-APP-GUID",
        "accessToken": "SOME-TOKEN",
    },
    "expect": {"work_status": "开机"},
}

LEARNED = [
    {
        "frames": [{"command": 132, "body_hex": "01"}],
        "observed": {"workStatus": {"from": 0, "to": 1}},
    }
]

CONTROLS = {
    "action_1": {
        "name": "开机",
        "kind": "mqtt",
        "command": 132,
        "body_hex": "01",
        "verified": False,
    }
}


def _markdown(**overrides: Any) -> str:
    kwargs: dict[str, Any] = {
        "device_name": "厨房油烟机",
        "dc": "RYYJ",
        "dt": "R8229",
        "transport": "MQTT",
        "extends": "python:hood",
        "profile_yaml": PROFILE_YAML,
        "fixture": dict(FIXTURE),
        "fixture_local_path": "/config/robam_roki/fixtures/RYYJ-R8229.json",
        "controls": dict(CONTROLS),
        "learned": LEARNED,
        "generated_at": GENERATED_AT,
        "version": "0.5.0",
    }
    kwargs.update(overrides)
    return export.build_export_markdown(**kwargs)


# -- structure -----------------------------------------------------------


def test_the_document_has_exactly_the_five_documented_sections() -> None:
    markdown = _markdown()
    for heading in (
        "## 1. 设备与匹配",
        "## 2. 验证清单",
        "## 3. profile YAML",
        "## 4. fixture",
        "## 5. 如何贡献",
    ):
        assert heading in markdown, heading
    assert "## 6." not in markdown
    assert "## 7." not in markdown


def test_the_redaction_promise_is_the_first_thing_after_the_title() -> None:
    lines = _markdown().splitlines()
    assert lines[0].startswith("# ROBAM ROKI profile export")
    assert lines[2] == export.REDACTION_NOTE
    assert "生成时间：2026-01-02 03:04" in "\n".join(lines)
    assert "集成版本：0.5.0" in "\n".join(lines)


def test_the_profile_yaml_sits_between_its_markers() -> None:
    markdown = _markdown()
    assert export.PROFILE_MARKER_START in markdown
    assert export.PROFILE_MARKER_END in markdown
    block = export.find_marked_block(
        markdown, export.PROFILE_MARKER_START, export.PROFILE_MARKER_END
    )
    assert block == PROFILE_YAML.strip()


def test_the_fixture_sits_between_its_markers_as_json() -> None:
    markdown = _markdown()
    block = export.find_marked_block(
        markdown, export.FIXTURE_MARKER_START, export.FIXTURE_MARKER_END
    )
    assert block is not None
    document = json.loads(block)
    assert document["schema"] == "roki-fixture/1"
    assert document["match"] == {"dc": "RYYJ", "dt": "R8229"}


def test_find_marked_block_returns_none_when_a_marker_is_missing() -> None:
    assert export.find_marked_block("no markers here", "<!-- a -->", "<!-- /a -->") is None
    assert (
        export.find_marked_block("<!-- a -->\nbody", "<!-- a -->", "<!-- /a -->")
        is None
    )


def test_the_device_section_carries_name_dc_dt_transport_and_extends() -> None:
    markdown = _markdown()
    for value in ("厨房油烟机", "RYYJ", "R8229", "MQTT", "python:hood"):
        assert value in markdown, value


def test_a_pipe_in_a_cell_cannot_break_the_table() -> None:
    markdown = _markdown(device_name="厨房|油烟机")
    assert "厨房\\|油烟机" in markdown


# -- the verification checklist -----------------------------------------


def test_the_checklist_row_has_all_six_columns() -> None:
    markdown = _markdown()
    assert (
        "| key | 名称 | 命令 | body | 观察到的变化 | verified |"
    ) in markdown
    assert "| action_1 | 开机 | 132 | 01 | workStatus 0 → 1 | false |" in markdown


def test_a_control_without_learning_evidence_shows_a_dash() -> None:
    markdown = _markdown(learned=None)
    assert "| action_1 | 开机 | 132 | 01 | — | false |" in markdown


def test_an_empty_controls_section_says_so() -> None:
    markdown = _markdown(controls={}, learned=None)
    assert "（这条 profile 没有 controls 条目。）" in markdown


def test_a_control_without_a_command_renders_dashes() -> None:
    markdown = _markdown(
        controls={"beep": {"kind": "invoke", "function": "137", "verified": True}},
        learned=None,
    )
    assert "| beep | beep | — | — | — | true |" in markdown


# -- privacy -------------------------------------------------------------


def test_the_fixture_secrets_are_redacted_in_the_document() -> None:
    markdown = _markdown()
    # Value patterns (the phone number) and key names (appGuid/accessToken).
    assert "13800000000" not in markdown
    assert "SOME-APP-GUID" not in markdown
    assert "SOME-TOKEN" not in markdown
    block = export.find_marked_block(
        markdown, export.FIXTURE_MARKER_START, export.FIXTURE_MARKER_END
    )
    properties = json.loads(block)["properties"]
    assert properties["phone"] == "**REDACTED**"
    assert properties["appGuid"] == "**REDACTED**"
    assert properties["accessToken"] == "**REDACTED**"
    # Ordinary telemetry stays readable: the fixture is the evidence.
    assert properties["workStatus"] == 1


def test_the_builder_cannot_even_receive_a_guid() -> None:
    # The device GUID is not a parameter, so no future caller can leak it.
    parameters = inspect.signature(export.build_export_markdown).parameters
    assert "guid" not in parameters
    assert "device_guid" not in parameters


# -- paths ---------------------------------------------------------------


def test_the_file_name_follows_the_documented_rule() -> None:
    assert (
        export.export_filename("RYYJ", "R8229", GENERATED_AT)
        == "RYYJ-R8229-20260102-0304.md"
    )


def test_a_hostile_dc_cannot_escape_the_directory() -> None:
    name = export.export_filename("../../etc", "R 8229", GENERATED_AT)
    assert "/" not in name
    assert ".." not in name
    assert name.endswith(".md")


def test_the_repository_paths_match_the_layout() -> None:
    assert (
        export.fixture_repo_path("RYYJ", "R8229")
        == "tests/fixtures/devices/RYYJ-R8229.json"
    )
    assert (
        export.profile_repo_path("RYYJ", "R8229")
        == "custom_components/robam_roki/profiles/RYYJ-R8229.yaml"
    )
    assert (
        export.profile_repo_path("RCOF")
        == "custom_components/robam_roki/profiles/RCOF.yaml"
    )

