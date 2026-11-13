"""Promote round trip: export .md -> repository files -> CI gate -> refusal.

This is the maintainer chain end to end, in a throwaway repository tree:
tools/promote_profile.py writes the profile and the fixture,
tools/check_fixtures.py reports the new profile as covered, the fixture carries
source=user, and promoting the same export again is refused because it would
replace real-device evidence.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
COMPONENT = REPO / "custom_components" / "robam_roki"
PROFILE_REL = "custom_components/robam_roki/profiles/ryyj-r8229.yaml"


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


export = _load_module("roki_export_roundtrip_test", COMPONENT / "export.py")
profiles = _load_module("roki_profiles_roundtrip_test", COMPONENT / "profiles.py")
promote = _load_module(
    "roki_promote_roundtrip_test", REPO / "tools" / "promote_profile.py"
)
checker = _load_module(
    "roki_check_roundtrip_test", REPO / "tools" / "check_fixtures.py"
)

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
    "properties": {"workStatus": 1, "level": 2},
    "expect": {"work_status": "开机"},
}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway repository root the promotion writes into."""
    return tmp_path / "repo"


def _export_md(tmp_path: Path) -> Path:
    text = export.build_export_markdown(
        device_name="厨房油烟机",
        dc="RYYJ",
        dt="R8229",
        transport="MQTT",
        extends="python:hood",
        profile_yaml=PROFILE_YAML,
        fixture=dict(FIXTURE),
        fixture_local_path="/config/robam_roki/fixtures/RYYJ-R8229.json",
        controls={
            "action_1": {
                "name": "开机",
                "kind": "mqtt",
                "command": 132,
                "body_hex": "01",
                "verified": False,
            }
        },
        generated_at=datetime(2026, 11, 13, 2, 45),
        version="0.5.0",
    )
    path = tmp_path / "export.md"
    path.write_text(text, encoding="utf-8")
    return path


def _promote(tmp_path: Path, repo: Path) -> Any:
    promotion = promote.plan(_export_md(tmp_path), repo)
    promote.apply(promotion)
    return promotion


def test_promote_writes_a_lower_case_profile_and_a_user_fixture(
    tmp_path: Path, repo: Path
) -> None:
    promotion = _promote(tmp_path, repo)
    # Built-in profile file names are lower-case ...
    assert promotion.profile_target.relative_to(repo).as_posix() == PROFILE_REL
    # ... while the fixture keeps the device spelling the gate expects.
    assert promotion.fixture_target.name == "RYYJ-R8229.json"
    assert promotion.fixture_target.is_file()

    promoted_yaml = promotion.profile_target.read_text(encoding="utf-8")
    assert yaml.safe_load(promoted_yaml) == yaml.safe_load(PROFILE_YAML)
    assert promoted_yaml.endswith("\n")

    fixture = json.loads(promotion.fixture_target.read_text(encoding="utf-8"))
    assert fixture["source"] == "user"
    assert fixture["match"] == {"dc": "RYYJ", "dt": "R8229"}


def test_the_promoted_profile_loads_and_exposes_its_controls(
    tmp_path: Path, repo: Path
) -> None:
    promotion = _promote(tmp_path, repo)
    profile = profiles.load_profile_file(
        promotion.profile_target, profiles.SOURCE_USER
    )
    assert profile is not None
    assert profile.dc == "RYYJ"
    matched = profiles.match_profile(
        {"dc": "RYYJ", "dt": "R8229"}, [profile]
    )
    assert matched is not None
    assert matched.controls["action_1"]["body_hex"] == "01"


def test_check_fixtures_reports_the_new_profile_as_covered(
    tmp_path: Path, repo: Path
) -> None:
    _promote(tmp_path, repo)
    result = checker.check_paths([PROFILE_REL], repo)
    assert result.missing == []
    assert result.uncovered == []
    assert result.changed_profiles == 1
    assert any(
        "dc=RYYJ dt=R8229" in entry for entry in result.covered
    ), result.covered


def test_the_gate_would_catch_a_missing_fixture(
    tmp_path: Path, repo: Path
) -> None:
    promotion = _promote(tmp_path, repo)
    promotion.fixture_target.unlink()
    result = checker.check_paths([PROFILE_REL], repo)
    assert result.missing, "the CI gate must fail without the fixture"
    assert any("RYYJ-R8229.json" in entry for entry in result.missing)


def test_a_second_promotion_is_refused_as_real_device_evidence(
    tmp_path: Path, repo: Path
) -> None:
    md = _export_md(tmp_path)
    promote.apply(promote.plan(md, repo))

    with pytest.raises(promote.PromotionError) as err:
        promote.apply(promote.plan(md, repo))
    message = str(err.value)
    assert "source=user" in message
    assert "--force" in message

    # A deliberate replacement is still possible, but it has to be explicit.
    promote.apply(promote.plan(md, repo), force=True)
