"""Tests for the Home Assistant draft_profile service (P2-C).

The integration package is loaded in an isolated package with the handful of
Home Assistant symbols the service needs stubbed out, so the whole service can
run offline against a fake coordinator.
"""

from __future__ import annotations

import asyncio
import importlib
import json
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

    def async_register(self, domain, name, handler, schema=None) -> None:
        self.registered[(domain, name)] = {"handler": handler, "schema": schema}


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
    asyncio.run(services.async_setup_services(hass))
    handler = hass.services.registered[(DOMAIN, "draft_profile")]["handler"]
    asyncio.run(handler(_Call(data)))


@pytest.fixture(autouse=True)
def _clear_notifications() -> Any:
    NOTIFICATIONS.clear()
    yield
    NOTIFICATIONS.clear()


# -- registration and validation ----------------------------------------


def test_service_registers_only_once(tmp_path: Path) -> None:
    hass = _FakeHass(tmp_path, [_FakeCoordinator()])
    asyncio.run(services.async_setup_services(hass))
    asyncio.run(services.async_setup_services(hass))
    assert list(hass.services.registered) == [(DOMAIN, "draft_profile")]


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
    assert "controls 段为注释" in message
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
    assert "level" in document["sensors"]
    assert "已继承内置驱动 python:hood" in NOTIFICATIONS[0]["message"]


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

    manifest = json.loads(
        (COMPONENT / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["version"] == "0.4.0"

    service_yaml = (COMPONENT / "services.yaml").read_text(encoding="utf-8")
    for field in ("device_id", "guid", "write_fixture"):
        assert field in service_yaml
    assert "draft_profile" in service_yaml


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
