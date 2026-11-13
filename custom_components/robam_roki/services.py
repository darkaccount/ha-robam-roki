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

from .api import RokiApiError
from .const import DOMAIN, LEARN_MAX_SECONDS
from .devices import PYTHON_DRIVERS, get_driver_for_device
from .draft import (
    DraftResult,
    controls_from_actions,
    draft_from_properties,
)

_LOGGER = logging.getLogger(__name__)

SERVICE_NAME = "draft_profile"
LEARN_START_SERVICE = "learn_start"
LEARN_STOP_SERVICE = "learn_stop"
PROFILES_SUBDIR = "profiles"
FIXTURES_SUBDIR = "fixtures"
CONFIG_SUBDIR = "robam_roki"


async def async_setup_services(hass: HomeAssistant) -> None:
    """Register the integration services exactly once."""
    if hass.services.has_service(DOMAIN, SERVICE_NAME):
        return

    async def _handle_draft(call: ServiceCall) -> None:
        await _async_draft_profile(hass, call)

    async def _handle_learn_start(call: ServiceCall) -> Any:
        return await _async_learn_start(hass, call)

    async def _handle_learn_stop(call: ServiceCall) -> Any:
        return await _async_learn_stop(hass, call)

    _register_service(
        hass, SERVICE_NAME, _handle_draft, _build_schema()
    )
    _register_service(
        hass,
        LEARN_START_SERVICE,
        _handle_learn_start,
        _build_learn_start_schema(),
        response=True,
    )
    _register_service(
        hass,
        LEARN_STOP_SERVICE,
        _handle_learn_stop,
        _build_learn_stop_schema(),
        response=True,
    )


def _register_service(
    hass: HomeAssistant,
    name: str,
    handler: Any,
    schema: Any | None,
    *,
    response: bool = False,
) -> None:
    """Register one service, opting into responses when HA supports it."""
    kwargs: dict[str, Any] = {"schema": schema}
    if response:
        try:
            from homeassistant.core import SupportsResponse
        except ImportError:  # offline test harness without the enum
            SupportsResponse = None  # type: ignore[assignment]
        if SupportsResponse is not None:
            kwargs["supports_response"] = SupportsResponse.OPTIONAL
    hass.services.async_register(DOMAIN, name, handler, **kwargs)


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


def _build_learn_start_schema() -> Any | None:
    """UI schema for learn_start; None when voluptuous is unavailable."""
    try:
        import voluptuous as vol
    except ImportError:  # pragma: no cover - only the offline test env
        return None
    return vol.Schema(
        {
            vol.Optional("device_id"): str,
            vol.Optional("guid"): str,
            vol.Optional("duration", default=LEARN_MAX_SECONDS): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=LEARN_MAX_SECONDS)
            ),
        }
    )


def _build_learn_stop_schema() -> Any | None:
    """UI schema for learn_stop; None when voluptuous is unavailable."""
    try:
        import voluptuous as vol
    except ImportError:  # pragma: no cover - only the offline test env
        return None
    return vol.Schema(
        {vol.Optional("device_id"): str, vol.Optional("guid"): str}
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


def _build_driver(
    coordinator: Any, device: dict[str, Any]
) -> Any | None:
    """The device's current driver, or None when it cannot be built."""
    try:
        return get_driver_for_device(coordinator, device)
    except Exception as err:  # noqa: BLE001 - a draft must not fail here
        _LOGGER.warning("could not build the driver for the draft: %s", err)
        return None


def _builtin_driver(driver: Any) -> tuple[str | None, frozenset[str] | None]:
    """Name and entity keys of the built-in Python driver behind this driver.

    A driver generated from a profile is not in the registry, so it gets no
    extends line: the draft itself is the replacement profile.
    """
    if driver is None:
        return None, None
    for name, driver_cls in PYTHON_DRIVERS.items():
        if type(driver) is driver_cls:
            keys = {spec.key for spec in driver.sensors()}
            keys |= {spec.key for spec in driver.binary_sensors()}
            return name, frozenset(keys)
    return None, None


def _expect_values(
    driver: Any, properties: dict[str, Any]
) -> dict[str, Any]:
    """Expected entity values computed with the device's current driver."""
    expect: dict[str, Any] = {}
    if driver is None:
        return expect
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
    config_dir: str,
    dc: str,
    dt: str,
    profile_yaml: str,
    fixture: dict[str, Any],
    write_fixture: bool,
) -> tuple[str, str | None]:
    """Write the draft files; always called in the executor.

    All blocking work (mkdir, json.dumps, write_text) lives here so the event
    loop never sees it -- Home Assistant flags those calls otherwise.
    """
    profile_dir = Path(config_dir) / CONFIG_SUBDIR / PROFILES_SUBDIR
    fixture_dir = Path(config_dir) / CONFIG_SUBDIR / FIXTURES_SUBDIR
    profile_path, fixture_path = _unique_paths(
        profile_dir, fixture_dir, dc, dt, write_fixture
    )
    profile_dir.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(profile_yaml, encoding="utf-8")
    written_fixture: str | None = None
    if write_fixture:
        fixture_dir.mkdir(parents=True, exist_ok=True)
        fixture_path.write_text(
            json.dumps(fixture, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        written_fixture = str(fixture_path)
    return str(profile_path), written_fixture


def _notify_success(
    hass: HomeAssistant,
    profile_path: Path,
    fixture_path: Path | None,
    result: DraftResult,
    builtin_driver: str | None = None,
    learned: list[dict[str, Any]] | None = None,
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
    if builtin_driver:
        lines.append(
            "  继承：已继承内置驱动 python:"
            f"{builtin_driver}，草稿只补充驱动未覆盖的新键"
        )
    entries = controls_from_actions(learned or [])
    if entries:
        lines.append(
            f"  学习结果：{len(learned or [])} 组动作 → "
            f"{len(entries)} 条 controls（verified: false，先在实验开关下验证）"
        )
    else:
        lines.append(
            "  提示：没有学习到的动作，controls 仍是注释模板；"
            "可先 learn_start/learn_stop 学习，或用 "
            "python tools/roki_cli.py collect --device <guid> --mark <标签> "
            "再 draft <bundle>。"
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


async def _async_learn_start(
    hass: HomeAssistant, call: ServiceCall
) -> dict[str, Any]:
    """Begin a passive learning session for one device."""
    data = dict(call.data)
    selected = _select_device(hass, data.get("device_id"), data.get("guid"))
    raw_duration = data.get("duration", LEARN_MAX_SECONDS)
    try:
        duration = int(raw_duration)
    except (TypeError, ValueError) as err:
        raise ServiceValidationError(
            "duration must be an integer number of seconds"
        ) from err
    if duration < 1:
        raise ServiceValidationError("duration must be at least 1 second")
    duration = min(LEARN_MAX_SECONDS, duration)
    try:
        return selected.coordinator.learn_start(selected.guid, duration)
    except RokiApiError as err:
        raise ServiceValidationError(str(err)) from err


async def _async_learn_stop(
    hass: HomeAssistant, call: ServiceCall
) -> dict[str, Any]:
    """Stop the running session and return the recorded actions."""
    data = dict(call.data)
    device_id = data.get("device_id")
    guid = data.get("guid")
    if device_id or guid:
        selected = _select_device(hass, device_id, guid)
        try:
            return await selected.coordinator.learn_stop(selected.guid)
        except RokiApiError as err:
            raise ServiceValidationError(str(err)) from err
    coordinators = _coordinators(hass)
    if not coordinators:
        raise ServiceValidationError(
            "no loaded ROBAM ROKI config entry; set the integration up first"
        )
    for coordinator in coordinators:
        if coordinator.learn_active():
            return await coordinator.learn_stop()
    return {"device_id": None, "actions": []}


async def _async_draft_profile(hass: HomeAssistant, call: ServiceCall) -> None:
    data = dict(call.data)
    selected = _select_device(
        hass, data.get("device_id"), data.get("guid")
    )
    write_fixture = bool(data.get("write_fixture", True))

    coordinator = selected.coordinator
    cached = (coordinator.data or {}).get(selected.guid) or {}
    driver = _build_driver(coordinator, selected.device)
    builtin_driver, builtin_keys = _builtin_driver(driver)

    learned: list[dict[str, Any]] = []
    learned_fn = getattr(coordinator, "learned_controls", None)
    if callable(learned_fn):
        learned = list(learned_fn(selected.guid) or [])

    def expect_fn(properties: dict[str, Any]) -> dict[str, Any]:
        # Read-only: only the coordinator cache and the driver's own logic.
        return _expect_values(driver, properties)

    result = draft_from_properties(
        selected.device,
        cached,
        expect_fn=expect_fn,
        builtin_driver=builtin_driver,
        builtin_keys=builtin_keys,
        learned_controls=learned,
    )
    if not result.profile_yaml:
        raise ServiceValidationError(
            "could not draft a profile: " + "; ".join(result.warnings)
        )

    match = result.fixture.get("match") or {}
    dc = str(match.get("dc") or "unknown")
    dt = str(match.get("dt") or "unknown")
    try:
        profile_path_text, fixture_path_text = await hass.async_add_executor_job(
            _write_draft,
            str(hass.config.path()),
            dc,
            dt,
            result.profile_yaml,
            result.fixture,
            write_fixture,
        )
    except OSError as err:
        _notify_failure(hass, str(err))
        return
    profile_path = Path(profile_path_text)
    fixture_path = Path(fixture_path_text) if fixture_path_text else None
    _notify_success(
        hass, profile_path, fixture_path, result, builtin_driver, learned
    )
