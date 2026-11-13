"""Learning mode: passively record the official App's frames for one device.

Real-device evidence: the App sends commands to the appliance and HA sees them
on the same MQTT broker.  Learning mode watches those frames without sending
anything, groups them by idle gaps and diffs the property snapshots so the
draft can later offer kind=mqtt controls with an observed effect.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

COMPONENT = (
    Path(__file__).resolve().parents[1] / "custom_components" / "robam_roki"
)
PACKAGE = "roki_learn_pkg_test"

APP_GUID = "RKDRD000000000000"
OTHER_APP_GUID = "RKDRD111111111111"
GUID = "R8229000000000000"
OTHER_GUID = "R8229000000000001"
DEVICE = {
    "guid": GUID,
    "name": "油烟机8229",
    "dt": "R8229",
    "deviceSource": 0,
}
OTHER_DEVICE = dict(DEVICE, guid=OTHER_GUID, name="油烟机另一台")


# -- minimal Home Assistant tree, built on top of conftest's stubs -------


def _module(name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__path__ = []
    return module


NOTIFICATIONS: list[dict[str, Any]] = []


def _install_stubs() -> None:
    ha = sys.modules.get("homeassistant") or _module("homeassistant")
    sys.modules["homeassistant"] = ha
    ha.__path__ = getattr(ha, "__path__", [])
    components = sys.modules.get("homeassistant.components") or _module(
        "homeassistant.components"
    )
    sys.modules["homeassistant.components"] = components
    ha.components = components
    notifications = _module(
        "homeassistant.components.persistent_notification"
    )

    def async_create(hass: Any, message: str, title: str | None = None, **kwargs: Any) -> None:
        NOTIFICATIONS.append({"message": message, "title": title})

    notifications.async_create = async_create
    sys.modules[
        "homeassistant.components.persistent_notification"
    ] = notifications
    components.persistent_notification = notifications

    core = sys.modules.get("homeassistant.core") or _module("homeassistant.core")
    sys.modules["homeassistant.core"] = core
    ha.core = core
    exceptions = sys.modules.get("homeassistant.exceptions") or _module(
        "homeassistant.exceptions"
    )
    sys.modules["homeassistant.exceptions"] = exceptions
    ha.exceptions = exceptions
    if not hasattr(exceptions, "ConfigEntryAuthFailed"):
        exceptions.ConfigEntryAuthFailed = type(
            "ConfigEntryAuthFailed", (Exception,), {}
        )
    helpers = sys.modules.get("homeassistant.helpers") or _module(
        "homeassistant.helpers"
    )
    sys.modules["homeassistant.helpers"] = helpers
    ha.helpers = helpers


def _load_package_module(name: str, relative: str) -> types.ModuleType:
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
api = _load_package_module("api", "api.py")
const = _load_package_module("const", "const.py")
mqtt = _load_package_module("mqtt", "mqtt.py")
coordinator_module = _load_package_module("coordinator", "coordinator.py")

RokiCoordinator = coordinator_module.RokiCoordinator
RokiApiError = api.RokiApiError


class _RecordingMqtt:
    app_guid = APP_GUID

    def __init__(self) -> None:
        self.published: list[bytes] = []

    async def async_publish(self, topic: str, payload: bytes) -> None:
        self.published.append(payload)

    async def async_stop(self) -> None:
        pass


class _FakeClient:
    user_id = "123456"
    app_guid = APP_GUID


class _FakeHass:
    def __init__(self) -> None:
        self.delayed: list[tuple[float, Any]] = []
        self.cancelled = 0
        self.tasks: list[asyncio.Task[Any]] = []

    def async_call_later(self, delay: float, action: Any) -> Any:
        self.delayed.append((delay, action))

        def _cancel() -> None:
            self.cancelled += 1

        return _cancel

    def async_create_task(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coro)
        self.tasks.append(task)
        return task

    async def drain(self) -> None:
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
            self.tasks.clear()


def _ensure_notifications() -> None:
    """Re-register our recorder if another test module replaced the stub."""
    components = sys.modules["homeassistant.components"]
    notifications = sys.modules.get(
        "homeassistant.components.persistent_notification"
    )
    if notifications is None or getattr(
        notifications, "async_create", None
    ) is None:
        notifications = _module(
            "homeassistant.components.persistent_notification"
        )
    sys.modules[
        "homeassistant.components.persistent_notification"
    ] = notifications

    def async_create(
        hass: Any, message: str, title: str | None = None, **kwargs: Any
    ) -> None:
        NOTIFICATIONS.append({"message": message, "title": title})

    notifications.async_create = async_create
    components.persistent_notification = notifications


def _make_coordinator(
    data: dict[str, dict[str, Any]] | None = None,
) -> Any:
    _ensure_notifications()
    instance = object.__new__(RokiCoordinator)
    instance.entry = types.SimpleNamespace(
        options={}, data={}, entry_id="test"
    )
    instance.client = _FakeClient()
    instance.devices = [dict(DEVICE), dict(OTHER_DEVICE)]
    instance.update_interval = timedelta(seconds=30)
    instance.data = data if data is not None else {GUID: {}}
    instance._mqtt_connected = True
    instance._mqtt = _RecordingMqtt()
    instance.hass = _FakeHass()
    instance.enable_sse = True
    instance._sse_task = None
    instance._mqtt_task = None
    instance._stopping = False
    return instance


def _frame(source: str, command: int, body: bytes = b"") -> bytes:
    assert len(source) == 17
    return source.encode("ascii") + bytes([command]) + body


def _topic(device_id: str = GUID) -> str:
    return mqtt.topic_for(device_id)


async def _stop(coordinator: Any) -> dict[str, Any]:
    result = await coordinator.learn_stop()
    assert coordinator.learn_active() is False
    return result


@pytest.fixture(autouse=True)
def _clear_notifications() -> Any:
    NOTIFICATIONS.clear()
    yield
    NOTIFICATIONS.clear()


# -- frame filtering -----------------------------------------------------


def test_device_reports_are_ignored() -> None:
    async def scenario() -> None:
        coordinator = _make_coordinator()
        coordinator.learn_start(GUID, 60)
        coordinator._handle_mqtt_message(_topic(), _frame(GUID, 131))
        result = await _stop(coordinator)
        assert result["actions"] == []

    asyncio.run(scenario())


def test_our_own_app_guid_is_ignored() -> None:
    async def scenario() -> None:
        coordinator = _make_coordinator()
        coordinator.learn_start(GUID, 60)
        coordinator._handle_mqtt_message(
            _topic(), _frame(APP_GUID, 132, b"\x01")
        )
        result = await _stop(coordinator)
        assert result["actions"] == []

    asyncio.run(scenario())


def test_frames_from_another_controller_are_recorded() -> None:
    async def scenario() -> None:
        coordinator = _make_coordinator()
        coordinator.learn_start(GUID, 60)
        coordinator._handle_mqtt_message(
            _topic(), _frame(OTHER_APP_GUID, 132, b"\x01")
        )
        result = await _stop(coordinator)
        assert result["device_id"] == GUID
        assert result["actions"] == [
            {
                "index": 1,
                "command": 132,
                "body_hex": "01",
                "observed": {},
                "truncated": False,
            }
        ]

    asyncio.run(scenario())


def test_frames_for_another_device_are_ignored() -> None:
    async def scenario() -> None:
        coordinator = _make_coordinator()
        coordinator.learn_start(GUID, 60)
        coordinator._handle_mqtt_message(
            _topic(OTHER_GUID), _frame(OTHER_APP_GUID, 132, b"\x01")
        )
        result = await _stop(coordinator)
        assert result["actions"] == []

    asyncio.run(scenario())


def test_malformed_payloads_are_ignored() -> None:
    async def scenario() -> None:
        coordinator = _make_coordinator()
        coordinator.learn_start(GUID, 60)
        coordinator._handle_mqtt_message(_topic(), b"short")
        coordinator._handle_mqtt_message(_topic(), b"\xff" * 20)
        result = await _stop(coordinator)
        assert result["actions"] == []

    asyncio.run(scenario())


# -- grouping and observed effects --------------------------------------


def test_groups_split_on_the_idle_gap_and_attach_the_observed_change() -> None:
    async def scenario() -> None:
        coordinator = _make_coordinator({GUID: {"workStatus": 0}})
        clock = [100.0]
        original = coordinator_module.time.monotonic
        coordinator_module.time.monotonic = lambda: clock[0]
        try:
            coordinator.learn_start(GUID, 60)

            clock[0] += 1.0
            coordinator._handle_mqtt_message(
                _topic(), _frame(OTHER_APP_GUID, 132, b"\x01")
            )
            clock[0] += 1.0
            coordinator._handle_mqtt_message(
                _topic(), _frame(OTHER_APP_GUID, 134, b"\x02")
            )
            coordinator.data[GUID] = {"workStatus": 1}

            clock[0] += 5.0  # gap > 2.5 s: the first group closes here
            coordinator._handle_mqtt_message(
                _topic(), _frame(OTHER_APP_GUID, 136, b"\x01")
            )
            result = await _stop(coordinator)
        finally:
            coordinator_module.time.monotonic = original

        assert [action["command"] for action in result["actions"]] == [
            132,
            134,
            136,
        ]
        observed = {"workStatus": {"from": 0, "to": 1}}
        assert result["actions"][0]["observed"] == observed
        assert result["actions"][1]["observed"] == observed
        assert result["actions"][2]["observed"] == {}

    asyncio.run(scenario())


def test_a_long_body_is_truncated_and_marked() -> None:
    async def scenario() -> None:
        coordinator = _make_coordinator()
        coordinator.learn_start(GUID, 60)
        coordinator._handle_mqtt_message(
            _topic(), _frame(OTHER_APP_GUID, 140, b"\xaa" * 70)
        )
        result = await _stop(coordinator)
        action = result["actions"][0]
        assert action["truncated"] is True
        assert action["body_hex"] == "aa" * const.LEARN_MAX_BODY_BYTES

    asyncio.run(scenario())


def test_at_most_fifty_groups_are_recorded() -> None:
    async def scenario() -> None:
        coordinator = _make_coordinator()
        clock = [0.0]
        original = coordinator_module.time.monotonic
        coordinator_module.time.monotonic = lambda: clock[0]
        try:
            coordinator.learn_start(GUID, 600)
            for _ in range(60):
                clock[0] += 3.0
                coordinator._handle_mqtt_message(
                    _topic(), _frame(OTHER_APP_GUID, 132, b"\x01")
                )
            result = await _stop(coordinator)
        finally:
            coordinator_module.time.monotonic = original
        assert len(result["actions"]) == const.LEARN_MAX_ACTIONS

    asyncio.run(scenario())


# -- session lifecycle ---------------------------------------------------


def test_concurrent_learning_is_refused() -> None:
    async def scenario() -> None:
        coordinator = _make_coordinator()
        coordinator.learn_start(GUID, 60)
        with pytest.raises(RokiApiError):
            coordinator.learn_start(OTHER_GUID, 60)
        await _stop(coordinator)
        coordinator.learn_start(OTHER_GUID, 60)  # allowed again after stop

    asyncio.run(scenario())


def test_the_duration_is_clamped_to_ten_minutes() -> None:
    async def scenario() -> None:
        coordinator = _make_coordinator()
        result = coordinator.learn_start(GUID, 9999)
        started = coordinator._learn["started_at"]
        deadline = coordinator._learn["deadline"]
        assert deadline - started == timedelta(seconds=600)
        assert "expires_at" in result
        await coordinator.learn_stop()

    asyncio.run(scenario())


def test_the_timeout_callback_stops_the_session() -> None:
    async def scenario() -> None:
        coordinator = _make_coordinator()
        coordinator.learn_start(GUID, 60)
        delay, action = coordinator.hass.delayed[-1]
        assert delay == 60
        assert asyncio.iscoroutinefunction(action)
        await action(None)
        assert coordinator.learn_active() is False
        # The end notification explains the empty result and its fallback.
        message = NOTIFICATIONS[-1]["message"]
        assert "没有记录到任何动作" in message
        assert "roki_cli.py collect" in message

    asyncio.run(scenario())


def test_a_reload_clears_the_buffer() -> None:
    async def scenario() -> None:
        coordinator = _make_coordinator()
        coordinator.learn_start(GUID, 60)
        assert coordinator.learn_active() is True
        await coordinator.async_stop_push()
        assert coordinator.learn_active() is False
        assert coordinator.hass.cancelled == 1

    asyncio.run(scenario())


def test_stopping_without_a_session_returns_an_empty_result() -> None:
    async def scenario() -> None:
        coordinator = _make_coordinator()
        result = await coordinator.learn_stop()
        assert result == {"device_id": None, "actions": []}

    asyncio.run(scenario())


def test_learning_never_sends_a_command() -> None:
    async def scenario() -> None:
        coordinator = _make_coordinator()
        coordinator.learn_start(GUID, 60)
        coordinator._handle_mqtt_message(
            _topic(), _frame(OTHER_APP_GUID, 132, b"\x01")
        )
        await coordinator.learn_stop()
        assert coordinator._mqtt.published == []

    asyncio.run(scenario())


# -- notifications and privacy ------------------------------------------


def test_notifications_announce_start_and_result() -> None:
    async def scenario() -> None:
        coordinator = _make_coordinator()
        coordinator.learn_start(GUID, 60)
        assert len(NOTIFICATIONS) == 1
        assert "已开始学习" in NOTIFICATIONS[0]["message"]
        assert "油烟机8229" in NOTIFICATIONS[0]["message"]
        assert "不会发送任何命令" in NOTIFICATIONS[0]["message"]

        coordinator._handle_mqtt_message(
            _topic(), _frame(OTHER_APP_GUID, 132, b"\x01")
        )
        result = await _stop(coordinator)
        assert len(NOTIFICATIONS) == 2
        message = NOTIFICATIONS[1]["message"]
        assert "共 1 个动作" in message
        assert "command=132" in message
        assert "body=01" in message
        assert result["actions"][0]["index"] == 1

    asyncio.run(scenario())


def test_learned_groups_are_kept_for_the_drafter() -> None:
    async def scenario() -> None:
        coordinator = _make_coordinator({GUID: {"workStatus": 0}})
        clock = [50.0]
        original = coordinator_module.time.monotonic
        coordinator_module.time.monotonic = lambda: clock[0]
        try:
            coordinator.learn_start(GUID, 60)
            clock[0] += 1.0
            coordinator._handle_mqtt_message(
                _topic(), _frame(OTHER_APP_GUID, 132, b"\x01")
            )
            clock[0] += 1.0
            coordinator._handle_mqtt_message(
                _topic(), _frame(OTHER_APP_GUID, 132, b"\x01")
            )
            coordinator.data[GUID] = {"workStatus": 1}
            clock[0] += 5.0
            coordinator._handle_mqtt_message(
                _topic(), _frame(OTHER_APP_GUID, 134, b"\x02")
            )
            await _stop(coordinator)

            groups = coordinator.learned_controls(GUID)
            assert len(groups) == 2
            assert groups[0]["frames"] == [
                {"command": 132, "body_hex": "01"},
                {"command": 132, "body_hex": "01"},
            ]
            assert groups[0]["observed"] == {
                "workStatus": {"from": 0, "to": 1}
            }
            assert groups[1]["frames"] == [
                {"command": 134, "body_hex": "02"}
            ]
            assert groups[1]["observed"] == {}

            # A reload drops them, as the workflow promises.
            await coordinator.async_stop_push()
            assert coordinator.learned_controls(GUID) == []
        finally:
            coordinator_module.time.monotonic = original

    asyncio.run(scenario())


def test_starting_a_new_session_drops_the_previous_groups() -> None:
    async def scenario() -> None:
        coordinator = _make_coordinator()
        coordinator.learn_start(GUID, 60)
        coordinator._handle_mqtt_message(
            _topic(), _frame(OTHER_APP_GUID, 132, b"\x01")
        )
        await _stop(coordinator)
        assert coordinator.learned_controls(GUID)

        coordinator.learn_start(GUID, 60)
        assert coordinator.learned_controls(GUID) == []
        await coordinator.learn_stop()

    asyncio.run(scenario())


def test_the_app_guid_never_leaves_the_learning_session() -> None:
    async def scenario() -> None:
        coordinator = _make_coordinator()
        coordinator.learn_start(GUID, 60)
        coordinator._handle_mqtt_message(
            _topic(), _frame(OTHER_APP_GUID, 132, b"\x01")
        )
        await _stop(coordinator)
        for notification in NOTIFICATIONS:
            assert OTHER_APP_GUID not in notification["message"]
            assert APP_GUID not in notification["message"]
        assert APP_GUID not in repr(coordinator.__dict__.get("_learn"))
        for action in NOTIFICATIONS:
            assert "source" not in action

    asyncio.run(scenario())
