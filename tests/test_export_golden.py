"""Golden lock for the export document.

The export is the hand-off format between a user and a maintainer: the profile
YAML and the fixture travel inside two marker blocks, and
tools/promote_profile.py reads exactly those.  A silent format change would
break every export already sitting in an issue, so this test pins the document
byte for byte with fixed inputs -- the timestamp and the version are injected,
never read from the clock or the manifest.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[1]
COMPONENT = REPO / "custom_components" / "robam_roki"
GOLDEN = REPO / "tests" / "fixtures" / "exports" / "sample-RYYJ-R8229.md"


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


export = _load_module("roki_export_golden_test", COMPONENT / "export.py")
promote = _load_module(
    "roki_promote_golden_test", REPO / "tools" / "promote_profile.py"
)

GENERATED_AT = datetime(2026, 11, 13, 2, 30)
VERSION = "0.5.0"

PROFILE_YAML = """schema: roki-profile/1
extends: python:hood
match:
  dc: RYYJ
  dt: R8229
controls:
  action_1:
    # 名称是按设备变化推测的，建议改成 App 上显示的名字
    name: "开机"
    kind: mqtt
    command: 132
    body_hex: "01"
    verified: false
"""

FIXTURE = {
    "schema": "roki-fixture/1",
    "source": "auto",
    "match": {"dc": "RYYJ", "dt": "R8229"},
    "properties": {"workStatus": 1, "level": 2, "phone": "13800000000"},
    "expect": {"work_status": "开机", "fault_code": None},
}

CONTROLS = {
    "action_1": {
        "name": "开机",
        "kind": "mqtt",
        "command": 132,
        "body_hex": "01",
        "verified": False,
    }
}

LEARNED = [
    {
        "frames": [{"command": 132, "body_hex": "01"}],
        "observed": {"workStatus": {"from": 0, "to": 1}},
    }
]


def build_sample() -> str:
    """The document every export is compared against (fixed inputs only)."""
    return export.build_export_markdown(
        device_name="厨房油烟机",
        dc="RYYJ",
        dt="R8229",
        transport="MQTT",
        extends="python:hood",
        profile_yaml=PROFILE_YAML,
        fixture=dict(FIXTURE),
        fixture_local_path="/config/robam_roki/fixtures/RYYJ-R8229.json",
        controls=dict(CONTROLS),
        learned=LEARNED,
        generated_at=GENERATED_AT,
        version=VERSION,
    )


def test_the_export_matches_the_committed_golden() -> None:
    assert GOLDEN.is_file(), f"missing golden: {GOLDEN}"
    golden = GOLDEN.read_text(encoding="utf-8")
    assert build_sample() == golden


def test_the_golden_keeps_the_five_sections_and_the_redaction_note() -> None:
    golden = GOLDEN.read_text(encoding="utf-8")
    for heading in ("## 1.", "## 2.", "## 3.", "## 4.", "## 5."):
        assert heading in golden, heading
    assert "## 6." not in golden
    assert "已脱敏：不含设备 GUID、手机号、token、App GUID" in golden
    assert golden.count("<!-- roki:profile-yaml -->") == 1
    assert golden.count("<!-- roki:fixture-json -->") == 1


def test_the_golden_is_still_promotable() -> None:
    profile_text, fixture = promote.parse_export(
        GOLDEN.read_text(encoding="utf-8")
    )
    document = yaml.safe_load(profile_text)
    assert document["schema"] == "roki-profile/1"
    assert document["match"] == {"dc": "RYYJ", "dt": "R8229"}
    assert document["controls"]["action_1"]["body_hex"] == "01"
    assert fixture["schema"] == "roki-fixture/1"
    # The phone number reaches the document only as the redaction marker.
    assert fixture["properties"]["phone"] == "**REDACTED**"


def test_the_sample_has_no_secret_left() -> None:
    golden = GOLDEN.read_text(encoding="utf-8")
    assert "13800000000" not in golden
    assert "**REDACTED**" in golden
