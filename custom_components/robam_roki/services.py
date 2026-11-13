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
from datetime import datetime
from pathlib import Path
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr

from . import builtin as builtin_support
from .api import RokiApiError
from .const import DOMAIN, LEARN_DEFAULT_SECONDS, LEARN_MAX_SECONDS
from .devices import get_driver_for_device
from .draft import (
    DraftResult,
    controls_from_actions,
    draft_from_properties,
)
from .export import build_export_markdown, export_filename
from .profiles import SOURCE_BUILTIN, match_profile

_LOGGER = logging.getLogger(__name__)

#: The built-in driver coverage lives in builtin.py so the CLI can answer the
#: same questions; these aliases keep the call sites below readable.
_builtin_driver = builtin_support.builtin_driver
_expect_values = builtin_support.expect_values

SERVICE_NAME = "draft_profile"
LEARN_START_SERVICE = "learn_start"
LEARN_STOP_SERVICE = "learn_stop"
EXPORT_SERVICE = "export_profile"
PROFILES_SUBDIR = "profiles"
FIXTURES_SUBDIR = "fixtures"
EXPORTS_SUBDIR = "exports"
CONFIG_SUBDIR = "robam_roki"

#: The profiles shipped with the integration; a device matched by one of these
#: does not need an export at all.
BUILTIN_PROFILES_DIR = Path(__file__).parent / PROFILES_SUBDIR


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

    async def _handle_export(call: ServiceCall) -> None:
        await _async_export_profile(hass, call)

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
    _register_service(
        hass, EXPORT_SERVICE, _handle_export, _build_export_schema()
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
            vol.Optional("duration", default=LEARN_DEFAULT_SECONDS): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=LEARN_MAX_SECONDS)
            ),
        }
    )


def _build_export_schema() -> Any | None:
    """UI schema for export_profile; None when voluptuous is unavailable."""
    try:
        import voluptuous as vol
    except ImportError:  # pragma: no cover - only the offline test env
        return None
    return vol.Schema(
        {vol.Optional("device_id"): str, vol.Optional("guid"): str}
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


#: The built-in driver coverage lives in builtin.py so the standalone CLI can
#: produce exactly the same extends / skipped-keys / expect answers.


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


def _other_drafts(
    profile_dir: Path, dc: str, dt: str, keep: Path
) -> list[str]:
    """Drafts for the same dc/dt already on disk, the new file excluded.

    A draft that matches the device equally well is the most common cause of a
    profile conflict, so the service names the ones it found instead of letting
    the user discover the warning in the log later.
    """
    prefix = f"draft-{dc}-{dt}"
    others: list[str] = []
    try:
        candidates = sorted(profile_dir.glob(f"{prefix}*.yaml"))
    except OSError as err:
        _LOGGER.debug("could not scan %s for old drafts: %s", profile_dir, err)
        return others
    for path in candidates:
        if path == keep or not path.is_file():
            continue
        stem = path.stem
        suffix = stem[len(prefix) + 1 :] if stem.startswith(f"{prefix}-") else ""
        if stem == prefix or (suffix and suffix.isdigit()):
            others.append(path.name)
    return others


def _write_draft(
    config_dir: str,
    dc: str,
    dt: str,
    profile_yaml: str,
    fixture: dict[str, Any],
    write_fixture: bool,
) -> tuple[str, str | None, list[str]]:
    """Write the draft files; always called in the executor.

    All blocking work (mkdir, glob, json.dumps, write_text) lives here so the
    event loop never sees it -- Home Assistant flags those calls otherwise.
    The old drafts are collected before writing: the new file is the one that
    does not exist yet, so anything matching is a leftover from an earlier run.
    """
    profile_dir = Path(config_dir) / CONFIG_SUBDIR / PROFILES_SUBDIR
    fixture_dir = Path(config_dir) / CONFIG_SUBDIR / FIXTURES_SUBDIR
    profile_path, fixture_path = _unique_paths(
        profile_dir, fixture_dir, dc, dt, write_fixture
    )
    old_drafts = _other_drafts(profile_dir, dc, dt, profile_path)
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
    return str(profile_path), written_fixture, old_drafts


def _notify_success(
    hass: HomeAssistant,
    profile_path: Path,
    fixture_path: Path | None,
    result: DraftResult,
    builtin_driver: str | None = None,
    learned: list[dict[str, Any]] | None = None,
    old_drafts: list[str] | None = None,
) -> None:
    """Paths and the next step first, then the whole YAML as an appendix."""
    entries = controls_from_actions(learned or [])
    lines = [
        "草稿已生成",
        f"  文件：{profile_path}",
    ]
    if fixture_path is not None:
        lines.append(f"  fixture：{fixture_path}")
    lines.append(
        "  下一步：设置 → 设备与服务 → 集成 → ROBAM ROKI → ⋮ → 重新加载 → 查看实体"
    )
    if entries:
        lines.append(
            "  注意：controls 里的动作重载后是 button 实体，"
            "先在选项里打开「允许未验证的控制」再逐个点击验证"
        )
    else:
        lines.append("  注意：本草稿只有只读传感器，不会生成控制实体")
    if old_drafts:
        lines.append(
            "  注意：检测到旧草稿 "
            + "、".join(old_drafts)
            + "，新文件不会覆盖它，建议删除以免匹配冲突"
            "（选项 → 清理草稿）"
        )
    if builtin_driver:
        lines.append(
            "  继承：已继承内置驱动 python:"
            f"{builtin_driver}，草稿只补充驱动未覆盖的新键"
        )
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
    raw_duration = data.get("duration", LEARN_DEFAULT_SECONDS)
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
        (
            profile_path_text,
            fixture_path_text,
            old_drafts,
        ) = await hass.async_add_executor_job(
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
        hass,
        profile_path,
        fixture_path,
        result,
        builtin_driver,
        learned,
        old_drafts,
    )

# -- export_profile ------------------------------------------------------


def _active_profile(coordinator: Any, device: dict[str, Any]) -> Any | None:
    """The profile the driver factory matched for this device, if any."""
    profiles = list(getattr(coordinator, "profiles", None) or [])
    if not profiles:
        return None
    try:
        return match_profile(device, profiles)
    except Exception as err:  # noqa: BLE001 - a broken profile must not crash
        _LOGGER.warning("could not match a profile for the export: %s", err)
        return None


def _is_builtin_profile(profile: Any) -> bool:
    """Whether the profile ships with the integration (nothing to export)."""
    if str(getattr(profile, "source", "")) == SOURCE_BUILTIN:
        return True
    path = getattr(profile, "path", None)
    if path is None:
        return False
    try:
        Path(path).resolve().relative_to(BUILTIN_PROFILES_DIR.resolve())
    except (OSError, ValueError):
        return False
    return True


def _config_dir(hass: HomeAssistant) -> str:
    return str(hass.config.path())


def _exports_dir(hass: HomeAssistant) -> Path:
    return Path(_config_dir(hass)) / CONFIG_SUBDIR / EXPORTS_SUBDIR


def _fixture_local_path(hass: HomeAssistant, dc: str, dt: str) -> str:
    return str(
        Path(_config_dir(hass))
        / CONFIG_SUBDIR
        / FIXTURES_SUBDIR
        / f"{dc}-{dt}.json"
    )


def _read_export_inputs(
    config_dir: str, profile_path: str, dc: str, dt: str
) -> tuple[str, dict[str, Any] | None, str]:
    """Read the profile, a reusable fixture and the version (executor job).

    Everything here touches the filesystem: the profile text, the fixture the
    draft_profile service may already have written, and manifest.json.
    """
    profile_yaml = Path(profile_path).read_text(encoding="utf-8")
    fixture: dict[str, Any] | None = None
    fixture_path = Path(config_dir) / CONFIG_SUBDIR / FIXTURES_SUBDIR
    fixture_path = fixture_path / f"{dc}-{dt}.json"
    try:
        if fixture_path.is_file():
            loaded = json.loads(fixture_path.read_text(encoding="utf-8"))
            fixture = loaded if isinstance(loaded, dict) else None
    except (OSError, ValueError) as err:
        _LOGGER.debug("could not reuse the fixture %s: %s", fixture_path, err)
    version = ""
    manifest = Path(__file__).with_name("manifest.json")
    try:
        version = str(
            json.loads(manifest.read_text(encoding="utf-8")).get("version") or ""
        )
    except (OSError, ValueError):
        version = ""
    return profile_yaml, fixture, version


def _write_export(directory: str, filename: str, markdown: str) -> str:
    """Write the export document; always called in the executor.

    The export produces exactly one file and it is always Markdown: no .yaml
    and no .json is ever written next to it.  A second export inside the same
    minute gets a numbered suffix instead of overwriting the first one.
    """
    base = Path(directory)
    base.mkdir(parents=True, exist_ok=True)
    path = base / filename
    index = 2
    while path.exists():
        path = base / f"{Path(filename).stem}-{index}{Path(filename).suffix}"
        index += 1
    path.write_text(markdown, encoding="utf-8")
    return str(path)


def _notify_export(
    hass: HomeAssistant, export_path: Path, fixture_local_path: str
) -> None:
    persistent_notification.async_create(
        hass,
        "\n".join(
            [
                "profile 导出已生成（只输出这一份 .md）",
                f"  文件：{export_path}",
                "  内容：设备与匹配 / 验证清单 / profile YAML / fixture / 如何贡献",
                f"  同一份 fixture 的本机路径：{fixture_local_path}",
                (
                    "  下一步：整份贴到 GitHub issue，或直接提 PR"
                    "（维护者用 tools/promote_profile.py 处理）。"
                ),
            ]
        ),
        title="ROBAM ROKI profile 导出",
    )


def _notify_export_failure(hass: HomeAssistant, reason: str) -> None:
    _LOGGER.warning("could not write the profile export: %s", reason)
    persistent_notification.async_create(
        hass,
        "导出写入失败\n"
        f"  原因：{reason}\n"
        "  请检查配置目录写权限后重试。",
        title="ROBAM ROKI profile 导出失败",
    )


async def _async_export_profile(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    """Export the matched user profile and its fixture as one Markdown file.

    Nothing is sent to the appliance and nothing leaves the configuration
    directory: the file is meant to be pasted into an issue or a PR.
    """
    data = dict(call.data)
    selected = _select_device(hass, data.get("device_id"), data.get("guid"))
    coordinator = selected.coordinator
    device = selected.device
    dc = str(device.get("dc") or "unknown")
    dt = str(device.get("dt") or "unknown")

    profile = _active_profile(coordinator, device)
    if profile is None:
        raise ServiceValidationError(
            "this device has no user profile yet: call robam_roki."
            "draft_profile first and export that draft"
        )
    if _is_builtin_profile(profile):
        raise ServiceValidationError(
            f"this device is already covered by the built-in profile "
            f"{getattr(profile, 'id', '')!r}; open an issue describing the "
            "problem instead of exporting a profile"
        )
    profile_path = getattr(profile, "path", None)
    if profile_path is None:
        raise ServiceValidationError(
            "the matched profile has no file on disk to export"
        )

    try:
        profile_yaml, fixture, version = await hass.async_add_executor_job(
            _read_export_inputs,
            _config_dir(hass),
            str(profile_path),
            dc,
            dt,
        )
    except OSError as err:
        raise ServiceValidationError(
            f"could not read the profile: {err}"
        ) from err

    learned_fn = getattr(coordinator, "learned_controls", None)
    learned = list(learned_fn(selected.guid) or []) if callable(learned_fn) else []
    if fixture is None:
        # No fixture from draft_profile: generate the same one the drafter
        # would write, from the coordinator cache only.
        driver = _build_driver(coordinator, device)
        builtin_driver, builtin_keys = _builtin_driver(driver)
        cached = (coordinator.data or {}).get(selected.guid) or {}

        def expect_fn(properties: dict[str, Any]) -> dict[str, Any]:
            return _expect_values(driver, properties)

        drafted = draft_from_properties(
            device,
            cached,
            expect_fn=expect_fn,
            builtin_driver=builtin_driver,
            builtin_keys=builtin_keys,
            learned_controls=learned,
        )
        fixture = drafted.fixture

    generated_at = datetime.now().astimezone()
    fixture_local_path = _fixture_local_path(hass, dc, dt)
    markdown = build_export_markdown(
        device_name=str(device.get("name") or ""),
        dc=dc,
        dt=dt,
        transport="REST" if coordinator.is_rest_device(selected.guid) else "MQTT",
        extends=(
            f"python:{profile.python_base}"
            if getattr(profile, "python_base", None)
            else None
        ),
        profile_yaml=profile_yaml,
        fixture=fixture,
        fixture_local_path=fixture_local_path,
        controls=dict(getattr(profile, "controls", None) or {}),
        learned=learned,
        generated_at=generated_at,
        version=version,
    )
    filename = export_filename(dc, dt, generated_at)
    try:
        export_path = await hass.async_add_executor_job(
            _write_export, str(_exports_dir(hass)), filename, markdown
        )
    except OSError as err:
        _notify_export_failure(hass, str(err))
        return
    _notify_export(hass, Path(export_path), fixture_local_path)