"""Countdown auto-off: the local deadline is the single source of truth.

Real-device evidence (tmp/activity.csv): command 144 only arms a timed run, the
appliance beeps at the end and keeps running, and the app itself sends the off
command.  The countdown also bounced (a stale 0 within 280 ms, a lost 2-minute
value, a transient empty snapshot).  These tests pin the behaviour that fixes
all of it, with a controllable clock and a fake event helper instead of real
minutes.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import time
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

COMPONENT = (
    Path(__file__).resolve().parents[1] / "custom_components" / "robam_roki"
)
PACKAGE = "roki_countdown_pkg_test"

GUID = "R8229000000000000"
DEVICE = {
    "guid": GUID,
    "name": "油烟机8229",
    "dt": "R8229",
    "deviceSource": 0,
}


class _Scheduler:
    """Captures async_track_point_in_utc_time and fires due callbacks."""

    def __init__(self) -> None:
        self.pending: list[dict[str, Any]] = []
        self.tasks: list[asyncio.Task[Any]] = []

    def track(self, hass: Any, action: Any, when: datetime) -> Any:
        entry = {"action": action, "when": when, "cancelled": False}
        self.pending.append(entry)

        def _unsub() -> None:
            entry["cancelled"] = True

        return _unsub

    def fire_due(self, now: datetime) -> None:
        for entry in list(self.pending):
            if entry["cancelled"] or entry["when"] > now:
                continue
            self.pending.remove(entry)
            result = entry["action"](now)
            if asyncio.iscoroutine(result):
                self.tasks.append(asyncio.ensure_future(result))



    async def drain(self) -> None:
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
            self.tasks.clear()
scheduler = _Scheduler()


def _module(name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__path__ = []
    return module


def _install_stubs() -> None:
    # Extend the permissive tree installed by conftest instead of replacing it:
    # modules collected after this one still import the real package.
    ha = sys.modules.get("homeassistant") or _module("homeassistant")
    sys.modules["homeassistant"] = ha
    ha.__path__ = getattr(ha, "__path__", [])

    components = sys.modules.get("homeassistant.components") or _module(
        "homeassistant.components"
    )
    sys.modules["homeassistant.components"] = components
    ha.components = components

    core = sys.modules.get("homeassistant.core") or _module("homeassistant.core")
    sys.modules["homeassistant.core"] = core
    ha.core = core
    if not hasattr(core, "HomeAssistant"):
        core.HomeAssistant = type("HomeAssistant", (), {})

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

    event = sys.modules.get("homeassistant.helpers.event") or _module(
        "homeassistant.helpers.event"
    )
    sys.modules["homeassistant.helpers.event"] = event
    helpers.event = event
    event.async_track_point_in_utc_time = scheduler.track


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
RokiError = api.RokiError


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, minutes: float) -> datetime:
        self.now += timedelta(minutes=minutes)
        return self.now


class _RecordingMqtt:
    app_guid = "RKDRD000000000000"

    def __init__(self) -> None:
        self.published: list[bytes] = []

    async def async_publish(self, topic: str, payload: bytes) -> None:
        self.published.append(payload)


class _FakeHass:
    def __init__(self) -> None:
        self.tasks: list[asyncio.Task[Any]] = []

    def async_create_task(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coro)
        self.tasks.append(task)
        return task

    async def drain(self) -> None:
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
            self.tasks.clear()


def _ensure_event_stub() -> types.ModuleType:
    """(Re)install the event helper capture for this test module.

    Other test modules replace parts of the shared homeassistant stub tree while
    they are imported; re-registering here keeps the lazy import inside the
    coordinator pointing at the scheduler.
    """
    event = sys.modules.get("homeassistant.helpers.event") or _module(
        "homeassistant.helpers.event"
    )
    sys.modules["homeassistant.helpers.event"] = event
    event.async_track_point_in_utc_time = scheduler.track
    helpers = sys.modules.get("homeassistant.helpers")
    if helpers is not None:
        helpers.event = event
    return event


def _make_coordinator(options: dict[str, Any] | None = None) -> Any:
    _ensure_event_stub()
    instance = object.__new__(RokiCoordinator)
    instance.entry = types.SimpleNamespace(
        options=dict(options or {}), data={}, entry_id="test"
    )
    instance.client = types.SimpleNamespace(user_id="1")
    instance.devices = [dict(DEVICE)]
    instance.update_interval = timedelta(seconds=30)
    instance._last_seen = {}
    instance._cloud_online = {}
    instance._mqtt_connected = True
    instance._mqtt = _RecordingMqtt()
    instance.hass = _FakeHass()
    instance.enable_sse = True
    instance._sse_task = None
    instance._mqtt_task = None
    instance._stopping = False
    instance.data = {}
    instance.clock = _Clock()
    instance._now = instance.clock
    scheduler.pending.clear()
    return instance


def _off_frames(coordinator: Any) -> list[bytes]:
    return [
        frame
        for frame in coordinator._mqtt.published
        if frame[17] == mqtt.CMD_FAN_STATUS and frame[-1] == 0
    ]



def _level_frames(coordinator: Any) -> list[bytes]:
    return [
        frame
        for frame in coordinator._mqtt.published
        if frame[17] == mqtt.CMD_FAN_LEVEL
    ]
async def _expire_grace(coordinator: Any) -> None:
    state = coordinator._state("_countdowns").get(GUID)
    assert state is not None
    state.command_sent_at = time.monotonic() - (
        const.COUNTDOWN_COMMAND_GRACE_SECONDS + 5
    )


# -- scheduling and cancel -------------------------------------------------


def test_a_gear_change_during_a_countdown_reasserts_the_gear() -> None:
    async def scenario() -> None:
        coordinator = _make_coordinator()
        coordinator.data[GUID] = {"workStatus": 1, "level": 1, "timing": 5}
        await coordinator.async_set_timing(GUID, 5, level=1)
        assert _level_frames(coordinator) == []

        # The user picks a higher gear right after arming the timer; the
        # report still shows the old gear, which is what the timer command
        # would otherwise drag back.
        previous = coordinator_module.COUNTDOWN_LEVEL_REASSERT_SECONDS
        coordinator_module.COUNTDOWN_LEVEL_REASSERT_SECONDS = 0
        try:
            await coordinator.async_set_level(GUID, 2)
            await coordinator.hass.drain()
        finally:
            coordinator_module.COUNTDOWN_LEVEL_REASSERT_SECONDS = previous

        frames = _level_frames(coordinator)
        assert len(frames) == 2, frames
        assert frames[0][-1] == 2 and frames[1][-1] == 2

    asyncio.run(scenario())


def test_a_gear_change_without_a_countdown_is_sent_once() -> None:
    async def scenario() -> None:
        coordinator = _make_coordinator()
        coordinator.data[GUID] = {"workStatus": 1, "level": 1}
        await coordinator.async_set_level(GUID, 2)
        await coordinator.hass.drain()

        assert len(_level_frames(coordinator)) == 1

    asyncio.run(scenario())


def test_powering_off_cancels_the_pending_gear_reassert() -> None:
    async def scenario() -> None:
        coordinator = _make_coordinator()
        coordinator.data[GUID] = {"workStatus": 1, "level": 1, "timing": 5}
        await coordinator.async_set_timing(GUID, 5, level=1)

        previous = coordinator_module.COUNTDOWN_LEVEL_REASSERT_SECONDS
        coordinator_module.COUNTDOWN_LEVEL_REASSERT_SECONDS = 5
        try:
            await coordinator.async_set_level(GUID, 2)
            await coordinator.async_set_power(GUID, False)
            await coordinator.hass.drain()
        finally:
            coordinator_module.COUNTDOWN_LEVEL_REASSERT_SECONDS = previous

        # Only the original gear command survives; the re-assert was dropped
        # with the countdown, so the fan cannot be restarted after the off.
        assert len(_level_frames(coordinator)) == 1

    asyncio.run(scenario())


def test_setting_a_countdown_arms_the_local_deadline() -> None:
    async def scenario() -> None:
        coordinator = _make_coordinator()
        await coordinator.async_set_timing(GUID, 5, level=2)

        assert coordinator.countdown_active(GUID) is True
        assert coordinator.countdown_remaining(GUID) == 5
        assert coordinator.countdown_deadline(GUID) == (
            coordinator.clock.now + timedelta(minutes=5)
        )
        assert len(scheduler.pending) == 1
        assert coordinator.data[GUID]["timing"] == 5

    asyncio.run(scenario())


def test_the_deadline_fires_the_off_command_exactly_once() -> None:
    async def scenario() -> None:
        coordinator = _make_coordinator()
        await coordinator.async_set_timing(GUID, 2, level=2)
        assert scheduler.pending

        coordinator.clock.advance(2)
        scheduler.fire_due(coordinator.clock.now)
        await scheduler.drain()
        await coordinator.hass.drain()

        assert len(_off_frames(coordinator)) == 1
        assert coordinator.countdown_active(GUID) is False

        # Firing again (or a stray callback) must not send a second command.
        scheduler.fire_due(coordinator.clock.now + timedelta(minutes=10))
        await scheduler.drain()
        await coordinator._async_auto_off(GUID)
        await coordinator.hass.drain()
        assert len(_off_frames(coordinator)) == 1

    asyncio.run(scenario())


def test_setting_zero_cancels_the_timer_and_sends_no_off() -> None:
    async def scenario() -> None:
        coordinator = _make_coordinator()
        await coordinator.async_set_timing(GUID, 5, level=2)
        entry = scheduler.pending[0]

        await coordinator.async_set_timing(GUID, 0, level=2)
        assert coordinator.countdown_active(GUID) is False
        assert entry["cancelled"] is True

        coordinator.clock.advance(10)
        scheduler.fire_due(coordinator.clock.now)
        await scheduler.drain()
        await coordinator.hass.drain()
        assert _off_frames(coordinator) == []

    asyncio.run(scenario())


def test_manual_power_off_cancels_the_countdown() -> None:
    async def scenario() -> None:
        coordinator = _make_coordinator()
        await coordinator.async_set_timing(GUID, 5, level=2)
        entry = scheduler.pending[0]

        await coordinator.async_set_power(GUID, False)
        assert coordinator.countdown_active(GUID) is False
        assert entry["cancelled"] is True
        assert len(_off_frames(coordinator)) == 1

    asyncio.run(scenario())


def test_the_option_off_keeps_the_appliance_timer_only() -> None:
    async def scenario() -> None:
        coordinator = _make_coordinator({"countdown_auto_off": False})
        await coordinator.async_set_timing(GUID, 2, level=2)

        assert scheduler.pending == []
        assert coordinator.countdown_remaining(GUID) == 2
        coordinator.clock.advance(3)
        assert coordinator.countdown_remaining(GUID) == 0
        assert _off_frames(coordinator) == []

    asyncio.run(scenario())


def test_restart_rebuilds_a_timer_from_the_cached_timing() -> None:
    async def scenario() -> None:
        coordinator = _make_coordinator()
        coordinator.data[GUID] = {"workStatus": 1, "level": 2, "timing": 7}

        coordinator.restore_countdown(GUID)
        assert coordinator.countdown_active(GUID) is True
        assert coordinator.countdown_remaining(GUID) == 7
        assert len(scheduler.pending) == 1

    asyncio.run(scenario())


# -- reports only confirm --------------------------------------------------


def test_stopping_the_push_loop_drops_pending_timers() -> None:
    async def scenario() -> None:
        coordinator = _make_coordinator()
        await coordinator.async_set_timing(GUID, 5, level=2)
        assert len(scheduler.pending) == 1
        coordinator._mqtt = None

        await coordinator.async_stop_push()

        assert scheduler.pending[0]["cancelled"] is True
        assert coordinator.countdown_active(GUID) is False

    asyncio.run(scenario())


def test_a_stale_zero_right_after_the_command_is_ignored() -> None:
    async def scenario() -> None:
        coordinator = _make_coordinator()
        await coordinator.async_set_timing(GUID, 5, level=2)

        coordinator.observe_report(GUID, {"timing": 0, "workStatus": 1})
        assert coordinator.countdown_remaining(GUID) == 5
        assert coordinator.countdown_active(GUID) is True

    asyncio.run(scenario())


def test_the_display_is_monotonic_and_ignores_a_larger_report() -> None:
    async def scenario() -> None:
        coordinator = _make_coordinator()
        await coordinator.async_set_timing(GUID, 5, level=2)
        await _expire_grace(coordinator)

        coordinator.observe_report(GUID, {"timing": 4})
        assert coordinator.countdown_remaining(GUID) == 4
        coordinator.observe_report(GUID, {"timing": 6})
        assert coordinator.countdown_remaining(GUID) == 4

    asyncio.run(scenario())


def test_two_zero_reports_mean_the_panel_cancelled_it() -> None:
    async def scenario() -> None:
        coordinator = _make_coordinator()
        await coordinator.async_set_timing(GUID, 5, level=2)
        await _expire_grace(coordinator)

        coordinator.observe_report(GUID, {"timing": 0, "workStatus": 1})
        assert coordinator.countdown_active(GUID) is True
        coordinator.observe_report(GUID, {"timing": 0, "workStatus": 1})
        assert coordinator.countdown_active(GUID) is False
        assert _off_frames(coordinator) == []

    asyncio.run(scenario())


def test_the_fallback_needs_two_confirmations() -> None:
    async def scenario() -> None:
        coordinator = _make_coordinator()
        # Simulate a timer we never owned (set on the app, then HA restarted):
        # the last positive timing is remembered but there is no deadline.
        coordinator._state("_last_positive_timing")[GUID] = 3

        coordinator.observe_report(GUID, {"timing": 0, "workStatus": 1})
        await coordinator.hass.drain()
        assert _off_frames(coordinator) == []

        coordinator.observe_report(GUID, {"timing": 0, "workStatus": 1})
        await coordinator.hass.drain()
        assert len(_off_frames(coordinator)) == 1

    asyncio.run(scenario())


def test_the_fallback_is_disabled_by_the_option() -> None:
    async def scenario() -> None:
        coordinator = _make_coordinator({"countdown_auto_off": False})
        coordinator._state("_last_positive_timing")[GUID] = 3
        for _ in range(3):
            coordinator.observe_report(GUID, {"timing": 0, "workStatus": 1})
        await coordinator.hass.drain()
        assert _off_frames(coordinator) == []

    asyncio.run(scenario())


def test_a_running_hood_without_a_known_timer_is_left_alone() -> None:
    async def scenario() -> None:
        coordinator = _make_coordinator()
        for _ in range(3):
            coordinator.observe_report(GUID, {"timing": 0, "workStatus": 1})
        await coordinator.hass.drain()
        assert _off_frames(coordinator) == []

    asyncio.run(scenario())


# -- the short-frame 2 minute decode fix ----------------------------------


def _status_frame(value: int) -> bytes:
    # 131 report: guid(17) + command + workStatus, level, light, clean, value.
    return (
        GUID.encode("ascii")
        + bytes([mqtt.REPORT_FAN_STATUS, 1, 2, 0, 0, value])
    )


def test_a_short_frame_two_is_decoded_as_a_countdown_when_one_runs() -> None:
    async def scenario() -> None:
        coordinator = _make_coordinator()
        seen: list[int] = []
        for value in (3, 2, 1, 0):
            coordinator._handle_mqtt_message(
                mqtt.topic_for(GUID), _status_frame(value)
            )
            seen.append(coordinator.data[GUID]["timing"])
        assert seen == [3, 2, 1, 0]

    asyncio.run(scenario())


def test_the_stateless_decoder_still_maps_the_short_two_to_wifi() -> None:
    # decode_fan_message is deliberately left alone; the coordinator corrects it.
    decoded = mqtt.decode_fan_message(_status_frame(2))
    assert decoded is not None
    _guid, _command, properties = decoded
    assert properties.get("wifi") == 2
    assert "timing" not in properties


def test_a_short_two_without_a_running_countdown_stays_wifi() -> None:
    async def scenario() -> None:
        coordinator = _make_coordinator()
        coordinator._handle_mqtt_message(mqtt.topic_for(GUID), _status_frame(2))
        assert coordinator.data[GUID].get("wifi") == 2
        assert "timing" not in coordinator.data[GUID]

    asyncio.run(scenario())


# -- available debounce ----------------------------------------------------


def test_a_transient_empty_snapshot_keeps_the_last_properties() -> None:
    async def scenario() -> None:
        coordinator = _make_coordinator()
        coordinator.data = {GUID: {"workStatus": 1, "level": 2}}
        coordinator.async_set_updated_data({GUID: {}})
        assert coordinator.data[GUID] == {"workStatus": 1, "level": 2}

        # After the grace period the empty snapshot is the new truth.
        coordinator._state("_empty_since")[GUID] = (
            time.monotonic() - const.COUNTDOWN_EMPTY_GRACE_SECONDS - 1
        )
        coordinator.async_set_updated_data({GUID: {}})
        assert coordinator.data.get(GUID) == {}

    asyncio.run(scenario())


def test_a_real_snapshot_wins_over_the_debounce() -> None:
    async def scenario() -> None:
        coordinator = _make_coordinator()
        coordinator.data = {GUID: {"workStatus": 1, "level": 2}}
        coordinator.async_set_updated_data({GUID: {"workStatus": 0, "level": 0}})
        assert coordinator.data[GUID]["level"] == 0

    asyncio.run(scenario())


# -- countdown integer contract -------------------------------------------


def test_remaining_is_always_an_integer() -> None:
    async def scenario() -> None:
        coordinator = _make_coordinator()
        await coordinator.async_set_timing(GUID, 2, level=2)
        coordinator.clock.advance(1.2)  # 48 s left: ceil to one minute
        value = coordinator.countdown_remaining(GUID)
        assert isinstance(value, int)
        assert value == 1

    asyncio.run(scenario())
