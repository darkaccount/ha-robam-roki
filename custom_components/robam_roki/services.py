"""Home Assistant service that drafts a read-only profile from cached data.

The service only reads the coordinator cache: it never asks the cloud for new
properties and never sends a command to an appliance.  The generated files are
written under the Home Assistant configuration directory so the user can review
them and reload the integration:
    <config>/robam_roki/profiles/draft-<dc>-<dt>.yaml
    <config>/robam_roki/fixtures/<dc>-<dt>.json
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .devices import get_driver_for_device
from .draft import DraftResult, draft_from_properties

_LOGGER = logging.getLogger(__name__)

SERVICE_NAME = "draft_profile"
PROFILES_SUBDIR = "profiles"
FIXTURES_SUBDIR = "fixtures"
CONFIG_SUBDIR = "robam_roki"


async def async_setup_services(hass: HomeAssistant) -> None:
    """Register the draft_profile service exactly once."""
    if hass.services.has_service(DOMAIN, SERVICE_NAME):
        return

    async def _handle(call: ServiceCall) -> None:
        await _async_draft_profile(hass, call)

    hass.services.async_register(
        DOMAIN, SERVICE_NAME, _handle, schema=_build_schema()
    )


def _build_schema() -> Any | None:
    """The UI validation schema, or None when voluptuous is unavailable.

    Home Assistant always ships voluptuous; the offline test environment does
    not, and the handler validates the same rules itself.
    """
    try:
        import voluptuous as vol
    except ImportError:  # pragma: no cover - only the offline test env
        return None
    return vol.Schema(
        {
            vol.Optional("device_id"): str,
            vol.Optional("guid"): str,
            vol.Optional("write_fixture", default=True): bool,
        }
    )


class _SelectedDevice:
    """The coordinator plus device record a draft was requested for."""

    def __init__(self, coordinator: Any, guid: str, device: dict[str, Any]) -> None:
        self.coordinator = coordinator
        self.guid = guid
        self.device = device


def _loaded_entries(hass: HomeAssistant) -> list[Any]:
    try:
        return list(hass.config_entries.async_loaded_entries(DOMAIN))
    except Exception:  # noqa: BLE001 - never break a service call
        return []


def _coordinators(hass: HomeAssistant) -> list[Any]:
    coordinators = []
    for entry in _loaded_entries(hass):
        runtime = getattr(entry, "runtime_data", None)
        coordinator = getattr(runtime, "coordinator", None)
        if coordinator is not None:
            coordinators.append(coordinator)
    return coordinators


def _select_device(
    hass: HomeAssistant, device_id: Any, guid: Any
) -> _SelectedDevice:
    if bool(device_id) == bool(guid):
        raise ServiceValidationError(
            "give exactly one of device_id or guid"
        )
    coordinators = _coordinators(hass)
    if not coordinators:
        raise ServiceValidationError(
            "no loaded ROBAM ROKI config entry; set the integration up first"
        )

    if guid:
        wanted = str(guid)
        for coordinator in coordinators:
            if wanted in coordinator.device_ids:
                return _SelectedDevice(
                    coordinator, wanted, coordinator.device_info_for(wanted)
                )
        raise ServiceValidationError(f"unknown device guid: {wanted}")

    registry = dr.async_get(hass)
    device_entry = registry.async_get(str(device_id))
    if device_entry is None:
        raise ServiceValidationError(f"unknown device_id: {device_id}")
    for domain, identifier in device_entry.identifiers:
        if domain != DOMAIN:
            continue
        for coordinator in coordinators:
            if str(identifier) in coordinator.device_ids:
                return _SelectedDevice(
                    coordinator,
                    str(identifier),
                    coordinator.device_info_for(str(identifier)),
                )
    raise ServiceValidationError(
        f"device {device_id} does not belong to this integration"
    )


def _expect_properties(
    coordinator: Any, device: dict[str, Any], properties: dict[str, Any]
) -> dict[str, Any]:
    """Expected entity values computed with the device's current driver."""
    try:
        driver = get_driver_for_device(coordinator, device)
    except Exception as err:  # noqa: BLE001 - a draft must not fail here
        _LOGGER.warning("could not build the driver for the draft: %s", err)
        return {}
    expect: dict[str, Any] = {}
    for spec in driver.sensors():
        if spec.value_fn is not None:
            try:
                expect[spec.key] = spec.value_fn(properties)
            except Exception:  # noqa: BLE001 - keep the rest of the fixture
                expect[spec.key] = None
        else:
            expect[spec.key] = properties.get(spec.key)
    for spec in driver.binary_sensors():
        if spec.is_on_fn is not None:
            try:
                expect[spec.key] = spec.is_on_fn(properties)
            except Exception:  # noqa: BLE001 - keep the rest of the fixture
                expect[spec.key] = None
        else:
            expect[spec.key] = properties.get(spec.key)
    return expect


def _unique_paths(
    profile_dir: Path,
    fixture_dir: Path,
    dc: str,
    dt: str,
    write_fixture: bool,
) -> tuple[Path, Path]:
    """Pick file names that never overwrite an existing draft."""
    for index in range(1, 1000):
        suffix = "" if index == 1 else f"-{index}"
        profile_path = profile_dir / f"draft-{dc}-{dt}{suffix}.yaml"
        fixture_path = fixture_dir / f"{dc}-{dt}{suffix}.json"
        if profile_path.exists():
            continue
        if write_fixture and fixture_path.exists():
            continue
        return profile_path, fixture_path
    raise ServiceValidationError("too many drafts with the same name")


def _write_draft(
    hass: HomeAssistant,
    result: DraftResult,
    write_fixture: bool,
) -> tuple[Path, Path | None]:
    match = result.fixture.get("match") or {}
    dc = str(match.get("dc") or "unknown")
    dt = str(match.get("dt") or "unknown")
    config_dir = Path(hass.config.path())
    profile_dir = config_dir / CONFIG_SUBDIR / PROFILES_SUBDIR
    fixture_dir = config_dir / CONFIG_SUBDIR / FIXTURES_SUBDIR
    profile_path, fixture_path = _unique_paths(
        profile_dir, fixture_dir, dc, dt, write_fixture
    )
    profile_dir.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(result.profile_yaml, encoding="utf-8")
    written_fixture: Path | None = None
    if write_fixture:
        fixture_dir.mkdir(parents=True, exist_ok=True)
        fixture_path.write_text(
            json.dumps(result.fixture, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        written_fixture = fixture_path
    return profile_path, written_fixture


def _notify_success(
    hass: HomeAssistant,
    profile_path: Path,
    fixture_path: Path | None,
    result: DraftResult,
) -> None:
    """Paths and the next step first, then the whole YAML as an appendix."""
    lines = [
        "草稿已生成",
        f"  文件：{profile_path}",
    ]
    if fixture_path is not None:
        lines.append(f"  fixture：{fixture_path}")
    lines.extend(
        [
            "  下一步：编辑 → 设置 → 设备与服务 → ROBAM ROKI → 重新加载 → 查看实体",
            "  注意：controls 段为注释，本版本不会发送任何控制命令",
        ]
    )
    for warning in result.warnings:
        lines.append(f"  警告：{warning}")
    lines.extend(
        [
            "",
            "──────── 以下为文件全文 ────────",
            result.profile_yaml.rstrip(),
        ]
    )
    persistent_notification.async_create(
        hass,
        "\n".join(lines),
        title="ROBAM ROKI 草稿已生成",
    )


def _notify_failure(hass: HomeAssistant, reason: str) -> None:
    _LOGGER.warning("could not write the profile draft: %s", reason)
    persistent_notification.async_create(
        hass,
        "草稿写入失败\n"
        f"  原因：{reason}\n"
        "  请检查配置目录写权限后重试。",
        title="ROBAM ROKI 草稿写入失败",
    )


async def _async_draft_profile(hass: HomeAssistant, call: ServiceCall) -> None:
    data = dict(call.data)
    selected = _select_device(
        hass, data.get("device_id"), data.get("guid")
    )
    write_fixture = bool(data.get("write_fixture", True))

    coordinator = selected.coordinator
    cached = (coordinator.data or {}).get(selected.guid) or {}

    def expect_fn(properties: dict[str, Any]) -> dict[str, Any]:
        # Read-only: only the coordinator cache and the driver's own logic.
        return _expect_properties(coordinator, selected.device, properties)

    result = draft_from_properties(selected.device, cached, expect_fn=expect_fn)
    if not result.profile_yaml:
        raise ServiceValidationError(
            "could not draft a profile: " + "; ".join(result.warnings)
        )

    try:
        profile_path, fixture_path = _write_draft(hass, result, write_fixture)
    except OSError as err:
        _notify_failure(hass, str(err))
        return
    _notify_success(hass, profile_path, fixture_path, result)
