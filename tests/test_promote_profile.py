"""Tests for tools/promote_profile.py (export .md -> repository files).

The sample export is produced by the real export module, so this module also
proves the writer (robam_roki.export) and the reader (the tool) agree on the
marker framing.
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
TOOL_PATH = REPO / "tools" / "promote_profile.py"

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
    "properties": {"workStatus": 1},
    "expect": {"work_status": "开机"},
}


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


export = _load_module("roki_export_promote_data", COMPONENT / "export.py")
promote = _load_module("roki_promote_tool", TOOL_PATH)


def _markdown(
    profile_yaml: str = PROFILE_YAML,
    fixture: dict[str, Any] | None = None,
) -> str:
    return export.build_export_markdown(
        device_name="厨房油烟机",
        dc="RYYJ",
        dt="R8229",
        transport="MQTT",
        extends="python:hood",
        profile_yaml=profile_yaml,
        fixture=fixture if fixture is not None else dict(FIXTURE),
        fixture_local_path="/config/robam_roki/fixtures/RYYJ-R8229.json",
        controls={},
        generated_at=datetime(2026, 1, 2, 3, 4),
        version="0.5.0",
    )


def _write_export(tmp_path: Path, **kwargs: Any) -> Path:
    path = tmp_path / "export.md"
    path.write_text(_markdown(**kwargs), encoding="utf-8")
    return path


def _tamper(markdown: str, marker: str, replacement: str) -> str:
    """Replace the content of one fenced marker block."""
    start = markdown.index(marker) + len(marker)
    end = markdown.index("<!--", start)
    block = markdown[start:end]
    fence_start = block.index("\n")
    fence_end = block.rindex("\n")
    return markdown[:start] + block[:fence_start] + "\n" + replacement + "\n" + markdown[start + fence_end :]


# -- planning ------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return tmp_path / "repo"


def test_plan_resolves_the_documented_paths(tmp_path: Path, repo: Path) -> None:
    promotion = promote.plan(_write_export(tmp_path), repo)
    assert promotion.profile_target == (
        repo / "custom_components" / "robam_roki" / "profiles" / "ryyj-r8229.yaml"
    )
    assert promotion.fixture_target == (
        repo / "tests" / "fixtures" / "devices" / "RYYJ-R8229.json"
    )
    assert promotion.profile_name == "ryyj-r8229"
    assert promotion.dc == "RYYJ"
    assert promotion.dt == "R8229"
    assert promotion.control_count == 1


def test_a_category_wide_match_uses_the_dc_alone(tmp_path: Path, repo: Path) -> None:
    profile = """schema: roki-profile/1
match:
  dc: RCOF
"""
    fixture = {
        "schema": "roki-fixture/1",
        "source": "auto",
        "match": {"dc": "RCOF"},
        "properties": {},
    }
    promotion = promote.plan(
        _write_export(tmp_path, profile_yaml=profile, fixture=fixture), repo
    )
    assert promotion.profile_target.name == "rcof.yaml"
    assert promotion.fixture_target.name == "RCOF.json"
    assert promotion.dt == ""


def test_a_dt_list_is_treated_as_category_wide(tmp_path: Path, repo: Path) -> None:
    profile = """schema: roki-profile/1
match:
  dc: RZQL
  dt: [C98A, C98B]
"""
    assert promote.profile_name_for(yaml.safe_load(profile)) == "rzql"


def test_the_name_override_wins(tmp_path: Path, repo: Path) -> None:
    promotion = promote.plan(_write_export(tmp_path), repo, "my-oven")
    assert promotion.profile_target.name == "my-oven.yaml"
    # The fixture name follows the fixture match, not the override.
    assert promotion.fixture_target.name == "RYYJ-R8229.json"


# -- writing -------------------------------------------------------------


def test_apply_writes_both_files(tmp_path: Path, repo: Path) -> None:
    promotion = promote.plan(_write_export(tmp_path), repo)
    promote.apply(promotion)
    assert promotion.profile_target.is_file()
    assert promotion.fixture_target.is_file()

    document = yaml.safe_load(promotion.profile_target.read_text(encoding="utf-8"))
    assert document["match"] == {"dc": "RYYJ", "dt": "R8229"}
    assert document["controls"]["action_1"]["name"] == "开机"

    fixture = json.loads(promotion.fixture_target.read_text(encoding="utf-8"))
    # The fixture records who captured it: this one is real-device evidence.
    assert fixture["source"] == "user"
    assert fixture["properties"] == {"workStatus": 1}


def test_a_second_promotion_overwrites_the_previous_files(tmp_path: Path, repo: Path) -> None:
    promotion = promote.plan(_write_export(tmp_path), repo)
    promote.apply(promotion)
    changed = PROFILE_YAML.replace("body_hex: \"01\"", "body_hex: \"02\"")
    again = promote.plan(_write_export(tmp_path, profile_yaml=changed), repo)
    promote.apply(again, force=True)
    assert "02" in again.profile_target.read_text(encoding="utf-8")


def test_a_user_fixture_is_not_overwritten_without_force(
    tmp_path: Path, repo: Path
) -> None:
    promotion = promote.plan(_write_export(tmp_path), repo)
    promote.apply(promotion)
    original = promotion.fixture_target.read_text(encoding="utf-8")

    changed = PROFILE_YAML.replace('body_hex: "01"', 'body_hex: "02"')
    again = promote.plan(_write_export(tmp_path, profile_yaml=changed), repo)
    with pytest.raises(promote.PromotionError) as excinfo:
        promote.apply(again)

    assert "source=user" in str(excinfo.value)
    # Nothing was written: the refusal happens before the first file.
    assert promotion.fixture_target.read_text(encoding="utf-8") == original
    assert "01" in again.profile_target.read_text(encoding="utf-8")


def test_force_overwrites_a_user_fixture(tmp_path: Path, repo: Path) -> None:
    promotion = promote.plan(_write_export(tmp_path), repo)
    promote.apply(promotion)

    changed = PROFILE_YAML.replace('body_hex: "01"', 'body_hex: "02"')
    again = promote.plan(_write_export(tmp_path, profile_yaml=changed), repo)
    promote.apply(again, force=True)

    assert "02" in again.profile_target.read_text(encoding="utf-8")

def test_dry_run_writes_nothing_and_prints_the_diff(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    promotion = promote.plan(_write_export(tmp_path), repo)
    promote.apply(promotion, dry_run=True)
    assert not promotion.profile_target.exists()
    assert not promotion.fixture_target.exists()
    output = capsys.readouterr().out
    assert "（dry-run）将写入" in output
    assert "ryyj-r8229.yaml" in output
    assert "RYYJ-R8229.json" in output
    assert "+schema: roki-profile/1" in output
    assert '"source": "user"' in output


def test_dry_run_shows_a_diff_against_an_existing_file(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = repo / "custom_components" / "robam_roki" / "profiles"
    target.mkdir(parents=True)
    (target / "ryyj-r8229.yaml").write_text("old: profile\n", encoding="utf-8")
    promote.apply(promote.plan(_write_export(tmp_path), repo), dry_run=True)
    output = capsys.readouterr().out
    assert "-old: profile" in output
    assert "+schema: roki-profile/1" in output


def test_the_follow_up_lists_the_gates(tmp_path: Path, repo: Path) -> None:
    promotion = promote.plan(_write_export(tmp_path), repo)
    lines = promote.follow_up(promotion)
    text = "\n".join(lines)
    assert "bash init.sh" in text
    assert "check_fixtures.py --base HEAD~1 --head HEAD" in text
    assert "隐私复核" in text
    assert "| RYYJ | R8229 | 1 个 controls" in text


# -- validation ----------------------------------------------------------


BAD_PROFILES = {
    "wrong schema": (
        "schema: roki-profile/2\nmatch:\n  dc: RYYJ\n",
        "schema must be",
    ),
    "no match": ("schema: roki-profile/1\n", "match mapping is required"),
    "no dc": (
        "schema: roki-profile/1\nmatch:\n  dt: R8229\n",
        "match.dc is required",
    ),
    "bad kind": (
        (
            "schema: roki-profile/1\nmatch:\n  dc: RYYJ\ncontrols:\n"
            "  x:\n    kind: write\n"
        ),
        "is not one of",
    ),
    "mqtt without command": (
        (
            "schema: roki-profile/1\nmatch:\n  dc: RYYJ\ncontrols:\n"
            "  x:\n    kind: mqtt\n    body_hex: \"01\"\n"
        ),
        "command: an integer 0-255 is required",
    ),
    "odd body": (
        (
            "schema: roki-profile/1\nmatch:\n  dc: RYYJ\ncontrols:\n"
            "  x:\n    kind: mqtt\n    command: 132\n    body_hex: \"abc\"\n"
        ),
        "even-length hex string",
    ),
    "invoke without function": (
        (
            "schema: roki-profile/1\nmatch:\n  dc: RYYJ\ncontrols:\n"
            "  x:\n    kind: invoke\n"
        ),
        "function: required",
    ),
    "property without off": (
        (
            "schema: roki-profile/1\nmatch:\n  dc: RYYJ\ncontrols:\n"
            "  x:\n    kind: property\n    on:\n      a: 1\n"
        ),
        "off: required",
    ),
}


@pytest.mark.parametrize("profile_yaml,expected", list(BAD_PROFILES.values()))
def test_an_invalid_profile_is_refused(
    tmp_path: Path,
    repo: Path,
    profile_yaml: str,
    expected: str,
) -> None:
    path = _write_export(tmp_path, profile_yaml=profile_yaml)
    with pytest.raises(promote.PromotionError) as err:
        promote.plan(path, repo)
    assert expected in str(err.value)


def test_a_fixture_for_another_device_is_refused(tmp_path: Path, repo: Path) -> None:
    fixture = dict(FIXTURE)
    fixture["match"] = {"dc": "RZQL", "dt": "C98A"}
    with pytest.raises(promote.PromotionError) as err:
        promote.plan(_write_export(tmp_path, fixture=fixture), repo)
    assert "fixture matches dc RZQL" in str(err.value)


def test_a_missing_marker_block_is_refused(tmp_path: Path, repo: Path) -> None:
    path = tmp_path / "plain.md"
    path.write_text("# just prose\n", encoding="utf-8")
    with pytest.raises(promote.PromotionError) as err:
        promote.plan(path, repo)
    assert "no profile block found" in str(err.value)

    markdown = _markdown()
    without_fixture = markdown.replace(
        export.FIXTURE_MARKER_START, "<!-- removed -->"
    )
    path.write_text(without_fixture, encoding="utf-8")
    with pytest.raises(promote.PromotionError) as err:
        promote.plan(path, repo)
    assert "no fixture block found" in str(err.value)


def test_a_broken_fixture_block_is_refused(tmp_path: Path, repo: Path) -> None:
    markdown = _tamper(_markdown(), export.FIXTURE_MARKER_START, "not json")
    path = tmp_path / "export.md"
    path.write_text(markdown, encoding="utf-8")
    with pytest.raises(promote.PromotionError) as err:
        promote.plan(path, repo)
    assert "not valid JSON" in str(err.value)


# -- the CLI -------------------------------------------------------------


def test_the_cli_writes_into_the_given_repo_root(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write_export(tmp_path)
    assert promote.main([str(path), "--repo-root", str(repo)]) == 0
    assert (repo / "custom_components" / "robam_roki" / "profiles" / "ryyj-r8229.yaml").is_file()
    output = capsys.readouterr().out
    assert "写入：" in output
    assert "bash init.sh" in output


def test_the_cli_reports_an_error_with_exit_code_2(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write_export(tmp_path, profile_yaml="schema: wrong")
    assert promote.main([str(path), "--repo-root", str(repo)]) == 2
    assert "error:" in capsys.readouterr().err


def test_the_cli_reports_a_missing_file(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        promote.main([str(tmp_path / "nope.md"), "--repo-root", str(repo)]) == 2
    )
    assert "cannot read" in capsys.readouterr().err