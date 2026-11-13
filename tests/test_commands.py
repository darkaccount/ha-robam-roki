"""Declarative command executor and the per-capability equivalence proof.

The executor (commands.py) only knows how to send a command.  This test pins
that it produces exactly the frames the coordinator used to send before the
migration: one poll (130) followed by the capability's own frame, byte for
byte, and that REST devices go through the generic property/invoke API.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import sys
import types
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

COMPONENT = (
    Path(__file__).resolve().parents[1] / "custom_components" / "robam_roki"
)
PACKAGE = "roki_commands_pkg_test"

APP_GUID = "RKDRD000000000000"
USER_ID = "123456"
MQTT_GUID = "R8229000000000000"
REST_GUID = "R9700abcdef01234"

MQTT_DEVICE = {
    "guid": MQTT_GUID,
    "name": "油烟机8229",
    "dt": "R8229",
    "deviceSource": 0,
}
REST_DEVICE = {
    "guid": REST_GUID,
    "name": "油烟机9700",
    "dt": "R9700",
    "deviceSource": 1,
}


# -- the pure executor module --------------------------------------------


def _load_commands_standalone() -> types.ModuleType:
    path = COMPONENT / "commands.py"
    spec = importlib.util.spec_from_file_location("roki_commands_pure", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["roki_commands_pure"] = module
    spec.loader.exec_module(module)
    return module


commands = _load_commands_standalone()


class _RecordingTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def async_set_property(self, key: str, value: Any) -> None:
        self.calls.append(("property", key, value))

    async def async_invoke(self, function: str, params: dict[str, Any]) -> None:
        self.calls.append(("invoke", function, dict(params)))

    async def async_send_frame(self, command: int, body: bytes) -> None:
        self.calls.append(("frame", command, body))


def _spec(**kwargs: Any) -> Any:
    return types.SimpleNamespace(
        kind=kwargs.pop("kind"),
        on=kwargs.pop("on", None),
        off=kwargs.pop("off", None),
        function=kwargs.pop("function", None),
        param=kwargs.pop("param", None),
        params=kwargs.pop("params", None),
        command=kwargs.pop("command", None),
        body_hex=kwargs.pop("body_hex", None),
        verified=kwargs.pop("verified", True),
    )


def test_property_commands_send_the_on_and_off_payload() -> None:
    transport = _RecordingTransport()
    spec = _spec(kind="property", on={"working_status": 1}, off={"working_status": 0})
    asyncio.run(commands.execute(spec, transport, on=True))
    asyncio.run(commands.execute(spec, transport, on=False))
    assert transport.calls == [
        ("property", "working_status", 1),
        ("property", "working_status", 0),
    ]


def test_invoke_commands_pass_the_parameter() -> None:
    transport = _RecordingTransport()
    spec = _spec(kind="invoke", function="134", param="level")
    asyncio.run(commands.execute(spec, transport, level=2))
    assert transport.calls == [("invoke", "134", {"level": 2})]


def test_invoke_commands_merge_the_static_params() -> None:
    transport = _RecordingTransport()
    spec = _spec(kind="invoke", function="144", params={"level": 2}, param="minutes")
    asyncio.run(commands.execute(spec, transport, minutes=15))
    assert transport.calls == [("invoke", "144", {"level": 2, "minutes": 15})]


def test_mqtt_commands_send_one_raw_frame() -> None:
    transport = _RecordingTransport()
    spec = _spec(kind="mqtt", command=140, body_hex="7879")
    asyncio.run(commands.execute(spec, transport))
    assert transport.calls == [("frame", 140, b"xy")]


def test_unverified_commands_are_refused_by_default() -> None:
    transport = _RecordingTransport()
    spec = _spec(kind="property", on={"working_status": 1}, verified=False)
    with pytest.raises(commands.UnverifiedCommandError):
        asyncio.run(commands.execute(spec, transport, on=True))
    assert transport.calls == []


def test_unverified_commands_run_when_the_option_is_on() -> None:
    transport = _RecordingTransport()
    spec = _spec(kind="property", on={"working_status": 1}, verified=False)
    asyncio.run(
        commands.execute(spec, transport, allow_unverified=True, on=True)
    )
    assert transport.calls == [("property", "working_status", 1)]


def test_bad_commands_raise_value_errors() -> None:
    transport = _RecordingTransport()
    with pytest.raises(ValueError):
        asyncio.run(commands.execute(_spec(kind="property", on={}), transport))
    with pytest.raises(ValueError):
        asyncio.run(commands.execute(_spec(kind="invoke", function="134", param="level"), transport))
    with pytest.raises(ValueError):
        asyncio.run(commands.execute(_spec(kind="nonsense"), transport))


def test_the_executor_does_not_import_home_assistant() -> None:
    source = (COMPONENT / "commands.py").read_text(encoding="utf-8")
    for line in source.splitlines():
        stripped = line.strip()
        assert not stripped.startswith("import homeassistant")
        assert not stripped.startswith("from homeassistant")


# -- the coordinator equivalence -----------------------------------------


def _install_stubs() -> None:
    """Extend the conftest tree; do not replace it for later test modules."""
    ha = sys.modules.get("homeassistant") or types.ModuleType("homeassistant")
    sys.modules["homeassistant"] = ha
    ha.__path__ = getattr(ha, "__path__", [])


def _load_package_module(name: str, relative: str) -> types.ModuleType:
    package = sys.modules.get(PACKAGE)
    if package is None:
        package = types.ModuleType(PACKAGE)
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
api = _load_package_module("api", "api.py")
const = _load_package_module("const", "const.py")
mqtt = _load_package_module("mqtt", "mqtt.py")
coordinator_module = _load_package_module("coordinator", "coordinator.py")

RokiCoordinator = coordinator_module.RokiCoordinator
RokiApiError = api.RokiApiError


class _FakeClient:
    user_id = USER_ID

    def __init__(self) -> None:
        self.rest_calls: list[tuple[Any, ...]] = []

    async def async_set_properties(
        self, device_id: str, properties: dict[str, Any]
    ) -> dict[str, Any]:
        self.rest_calls.append(("set_properties", device_id, dict(properties)))
        return {}

    async def async_invoke(
        self, device_id: str, function: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        self.rest_calls.append(("invoke", device_id, str(function), dict(params)))
        return {}

    async def async_set_power(self, *args: Any) -> dict[str, Any]:
        raise AssertionError("the executor must use the generic property API")

    async def async_set_level(self, *args: Any) -> dict[str, Any]:
        raise AssertionError("the executor must use the generic invoke API")

    async def async_set_light(self, *args: Any) -> dict[str, Any]:
        raise AssertionError("the executor must use the generic property API")


class _RecordingMqtt:
    app_guid = APP_GUID

    def __init__(self) -> None:
        self.published: list[bytes] = []

    async def async_publish(self, topic: str, payload: bytes) -> None:
        self.published.append(payload)


class _FakeHass:
    def async_create_task(self, coro: Any) -> Any:
        coro.close()
        return types.SimpleNamespace(done=lambda: False)


def _make_coordinator(device: dict[str, Any], client: Any = None) -> Any:
    instance = object.__new__(RokiCoordinator)
    instance.entry = types.SimpleNamespace(options={}, data={}, entry_id="t")
    instance.client = client or _FakeClient()
    instance.devices = [dict(device)]
    instance.update_interval = timedelta(seconds=30)
    instance.data = {}
    instance._mqtt_connected = True
    instance._mqtt = _RecordingMqtt()
    instance.hass = _FakeHass()
    instance.enable_sse = True
    instance._sse_task = None
    instance._mqtt_task = None
    instance._stopping = False
    return instance


def _expected_frames(*commands_: bytes) -> list[bytes]:
    poll = mqtt.encode_fan_command(APP_GUID, mqtt.CMD_FAN_POLL)
    return [poll, *commands_]


def _frames(coordinator: Any) -> list[bytes]:
    return list(coordinator._mqtt.published)


# -- MQTT: byte-for-byte equality with the pre-executor frames -----------


@pytest.mark.parametrize("on", [True, False])
def test_power_frames_match_the_old_bytes(on: bool) -> None:
    coordinator = _make_coordinator(MQTT_DEVICE)
    asyncio.run(coordinator.async_execute(MQTT_GUID, "power", on=on))
    expected = _expected_frames(
        mqtt.encode_fan_command(
            APP_GUID, mqtt.CMD_FAN_STATUS, user_id=USER_ID, on=on
        )
    )
    assert _frames(coordinator) == expected


@pytest.mark.parametrize("level", [1, 2, 3, 6])
def test_level_frames_match_the_old_bytes(level: int) -> None:
    coordinator = _make_coordinator(MQTT_DEVICE)
    asyncio.run(coordinator.async_execute(MQTT_GUID, "level", level=level))
    expected = _expected_frames(
        mqtt.encode_fan_command(
            APP_GUID, mqtt.CMD_FAN_LEVEL, user_id=USER_ID, level=level
        )
    )
    assert _frames(coordinator) == expected


@pytest.mark.parametrize("on", [True, False])
def test_light_frames_match_the_old_bytes(on: bool) -> None:
    coordinator = _make_coordinator(MQTT_DEVICE)
    asyncio.run(coordinator.async_execute(MQTT_GUID, "light", on=on))
    expected = _expected_frames(
        mqtt.encode_fan_command(
            APP_GUID, mqtt.CMD_FAN_LIGHT, user_id=USER_ID, on=on
        )
    )
    assert _frames(coordinator) == expected


def test_timing_still_uses_the_countdown_path_byte_for_byte() -> None:
    coordinator = _make_coordinator(MQTT_DEVICE)
    coordinator.data = {MQTT_GUID: {"workStatus": 1, "level": 1}}
    asyncio.run(
        coordinator.async_execute(
            MQTT_GUID, "timing", minutes=15, level=2
        )
    )
    expected = _expected_frames(
        mqtt.encode_fan_command(
            APP_GUID, mqtt.CMD_FAN_TIME, user_id=USER_ID, level=2, minutes=15
        )
    )
    assert _frames(coordinator) == expected
    # The countdown policy came along untouched.
    assert coordinator.countdown_active(MQTT_GUID) is True
    assert coordinator.countdown_remaining(MQTT_GUID) == 15


def test_timing_delegation_keeps_the_rest_refusal() -> None:
    coordinator = _make_coordinator(REST_DEVICE)
    with pytest.raises(RokiApiError):
        asyncio.run(
            coordinator.async_execute(
                REST_DEVICE["guid"], "timing", minutes=15, level=2
            )
        )
    assert coordinator.client.rest_calls == []


# -- the thin wrappers produce exactly the executor frames ---------------


def test_wrappers_match_the_executor_for_power_level_and_light() -> None:
    for capability, kwargs, wrapper in (
        ("power", {"on": True}, "async_set_power"),
        ("level", {"level": 2}, "async_set_level"),
        ("light", {"on": False}, "async_set_light"),
    ):
        executor = _make_coordinator(MQTT_DEVICE)
        asyncio.run(
            executor.async_execute(MQTT_GUID, capability, **kwargs)
        )
        wrapper_coordinator = _make_coordinator(MQTT_DEVICE)
        asyncio.run(getattr(wrapper_coordinator, wrapper)(MQTT_GUID, **kwargs))
        assert _frames(wrapper_coordinator) == _frames(executor), capability


# -- REST: the generic property and invoke API ---------------------------


def test_rest_power_uses_the_property_api() -> None:
    coordinator = _make_coordinator(REST_DEVICE)
    asyncio.run(coordinator.async_execute(REST_DEVICE["guid"], "power", on=True))
    asyncio.run(coordinator.async_execute(REST_DEVICE["guid"], "power", on=False))
    assert coordinator.client.rest_calls == [
        ("set_properties", REST_GUID, {"working_status": 1}),
        ("set_properties", REST_GUID, {"working_status": 0}),
    ]


def test_rest_light_uses_the_property_api() -> None:
    coordinator = _make_coordinator(REST_DEVICE)
    asyncio.run(coordinator.async_execute(REST_DEVICE["guid"], "light", on=True))
    assert coordinator.client.rest_calls == [
        ("set_properties", REST_GUID, {"light_status": 1})
    ]


def test_rest_level_uses_the_invoke_api() -> None:
    coordinator = _make_coordinator(REST_DEVICE)
    asyncio.run(coordinator.async_execute(REST_DEVICE["guid"], "level", level=3))
    assert coordinator.client.rest_calls == [
        ("invoke", REST_GUID, "134", {"level": 3})
    ]


# -- raw MQTT frames (kind=mqtt) ----------------------------------------


def test_raw_frame_layout_is_source_command_terminal_uid_body() -> None:
    coordinator = _make_coordinator(MQTT_DEVICE)
    transport = coordinator_module._DeviceTransport(coordinator, MQTT_GUID)
    asyncio.run(transport.async_send_frame(140, b"\x01\x02"))
    expected = (
        APP_GUID.encode("ascii")          # 17-byte source (app) guid
        + bytes([140])                    # command
        + bytes([mqtt.TERMINAL_TYPE_APP])  # terminal type
        + USER_ID.encode("utf-8").ljust(10, b"\x00")  # 10-byte user id
        + b"\x01\x02"                    # declarative body
    )
    assert _frames(coordinator) == [expected]


def test_raw_frames_are_refused_on_rest_devices() -> None:
    coordinator = _make_coordinator(REST_DEVICE)
    transport = coordinator_module._DeviceTransport(
        coordinator, REST_DEVICE["guid"]
    )
    with pytest.raises(RokiApiError):
        asyncio.run(transport.async_send_frame(140, b""))


def test_the_coordinator_also_refuses_unverified_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec(
        kind="property",
        on={"working_status": 1},
        off={"working_status": 0},
        verified=False,
    )

    class _Driver:
        def commands(self) -> dict[str, Any]:
            return {"power": spec}

    devices_module = importlib.import_module(f"{PACKAGE}.devices")
    monkeypatch.setattr(
        devices_module, "get_driver_for_device", lambda *_: _Driver()
    )
    coordinator = _make_coordinator(MQTT_DEVICE)
    with pytest.raises(RokiApiError):
        asyncio.run(coordinator.async_execute(MQTT_GUID, "power", on=True))
    assert _frames(coordinator) == []

    monkeypatch.setattr(coordinator, "allow_unverified_controls", lambda: True)
    asyncio.run(coordinator.async_execute(MQTT_GUID, "power", on=True))
    assert len(_frames(coordinator)) == 2


# -- capability gating ---------------------------------------------------


def test_an_unknown_capability_is_refused() -> None:
    coordinator = _make_coordinator(MQTT_DEVICE)
    with pytest.raises(RokiApiError):
        asyncio.run(
            coordinator.async_execute(MQTT_GUID, "not-a-capability", on=True)
        )
    assert _frames(coordinator) == []
