"""Repairs issues for profile problems and the experimental controls option.

The integration never raises during setup because of a profile; instead the
problems that a user can fix are surfaced as Home Assistant repair issues:
a user profile that failed to parse, two profiles matching one device equally
well, or the experimental-controls option being on.

The refresh function is idempotent: it recreates exactly the issues that still
apply and deletes the ones that no longer do (a fixed file, a removed profile,
the option switched off), including issues created before a restart.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

from .const import (
    CONF_ALLOW_UNVERIFIED_CONTROLS,
    DEFAULT_ALLOW_UNVERIFIED_CONTROLS,
    DOMAIN,
)
from .profiles import (
    SOURCE_USER,
    _dt_precision,
    load_profile_file,
)

_LOGGER = logging.getLogger(__name__)

ISSUE_PROFILE_PARSE = "profile_parse_failed"
ISSUE_PROFILE_CONFLICT = "profile_conflict"
ISSUE_UNVERIFIED = "unverified_controls_enabled"


def _slug(text: Any) -> str:
    """A short, stable, lower-case fragment usable in an issue_id."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return (cleaned or "item")[:48]


def _parse_issue_id(path: Path) -> str:
    return f"{ISSUE_PROFILE_PARSE}_{_slug(path.name)}"


def _conflict_issue_id(dc: str, profile_ids: list[str]) -> str:
    return f"{ISSUE_PROFILE_CONFLICT}_{_slug(dc)}_{_slug('-'.join(profile_ids))}"


def _registry(hass: Any) -> Any:
    """The issue registry instance (imported lazily for the offline tests)."""
    from homeassistant.helpers import issue_registry as ir

    return ir.async_get(hass)


def _existing_issue_ids(hass: Any) -> set[str]:
    """Issue ids this integration already created (survives restarts)."""
    issues = getattr(_registry(hass), "issues", None) or {}
    ids: set[str] = set()
    for key in issues:
        if isinstance(key, tuple) and len(key) == 2 and key[0] == DOMAIN:
            ids.add(str(key[1]))
        elif isinstance(key, str) and key:
            ids.add(key)
    return ids


def _scan_parse_failures(user_dir: Path) -> list[dict[str, Any]]:
    """Files under the user profile directory that cannot become a profile."""
    directory = Path(user_dir)
    if not directory.is_dir():
        return []
    failures: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.yaml")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as err:
            detail = f"无法读取文件：{err}"
        else:
            try:
                yaml.safe_load(text)
            except yaml.YAMLError as err:
                detail = f"YAML 语法错误：{err}"
            else:
                if load_profile_file(path) is not None:
                    continue
                detail = "profile 校验未通过（详见 Home Assistant 日志）"
        failures.append(
            {
                "issue_id": _parse_issue_id(path),
                "translation_key": ISSUE_PROFILE_PARSE,
                "placeholders": {
                    "path": str(path),
                    "error": detail,
                },
            }
        )
    return failures


def _conflict_groups(
    profiles: list[Any], devices: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Devices matched equally well by more than one profile of one source."""
    groups: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for device in devices:
        dc = str(device.get("dc") or "").strip().upper()
        dt = str(device.get("dt") or "").strip().upper()
        if not dc:
            continue
        candidates: list[tuple[Any, int]] = []
        for profile in profiles:
            if not str(getattr(profile, "dc", "") or "").strip():
                continue
            if str(profile.dc).strip().upper() != dc:
                continue
            precision = _dt_precision(profile, dt)
            if precision < 0:
                continue
            candidates.append((profile, precision))
        if len(candidates) < 2:
            continue
        candidates.sort(
            key=lambda item: (
                0 if getattr(item[0], "source", "") == SOURCE_USER else 1,
                -item[1],
                str(getattr(item[0], "path", None) or item[0].id),
            )
        )
        best, best_precision = candidates[0]
        tied = [
            profile
            for profile, precision in candidates[1:]
            if precision == best_precision
            and profile.source == best.source
        ]
        if not tied:
            continue
        profile_ids = sorted({best.id, *(profile.id for profile in tied)})
        key = (dc, tuple(profile_ids))
        if key in seen:
            continue
        seen.add(key)
        groups.append(
            {
                "issue_id": _conflict_issue_id(dc, profile_ids),
                "translation_key": ISSUE_PROFILE_CONFLICT,
                "placeholders": {
                    "dc": dc,
                    "device": str(
                        device.get("name")
                        or device.get("guid")
                        or device.get("dt")
                        or ""
                    ),
                    "profiles": ", ".join(profile_ids),
                },
            }
        )
    return groups


def _desired_issues(
    profiles: list[Any],
    devices: list[dict[str, Any]],
    user_dir: Path | None,
    allow_unverified: bool,
) -> dict[str, dict[str, Any]]:
    desired: dict[str, dict[str, Any]] = {}
    if user_dir is not None:
        for failure in _scan_parse_failures(Path(user_dir)):
            desired[failure["issue_id"]] = failure
    for group in _conflict_groups(profiles, devices):
        desired[group["issue_id"]] = group
    if allow_unverified:
        desired[ISSUE_UNVERIFIED] = {
            "issue_id": ISSUE_UNVERIFIED,
            "translation_key": ISSUE_UNVERIFIED,
            "placeholders": {},
        }
    return desired


async def async_refresh_issues(
    hass: Any,
    coordinator: Any,
    *,
    user_dir: Path | None = None,
) -> None:
    """Create the issues that apply now and delete the ones that do not."""
    from homeassistant.helpers import issue_registry as ir

    profiles = list(getattr(coordinator, "profiles", None) or [])
    devices = list(getattr(coordinator, "devices", None) or [])
    entry = getattr(coordinator, "entry", None)
    options = getattr(entry, "options", None) or {}
    allow_unverified = bool(
        options.get(
            CONF_ALLOW_UNVERIFIED_CONTROLS,
            DEFAULT_ALLOW_UNVERIFIED_CONTROLS,
        )
    )

    desired = _desired_issues(
        profiles, devices, user_dir, allow_unverified
    )
    existing = _existing_issue_ids(hass)
    for issue_id in existing - set(desired):
        ir.async_delete_issue(hass, DOMAIN, issue_id)
    for issue_id, spec in desired.items():
        if issue_id in existing:
            # Never create the same issue twice; HA keeps the open one.
            continue
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            translation_key=spec["translation_key"],
            translation_placeholders=spec["placeholders"],
        )
    if desired or existing:
        _LOGGER.debug(
            "repairs: %d active issue(s), %d stale removed",
            len(desired),
            len(existing - set(desired)),
        )
