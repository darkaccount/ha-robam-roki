"""Online-state decision tests for the coordinator, plus the countdown command.

The coordinator imports ``homeassistant.helpers.update_coordinator``, which is
not available outside a Home Assistant install, so the module is loaded with
those symbols stubbed.  What is under test is the *decision*: which transport
answers, what "unknown" means, and that losing our own socket is not reported
as the hood being offline -- and, for the countdown, that command 144 carries
the gear the hood is actually running on.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import time
import types
from pathlib import Path
from typing import Any

import pytest

COMPONENT = (
    Path(__file__).resolve().parents[1] / "custom_components" / "robam_roki"
)
PACKAGE = "roki_coordinator_pkg_test"

MQTT_GUID = "R8229000000000000"
REST_GUID = "R9700abcdef01234"


class _Stub:
    def __init__(self, name: str = "stub") -> None:
        self._name = name

    def __getattr__(self, item: str) -> Any:
        value = _Stub(f"{self._name}.{item}")
        setattr(self, item, value)
        return value

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return _Stub(f"{self._name}()")


def _module(name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__path__ = []
    return module


def _install_stubs() -> None:
    """Stub the Home Assistant symbols coordinator.py imports at module level."""
    ha = _module("homeassistant")
    config_entries = _module("homeassistant.config_entries")

    class ConfigEntry:
        pass

    config_entries.ConfigEntry = ConfigEntry
    ha.config_entries = config_entries

    core = _module("homeassistant.core")
    core.HomeAssistant = _Stub("HomeAssistant")
    ha.core = core

    exceptions = _module("homeassistant.exceptions")

    class ConfigEntryAuthFailed(Exception):
        pass

    exceptions.ConfigEntryAuthFailed = ConfigEntryAuthFailed
    ha.exceptions = exceptions

    helpers = _module("homeassistant.helpers")
    update_coordinator = _module("homeassistant.helpers.update_coordinator")

    class DataUpdateCoordinator:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.data = None
            self.name = kwargs.get("name")
            self.update_interval = kwargs.get("update_interval")

        # The real base class is Generic[...]; the coordinator subscripts it.
        def __class_getitem__(cls, item: Any) -> type:
            return cls

        # The real one notifies listeners too; the integration only reads
        # ``self.data`` afterwards, so storing it is enough here.
        def async_set_updated_data(self, data: Any) -> None:
            self.data = data

    class UpdateFailed(Exception):
        pass

    update_coordinator.DataUpdateCoordinator = DataUpdateCoordinator
    update_coordinator.UpdateFailed = UpdateFailed
    helpers.update_coordinator = update_coordinator

    sys.modules.update(
        {
            "homeassistant": ha,
            "homeassistant.config_entries": config_entries,
            "homeassistant.core": core,
            "homeassistant.exceptions": exceptions,
            "homeassistant.helpers": helpers,
            "homeassistant.helpers.update_coordinator": update_coordinator,
        }
    )


def _load_package_module(name: str, relative: str) -> types.ModuleType:
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
api = _load_package_module("api", "api.py")
const = _load_package_module("const", "const.py")
mqtt = _load_package_module("mqtt", "mqtt.py")
coordinator_module = _load_package_module("coordinator", "coordinator.py")

RokiCoordinator = coordinator_module.RokiCoordinator

MQTT_DEVICE = {"guid": MQTT_GUID, "name": "油烟机8229", "dt": "R8229", "deviceSource": 0}
REST_DEVICE = {"guid": REST_GUID, "name": "油烟机9700", "dt": "R9700", "deviceSource": 1}


class _FakeClient:
    user_id = "1234567890"


class _RecordingMqtt:
    """Stands in for the broker session, recording published frames."""

    app_guid = "RKDRD000000000000"

    def __init__(self) -> None:
        self.published: list[bytes] = []

    async def async_publish(self, topic, payload):
        self.published.append(payload)

    async def async_stop(self):
        pass


class _FakeHass:
    """Records task creation so the startup contract can be asserted."""

    def __init__(self) -> None:
        self.background: list[str] = []
        self.foreground = 0

    def async_create_task(self, coro):
        self.foreground += 1
        coro.close()
        return types.SimpleNamespace(done=lambda: False)

    def async_create_background_task(self, coro, name, eager_start=True):
        self.background.append(name)
        coro.close()
        return types.SimpleNamespace(done=lambda: False)


def _make_coordinator(devices: list[dict[str, Any]], scan_interval: int = 30) -> Any:
    """Build a coordinator without running its __init__ side effects."""
    instance = object.__new__(RokiCoordinator)
    instance.entry = types.SimpleNamespace(options={}, data={}, entry_id="test")
    instance.client = _FakeClient()
    instance.devices = devices
    instance.update_interval = __import__("datetime").timedelta(seconds=scan_interval)
    instance._last_seen = {}
    instance._cloud_online = {}
    instance._mqtt_connected = False
    instance.hass = _FakeHass()
    instance.enable_sse = True
    instance._sse_task = None
    instance._mqtt_task = None
    instance._stopping = False
    return instance


# -- MQTT devices -------------------------------------------------------


def test_mqtt_hood_is_online_right_after_a_report():
    instance = _make_coordinator([MQTT_DEVICE])
    instance._mqtt_connected = True
    instance._last_seen[MQTT_GUID] = time.time()
    assert instance.online_state(MQTT_GUID) is True


def test_mqtt_hood_goes_offline_after_the_threshold():
    instance = _make_coordinator([MQTT_DEVICE])
    instance._mqtt_connected = True
    poll_interval = instance._mqtt_poll_interval
    # Just inside the window: still online.
    instance._last_seen[MQTT_GUID] = time.time() - (
        poll_interval * const.OFFLINE_AFTER_POLLS - 1
    )
    assert instance.online_state(MQTT_GUID) is True
    # Just outside: offline.
    instance._last_seen[MQTT_GUID] = time.time() - (
        poll_interval * const.OFFLINE_AFTER_POLLS + 1
    )
    assert instance.online_state(MQTT_GUID) is False


def test_mqtt_hood_that_never_reported_is_unknown():
    instance = _make_coordinator([MQTT_DEVICE])
    instance._mqtt_connected = True
    assert instance.online_state(MQTT_GUID) is None


def test_losing_our_own_socket_is_not_the_hood_going_offline():
    # The regression that matters: a dead broker connection must read as
    # "unknown", not as "the hood is offline".
    instance = _make_coordinator([MQTT_DEVICE])
    instance._mqtt_connected = False
    instance._last_seen[MQTT_GUID] = time.time() - 3600
    assert instance.online_state(MQTT_GUID) is None


def test_mqtt_poll_interval_is_at_least_five_seconds():
    instance = _make_coordinator([MQTT_DEVICE], scan_interval=30)
    assert instance._mqtt_poll_interval == 10.0
    instance.update_interval = __import__("datetime").timedelta(seconds=6)
    assert instance._mqtt_poll_interval == 5.0


# -- REST/SSE devices ---------------------------------------------------


def test_rest_hood_uses_the_cloud_answer():
    instance = _make_coordinator([REST_DEVICE])
    instance._cloud_online[REST_GUID] = 1
    assert instance.online_state(REST_GUID) is True
    instance._cloud_online[REST_GUID] = 0
    assert instance.online_state(REST_GUID) is False


def test_rest_hood_is_unknown_before_the_first_answer():
    instance = _make_coordinator([REST_DEVICE])
    assert instance.online_state(REST_GUID) is None


def test_rest_hood_online_state_does_not_depend_on_the_socket():
    instance = _make_coordinator([REST_DEVICE])
    instance._mqtt_connected = False
    instance._cloud_online[REST_GUID] = 1
    assert instance.online_state(REST_GUID) is True


# -- diagnostics --------------------------------------------------------


def test_details_explain_the_mqtt_decision():
    instance = _make_coordinator([MQTT_DEVICE])
    instance._mqtt_connected = True
    instance._last_seen[MQTT_GUID] = time.time()
    details = instance.online_details(MQTT_GUID)
    assert details["通道"] == "mqtt"
    assert details["MQTT 已连接"] is True
    assert details["最近上报"] is not None
    assert str(const.OFFLINE_AFTER_POLLS) in details["判定"]


def test_details_explain_the_rest_decision():
    instance = _make_coordinator([REST_DEVICE])
    instance._cloud_online[REST_GUID] = 1
    details = instance.online_details(REST_GUID)
    assert details == {"通道": "rest", "云端状态码": 1}


def test_mixed_account_reports_each_device_on_its_own_channel():
    instance = _make_coordinator([MQTT_DEVICE, REST_DEVICE])
    instance._mqtt_connected = True
    instance._last_seen[MQTT_GUID] = time.time()
    instance._cloud_online[REST_GUID] = 0
    assert instance.online_state(MQTT_GUID) is True
    assert instance.online_state(REST_GUID) is False


# -- the countdown (command 144) -----------------------------------------


def _timing_coordinator(level: int = 6) -> Any:
    instance = _make_coordinator([MQTT_DEVICE])
    instance._mqtt = _RecordingMqtt()
    instance.data = {MQTT_GUID: {"workStatus": 1, "level": level}}
    return instance


def _frames(instance) -> list[bytes]:
    return instance._mqtt.published


def _decode_144(payload: bytes) -> tuple[int, int] | None:
    if len(payload) < 31 or payload[17] != mqtt.CMD_FAN_TIME:
        return None
    return payload[29], payload[30]


def test_the_countdown_carries_the_level_the_caller_passed():
    # The hood reports gear and countdown in separate frames, so a level read
    # back from coordinator data right after a gear change can be the previous
    # one.  An explicit level keeps command 144 from dragging the fan back.
    instance = _timing_coordinator(level=2)
    asyncio.run(instance.async_set_timing(MQTT_GUID, 30, level=6))
    frame = next(p for p in _frames(instance) if _decode_144(p))
    assert _decode_144(frame) == (6, 30)


def test_the_countdown_falls_back_to_the_reported_level():
    instance = _timing_coordinator(level=3)
    asyncio.run(instance.async_set_timing(MQTT_GUID, 15))
    frame = next(p for p in _frames(instance) if _decode_144(p))
    assert _decode_144(frame) == (3, 15)


def test_the_countdown_is_refused_while_the_hood_is_off():
    # Command 144 restarts the fan on the level it carries; with no level there
    # is nothing honest to send.
    instance = _timing_coordinator(level=0)
    with pytest.raises(coordinator_module.RokiApiError):
        asyncio.run(instance.async_set_timing(MQTT_GUID, 30, level=0))
    assert _frames(instance) == []


def test_the_countdown_clamps_to_the_app_range():
    instance = _timing_coordinator(level=2)
    asyncio.run(instance.async_set_timing(MQTT_GUID, 999))
    frame = next(p for p in _frames(instance) if _decode_144(p))
    assert _decode_144(frame) == (2, const.MAX_COUNTDOWN_MINUTES)


def test_the_countdown_rejects_a_value_the_byte_cannot_hold():
    # The coordinator clamps to the offered range; anything past the protocol's
    # own 255 must not be silently truncated by the encoder.
    instance = _timing_coordinator(level=2)
    with pytest.raises(ValueError):
        mqtt.encode_fan_command(
            instance._mqtt.app_guid, mqtt.CMD_FAN_TIME, level=2, minutes=300
        )


def test_the_countdown_polls_before_the_command():
    # AbsFan sends 130 before every command; the hood answers 131 and only then
    # accepts the 144.
    instance = _timing_coordinator(level=2)
    asyncio.run(instance.async_set_timing(MQTT_GUID, 10))
    commands = [p[17] for p in _frames(instance)]
    assert commands == [mqtt.CMD_FAN_POLL, mqtt.CMD_FAN_TIME]


def test_the_countdown_updates_the_optimistic_state():
    instance = _timing_coordinator(level=2)
    asyncio.run(instance.async_set_timing(MQTT_GUID, 45))
    assert instance.data[MQTT_GUID]["timing"] == 45


def test_the_countdown_is_refused_for_rest_devices():
    # The app calls set_air_fresh on IoT devices, which is not a countdown.
    instance = _make_coordinator([REST_DEVICE])
    instance._mqtt = _RecordingMqtt()
    with pytest.raises(coordinator_module.RokiApiError):
        asyncio.run(instance.async_set_timing(REST_GUID, 30))


# -- startup contract ----------------------------------------------------


def test_start_push_tracks_long_running_loops_as_background_tasks():
    # Regression: these loops never finish, so tracking them as regular tasks
    # made Home Assistant's bootstrap wait ~5 minutes for them on every start.
    instance = _make_coordinator([MQTT_DEVICE, REST_DEVICE])
    instance.async_start_push()
    assert "robam_roki sse" in instance.hass.background
    assert "robam_roki mqtt" in instance.hass.background
    assert instance.hass.foreground == 0


def test_start_push_is_idempotent():
    instance = _make_coordinator([MQTT_DEVICE, REST_DEVICE])
    instance.async_start_push()
    instance.async_start_push()
    assert instance.hass.background.count("robam_roki sse") == 1
    assert instance.hass.background.count("robam_roki mqtt") == 1


def test_coordinator_name_masks_the_account_phone():
    instance = RokiCoordinator(
        _FakeHass(),
        types.SimpleNamespace(title="13800000000"),
        _FakeClient(),
        [MQTT_DEVICE],
    )
    assert instance.name == "robam_roki 138****0000"
    assert "13800000000" not in instance.name
