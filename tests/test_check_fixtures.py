"""Tests for tools/check_fixtures.py, the CI fixture-coverage guard."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

TOOL_PATH = (
    Path(__file__).resolve().parents[1] / "tools" / "check_fixtures.py"
)
PROFILES = "custom_components/robam_roki/profiles"
FIXTURES = "tests/fixtures/devices"


def _load_tool() -> Any:
    name = "roki_check_fixtures_test"
    spec = importlib.util.spec_from_file_location(name, TOOL_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


check = _load_tool()


def _write_profile(
    root: Path,
    name: str,
    dc: str,
    dt: str | None = None,
    *,
    in_shared: bool = False,
) -> str:
    directory = root / PROFILES
    relative = PROFILES
    if in_shared:
        directory = directory / "_shared"
        relative = f"{PROFILES}/_shared"
    directory.mkdir(parents=True, exist_ok=True)
    lines = ["schema: roki-profile/1", "match:", f"  dc: {dc}"]
    if dt is not None:
        lines.append(f"  dt: {dt}")
    (directory / name).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f"{relative}/{name}"


def _write_fixture(root: Path, dc: str, dt: str | None = None) -> None:
    directory = root / FIXTURES
    directory.mkdir(parents=True, exist_ok=True)
    name = f"{dc}-{dt}.json" if dt else f"{dc}.json"
    (directory / name).write_text(
        '{"schema": "roki-fixture/1"}', encoding="utf-8"
    )


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


# -- the pure check ------------------------------------------------------


def test_changed_profile_without_a_fixture_fails(tmp_path: Path) -> None:
    changed = [_write_profile(tmp_path, "new.yaml", "RYYJ", "R8229")]
    result = check.check_paths(changed, tmp_path)
    assert result.ok is False
    assert any("RYYJ-R8229.json" in entry for entry in result.missing)


def test_changed_profile_with_a_fixture_passes(tmp_path: Path) -> None:
    changed = [_write_profile(tmp_path, "new.yaml", "RYYJ", "R8229")]
    _write_fixture(tmp_path, "RYYJ", "R8229")
    result = check.check_paths(changed, tmp_path)
    assert result.ok is True
    assert result.missing == []
    assert any("dc=RYYJ" in entry for entry in result.covered)


def test_untouched_profile_without_a_fixture_is_only_a_warning(
    tmp_path: Path,
) -> None:
    _write_profile(tmp_path, "old.yaml", "RCOF", "X1")
    result = check.check_paths(["README.md"], tmp_path)
    assert result.ok is True
    assert result.missing == []
    assert any("dc=RCOF" in entry for entry in result.uncovered)


def test_shared_fragments_are_skipped(tmp_path: Path) -> None:
    changed = [
        _write_profile(tmp_path, "base.yaml", "RYYJ", in_shared=True)
    ]
    result = check.check_paths(changed, tmp_path)
    assert result.ok is True
    assert result.skipped


def test_unparsable_profile_is_skipped(tmp_path: Path) -> None:
    directory = tmp_path / PROFILES
    directory.mkdir(parents=True)
    (directory / "broken.yaml").write_text(
        "schema: [unclosed", encoding="utf-8"
    )
    result = check.check_paths([f"{PROFILES}/broken.yaml"], tmp_path)
    assert result.ok is True
    assert result.skipped


def test_non_profile_changes_are_ignored(tmp_path: Path) -> None:
    result = check.check_paths(
        ["custom_components/robam_roki/draft.py", "tests/test_x.py"], tmp_path
    )
    assert result.ok is True
    assert result.changed_profiles == 0


def test_dt_list_requires_a_fixture_per_model(tmp_path: Path) -> None:
    directory = tmp_path / PROFILES
    directory.mkdir(parents=True)
    (directory / "multi.yaml").write_text(
        "schema: roki-profile/1\nmatch:\n  dc: RZZZ\n  dt: [M1, M2]\n",
        encoding="utf-8",
    )
    _write_fixture(tmp_path, "RZZZ", "M1")
    result = check.check_paths([f"{PROFILES}/multi.yaml"], tmp_path)
    assert result.ok is False
    assert any("RZZZ-M2.json" in entry for entry in result.missing)


# -- the CLI -------------------------------------------------------------


def test_main_returns_two_when_the_diff_cannot_be_read() -> None:
    # An unknown revision makes git fail; the tool must distinguish that from
    # a fixture problem (exit 1) and report exit 2.
    rc = check.main(["--base", "definitely-not-a-revision", "--head", "HEAD"])
    assert rc == 2


def test_main_end_to_end_in_a_temporary_repository(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Tester")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD").strip()

    _write_profile(repo, "RYYJ-R8229.yaml", "RYYJ", "R8229")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "profile")
    assert check.main(["--repo", str(repo), "--base", base, "--head", "HEAD"]) == 1

    _write_fixture(repo, "RYYJ", "R8229")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fixture")
    assert check.main(["--repo", str(repo), "--base", base, "--head", "HEAD"]) == 0
