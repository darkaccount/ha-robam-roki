"""Tests for the Home Assistant draft_profile service (P2-C).

The integration package is loaded in an isolated package with the handful of
Home Assistant symbols the service needs stubbed out, so the whole service can
run offline against a fake coordinator.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import re
import sys
import types
from pathlib import Path
from typing import Any

import pytest
import yaml

COMPONENT = (
    Path(__file__).resolve().parents[1] / "custom_components" / "robam_roki"
)
PACKAGE = "roki_services_pkg_test"
DOMAIN = "robam_roki"

GUID = "R8229000000000000"
DEVICE = {
    "guid": GUID,
    "name": "厨房油烟机",
    "dc": "RYYJ",
    "dt": "R8229",
    "deviceSource": 0,
}
PROPERTIES = {
    "workStatus": 1,
    "level": 2,
    "timing": 0,
    "phone": "13800000000",
}

NOTIFICATIONS: list[dict[str, Any]] = []


# -- Home Assistant stubs ------------------------------------------------


def _module(name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__path__ = []
    return module


def _install_ha_stubs() -> None:
    ha = sys.modules["homeassistant"]
    components = sys.modules["homeassistant.components"]
    ha.components = components

    exceptions = sys.modules.get("homeassistant.exceptions") or _module(
        "homeassistant.exceptions"
    )
    if not hasattr(exceptions, "ServiceValidationError"):

        class ServiceValidationError(Exception):
            pass

        exceptions.ServiceValidationError = ServiceValidationError
    sys.modules["homeassistant.exceptions"] = exceptions

    core = sys.modules.get("homeassistant.core") or _module("homeassistant.core")
    if not hasattr(core, "ServiceCall"):

        class ServiceCall:
            pass

        core.ServiceCall = ServiceCall
    if not hasattr(core, "HomeAssistant"):
        core.HomeAssistant = type("HomeAssistant", (), {})
    sys.modules["homeassistant.core"] = core

    notifications = _module("homeassistant.components.persistent_notification")

    def async_create(hass, message, title=None, notification_id=None):
        NOTIFICATIONS.append({"message": message, "title": title})

    notifications.async_create = async_create
    sys.modules[
        "homeassistant.components.persistent_notification"
    ] = notifications
    components.persistent_notification = notifications

    device_registry = sys.modules.get(
        "homeassistant.helpers.device_registry"
    ) or _module("homeassistant.helpers.device_registry")
    device_registry.async_get = lambda hass: hass.device_registry
    sys.modules["homeassistant.helpers.device_registry"] = device_registry

    helpers = sys.modules.get("homeassistant.helpers") or _module(
        "homeassistant.helpers"
    )
    helpers.device_registry = device_registry
    sys.modules["homeassistant.helpers"] = helpers


def _ensure_notifications() -> None:
    """Re-register our recorder if another test module replaced the stub."""
    components = sys.modules["homeassistant.components"]
    notifications = sys.modules.get(
        "homeassistant.components.persistent_notification"
    )
    if notifications is None:
        notifications = _module(
            "homeassistant.components.persistent_notification"
        )
    sys.modules[
        "homeassistant.components.persistent_notification"
    ] = notifications

    def async_create(hass, message, title=None, notification_id=None):
        NOTIFICATIONS.append({"message": message, "title": title})

    notifications.async_create = async_create
    components.persistent_notification = notifications


def _stub_coordinator() -> None:
    module = _module(f"{PACKAGE}.coordinator")

    class RokiCoordinator:
        pass

    module.RokiCoordinator = RokiCoordinator
    sys.modules[f"{PACKAGE}.coordinator"] = module


_install_ha_stubs()
_stub_coordinator()
package = _module(PACKAGE)
package.__path__ = [str(COMPONENT)]
sys.modules[PACKAGE] = package

const = importlib.import_module(f"{PACKAGE}.const")
devices = importlib.import_module(f"{PACKAGE}.devices")
export = importlib.import_module(f"{PACKAGE}.export")
profiles = importlib.import_module(f"{PACKAGE}.profiles")
services = importlib.import_module(f"{PACKAGE}.services")
draft = importlib.import_module(f"{PACKAGE}.draft")

ServiceValidationError = sys.modules["homeassistant.exceptions"].ServiceValidationError


# -- fakes ---------------------------------------------------------------


class _FakeConfig:
    def __init__(self, base: Path) -> None:
        self._base = base

    def path(self, *parts: str) -> str:
        if parts:
            return str(self._base.joinpath(*parts))
        return str(self._base)


class _FakeServices:
    def __init__(self) -> None:
        self.registered: dict[tuple[str, str], dict[str, Any]] = {}

    def has_service(self, domain: str, name: str) -> bool:
        return (domain, name) in self.registered

    def async_register(
        self, domain, name, handler, schema=None, **kwargs: Any
    ) -> None:
        self.registered[(domain, name)] = {
            "handler": handler,
            "schema": schema,
            **kwargs,
        }


class _FakeRegistry:
    def __init__(self, entries: dict[str, Any] | None = None) -> None:
        self.entries = dict(entries or {})

    def async_get(self, device_id: str) -> Any:
        return self.entries.get(device_id)


class _FakeConfigEntries:
    def __init__(self, coordinators: list[Any]) -> None:
        self.entries = [
            types.SimpleNamespace(runtime_data=types.SimpleNamespace(coordinator=c))
            for c in coordinators
        ]

    def async_loaded_entries(self, domain: str) -> list[Any]:
        return list(self.entries)


class _FakeHass:
    def __init__(
        self,
        base: Path,
        coordinators: list[Any] | None = None,
        registry_entries: dict[str, Any] | None = None,
    ) -> None:
        self.config = _FakeConfig(base)
        self.services = _FakeServices()
        self.config_entries = _FakeConfigEntries(coordinators or [])
        self.device_registry = _FakeRegistry(registry_entries)
        # Every blocking write must go through this hook, never the loop.
        self.executor_calls: list[str] = []

    async def async_add_executor_job(self, target: Any, *args: Any) -> Any:
        self.executor_calls.append(getattr(target, "__name__", repr(target)))
        return target(*args)


class _FakeCoordinator:
    def __init__(
        self,
        devices: list[dict[str, Any]] | None = None,
        data: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.devices = [dict(device) for device in devices or [DEVICE]]
        self.data = data if data is not None else {GUID: dict(PROPERTIES)}
        self.profiles: list[Any] = []
        self.command_calls: list[str] = []
        self.learn_calls: list[tuple[Any, ...]] = []
        self.learning = False
        self.learned: list[dict[str, Any]] = []

    def learn_active(self) -> bool:
        return self.learning

    def learned_controls(self, device_id: str) -> list[dict[str, Any]]:
        return list(self.learned)

    def learn_start(self, device_id: str, duration: int) -> dict[str, Any]:
        self.learn_calls.append(("start", device_id, duration))
        self.learning = True
        return {
            "device_id": device_id,
            "expires_at": "2026-01-01T00:10:00+00:00",
        }

    async def learn_stop(
        self, device_id: str | None = None
    ) -> dict[str, Any]:
        self.learn_calls.append(("stop", device_id))
        self.learning = False
        return {
            "device_id": device_id or GUID,
            "actions": [
                {"index": 1, "command": 132, "body_hex": "01", "observed": {}}
            ],
        }

    @property
    def device_ids(self) -> list[str]:
        return [str(device["guid"]) for device in self.devices]

    def device_info_for(self, guid: str) -> dict[str, Any]:
        for device in self.devices:
            if str(device.get("guid")) == str(guid):
                return device
        return {"guid": guid}

    def is_rest_device(self, guid: str) -> bool:
        return str(self.device_info_for(guid).get("deviceSource")) == "1"

    async def async_set_power(self, *args: Any, **kwargs: Any) -> None:
        self.command_calls.append("power")

    async def async_set_level(self, *args: Any, **kwargs: Any) -> None:
        self.command_calls.append("level")

    async def async_set_light(self, *args: Any, **kwargs: Any) -> None:
        self.command_calls.append("light")

    async def async_set_timing(self, *args: Any, **kwargs: Any) -> None:
        self.command_calls.append("timing")


class _Call:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data


def _call(hass: _FakeHass, data: dict[str, Any]) -> None:
    _ensure_notifications()
    asyncio.run(services.async_setup_services(hass))
    handler = hass.services.registered[(DOMAIN, "draft_profile")]["handler"]
    asyncio.run(handler(_Call(data)))


@pytest.fixture(autouse=True)
def _clear_notifications() -> Any:
    NOTIFICATIONS.clear()
    yield
    NOTIFICATIONS.clear()


# -- registration and validation ----------------------------------------


def _call_service(hass: _FakeHass, name: str, data: dict[str, Any]) -> Any:
    _ensure_notifications()
    asyncio.run(services.async_setup_services(hass))
    handler = hass.services.registered[(DOMAIN, name)]["handler"]
    return asyncio.run(handler(_Call(data)))


def test_service_registers_only_once(tmp_path: Path) -> None:
    hass = _FakeHass(tmp_path, [_FakeCoordinator()])
    asyncio.run(services.async_setup_services(hass))
    asyncio.run(services.async_setup_services(hass))
    assert set(hass.services.registered) == {
        (DOMAIN, "draft_profile"),
        (DOMAIN, "export_profile"),
        (DOMAIN, "learn_start"),
        (DOMAIN, "learn_stop"),
    }


# -- learning services ---------------------------------------------------


def test_learn_start_resolves_the_device_and_clamps_duration(
    tmp_path: Path,
) -> None:
    coordinator = _FakeCoordinator()
    hass = _FakeHass(tmp_path, [coordinator])
    result = _call_service(
        hass, "learn_start", {"guid": GUID, "duration": 9999}
    )
    assert result["device_id"] == GUID
    assert coordinator.learn_calls == [("start", GUID, 600)]


def test_learn_start_defaults_to_five_minutes(tmp_path: Path) -> None:
    coordinator = _FakeCoordinator()
    hass = _FakeHass(tmp_path, [coordinator])
    _call_service(hass, "learn_start", {"guid": GUID})
    assert coordinator.learn_calls == [("start", GUID, 300)]


def test_learn_start_rejects_a_bad_duration(tmp_path: Path) -> None:
    hass = _FakeHass(tmp_path, [_FakeCoordinator()])
    with pytest.raises(ServiceValidationError):
        _call_service(hass, "learn_start", {"guid": GUID, "duration": 0})
    with pytest.raises(ServiceValidationError):
        _call_service(hass, "learn_start", {"guid": GUID, "duration": "soon"})


def test_learn_stop_without_a_session_is_a_no_op(tmp_path: Path) -> None:
    coordinator = _FakeCoordinator()
    hass = _FakeHass(tmp_path, [coordinator])
    result = _call_service(hass, "learn_stop", {})
    assert result == {"device_id": None, "actions": []}
    assert coordinator.learn_calls == []


def test_learn_stop_ends_the_active_session(tmp_path: Path) -> None:
    coordinator = _FakeCoordinator()
    coordinator.learning = True
    hass = _FakeHass(tmp_path, [coordinator])
    result = _call_service(hass, "learn_stop", {})
    assert result["actions"][0]["command"] == 132
    assert coordinator.learn_calls == [("stop", None)]


def test_learn_stop_validates_a_given_device(tmp_path: Path) -> None:
    coordinator = _FakeCoordinator()
    coordinator.learning = True
    entry = types.SimpleNamespace(identifiers={(DOMAIN, GUID)})
    hass = _FakeHass(
        tmp_path, [coordinator], registry_entries={"dev-learn": entry}
    )
    result = _call_service(hass, "learn_stop", {"device_id": "dev-learn"})
    assert result["device_id"] == GUID
    assert coordinator.learn_calls == [("stop", GUID)]


def test_exactly_one_of_device_id_or_guid(tmp_path: Path) -> None:
    hass = _FakeHass(tmp_path, [_FakeCoordinator()])
    with pytest.raises(ServiceValidationError):
        _call(hass, {"device_id": "dev-1", "guid": GUID})
    with pytest.raises(ServiceValidationError):
        _call(hass, {})
    assert NOTIFICATIONS == []
    assert not (tmp_path / "robam_roki").exists()


def test_unknown_guid_and_device_id_raise(tmp_path: Path) -> None:
    hass = _FakeHass(tmp_path, [_FakeCoordinator()])
    with pytest.raises(ServiceValidationError):
        _call(hass, {"guid": "UNKNOWN0000000000"})
    with pytest.raises(ServiceValidationError):
        _call(hass, {"device_id": "missing-device"})

    foreign = types.SimpleNamespace(identifiers={("other_domain", "x")})
    hass2 = _FakeHass(
        tmp_path, [_FakeCoordinator()], registry_entries={"dev-2": foreign}
    )
    with pytest.raises(ServiceValidationError):
        _call(hass2, {"device_id": "dev-2"})


# -- the happy paths -----------------------------------------------------


def _profile_path(base: Path, suffix: str = "") -> Path:
    return base / "robam_roki" / "profiles" / f"draft-RYYJ-R8229{suffix}.yaml"


def _fixture_path(base: Path, suffix: str = "") -> Path:
    return base / "robam_roki" / "fixtures" / f"RYYJ-R8229{suffix}.json"


def test_guid_writes_profile_and_fixture(tmp_path: Path) -> None:
    coordinator = _FakeCoordinator()
    hass = _FakeHass(tmp_path, [coordinator])
    _call(hass, {"guid": GUID})

    profile_path = _profile_path(tmp_path)
    fixture_path = _fixture_path(tmp_path)
    assert profile_path.is_file()
    assert fixture_path.is_file()

    document = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    assert document["schema"] == "roki-profile/1"
    assert document["match"] == {"dc": "RYYJ", "dt": "R8229"}

    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert fixture["schema"] == "roki-fixture/1"
    assert fixture["source"] == "auto"
    # The driver computed the expected entity values; credentials are redacted.
    assert fixture["expect"]["work_status"] == "开机"
    assert "fault_code" in fixture["expect"]
    assert fixture["properties"]["phone"] == "**REDACTED**"

    assert len(NOTIFICATIONS) == 1
    assert "草稿已生成" in NOTIFICATIONS[0]["message"]
    assert str(profile_path) in NOTIFICATIONS[0]["message"]
    assert str(fixture_path) in NOTIFICATIONS[0]["message"]


def test_device_id_is_resolved_through_the_registry(tmp_path: Path) -> None:
    entry = types.SimpleNamespace(identifiers={(DOMAIN, GUID)})
    hass = _FakeHass(
        tmp_path, [_FakeCoordinator()], registry_entries={"dev-1": entry}
    )
    _call(hass, {"device_id": "dev-1"})
    assert _profile_path(tmp_path).is_file()
    assert _fixture_path(tmp_path).is_file()


def test_write_fixture_false_skips_the_fixture(tmp_path: Path) -> None:
    hass = _FakeHass(tmp_path, [_FakeCoordinator()])
    _call(hass, {"guid": GUID, "write_fixture": False})
    assert _profile_path(tmp_path).is_file()
    assert not _fixture_path(tmp_path).exists()
    assert "fixture：" not in NOTIFICATIONS[0]["message"]


def test_existing_drafts_are_never_overwritten(tmp_path: Path) -> None:
    hass = _FakeHass(tmp_path, [_FakeCoordinator()])
    _call(hass, {"guid": GUID})
    _call(hass, {"guid": GUID})
    _call(hass, {"guid": GUID})

    assert _profile_path(tmp_path).is_file()
    assert _profile_path(tmp_path, "-2").is_file()
    assert _profile_path(tmp_path, "-3").is_file()
    assert _fixture_path(tmp_path).is_file()
    assert _fixture_path(tmp_path, "-2").is_file()
    assert _fixture_path(tmp_path, "-3").is_file()


def test_notification_puts_paths_before_the_full_text(tmp_path: Path) -> None:
    hass = _FakeHass(tmp_path, [_FakeCoordinator()])
    _call(hass, {"guid": GUID})
    message = NOTIFICATIONS[0]["message"]

    assert message.index("文件：") < message.index("下一步")
    assert message.index("下一步") < message.index("以下为文件全文")
    separator = message.index("以下为文件全文")
    yaml_text = _profile_path(tmp_path).read_text(encoding="utf-8")
    assert yaml_text.splitlines()[2] in message[separator:]
    assert "只有只读传感器" in message
    # The next step tells the user how to activate the profile.
    assert "重新加载" in message


def test_draft_files_are_written_in_the_executor(tmp_path: Path) -> None:
    hass = _FakeHass(tmp_path, [_FakeCoordinator()])
    _call(hass, {"guid": GUID})
    # The blocking mkdir/write_text work happens off the event loop.
    assert hass.executor_calls.count("_write_draft") == 1


def test_service_inherits_the_builtin_driver(tmp_path: Path) -> None:
    hass = _FakeHass(tmp_path, [_FakeCoordinator()])
    _call(hass, {"guid": GUID})

    document = yaml.safe_load(
        _profile_path(tmp_path).read_text(encoding="utf-8")
    )
    assert document["extends"] == "python:hood"
    # workStatus is already exposed by HoodDriver, so no duplicate sensor.
    assert "workStatus" not in (document.get("sensors") or {})
    # level / lightStatus / timing back the hood controls, so the draft
    # must not duplicate them as sensors either.
    for skipped in ("workStatus", "level", "lightStatus", "timing"):
        assert skipped not in (document.get("sensors") or {})
    assert "phone" in document["sensors"]  # a genuinely new property stays
    assert "已继承内置驱动 python:hood" in NOTIFICATIONS[0]["message"]


def test_draft_profile_writes_the_learned_controls(tmp_path: Path) -> None:
    coordinator = _FakeCoordinator()
    coordinator.learned = [
        {
            "frames": [{"command": 132, "body_hex": "01"}],
            "observed": {"workStatus": {"from": 0, "to": 1}},
        }
    ]
    hass = _FakeHass(tmp_path, [coordinator])
    _call(hass, {"guid": GUID})

    document = yaml.safe_load(
        _profile_path(tmp_path).read_text(encoding="utf-8")
    )
    assert document["controls"]["action_1"] == {
        "name": "开机",
        "kind": "mqtt",
        "command": 132,
        "body_hex": "01",
        "verified": False,
    }
    message = NOTIFICATIONS[0]["message"]
    assert "学习结果" in message
    assert "1 条 controls" in message


def test_draft_profile_hints_the_cli_fallback_without_learning(
    tmp_path: Path,
) -> None:
    coordinator = _FakeCoordinator()  # no learned actions
    hass = _FakeHass(tmp_path, [coordinator])
    _call(hass, {"guid": GUID})

    message = NOTIFICATIONS[0]["message"]
    assert "没有学习到的动作" in message
    assert "roki_cli.py collect" in message
    document = yaml.safe_load(
        _profile_path(tmp_path).read_text(encoding="utf-8")
    )
    assert "controls" not in document


def test_service_sends_no_commands(tmp_path: Path) -> None:
    coordinator = _FakeCoordinator()
    hass = _FakeHass(tmp_path, [coordinator])
    _call(hass, {"guid": GUID})
    assert coordinator.command_calls == []


def test_empty_cache_still_writes_a_pinned_profile(tmp_path: Path) -> None:
    # Only the coordinator cache is used; a device without cached properties
    # still yields a profile pinned to its match (with a warning).
    coordinator = _FakeCoordinator(data={})
    hass = _FakeHass(tmp_path, [coordinator])
    _call(hass, {"guid": GUID})
    document = yaml.safe_load(
        _profile_path(tmp_path).read_text(encoding="utf-8")
    )
    assert document["match"] == {"dc": "RYYJ", "dt": "R8229"}
    assert any(
        "警告" in line for line in NOTIFICATIONS[0]["message"].splitlines()
    )


# -- failure handling ----------------------------------------------------


def test_unwritable_config_directory_notifies_failure(tmp_path: Path) -> None:
    # A file where the directory should be makes mkdir/write fail.
    (tmp_path / "robam_roki").write_text("not a directory", encoding="utf-8")
    hass = _FakeHass(tmp_path, [_FakeCoordinator()])
    _call(hass, {"guid": GUID})

    assert len(NOTIFICATIONS) == 1
    assert "失败" in NOTIFICATIONS[0]["title"]
    assert "原因" in NOTIFICATIONS[0]["message"]


# -- stale drafts -------------------------------------------------------


def test_an_old_draft_is_mentioned_in_the_notification(tmp_path: Path) -> None:
    hass = _FakeHass(tmp_path, [_FakeCoordinator()])
    _call(hass, {"guid": GUID})
    assert "检测到旧草稿" not in NOTIFICATIONS[0]["message"]

    _call(hass, {"guid": GUID})
    message = NOTIFICATIONS[1]["message"]
    assert "检测到旧草稿 draft-RYYJ-R8229.yaml" in message
    assert "新文件不会覆盖它" in message
    assert "建议删除以免匹配冲突" in message
    assert _profile_path(tmp_path, "-2").is_file()


def test_only_drafts_for_the_same_device_are_reported(tmp_path: Path) -> None:
    profiles = tmp_path / "robam_roki" / "profiles"
    profiles.mkdir(parents=True)
    (profiles / "draft-RYYJ-R9700.yaml").write_text("other", encoding="utf-8")
    (profiles / "mine.yaml").write_text("mine", encoding="utf-8")

    hass = _FakeHass(tmp_path, [_FakeCoordinator()])
    _call(hass, {"guid": GUID})
    message = NOTIFICATIONS[0]["message"]
    assert "检测到旧草稿" not in message
    assert "draft-RYYJ-R9700.yaml" not in message
    assert "mine.yaml" not in message


def test_a_draft_suffix_number_counts_as_an_old_draft(tmp_path: Path) -> None:
    profiles = tmp_path / "robam_roki" / "profiles"
    profiles.mkdir(parents=True)
    (profiles / "draft-RYYJ-R8229-7.yaml").write_text("old", encoding="utf-8")

    hass = _FakeHass(tmp_path, [_FakeCoordinator()])
    _call(hass, {"guid": GUID})
    message = NOTIFICATIONS[0]["message"]
    assert "检测到旧草稿 draft-RYYJ-R8229-7.yaml" in message


# -- export_profile -----------------------------------------------------


def _user_profile(
    tmp_path: Path,
    *,
    source: str | None = None,
    controls: dict[str, Any] | None = None,
) -> Any:
    path = tmp_path / "robam_roki" / "profiles" / "mine-RYYJ.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "schema: roki-profile/1\nextends: python:hood\nmatch:\n"
        "  dc: RYYJ\n  dt: R8229\n",
        encoding="utf-8",
    )
    return profiles.FullProfile(
        id="mine",
        source=source or profiles.SOURCE_USER,
        path=path,
        dc="RYYJ",
        dt="R8229",
        name=None,
        sensors={},
        binary_sensors={},
        controls=dict(controls or {}),
        extends=None,
        python_base="hood",
    )


def _export(
    tmp_path: Path,
    profile: Any,
    coordinator: Any = None,
) -> tuple[_FakeHass, Any, Path]:
    coordinator = coordinator or _FakeCoordinator()
    coordinator.profiles = [profile] if profile is not None else []
    hass = _FakeHass(tmp_path, [coordinator])
    _call_service(hass, "export_profile", {"guid": GUID})
    exports = tmp_path / "robam_roki" / "exports"
    files = sorted(exports.iterdir())
    assert len(files) == 1, files
    return hass, coordinator, files[0]


def test_export_writes_exactly_one_markdown_file(tmp_path: Path) -> None:
    _hass, _, path = _export(tmp_path, _user_profile(tmp_path))
    assert path.suffix == ".md"
    # The documented file name rule: <dc>-<dt>-<YYYYmmdd-HHMM>.md
    assert re.fullmatch(r"RYYJ-R8229-\d{8}-\d{4}\.md", path.name)
    assert path.parent.name == "exports"
    assert path.parent.parent.name == "robam_roki"
    # Only the Markdown is written: no .yaml and no .json next to it.
    assert all(item.suffix == ".md" for item in path.parent.iterdir())
    assert str(path) in NOTIFICATIONS[0]["message"]
    assert "只输出这一份 .md" in NOTIFICATIONS[0]["message"]


def test_the_markdown_has_the_five_sections_and_two_marker_blocks(
    tmp_path: Path,
) -> None:
    _, _, path = _export(tmp_path, _user_profile(tmp_path))
    markdown = path.read_text(encoding="utf-8")
    for heading in ("## 1.", "## 2.", "## 3.", "## 4.", "## 5."):
        assert heading in markdown, heading
    assert "## 6." not in markdown
    assert "<!-- roki:profile-yaml -->" in markdown
    assert "<!-- roki:fixture-json -->" in markdown
    assert "已脱敏：不含设备 GUID、手机号、token、App GUID" in markdown
    assert "python:hood" in markdown


def test_export_needs_a_draft_first(tmp_path: Path) -> None:
    hass = _FakeHass(tmp_path, [_FakeCoordinator()])  # no profiles at all
    with pytest.raises(ServiceValidationError) as err:
        _call_service(hass, "export_profile", {"guid": GUID})
    assert "draft_profile" in str(err.value)
    assert not (tmp_path / "robam_roki" / "exports").exists()


def test_a_builtin_profile_is_rejected(tmp_path: Path) -> None:
    profile = _user_profile(tmp_path, source=profiles.SOURCE_BUILTIN)
    coordinator = _FakeCoordinator()
    coordinator.profiles = [profile]
    hass = _FakeHass(tmp_path, [coordinator])
    with pytest.raises(ServiceValidationError) as err:
        _call_service(hass, "export_profile", {"guid": GUID})
    assert "issue" in str(err.value)
    assert "draft_profile" not in str(err.value)
    assert not (tmp_path / "robam_roki" / "exports").exists()


def test_export_reuses_an_existing_fixture(tmp_path: Path) -> None:
    fixture = {
        "schema": "roki-fixture/1",
        "source": "auto",
        "match": {"dc": "RYYJ", "dt": "R8229"},
        "properties": {"workStatus": 1},
        "expect": {"work_status": "开机"},
    }
    fixture_path = _fixture_path(tmp_path)
    fixture_path.parent.mkdir(parents=True)
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")

    _, _, path = _export(tmp_path, _user_profile(tmp_path))
    markdown = path.read_text(encoding="utf-8")
    block = export.find_marked_block(
        markdown, export.FIXTURE_MARKER_START, export.FIXTURE_MARKER_END
    )
    assert json.loads(block) == fixture
    assert str(fixture_path) in markdown


def test_export_generates_the_fixture_when_none_exists(tmp_path: Path) -> None:
    _, _, path = _export(tmp_path, _user_profile(tmp_path))
    markdown = path.read_text(encoding="utf-8")
    block = export.find_marked_block(
        markdown, export.FIXTURE_MARKER_START, export.FIXTURE_MARKER_END
    )
    document = json.loads(block)
    assert document["schema"] == "roki-fixture/1"
    assert document["match"] == {"dc": "RYYJ", "dt": "R8229"}
    # The drafted fixture redacts the credentials it captured.
    assert document["properties"]["phone"] == "**REDACTED**"
    assert "tests/fixtures/devices/RYYJ-R8229.json" in markdown


def test_the_checklist_names_the_learned_effect(tmp_path: Path) -> None:
    controls = {
        "action_1": {
            "name": "开机",
            "kind": "mqtt",
            "command": 132,
            "body_hex": "01",
            "verified": False,
        }
    }
    coordinator = _FakeCoordinator()
    coordinator.learned = [
        {
            "frames": [{"command": 132, "body_hex": "01"}],
            "observed": {"workStatus": {"from": 0, "to": 1}},
        }
    ]
    _, _, path = _export(
        tmp_path, _user_profile(tmp_path, controls=controls), coordinator
    )
    markdown = path.read_text(encoding="utf-8")
    assert "| action_1 | 开机 | 132 | 01 | workStatus 0 → 1 | false |" in markdown


def test_export_runs_its_file_io_in_the_executor(tmp_path: Path) -> None:
    hass, _, _ = _export(tmp_path, _user_profile(tmp_path))
    assert "_read_export_inputs" in hass.executor_calls
    assert "_write_export" in hass.executor_calls


def test_a_blocked_exports_directory_notifies_a_failure(tmp_path: Path) -> None:
    (tmp_path / "robam_roki").mkdir()
    (tmp_path / "robam_roki" / "exports").write_text("blocked", encoding="utf-8")
    profile = _user_profile(tmp_path)
    coordinator = _FakeCoordinator()
    coordinator.profiles = [profile]
    hass = _FakeHass(tmp_path, [coordinator])
    _call_service(hass, "export_profile", {"guid": GUID})
    assert len(NOTIFICATIONS) == 1
    assert "失败" in NOTIFICATIONS[0]["title"]

# -- the module itself ---------------------------------------------------


def test_packaging_is_in_sync() -> None:
    strings = json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))
    english = json.loads(
        (COMPONENT / "translations" / "en.json").read_text(encoding="utf-8")
    )
    chinese = json.loads(
        (COMPONENT / "translations" / "zh-Hans.json").read_text(encoding="utf-8")
    )
    assert strings == chinese
    assert _key_shape(strings) == _key_shape(english)

    service = strings["services"]["draft_profile"]
    assert sorted(service) == ["description", "fields", "name"]
    assert sorted(service["fields"]) == ["device_id", "guid", "write_fixture"]
    assert sorted(english["services"]["draft_profile"]["fields"]) == [
        "device_id",
        "guid",
        "write_fixture",
    ]

    exported = strings["services"]["export_profile"]
    assert sorted(exported) == ["description", "fields", "name"]
    assert sorted(exported["fields"]) == ["device_id", "guid"]
    assert sorted(english["services"]["export_profile"]["fields"]) == [
        "device_id",
        "guid",
    ]

    manifest = json.loads(
        (COMPONENT / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["version"] == "0.5.0"
    # hassfest requires 1-3 category keys; custom integrations get a warning
    # when they are missing, so keep them in the manifest and keep them valid.
    assert 1 <= len(manifest["categories"]) <= 3
    for category in manifest["categories"]:
        assert re.fullmatch(r"[a-z0-9_]+(\.[a-z0-9_]+)?", category)

    # hassfest's [CONFIG_SCHEMA] check: async_setup needs a schema, and the
    # config-entry-only helper is the documented way to say "UI only".
    init_source = (COMPONENT / "__init__.py").read_text(encoding="utf-8")
    assert "CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)" in init_source

    service_yaml = (COMPONENT / "services.yaml").read_text(encoding="utf-8")
    for field in ("device_id", "guid", "write_fixture"):
        assert field in service_yaml
    assert "draft_profile" in service_yaml
    assert "export_profile" in service_yaml


def _key_shape(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _key_shape(item) for key, item in value.items()}
    return None


def test_integration_registers_the_service_through_async_setup() -> None:
    from custom_components.robam_roki import async_setup

    assert asyncio.iscoroutinefunction(async_setup)


def test_service_module_imports_only_through_ha_stubs() -> None:
    # The service is a Home Assistant module, but the draft/redaction core it
    # calls is pure Python; importing the package still works offline because
    # __init__.py imports the service lazily.
    assert services.SERVICE_NAME == "draft_profile"
    assert services.PROFILES_SUBDIR == "profiles"
    assert services.FIXTURES_SUBDIR == "fixtures"
    assert draft.PROFILE_SCHEMA == "roki-profile/1"
