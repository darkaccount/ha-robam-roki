"""Repair issues: profile parse failures, profile conflicts, opt-in warning.

The issue registry is stubbed at module level; the profile loader is the real
one, so the tests exercise the same loading path the integration uses.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

COMPONENT = (
    Path(__file__).resolve().parents[1] / "custom_components" / "robam_roki"
)
PACKAGE = "roki_repairs_pkg_test"

DOMAIN = "robam_roki"
ALLOW_OPTION = "allow_unverified_controls"


def _module(name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__path__ = []
    return module


class _FakeRegistry:
    def __init__(self) -> None:
        self.issues: dict[tuple[str, str], Any] = {}
        self.created: list[dict[str, Any]] = []
        self.deleted: list[tuple[str, str]] = []

    def async_create_issue(
        self,
        hass: Any,
        domain: str,
        issue_id: str,
        *,
        translation_key: str | None = None,
        translation_placeholders: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self.created.append(
            {
                "domain": domain,
                "issue_id": issue_id,
                "translation_key": translation_key,
                "placeholders": translation_placeholders,
                **kwargs,
            }
        )
        self.issues[(domain, issue_id)] = types.SimpleNamespace(
            translation_key=translation_key,
            translation_placeholders=translation_placeholders,
        )

    def async_delete_issue(
        self, hass: Any, domain: str, issue_id: str
    ) -> None:
        self.deleted.append((domain, issue_id))
        self.issues.pop((domain, issue_id), None)


def _install_stubs() -> None:
    ha = sys.modules.get("homeassistant") or _module("homeassistant")
    sys.modules["homeassistant"] = ha
    ha.__path__ = getattr(ha, "__path__", [])
    helpers = sys.modules.get("homeassistant.helpers") or _module(
        "homeassistant.helpers"
    )
    sys.modules["homeassistant.helpers"] = helpers
    ha.helpers = helpers
    issue_registry = _module("homeassistant.helpers.issue_registry")

    def async_get(hass: Any) -> Any:
        return hass.issue_registry

    def async_create_issue(
        hass: Any, domain: str, issue_id: str, **kwargs: Any
    ) -> None:
        hass.issue_registry.async_create_issue(
            hass, domain, issue_id, **kwargs
        )

    def async_delete_issue(hass: Any, domain: str, issue_id: str) -> None:
        hass.issue_registry.async_delete_issue(hass, domain, issue_id)

    issue_registry.async_get = async_get
    issue_registry.async_create_issue = async_create_issue
    issue_registry.async_delete_issue = async_delete_issue
    sys.modules["homeassistant.helpers.issue_registry"] = issue_registry


def _load(name: str, relative: str) -> types.ModuleType:
    package = sys.modules.get(PACKAGE)
    if package is None:
        package = _module(PACKAGE)
        package.__path__ = [str(COMPONENT)]
        sys.modules[PACKAGE] = package
    path = COMPONENT / relative
    spec = importlib.util.spec_from_file_location(f"{PACKAGE}.{name}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"{PACKAGE}.{name}"] = module
    spec.loader.exec_module(module)
    return module


_install_stubs()
const = _load("const", "const.py")
profiles = _load("profiles", "profiles.py")
repairs = _load("repairs", "repairs.py")


class _FakeConfig:
    def __init__(self, base: Path) -> None:
        self._base = base

    def path(self, *parts: str) -> str:
        return str(self._base.joinpath(*parts))


class _FakeHass:
    def __init__(self, base: Path) -> None:
        self.config = _FakeConfig(base)
        self.issue_registry = _FakeRegistry()


class _FakeEntry:
    def __init__(self, options: dict[str, Any] | None = None) -> None:
        self.options = dict(options or {})


class _FakeCoordinator:
    def __init__(
        self,
        profiles_list: list[Any],
        devices: list[dict[str, Any]],
        options: dict[str, Any] | None = None,
    ) -> None:
        self.profiles = profiles_list
        self.devices = devices
        self.entry = _FakeEntry(options)


def _user_dir(tmp_path: Path) -> Path:
    path = tmp_path / "config" / "robam_roki" / "profiles"
    path.mkdir(parents=True)
    return path


def _write_yaml(directory: Path, name: str, text: str) -> Path:
    path = directory / name
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


MINIMAL = """
schema: roki-profile/1
match:
  dc: RYYJ
  dt: R8229
"""

DEVICE = {"guid": "R8229000000000000", "name": "油烟机8229", "dc": "RYYJ", "dt": "R8229"}


def _refresh(
    hass: _FakeHass,
    coordinator: _FakeCoordinator,
    user_dir: Path,
) -> None:
    import asyncio

    asyncio.run(
        repairs.async_refresh_issues(
            hass, coordinator, user_dir=user_dir
        )
    )


# -- profile parse failures ---------------------------------------------


def test_a_broken_profile_creates_an_issue_and_a_fix_removes_it(
    tmp_path: Path,
) -> None:
    hass = _FakeHass(tmp_path)
    user_dir = _user_dir(tmp_path)
    broken = _write_yaml(user_dir, "bad.yaml", "schema: [unclosed")
    coordinator = _FakeCoordinator([], [DEVICE])

    _refresh(hass, coordinator, user_dir)
    assert len(hass.issue_registry.created) == 1
    issue = hass.issue_registry.created[0]
    assert issue["domain"] == DOMAIN
    assert issue["translation_key"] == "profile_parse_failed"
    assert issue["issue_id"] == "profile_parse_failed_bad-yaml"
    assert issue["placeholders"]["path"] == str(broken)
    assert "YAML" in issue["placeholders"]["error"]

    # Fixing the file removes the issue on the next refresh.
    broken.write_text(MINIMAL, encoding="utf-8")
    coordinator.profiles = profiles.load_profile_dir(
        user_dir, profiles.SOURCE_USER
    )
    _refresh(hass, coordinator, user_dir)
    assert ("robam_roki", "profile_parse_failed_bad-yaml") in (
        hass.issue_registry.deleted
    )
    assert hass.issue_registry.issues == {}


def test_a_profile_that_fails_validation_is_reported(tmp_path: Path) -> None:
    hass = _FakeHass(tmp_path)
    user_dir = _user_dir(tmp_path)
    _write_yaml(
        user_dir,
        "wrong.yaml",
        "schema: other-schema\nmatch:\n  dc: RYYJ",
    )
    _refresh(hass, _FakeCoordinator([], [DEVICE]), user_dir)
    issue = hass.issue_registry.created[0]
    assert issue["issue_id"] == "profile_parse_failed_wrong-yaml"
    assert "校验" in issue["placeholders"]["error"]


# -- profile conflicts ---------------------------------------------------


def test_equally_specific_profiles_create_a_conflict_issue(
    tmp_path: Path,
) -> None:
    hass = _FakeHass(tmp_path)
    user_dir = _user_dir(tmp_path)
    _write_yaml(user_dir, "a-first.yaml", MINIMAL)
    _write_yaml(user_dir, "b-second.yaml", MINIMAL)
    coordinator = _FakeCoordinator(
        profiles.load_profile_dir(user_dir, profiles.SOURCE_USER),
        [DEVICE],
    )

    _refresh(hass, coordinator, user_dir)
    conflicts = [
        issue
        for issue in hass.issue_registry.created
        if issue["translation_key"] == "profile_conflict"
    ]
    assert len(conflicts) == 1
    issue = conflicts[0]
    assert issue["issue_id"].startswith("profile_conflict_ryyj_")
    assert issue["placeholders"]["dc"] == "RYYJ"
    assert "a-first" in issue["placeholders"]["profiles"]
    assert "b-second" in issue["placeholders"]["profiles"]

    # Removing one profile clears the conflict.
    (user_dir / "b-second.yaml").unlink()
    coordinator.profiles = profiles.load_profile_dir(
        user_dir, profiles.SOURCE_USER
    )
    _refresh(hass, coordinator, user_dir)
    assert ("robam_roki", issue["issue_id"]) in hass.issue_registry.deleted
    assert hass.issue_registry.issues == {}


def test_specific_and_generic_profile_is_not_a_conflict(tmp_path: Path) -> None:
    hass = _FakeHass(tmp_path)
    user_dir = _user_dir(tmp_path)
    _write_yaml(user_dir, "specific.yaml", MINIMAL)
    _write_yaml(
        user_dir,
        "generic.yaml",
        """
        schema: roki-profile/1
        match:
          dc: RYYJ
        """,
    )
    coordinator = _FakeCoordinator(
        profiles.load_profile_dir(user_dir, profiles.SOURCE_USER),
        [DEVICE],
    )
    _refresh(hass, coordinator, user_dir)
    assert [
        issue
        for issue in hass.issue_registry.created
        if issue["translation_key"] == "profile_conflict"
    ] == []


# -- the experimental controls warning ----------------------------------


def test_the_unverified_option_creates_and_removes_the_warning(
    tmp_path: Path,
) -> None:
    hass = _FakeHass(tmp_path)
    user_dir = _user_dir(tmp_path)
    coordinator = _FakeCoordinator([], [DEVICE], {ALLOW_OPTION: True})

    _refresh(hass, coordinator, user_dir)
    issue = hass.issue_registry.created[0]
    assert issue["issue_id"] == "unverified_controls_enabled"
    assert issue["translation_key"] == "unverified_controls_enabled"

    coordinator.entry.options[ALLOW_OPTION] = False
    _refresh(hass, coordinator, user_dir)
    assert (
        "robam_roki",
        "unverified_controls_enabled",
    ) in hass.issue_registry.deleted
    assert hass.issue_registry.issues == {}


# -- idempotency and restarts -------------------------------------------


def test_refreshing_twice_does_not_duplicate_an_issue(tmp_path: Path) -> None:
    hass = _FakeHass(tmp_path)
    user_dir = _user_dir(tmp_path)
    _write_yaml(user_dir, "bad.yaml", "schema: [unclosed")
    coordinator = _FakeCoordinator([], [DEVICE])
    _refresh(hass, coordinator, user_dir)
    _refresh(hass, coordinator, user_dir)
    assert len(hass.issue_registry.created) == 1


def test_an_issue_from_a_previous_run_is_deleted_when_it_no_longer_applies(
    tmp_path: Path,
) -> None:
    hass = _FakeHass(tmp_path)
    user_dir = _user_dir(tmp_path)
    hass.issue_registry.issues[("robam_roki", "profile_parse_failed_old-yaml")] = (
        types.SimpleNamespace(translation_key="profile_parse_failed")
    )
    _refresh(hass, _FakeCoordinator([], [DEVICE]), user_dir)
    assert (
        "robam_roki",
        "profile_parse_failed_old-yaml",
    ) in hass.issue_registry.deleted
