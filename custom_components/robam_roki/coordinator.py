"""Data coordinator for the ROBAM / ROKI integration."""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    RokiApiError,
    RokiAuthError,
    RokiClient,
    RokiError,
    as_int,
    normalize_properties,
)
from .commands import UnverifiedCommandError
from .commands import execute as execute_command
from .draft import strip_frame_envelope
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_ALLOW_UNVERIFIED_CONTROLS,
    CONF_APP_GUID,
    CONF_COUNTDOWN_AUTO_OFF,
    CONF_TOKEN_EXPIRES_AT,
    CONF_TOKEN_TYPE,
    COUNTDOWN_COMMAND_GRACE_SECONDS,
    COUNTDOWN_EMPTY_GRACE_SECONDS,
    COUNTDOWN_TOLERANCE_MINUTES,
    DEFAULT_ALLOW_UNVERIFIED_CONTROLS,
    DEFAULT_COUNTDOWN_AUTO_OFF,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    LEARN_DEFAULT_SECONDS,
    LEARN_GAP_SECONDS,
    LEARN_MAX_ACTIONS,
    LEARN_MAX_BODY_BYTES,
    LEARN_MAX_SECONDS,
    MAX_COUNTDOWN_MINUTES,
    OFFLINE_AFTER_POLLS,
)
from .mqtt import (
    CMD_FAN_LEVEL,
    CMD_FAN_LIGHT,
    CMD_FAN_POLL,
    CMD_FAN_STATUS,
    CMD_FAN_TIME,
    GUID_LENGTH,
    TERMINAL_TYPE_APP,
    RokiMqttClient,
    RokiMqttError,
    decode_fan_message,
    encode_fan_command,
    topic_for,
)

_LOGGER = logging.getLogger(__name__)

_PHONE_RE = re.compile(r"(?<!\d)(1\d{2})\d{4}(\d{4})(?!\d)")


def mask_phone(value: str) -> str:
    """Mask a mainland phone number so logs and diagnostics stay anonymous."""
    return _PHONE_RE.sub(r"\1****\2", str(value))


#: MQTT property writes map onto dedicated command frames; the REST transport
#: writes the very same property through the cloud property API instead.
_MQTT_PROPERTY_COMMANDS: dict[str, int] = {
    "working_status": CMD_FAN_STATUS,
    "light_status": CMD_FAN_LIGHT,
}


class _DeviceTransport:
    """Bind the declarative executor to one device and its channel.

    The transport does the protocol translation and nothing else; choosing a
    command, optimistic state and the countdown policies belong to the
    coordinator (see async_execute).
    """

    def __init__(self, coordinator: RokiCoordinator, device_id: str) -> None:
        self._coordinator = coordinator
        self._device_id = device_id

    async def async_set_property(self, key: str, value: Any) -> None:
        coordinator = self._coordinator
        if coordinator.is_rest_device(self._device_id):
            try:
                await coordinator.client.async_set_properties(
                    self._device_id, {key: value}
                )
            except RokiApiError:
                if key != "light_status":
                    raise
                # The write API accepts both spellings; the old light helper
                # retried camelCase when snake_case was rejected.
                await coordinator.client.async_set_properties(
                    self._device_id, {"lightStatus": value}
                )
            return
        command = _MQTT_PROPERTY_COMMANDS.get(key)
        if command is None:
            raise RokiApiError(f"Unknown MQTT property command: {key}")
        await coordinator._async_mqtt_command(
            self._device_id, command, on=bool(value)
        )

    async def async_invoke(
        self, function: str, params: dict[str, Any]
    ) -> None:
        coordinator = self._coordinator
        if coordinator.is_rest_device(self._device_id):
            await coordinator.client.async_invoke(
                self._device_id, function, params
            )
            return
        try:
            command = int(function)
        except (TypeError, ValueError) as err:
            raise RokiApiError(
                f"Unsupported MQTT function: {function!r}"
            ) from err
        if command == CMD_FAN_LEVEL:
            await coordinator._async_mqtt_command(
                self._device_id,
                command,
                level=as_int(params.get("level"), 0),
            )
            return
        if command == CMD_FAN_TIME:
            await coordinator._async_mqtt_command(
                self._device_id,
                command,
                level=as_int(params.get("level"), 0),
                minutes=as_int(params.get("minutes"), 0),
            )
            return
        raise RokiApiError(f"Unsupported MQTT function: {function!r}")

    async def async_send_frame(self, command: int, body: bytes) -> None:
        coordinator = self._coordinator
        if coordinator.is_rest_device(self._device_id):
            raise RokiApiError(
                "Raw MQTT frames are not supported by REST devices"
            )
        mqtt = getattr(coordinator, "_mqtt", None)
        if mqtt is None:
            raise RokiApiError("MQTT is not connected yet")
        from .devices.base import MAX_MQTT_BODY_BYTES

        if len(body) > MAX_MQTT_BODY_BYTES:
            raise RokiApiError(
                f"raw command body exceeds {MAX_MQTT_BODY_BYTES} bytes"
            )
        source = str(getattr(mqtt, "app_guid", ""))
        if len(source) != GUID_LENGTH:
            raise RokiApiError(
                f"MQTT app guid must be {GUID_LENGTH} characters"
            )
        user_id = str(getattr(coordinator.client, "user_id", "") or "")
        # Frame layout: 17-byte source guid, command byte, terminal type,
        # 10-byte zero-padded user id, then the declarative body.
        payload = (
            source.encode("ascii")
            + bytes([int(command) & 0xFF, TERMINAL_TYPE_APP])
            + user_id.encode("utf-8")[:10].ljust(10, b"\x00")
            + bytes(body)
        )
        try:
            await mqtt.async_publish(topic_for(self._device_id), payload)
        except RokiMqttError as err:
            raise RokiApiError(str(err)) from err


# Command 144 carries the gear to run on, and the appliance applies it a
# little later.  If the user changes gear right after arming a timer, the
# timer's gear can land last and drag the fan back for a few seconds.  A
# single idempotent 134 re-assert fixes the flicker without touching 144.
COUNTDOWN_LEVEL_REASSERT_SECONDS = 2.5


#: Frames the official App sends just to read state (130 poll, 128 query).
#: Real-device evidence: 60+ of the 68 recorded "actions" were these read-only
#: frames, and their steady beat kept refreshing the group gap.
IGNORED_LEARN_COMMANDS = frozenset({128, 130})


def _device_from_topic(topic: str) -> str | None:
    """Recover the device guid from a MQTT topic (/u/R8229/000000000000)."""
    parts = str(topic).split("/")
    if len(parts) < 4:
        return None
    guid = parts[2] + parts[3]
    return guid or None


def _snapshot_diff(
    before: dict[str, Any], after: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """The property changes between two snapshots: key -> {from, to}."""
    observed: dict[str, dict[str, Any]] = {}
    for key in sorted(set(before) | set(after)):
        old = before.get(key)
        new = after.get(key)
        if old != new:
            observed[str(key)] = {"from": old, "to": new}
    return observed


@dataclass
class _Countdown:
    """Local truth for one device's countdown.

    The device's own timer only beeps at the deadline, so the integration owns
    the deadline, schedules the off command and treats reports as confirmation.
    """

    deadline: datetime
    command_sent_at: float = 0.0
    last_display: int | None = None
    zero_reports: int = 0
    off_sent: bool = False
    handle: Callable[[], None] | None = None


class RokiCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Poll the hood properties and merge SSE pushes."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: RokiClient,
        devices: list[dict[str, Any]],
        *,
        scan_interval: int = DEFAULT_SCAN_INTERVAL,
        enable_sse: bool = True,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {mask_phone(entry.title)}",
            update_interval=timedelta(seconds=scan_interval),
        )
        self.entry = entry
        self.client = client
        self.devices = devices
        self.enable_sse = enable_sse

        self._sse_task: asyncio.Task[None] | None = None
        self._sse_connected = False
        self._mqtt_task: asyncio.Task[None] | None = None
        self._mqtt: RokiMqttClient | None = None
        self._mqtt_connected = False
        self._refresh_task: asyncio.Task[None] | None = None
        self._stopping = False
        # Wall clock (time.time) of the last inbound frame per device, used to
        # decide whether a hood has gone quiet.
        self._last_seen: dict[str, float] = {}
        # Cloud-side online codes for REST/SSE devices, refreshed in step with
        # the coordinator interval.
        self._cloud_online: dict[str, int | None] = {}
        # Countdown state: a local deadline per device, plus the last positive
        # timing report used by the fallback path.
        self._countdowns: dict[str, _Countdown] = {}
        self._last_positive_timing: dict[str, int] = {}
        self._fallback_strikes: dict[str, int] = {}
        # First time each device reported an empty snapshot (available debounce).
        self._empty_since: dict[str, float] = {}
        # Learning mode buffer; None when no session runs.  Memory only: a
        # reload drops it, which is exactly what the workflow asks for.
        self._learn: dict[str, Any] | None = None

    @property
    def device_ids(self) -> list[str]:
        return [str(device["guid"]) for device in self.devices]

    def device_info_for(self, guid: str) -> dict[str, Any]:
        for device in self.devices:
            if str(device.get("guid")) == guid:
                return device
        return {"guid": guid, "name": guid}

    def is_rest_device(self, guid: str) -> bool:
        """IoT/SSE devices (``deviceSource == 1``) use the REST write API."""
        source = self.device_info_for(guid).get("deviceSource")
        return str(source) == "1"

    @property
    def mqtt_device_ids(self) -> list[str]:
        return [guid for guid in self.device_ids if not self.is_rest_device(guid)]

    # -- online state -----------------------------------------------------

    def online_state(self, device_id: str) -> bool | None:
        """Whether the hood is currently reachable, or ``None`` if unknown.

        The two transports answer this differently and are deliberately kept
        apart:

        * MQTT devices do not report connectivity anywhere, so the only signal
          is whether they answered the last polls.  When our own socket to the
          broker is down we have no signal at all and return ``None`` -- losing
          the cloud is not the same as the hood being offline.
        * REST/SSE devices get a real answer from the cloud's online-state
          endpoint.
        """
        if self.is_rest_device(device_id):
            code = self._cloud_online.get(device_id)
            if code is None:
                return None
            # The code's exact meaning is unconfirmed, so anything non-zero
            # counts as reachable and the raw value stays in the attributes.
            return code != 0
        if not self._mqtt_connected:
            return None
        last_seen = self._last_seen.get(device_id)
        if last_seen is None:
            return None
        timeout = self._mqtt_poll_interval * OFFLINE_AFTER_POLLS
        return (time.time() - last_seen) <= timeout

    @property
    def _mqtt_poll_interval(self) -> float:
        return max(5.0, float(self.update_interval.total_seconds() / 3))

    def online_details(self, device_id: str) -> dict[str, Any]:
        """Diagnostics explaining how :meth:`online_state` was decided."""
        if self.is_rest_device(device_id):
            return {
                "通道": "rest",
                "云端状态码": self._cloud_online.get(device_id),
            }
        last_seen = self._last_seen.get(device_id)
        return {
            "通道": "mqtt",
            "判定": f"超过 {OFFLINE_AFTER_POLLS} 轮未应答",
            "最近上报": (
                datetime.fromtimestamp(last_seen).isoformat()
                if last_seen
                else None
            ),
            "MQTT 已连接": self._mqtt_connected,
        }

    # -- polling ----------------------------------------------------------

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        results = await asyncio.gather(
            *(self.client.async_get_properties(guid) for guid in self.device_ids),
            return_exceptions=True,
        )

        data: dict[str, dict[str, Any]] = dict(self.data or {})
        auth_errors = 0
        other_errors: list[Exception] = []

        for guid, result in zip(self.device_ids, results, strict=True):
            if isinstance(result, RokiAuthError):
                auth_errors += 1
            elif isinstance(result, Exception):
                other_errors.append(result)
                _LOGGER.debug("Property query for %s failed: %s", guid, result)
            else:
                data[guid] = result
                if self.is_rest_device(guid):
                    # REST polls are authoritative for these devices.
                    self.observe_report(guid, result)

        if self.device_ids and auth_errors == len(self.device_ids):
            raise ConfigEntryAuthFailed("ROKI session expired")
        if self.device_ids and len(other_errors) == len(self.device_ids):
            raise UpdateFailed(f"Could not reach the ROKI cloud: {other_errors[0]}")

        self._persist_session()
        self.hass.async_create_task(self._async_refresh_online_state())
        return data

    async def _async_refresh_online_state(self) -> None:
        """Ask the cloud which REST/SSE devices are online.

        Fire-and-forget on the coordinator's own interval: a failure here only
        leaves the previous answer in place, and the sensor keeps reporting the
        last known state instead of flipping to unknown.
        """
        rest_ids = [guid for guid in self.device_ids if self.is_rest_device(guid)]
        if not rest_ids:
            return
        try:
            states = await self.client.async_get_online_state(rest_ids)
        except RokiError as err:
            _LOGGER.debug("Online state query failed: %s", err)
            return
        for device_id, status in states.items():
            self._cloud_online[device_id] = status

    def _persist_session(self) -> None:
        """Store a refreshed token back into the config entry."""
        updates: dict[str, Any] = {}
        if self.entry.data.get(CONF_ACCESS_TOKEN) != self.client.access_token:
            updates[CONF_ACCESS_TOKEN] = self.client.access_token
        if self.entry.data.get(CONF_TOKEN_TYPE) != self.client.token_type:
            updates[CONF_TOKEN_TYPE] = self.client.token_type
        if self.entry.data.get(CONF_TOKEN_EXPIRES_AT) != self.client.expires_at:
            updates[CONF_TOKEN_EXPIRES_AT] = self.client.expires_at
        if self.client.app_guid and self.entry.data.get(CONF_APP_GUID) != self.client.app_guid:
            updates[CONF_APP_GUID] = self.client.app_guid
        if updates:
            self.hass.config_entries.async_update_entry(
                self.entry, data={**self.entry.data, **updates}
            )

    # -- optimistic updates ----------------------------------------------

    def async_set_updated_data(self, data: Any) -> None:
        """Publish new data, debouncing transient empty snapshots."""
        super().async_set_updated_data(self._debounce_empty_snapshots(data))

    def _debounce_empty_snapshots(self, data: Any) -> dict[str, dict[str, Any]]:
        """Keep the previous properties during a short empty snapshot.

        The real-device log showed every entity becoming unavailable for about
        a second while the poll briefly returned nothing.  An empty snapshot is
        held for COUNTDOWN_EMPTY_GRACE_SECONDS; if it persists, it is accepted
        and the entities go unavailable as before.
        """
        merged = dict(data or {})
        now = time.monotonic()
        previous_all = self.__dict__.get("data") or {}
        empty_since = self._state("_empty_since")
        for guid in self.device_ids:
            if merged.get(guid):
                empty_since.pop(guid, None)
                continue
            previous = previous_all.get(guid) or {}
            if not previous:
                continue
            since = empty_since.setdefault(guid, now)
            if now - since < COUNTDOWN_EMPTY_GRACE_SECONDS:
                merged[guid] = previous
            else:
                empty_since.pop(guid, None)
        return merged

    def async_apply(self, device_id: str, properties: dict[str, Any]) -> None:
        """Merge an optimistic state update into the coordinator data."""
        current = dict(self.data or {})
        merged = {**current.get(device_id, {}), **normalize_properties(properties)}
        current[device_id] = merged
        self.async_set_updated_data(current)

    def async_schedule_refresh(
        self, device_id: str | None = None, delay: float = 2.0
    ) -> None:
        """Refresh a moment after a command so the cloud state settles.

        MQTT devices report their new state on the broker, so polling them
        right away can overwrite it with a stale cloud cache value.
        """
        if device_id is not None and not self.is_rest_device(device_id):
            return
        if self._refresh_task is not None and not self._refresh_task.done():
            return

        async def _refresh() -> None:
            await asyncio.sleep(delay)
            await self.async_request_refresh()

        self._refresh_task = self.hass.async_create_task(_refresh())

    # -- commands ---------------------------------------------------------

    def allow_unverified_controls(self) -> bool:
        """Whether experimental (unverified) controls may be shown and sent."""
        entry = self.__dict__.get("entry")
        options = getattr(entry, "options", None) or {}
        return bool(
            options.get(
                CONF_ALLOW_UNVERIFIED_CONTROLS,
                DEFAULT_ALLOW_UNVERIFIED_CONTROLS,
            )
        )

    async def async_execute(
        self, device_id: str, capability: str, **kwargs: Any
    ) -> None:
        """Execute one capability through the declarative command table.

        Policy stays here: the countdown delegates to async_set_timing (its
        deadline, grace and auto-off behaviour is untouched), powering off
        cancels the countdown, and a gear change while a countdown runs
        schedules the single re-assert the real-device run needed.
        """
        if capability == "timing":
            await self.async_set_timing(
                device_id,
                int(kwargs.get("minutes", 0)),
                level=kwargs.get("level"),
            )
            return
        if capability == "power" and kwargs.get("on") is False:
            self.cancel_countdown(device_id)
        await self._async_execute_command(device_id, capability, **kwargs)
        if capability == "level" and kwargs.get("level") is not None:
            self._schedule_level_reassert(device_id, int(kwargs["level"]))

    async def _async_execute_command(
        self, device_id: str, capability: str, **kwargs: Any
    ) -> None:
        """Send the command frames only; callers own the state policy."""
        # devices imports this module, so the factory is imported lazily.
        from .devices import get_driver_for_device

        driver = get_driver_for_device(self, self.device_info_for(device_id))
        spec = driver.commands().get(capability)
        if spec is None:
            raise RokiApiError(
                f"{capability} is not supported by this device"
            )
        try:
            await execute_command(
                spec,
                _DeviceTransport(self, device_id),
                allow_unverified=self.allow_unverified_controls(),
                **kwargs,
            )
        except UnverifiedCommandError as err:
            raise RokiApiError(str(err)) from err

    async def async_set_power(self, device_id: str, on: bool) -> None:
        """Thin wrapper kept for callers that predate the executor."""
        await self.async_execute(device_id, "power", on=on)

    async def async_set_level(self, device_id: str, level: int) -> None:
        await self.async_execute(device_id, "level", level=level)

    async def _async_write_level(self, device_id: str, level: int) -> None:
        """Send a gear command without scheduling another re-assert."""
        await self._async_execute_command(device_id, "level", level=level)

    def _schedule_level_reassert(self, device_id: str, level: int) -> None:
        """Re-assert the gear once when a countdown is armed."""
        if not self.countdown_active(device_id):
            return
        store = self._state("_level_reassert")
        previous = store.pop(device_id, None)
        if previous is not None:
            previous.cancel()
        store[device_id] = self.hass.async_create_task(
            self._reassert_level_later(device_id, level)
        )

    async def _reassert_level_later(self, device_id: str, level: int) -> None:
        await asyncio.sleep(COUNTDOWN_LEVEL_REASSERT_SECONDS)
        if not self.countdown_active(device_id):
            return
        properties = (self.__dict__.get("data") or {}).get(device_id) or {}
        if as_int(properties.get("workStatus"), 0) != 1:
            return
        if as_int(properties.get("level"), -1) == level:
            return
        try:
            await self._async_write_level(device_id, level)
        except RokiError as err:
            _LOGGER.debug("gear re-assert for %s failed: %s", device_id, err)

    async def async_set_light(self, device_id: str, on: bool) -> None:
        """Thin wrapper kept for callers that predate the executor."""
        await self.async_execute(device_id, "light", on=on)

    async def async_set_timing(
        self, device_id: str, minutes: int, *, level: int | None = None
    ) -> None:
        """Set (or clear) the countdown in minutes.

        Command 144 always carries the level to run on, and the app sends the
        level the hood is currently on, so the countdown survives the command
        instead of restarting the fan at another gear.  A minutes value of 0
        cancels the countdown, exactly like the app's confirm dialog.

        ``level`` should be passed by callers that just issued a gear change:
        the hood reports gear and countdown in separate frames, so reading the
        level back from coordinator data right after a change can return the
        previous gear and silently drag the fan back to it.
        """
        minutes = max(0, min(MAX_COUNTDOWN_MINUTES, int(minutes)))
        if self.is_rest_device(device_id):
            # The app calls set_air_fresh on IoT devices, which looks like a
            # ventilation helper rather than a countdown; not implemented until
            # it can be verified on real hardware.
            raise RokiApiError(
                "Countdown is not supported for this device model"
            )
        if level is None:
            level = self._current_level(device_id)
        if level <= 0:
            raise RokiApiError(
                "The hood must be running before a countdown can be set"
            )
        await self._async_mqtt_command(
            device_id, CMD_FAN_TIME, level=level, minutes=minutes
        )
        self.async_apply(device_id, {"timing": minutes})
        self._arm_countdown(device_id, minutes)

    def _current_level(self, device_id: str) -> int:
        properties = (self.data or {}).get(device_id) or {}
        return as_int(properties.get("level"), 0)

    # -- countdown (local deadline + auto-off) ----------------------------

    def _now(self) -> datetime:
        return datetime.now(UTC)

    def _state(self, name: str) -> dict[str, Any]:
        """Return a lazily created state store.

        Some tests build the coordinator with object.__new__ to skip the
        __init__ side effects, so the stores must tolerate a missing attribute.
        """
        store: dict[str, Any] | None = self.__dict__.get(name)
        if store is None:
            store = {}
            setattr(self, name, store)
        return store

    def countdown_auto_off_enabled(self) -> bool:
        """Whether the integration should send the off command itself."""
        return bool(
            self.entry.options.get(
                CONF_COUNTDOWN_AUTO_OFF, DEFAULT_COUNTDOWN_AUTO_OFF
            )
        )

    def countdown_active(self, device_id: str) -> bool:
        return device_id in self._state("_countdowns")

    def countdown_deadline(self, device_id: str) -> datetime | None:
        state = self._state("_countdowns").get(device_id)
        return None if state is None else state.deadline

    def countdown_remaining(self, device_id: str) -> int | None:
        """Minutes left on the local deadline, never increasing mid-run."""
        state = self._state("_countdowns").get(device_id)
        if state is None:
            return None
        minutes = self._remaining_minutes(state)
        if state.last_display is not None:
            minutes = min(minutes, state.last_display)
        state.last_display = minutes
        return minutes

    def _remaining_minutes(self, state: _Countdown) -> int:
        seconds = (state.deadline - self._now()).total_seconds()
        if seconds <= 0:
            return 0
        return max(1, math.ceil(seconds / 60.0))

    def restore_countdown(self, device_id: str) -> None:
        """Rebuild the timer from the cached timing after a restart."""
        if device_id in self._state("_countdowns"):
            return
        properties = (self.__dict__.get("data") or {}).get(device_id) or {}
        reported = as_int(properties.get("timing"), 0)
        if reported > 0:
            self._arm_countdown(device_id, reported, sent=False)

    def cancel_countdown(self, device_id: str) -> None:
        pending = self._state("_level_reassert").pop(device_id, None)
        if pending is not None:
            pending.cancel()
        """Forget the countdown and its timer (manual off, or set to 0)."""
        state = self._state("_countdowns").pop(device_id, None)
        self._state("_last_positive_timing").pop(device_id, None)
        self._state("_fallback_strikes").pop(device_id, None)
        if state is not None and state.handle is not None:
            state.handle()
            state.handle = None

    def _arm_countdown(
        self, device_id: str, minutes: int, *, sent: bool = True
    ) -> None:
        minutes = max(0, int(minutes))
        self._cancel_timer(device_id)
        if minutes <= 0:
            self._state("_countdowns").pop(device_id, None)
            self._state("_last_positive_timing").pop(device_id, None)
            self._state("_fallback_strikes").pop(device_id, None)
            return
        state = _Countdown(
            deadline=self._now() + timedelta(minutes=minutes),
            command_sent_at=time.monotonic() if sent else 0.0,
            last_display=minutes,
        )
        self._state("_countdowns")[device_id] = state
        self._state("_last_positive_timing")[device_id] = minutes
        self._state("_fallback_strikes").pop(device_id, None)
        if self.countdown_auto_off_enabled():
            self._schedule_auto_off(device_id)

    def _cancel_timer(self, device_id: str) -> None:
        state = self._state("_countdowns").get(device_id)
        if state is not None and state.handle is not None:
            state.handle()
            state.handle = None

    def _schedule_auto_off(self, device_id: str) -> None:
        """Schedule the single off command at the local deadline."""
        state = self._state("_countdowns").get(device_id)
        if state is None:
            return
        self._cancel_timer(device_id)
        try:
            from homeassistant.helpers.event import (
                async_track_point_in_utc_time,
            )
        except ImportError:  # offline test harness without event helpers
            return

        async def _fire(_now: datetime) -> None:
            state.handle = None
            # HA runs sync callbacks in a worker thread, where creating a task is
            # not thread safe; await here instead.
            await self._async_auto_off(device_id)

        state.handle = async_track_point_in_utc_time(
            self.hass, _fire, state.deadline
        )

    async def _async_auto_off(self, device_id: str) -> None:
        """Send the off command once the local deadline has really passed."""
        state = self._state("_countdowns").get(device_id)
        if state is None or state.off_sent:
            return
        if self._remaining_minutes(state) > 0:
            self._schedule_auto_off(device_id)
            return
        state.off_sent = True
        state.handle = None
        try:
            await self._async_send_off(device_id)
        finally:
            self._state("_countdowns").pop(device_id, None)
            self._state("_last_positive_timing").pop(device_id, None)

    async def _async_send_off(self, device_id: str) -> None:
        try:
            await self.async_set_power(device_id, False)
        except RokiError as err:
            _LOGGER.warning(
                "countdown auto-off for %s failed: %s", device_id, err
            )

    def observe_report(
        self, device_id: str, properties: dict[str, Any]
    ) -> None:
        """Use a device report only to confirm or refine the local deadline."""
        if not isinstance(properties, dict):
            return
        reported: int | None = None
        if "timing" in properties:
            reported = as_int(properties.get("timing"), -1)
        work_status: int | None = None
        if "workStatus" in properties:
            work_status = as_int(properties.get("workStatus"), -1)
        state = self._state("_countdowns").get(device_id)
        if state is None:
            if reported is not None and reported > 0:
                # Adopt a timer set on the app/panel (or rebuilt at startup).
                self._arm_countdown(device_id, reported, sent=False)
                return
            self._fallback_report(device_id, reported, work_status)
            return
        if reported is None:
            return
        if (
            state.command_sent_at
            and time.monotonic() - state.command_sent_at
            < COUNTDOWN_COMMAND_GRACE_SECONDS
        ):
            # A stale snapshot right after our command: ignore it.
            return
        remaining = self._remaining_minutes(state)
        if reported == 0:
            if remaining <= COUNTDOWN_TOLERANCE_MINUTES:
                return
            state.zero_reports += 1
            if state.zero_reports >= 2:
                # Two confirmations and more than a minute left: the device
                # truth is that the countdown was cancelled on the panel.
                self.cancel_countdown(device_id)
            return
        state.zero_reports = 0
        if reported > remaining + COUNTDOWN_TOLERANCE_MINUTES:
            # Stale snapshot: the display only goes down during one countdown.
            return
        if reported < remaining:
            state.deadline = self._now() + timedelta(minutes=reported)
            state.last_display = reported
            self._cancel_timer(device_id)
            if self.countdown_auto_off_enabled():
                self._schedule_auto_off(device_id)

    def _fallback_report(
        self,
        device_id: str,
        reported: int | None,
        work_status: int | None,
    ) -> None:
        """Last resort for timers we never owned (app/panel, HA restart)."""
        last_positive = self._state("_last_positive_timing").get(device_id, 0)
        if (
            reported == 0
            and work_status == 1
            and last_positive > 0
            and self.countdown_auto_off_enabled()
        ):
            strikes = self._state("_fallback_strikes").get(device_id, 0) + 1
            self._state("_fallback_strikes")[device_id] = strikes
            if strikes >= 2:
                self._state("_fallback_strikes")[device_id] = 0
                self._state("_last_positive_timing").pop(device_id, None)
                self.hass.async_create_task(self._async_send_off(device_id))
            return
        self._state("_fallback_strikes").pop(device_id, None)

    def _coerce_short_frame_timing(
        self, device_id: str, properties: dict[str, Any]
    ) -> dict[str, Any]:
        """Reinterpret a short-frame 2 as the countdown while one is running.

        decode_fan_message mirrors FanMsgMar case 131 verbatim: on a short
        frame the value 2 means the wifi signal, so a genuine 2-minute
        countdown would disappear (the display jumps 3 -> 1).  The decoder
        stays stateless exactly like the app; the correction belongs here,
        where the device's previous timing is known.
        """
        if "timing" in properties or properties.get("wifi") != 2:
            return properties
        if not self._countdown_known_positive(device_id):
            return properties
        fixed = dict(properties)
        fixed.pop("wifi", None)
        fixed["timing"] = 2
        return fixed

    def _countdown_known_positive(self, device_id: str) -> bool:
        state = self._state("_countdowns").get(device_id)
        if state is not None and self._remaining_minutes(state) > 0:
            return True
        if self._state("_last_positive_timing").get(device_id, 0) > 0:
            return True
        properties = (self.__dict__.get("data") or {}).get(device_id) or {}
        return as_int(properties.get("timing"), 0) > 0

    # -- learning mode (passive capture of the App's commands) ------------

    def learn_active(self) -> bool:
        return self.__dict__.get("_learn") is not None

    def learn_start(
        self, device_id: str, duration: int = LEARN_DEFAULT_SECONDS
    ) -> dict[str, Any]:
        """Start passively recording the frames the official App sends."""
        if self.learn_active():
            raise RokiApiError(
                "learning already running for another device"
            )
        seconds = max(1, min(LEARN_MAX_SECONDS, int(duration)))
        properties = dict(
            (self.__dict__.get("data") or {}).get(device_id) or {}
        )
        started = self._now()
        state: dict[str, Any] = {
            "guid": device_id,
            "started_at": started,
            "started_monotonic": time.monotonic(),
            "deadline": started + timedelta(seconds=seconds),
            "last_snapshot": properties,
            "actions": [],
            "groups_detail": [],
            "group_start": 0,
            "groups": 0,
            "last_frame_at": None,
            "cancel": None,
        }
        self._learn = state
        self._state("_learned_groups").pop(device_id, None)
        state["cancel"] = self._schedule_learn_timeout(seconds)
        name = self.device_info_for(device_id).get("name") or device_id
        self._notify(
            f"已开始学习 {name}（最长 {seconds} 秒，到时间会自动结束，"
            "也可以手动提前结束）。\n"
            "现在用官方 App 操作你要的功能；学习期间不会发送任何命令。",
            title="ROBAM ROKI 学习模式",
        )
        return {
            "device_id": device_id,
            "expires_at": state["deadline"].isoformat(),
        }

    def _schedule_learn_timeout(self, seconds: int) -> Any | None:
        """Schedule the auto-stop with the real Home Assistant helper.

        HomeAssistant has no async_call_later method (the first real-device
        run failed with "Unknown error" because of that); the helper lives in
        homeassistant.helpers.event and is imported lazily so the offline test
        harness can skip it.
        """
        try:
            from homeassistant.helpers.event import async_call_later
        except ImportError:  # offline test harness without the helper
            return None
        return async_call_later(
            self.hass, seconds, self._async_learn_timeout
        )

    async def _async_learn_timeout(self, now: Any = None) -> None:
        """The duration elapsed: stop the session through the shared path."""
        if self.learn_active():
            await self.learn_stop()

    def _clear_learn_session(self) -> None:
        """Drop the buffer and its timer (reload, unload, or stop)."""
        state = self.__dict__.get("_learn")
        self._learn = None
        if state is None:
            return
        cancel = state.get("cancel")
        if cancel is not None:
            try:
                cancel()
            except Exception:
                _LOGGER.debug(
                    "cancelling the learning timer failed", exc_info=True
                )

    async def learn_stop(
        self, device_id: str | None = None
    ) -> dict[str, Any]:
        """Finish the session, group the frames and return the actions."""
        state = self.__dict__.get("_learn")
        if state is None:
            return {"device_id": None, "actions": []}
        if device_id is not None and str(device_id) != str(state["guid"]):
            raise RokiApiError("learning is running for a different device")
        self._clear_learn_session()
        self._close_learn_group(state)
        self._state("_learned_groups")[str(state["guid"])] = list(
            state["groups_detail"]
        )
        actions = [
            {
                "index": index + 1,
                "command": action["command"],
                "body_hex": action["body_hex"],
                "observed": action["observed"],
                "truncated": action["truncated"],
            }
            for index, action in enumerate(state["actions"])
        ]
        self._notify(
            self._learn_result_text(str(state["guid"]), actions),
            title="ROBAM ROKI 学习模式",
        )
        return {"device_id": state["guid"], "actions": actions}

    def learned_controls(self, device_id: str) -> list[dict[str, Any]]:
        """The last learning session's groups, grouped for the drafter.

        Memory only: a reload clears them, so the draft must be generated in
        the same session that learned the actions.
        """
        groups = self._state("_learned_groups").get(device_id) or []
        return [
            {
                "frames": [dict(frame) for frame in group.get("frames", [])],
                "observed": dict(group.get("observed") or {}),
            }
            for group in groups
        ]

    def _record_learn_frame(self, topic: str, payload: bytes) -> None:
        """Record one inbound frame if a session is watching this device.

        The raw payload is inspected before decoding, because the whole point
        is to capture commands this integration does not understand yet.
        """
        state = self.__dict__.get("_learn")
        if state is None or len(payload) < 18:
            return
        device_id = _device_from_topic(topic)
        if device_id is None or device_id != str(state["guid"]):
            return
        if payload[17] in IGNORED_LEARN_COMMANDS:
            # Polls/queries are reads, not user actions; recording them would
            # also refresh last_frame_at and collapse the whole session into
            # one group.
            return
        try:
            source = payload[:17].decode("ascii")
        except UnicodeDecodeError:
            return
        if source == device_id:
            return  # the appliance reporting its own state
        client = self.__dict__.get("client")
        if client is not None and source == str(
            getattr(client, "app_guid", "") or ""
        ):
            return  # our own command (auto-off, an entity click)
        now = time.monotonic()
        last = state["last_frame_at"]
        if last is None:
            state["groups"] = 1
        elif now - last > LEARN_GAP_SECONDS:
            self._close_learn_group(state)
            if state["groups"] >= LEARN_MAX_ACTIONS:
                return
            state["groups"] += 1
        action_offset = int(
            (now - state["started_monotonic"]) * 1000
        )
        body = payload[18:]
        truncated = len(body) > LEARN_MAX_BODY_BYTES
        state["actions"].append(
            {
                "offset_ms": action_offset,
                "command": payload[17],
                "body_hex": strip_frame_envelope(
                    body[:LEARN_MAX_BODY_BYTES].hex()
                ),
                "truncated": truncated,
                "observed": {},
            }
        )
        state["last_frame_at"] = now

    def _close_learn_group(self, state: dict[str, Any]) -> None:
        """Attach the property diff observed at the end of the current group."""
        actions = state["actions"]
        start = state["group_start"]
        if start >= len(actions):
            return
        snapshot = dict(
            (self.__dict__.get("data") or {}).get(str(state["guid"])) or {}
        )
        observed = _snapshot_diff(state["last_snapshot"], snapshot)
        state["last_snapshot"] = snapshot
        group_actions = actions[start:]
        for action in group_actions:
            action["observed"] = observed
        state["groups_detail"].append(
            {
                "frames": [
                    {
                        "command": action["command"],
                        "body_hex": action["body_hex"],
                    }
                    for action in group_actions
                ],
                "observed": observed,
            }
        )
        state["group_start"] = len(actions)

    def _learn_result_text(
        self, device_id: str, actions: list[dict[str, Any]]
    ) -> str:
        name = self.device_info_for(device_id).get("name") or device_id
        if not actions:
            return (
                f"学习结束：{name} 没有记录到任何动作。\n"
                "请确认官方 App 与 HA 登录的是同一个云端账号，"
                "且操作确实发到了这台设备。\n"
                "失败分支：python tools/roki_cli.py collect "
                f"--device {device_id} --mark <标签>，然后 "
                "python tools/roki_cli.py draft <bundle>。"
            )
        lines = [f"学习结束：{name} 共 {len(actions)} 个动作："]
        for action in actions:
            line = (
                f"  #{action['index']} command={action['command']} "
                f"body={action['body_hex']}"
            )
            if action["truncated"]:
                line += "（body 已截断到 64 字节）"
            observed = action.get("observed") or {}
            if observed:
                effects = ", ".join(
                    f"{key} {change['from']}→{change['to']}"
                    for key, change in observed.items()
                )
                line += f"（观察到 {effects}）"
            lines.append(line)
        return "\n".join(lines)

    def _notify(self, message: str, *, title: str) -> None:
        """Create a persistent notification without depending on HA at import."""
        try:
            from homeassistant.components import persistent_notification
        except ImportError:  # offline test harness without the component
            return
        try:
            persistent_notification.async_create(
                self.hass, message, title=title
            )
        except Exception:
            _LOGGER.debug(
                "could not create a learning notification", exc_info=True
            )

    async def _async_mqtt_command(
        self,
        device_id: str,
        command: int,
        *,
        level: int | None = None,
        on: bool | None = None,
        minutes: int | None = None,
    ) -> None:
        mqtt = self._mqtt
        if mqtt is None:
            raise RokiApiError(
                "MQTT is not connected yet, retry in a few seconds"
            )
        payload = encode_fan_command(
            mqtt.app_guid,
            command,
            user_id=self.client.user_id or "",
            level=level,
            on=on,
            minutes=minutes,
        )
        try:
            # The app always polls (130) right before a command; the hood
            # answers 131 and then accepts the command with a 133/135/137 rc.
            await mqtt.async_publish(
                topic_for(device_id),
                encode_fan_command(mqtt.app_guid, CMD_FAN_POLL),
            )
            await mqtt.async_publish(topic_for(device_id), payload)
        except RokiMqttError as err:
            raise RokiApiError(str(err)) from err

    # -- push updates (SSE for IoT devices, MQTT for legacy ones) ---------

    def async_start_push(self) -> None:
        if not self.enable_sse:
            return
        if (
            any(self.is_rest_device(guid) for guid in self.device_ids)
            and self._sse_task is None
        ):
            self._sse_task = self.hass.async_create_background_task(
                self._sse_loop(), f"{DOMAIN} sse"
            )
        if self.mqtt_device_ids and self._mqtt_task is None:
            self._mqtt_task = self.hass.async_create_background_task(
                self._mqtt_loop(), f"{DOMAIN} mqtt"
            )

    async def async_stop_push(self) -> None:
        self._stopping = True
        # Drop the pending auto-off timers.  A reload (options change, entry
        # update) builds a fresh coordinator, and an orphaned callback would
        # otherwise fire against a client that is already gone.
        for device_id in list(self._state("_countdowns")):
            self.cancel_countdown(device_id)
        for pending in list(self._state("_level_reassert").values()):
            pending.cancel()
        self._state("_level_reassert").clear()
        self._clear_learn_session()
        self._state("_learned_groups").clear()
        for task in (self._sse_task, self._mqtt_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._sse_task = None
        self._mqtt_task = None
        if self._mqtt is not None:
            await self._mqtt.async_stop()
            self._mqtt = None

    # -- server sent events ----------------------------------------------

    async def _sse_loop(self) -> None:
        rest_ids = [guid for guid in self.device_ids if self.is_rest_device(guid)]
        if not rest_ids:
            return
        backoff = 5
        while not self._stopping:
            try:
                connect_id = await self.client.async_get_app_guid()
                if not self.client.user_id:
                    await self.client.async_get_user()
                async for event in self.client.async_sse_events(
                    rest_ids,
                    connect_id=connect_id or self.entry.entry_id,
                    uid=self.client.user_id or "",
                ):
                    self._sse_connected = True
                    backoff = 5
                    self._handle_sse_event(event)
                # The server closed the stream normally; reconnect after a
                # short pause instead of spinning.
                self._sse_connected = False
                if not self._stopping:
                    await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                raise
            except RokiAuthError as err:
                self._sse_connected = False
                _LOGGER.debug("SSE auth error: %s", err)
                try:
                    await self.client.async_force_relogin()
                except RokiError:
                    pass
                await asyncio.sleep(30)
            except Exception as err:  # noqa: BLE001 - keep the stream alive
                self._sse_connected = False
                _LOGGER.debug("SSE disconnected: %s", err)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120)

    def _handle_sse_event(self, event: dict[str, Any]) -> None:
        device_id = event.get("deviceId") or event.get("device_id")
        properties = event.get("data")
        if not device_id or not isinstance(properties, dict):
            return
        if str(device_id) not in self.device_ids:
            return
        self.async_apply(str(device_id), properties)

    # -- MQTT (deviceSource == 0) ---------------------------------------

    async def _mqtt_loop(self) -> None:
        backoff = 5
        while not self._stopping:
            mqtt: RokiMqttClient | None = None
            try:
                app_guid = await self.client.async_get_app_guid()
                if not app_guid:
                    raise RokiMqttError("No app guid for MQTT")
                if not self.client.user_id:
                    await self.client.async_get_user()

                mqtt = RokiMqttClient(
                    app_guid, on_message=self._handle_mqtt_message
                )
                topics = {topic_for(guid) for guid in self.mqtt_device_ids}
                topics.update(
                    topic_for(guid, broadcast=True)
                    for guid in self.mqtt_device_ids
                )
                # Direct replies/acks are unicast to the app guid.
                topics.add(topic_for(app_guid))
                mqtt.add_topics(topics)

                await mqtt.async_start()
                await mqtt.async_wait_connected(20)
                self._mqtt = mqtt
                self._mqtt_connected = True
                backoff = 5
                _LOGGER.debug("ROKI MQTT connected as %s", app_guid)

                # The hood does not broadcast its state: the app asks for it
                # with command 130 (AbsFan.onPolling) and the device answers
                # with 131 on our unicast topic.
                poll_interval = self._mqtt_poll_interval
                while not self._stopping and mqtt.connected:
                    await self._async_mqtt_poll(mqtt)
                    await asyncio.sleep(poll_interval)
                _LOGGER.debug("ROKI MQTT connection lost")
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - keep retrying
                _LOGGER.debug("ROKI MQTT setup failed: %s", err)
            finally:
                self._mqtt_connected = False
                if self._mqtt is not None:
                    await self._mqtt.async_stop()
                    self._mqtt = None
                elif mqtt is not None:
                    await mqtt.async_stop()
            if not self._stopping:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120)

    async def _async_mqtt_poll(self, mqtt: RokiMqttClient) -> None:
        """Ask every MQTT hood for its current state (command 130)."""
        for guid in self.mqtt_device_ids:
            payload = encode_fan_command(mqtt.app_guid, CMD_FAN_POLL)
            try:
                await mqtt.async_publish(topic_for(guid), payload)
            except RokiMqttError as err:
                _LOGGER.debug("Polling %s over MQTT failed: %s", guid, err)

    def _handle_mqtt_message(self, topic: str, payload: bytes) -> None:
        self._record_learn_frame(topic, payload)
        decoded = decode_fan_message(payload)
        if decoded is None:
            return
        device_id, command, properties = decoded
        if device_id not in self.device_ids:
            return
        properties = self._coerce_short_frame_timing(device_id, properties)
        self._last_seen[device_id] = time.time()
        if "rc" in properties:
            # 133/135/137: the hood's answer to a command we published.
            rc = properties["rc"]
            if rc:
                _LOGGER.warning(
                    "ROKI MQTT command %s on %s was refused (rc=%s)",
                    command,
                    device_id,
                    rc,
                )
            return
        self.observe_report(device_id, properties)
        self.async_apply(device_id, properties)

    @property
    def sse_connected(self) -> bool:
        return self._sse_connected

    @property
    def mqtt_connected(self) -> bool:
        return self._mqtt_connected
