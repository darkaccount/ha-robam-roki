#!/usr/bin/env python3
"""CI guard: a changed built-in profile must come with a device fixture.

Usage:
    python tools/check_fixtures.py --base HEAD~1 --head HEAD

Rules:
  * every changed file under custom_components/robam_roki/profiles/ that
    matches devices (it has match.dc) must have a fixture under
    tests/fixtures/devices/; a missing fixture exits with code 1;
  * profiles without a fixture that were not touched in this diff are printed
    as an "uncovered" warning only (they never fail the build);
  * _shared/ fragments and unparsable YAML are skipped, because they do not
    describe a device themselves.

The fixtures are the regression assets for real-device payloads, so adding a
profile without one would leave its behaviour untested.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
PROFILES_PREFIX = "custom_components/robam_roki/profiles"
FIXTURES_PREFIX = "tests/fixtures/devices"


class CheckError(Exception):
    """The diff could not be read."""


@dataclass
class CheckResult:
    """Everything the check found, split by how it should be reported."""

    missing: list[str] = field(default_factory=list)
    uncovered: list[str] = field(default_factory=list)
    covered: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    changed_profiles: int = 0

    @property
    def ok(self) -> bool:
        return not self.missing


def _targets(path: Path) -> list[tuple[str, str]]:
    """The (dc, dt) pairs a profile file matches, or [] when it is a fragment."""
    if path.parent.name == "_shared":
        return []
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return []
    if not isinstance(document, dict):
        return []
    match = document.get("match")
    if not isinstance(match, dict):
        return []
    dc = str(match.get("dc") or "").strip()
    if not dc:
        return []
    raw_dt = match.get("dt")
    if isinstance(raw_dt, str):
        dt = raw_dt.strip()
        return [(dc, dt)] if dt else [(dc, "")]
    if isinstance(raw_dt, (list, tuple)):
        targets = [(dc, str(item).strip()) for item in raw_dt if str(item).strip()]
        return targets or [(dc, "")]
    return [(dc, "")]


def fixture_name(dc: str, dt: str) -> str:
    return f"{dc}-{dt}.json" if dt else f"{dc}.json"


def _normalise(rel: str) -> str:
    return rel.replace("\\", "/").strip()


def check_paths(changed: list[str], repo_root: Path = REPO_ROOT) -> CheckResult:
    """Check one list of changed repository paths."""
    result = CheckResult()
    profiles_dir = repo_root / PROFILES_PREFIX
    fixtures_dir = repo_root / FIXTURES_PREFIX

    changed_profiles: list[str] = []
    for raw in changed:
        rel = _normalise(raw)
        if not rel.startswith(PROFILES_PREFIX + "/"):
            continue
        if not rel.endswith((".yaml", ".yml")):
            continue
        changed_profiles.append(rel)

    result.changed_profiles = len(changed_profiles)
    for rel in changed_profiles:
        path = repo_root / rel
        if not path.is_file():
            result.missing.append(f"{rel} (file not found)")
            continue
        targets = _targets(path)
        if not targets:
            result.skipped.append(rel)
            continue
        for dc, dt in targets:
            name = fixture_name(dc, dt)
            if not (fixtures_dir / name).is_file():
                result.missing.append(f"{rel} -> missing {FIXTURES_PREFIX}/{name}")

    seen: set[str] = set()
    for path in sorted(profiles_dir.rglob("*.yaml")):
        rel = str(path.relative_to(repo_root)).replace("\\", "/")
        for dc, dt in _targets(path):
            label = f"dc={dc} dt={dt or 'any'}"
            if label in seen:
                continue
            seen.add(label)
            name = fixture_name(dc, dt)
            if (fixtures_dir / name).is_file():
                result.covered.append(f"{label} ({name})")
            else:
                result.uncovered.append(f"{label} (from {rel})")
    return result


def changed_paths(base: str, head: str, repo_root: Path = REPO_ROOT) -> list[str]:
    """Paths added or modified between two revisions, via git."""
    command = [
        "git",
        "diff",
        "--name-only",
        "--diff-filter=AM",
        f"{base}..{head}",
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as err:  # pragma: no cover - git missing
        raise CheckError(f"cannot run git: {err}") from err
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"git exited with {completed.returncode}"
        raise CheckError(f"cannot diff {base}..{head}: {detail}")
    return [line for line in completed.stdout.splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fail when a changed built-in profile has no device fixture; "
            "uncovered profiles are reported as warnings only."
        )
    )
    parser.add_argument("--base", default="HEAD~1", help="base revision")
    parser.add_argument("--head", default="HEAD", help="head revision")
    parser.add_argument(
        "--repo",
        default=str(REPO_ROOT),
        help="repository root (defaults to this checkout)",
    )
    args = parser.parse_args(argv)
    repo_root = Path(args.repo).resolve()

    try:
        changed = changed_paths(args.base, args.head, repo_root)
    except CheckError as err:
        print(f"check_fixtures: {err}", file=sys.stderr)
        return 2

    result = check_paths(changed, repo_root)
    print(f"changed profiles: {result.changed_profiles}")
    for entry in result.covered:
        print(f"covered: {entry}")
    for entry in result.uncovered:
        print(f"uncovered (warning): {entry}")
    for entry in result.skipped:
        print(f"skipped (fragment or unreadable): {entry}")
    for entry in result.missing:
        print(f"MISSING FIXTURE: {entry}", file=sys.stderr)

    if not result.ok:
        print(
            "check_fixtures: add the fixture(s) above under "
            f"{FIXTURES_PREFIX}/",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
