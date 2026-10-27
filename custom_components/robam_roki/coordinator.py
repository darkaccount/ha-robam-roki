"""Data coordinator for the ROBAM / ROKI integration."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta
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
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_APP_GUID,
    CONF_TOKEN_EXPIRES_AT,
    CONF_TOKEN_TYPE,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_COUNTDOWN_MINUTES,
    OFFLINE_AFTER_POLLS,
)
from .mqtt import (
    CMD_FAN_LEVEL,
    CMD_FAN_LIGHT,
    CMD_FAN_POLL,
    CMD_FAN_STATUS,
    CMD_FAN_TIME,
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

    async def async_set_power(self, device_id: str, on: bool) -> None:
        if self.is_rest_device(device_id):
            await self.client.async_set_power(device_id, on)
            return
        await self._async_mqtt_command(device_id, CMD_FAN_STATUS, on=on)

    async def async_set_level(self, device_id: str, level: int) -> None:
        if self.is_rest_device(device_id):
            await self.client.async_set_level(device_id, level)
            return
        await self._async_mqtt_command(device_id, CMD_FAN_LEVEL, level=level)

    async def async_set_light(self, device_id: str, on: bool) -> None:
        if self.is_rest_device(device_id):
            await self.client.async_set_light(device_id, on)
            return
        await self._async_mqtt_command(device_id, CMD_FAN_LIGHT, on=on)

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

    def _current_level(self, device_id: str) -> int:
        properties = (self.data or {}).get(device_id) or {}
        return as_int(properties.get("level"), 0)

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
        decoded = decode_fan_message(payload)
        if decoded is None:
            return
        device_id, command, properties = decoded
        if device_id not in self.device_ids:
            return
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
        self.async_apply(device_id, properties)

    @property
    def sse_connected(self) -> bool:
        return self._sse_connected

    @property
    def mqtt_connected(self) -> bool:
        return self._mqtt_connected
