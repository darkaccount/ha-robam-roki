"""MQTT transport for ROBAM / ROKI hoods that are not IoT/SSE devices.

Devices with ``deviceSource == 0`` are controlled through the vendor's MQTT
broker instead of the REST IoT API.  This module reimplements the small subset
of MQTT 3.1.1 the Android client uses (QoS 0 only), so the integration keeps
its ``aiohttp`` + ``cryptography`` dependency set.

Protocol facts taken from ``com.legent.plat.io.device.mqtt.MqttBus``,
``com.legent.plat.io.device.mqtt.Topic``, ``MqttProtocol`` and
``com.robam.common.io.device.marshal.FanMsgMar`` (ROKI 6.4.0).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import struct
import time
from collections.abc import Callable
from typing import Any

_LOGGER = logging.getLogger(__name__)

MQTT_HOST = "mqtt.myroki.com"
MQTT_PORT = 1883
MQTT_KEEPALIVE = 30
MQTT_SIGN_KEY = "Kp0lxmm1"  # ApiSecurityExample.hmacSha256 key in MqttBus
MQTT_DEVICE_TYPE = "RKDRD"

TERMINAL_TYPE_APP = 11

# Fan command ids (FanMsgMar).
CMD_FAN_STATUS = 132
CMD_FAN_LEVEL = 134
CMD_FAN_LIGHT = 136
# AbsFan.setFanTimeWork / Fan8229.setFanTimeWork: countdown on the current
# gear.  The body carries FanLevel *and* FanTime, and the app always sends the
# level it currently runs on (FanCtr8229View.onStartClock uses fan.level).
CMD_FAN_TIME = 144

# Report pushed by the hood.
REPORT_FAN_STATUS = 131
# Status poll the app sends for non-SSE devices (AbsFan.onPolling).
CMD_FAN_POLL = 130
REPLY_FAN_STATUS = 133
REPLY_FAN_LEVEL = 135
REPLY_FAN_LIGHT = 137
REPLY_FAN_TIME = 145
# The device answers a command with the next odd id and a single RC byte.
COMMAND_REPLIES = {
    CMD_FAN_STATUS: REPLY_FAN_STATUS,
    CMD_FAN_LEVEL: REPLY_FAN_LEVEL,
    CMD_FAN_LIGHT: REPLY_FAN_LIGHT,
    CMD_FAN_TIME: REPLY_FAN_TIME,
}

GUID_LENGTH = 17

# The 131 report ends with four bytes whose meaning the app disambiguates by
# frame length.  A full status report is 23 bytes after the command byte
# (4 base + 19 extended), and on those the byte at offset 4 is the wifi signal
# strength, not the countdown.  On the shorter frames it is FanTime.
STATUS_REPORT_LONG_LENGTH = 19


class RokiMqttError(Exception):
    """Raised when the MQTT broker cannot be reached or rejects the client."""


def mqtt_credentials(app_guid: str, timestamp_ms: int | None = None) -> tuple[str, str, str]:
    """Return ``(client_id, username, password)`` for the ROKI broker."""
    if len(app_guid) != GUID_LENGTH:
        raise ValueError(f"app guid must be {GUID_LENGTH} characters: {app_guid!r}")
    stamp = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    client_id = (
        f"{MQTT_DEVICE_TYPE}.{app_guid}"
        f"|securemode=2,signmethod=hmacsha256,random={stamp}|"
    )
    username = f"{app_guid}&{MQTT_DEVICE_TYPE}"
    sign_source = (
        f"clientid{MQTT_DEVICE_TYPE}.{app_guid}"
        f"deviceGuid{app_guid}"
        f"deviceType{MQTT_DEVICE_TYPE}"
        f"random{stamp}"
    )
    password = hmac.new(
        MQTT_SIGN_KEY.encode("utf-8"),
        sign_source.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return client_id, username, password


def topic_for(guid: str, *, broadcast: bool = False) -> str:
    """Build the topic the app publishes to (unicast) or listens on (broadcast)."""
    if len(guid) != GUID_LENGTH:
        raise ValueError(f"device guid must be {GUID_LENGTH} characters: {guid!r}")
    kind = "b" if broadcast else "u"
    return f"/{kind}/{guid[:5]}/{guid[5:]}"


def encode_fan_command(
    source_guid: str,
    command: int,
    *,
    user_id: str | int = "",
    level: int | None = None,
    on: bool | None = None,
    minutes: int | None = None,
) -> bytes:
    """Encode a fan command the way ``MqttProtocol.encode`` does.

    ``MqttProtocol.encode`` prefixes the frame with ``msg.getSource()``, and
    for a request (``Msg.newRequestMsg``) the source is the *app* guid while
    the target is the device.  So the first 17 bytes are the app guid, the
    device is addressed by the topic.

    Layout: 17-byte source guid, one command byte, then the marshalled body.
    Commands 132/134/136 carry ``TerminalType``, the 10-byte zero-padded user
    id and one value byte.  Command 130 (``AbsFan.onPolling``) carries only
    ``TerminalType``.  Command 144 (``Fan8229.setFanTimeWork``) carries
    ``TerminalType``, the user id, the level to run on and the minutes.
    """
    if command not in (
        CMD_FAN_POLL,
        CMD_FAN_STATUS,
        CMD_FAN_LEVEL,
        CMD_FAN_LIGHT,
        CMD_FAN_TIME,
    ):
        raise ValueError(f"unsupported fan command: {command}")
    if len(source_guid) != GUID_LENGTH:
        raise ValueError(
            f"source guid must be {GUID_LENGTH} characters: {source_guid!r}"
        )

    body = bytearray(source_guid.encode("ascii"))
    body.append(command)
    body.append(TERMINAL_TYPE_APP)
    if command == CMD_FAN_POLL:
        return bytes(body)
    body += str(user_id).encode("utf-8").ljust(10, b"\x00")

    if command == CMD_FAN_TIME:
        if level is None or minutes is None:
            raise ValueError(
                "level and minutes are required for command 144"
            )
        # FanTime travels as a single byte.  Reject rather than mask: `& 0xFF`
        # would silently turn 300 minutes into 44, which is far harder to
        # diagnose than a raised error.
        if not 0 <= minutes <= 255:
            raise ValueError(
                f"minutes must fit in one byte (0-255), got {minutes}"
            )
        body.append(int(level) & 0xFF)
        body.append(int(minutes) & 0xFF)
    elif command == CMD_FAN_LEVEL:
        if level is None:
            raise ValueError("level is required for command 134")
        body.append(int(level) & 0xFF)
    else:
        if on is None:
            raise ValueError("on is required for commands 132/136")
        body.append(1 if on else 0)
    return bytes(body)


def decode_fan_message(payload: bytes) -> tuple[str, int, dict[str, Any]] | None:
    """Decode a fan report or command reply.

    Returns ``(device_guid, command, properties)`` or ``None`` when the frame
    is not a fan message this integration understands.  Command 131 is the
    hood's status report, commands 133/135/137/145 carry the ``rc`` of the
    command they answer.
    """
    if len(payload) < 18:
        return None
    try:
        guid = payload[:GUID_LENGTH].decode("ascii")
    except UnicodeDecodeError:
        return None
    if len(guid) != GUID_LENGTH or not guid.isalnum():
        return None
    command = payload[17]
    body = payload[18:]
    if command == REPORT_FAN_STATUS and len(body) >= 4:
        properties: dict[str, Any] = {
            "workStatus": body[0],
            "level": body[1],
            "lightStatus": body[2],
            "cleanRequired": body[3],
        }
        # FanMsgMar case 131 disambiguates byte 4 with the frame length:
        #   if (length != 19 && value == 2) -> FanWIfi
        #   else                            -> FanTime
        # Reproduced verbatim rather than second-guessed.  It means a genuine
        # 2-minute countdown is reported as the wifi signal on a short frame,
        # and a 2-minute countdown on a long frame is reported correctly; the
        # app lives with that, so we do too.
        extended = len(body) - 4
        if extended:
            value = body[4]
            if extended != STATUS_REPORT_LONG_LENGTH and value == 2:
                properties["wifi"] = value
            else:
                properties["timing"] = value
        return guid, command, properties
    if command in COMMAND_REPLIES.values() and body:
        return guid, command, {"rc": body[0]}
    return None


def _remaining_length(length: int) -> bytes:
    out = bytearray()
    while True:
        byte = length % 128
        length //= 128
        if length:
            byte |= 0x80
        out.append(byte)
        if not length:
            return bytes(out)


def _mqtt_string(value: str) -> bytes:
    raw = value.encode("utf-8")
    return struct.pack(">H", len(raw)) + raw


class RokiMqttClient:
    """A tiny reconnecting MQTT 3.1.1 client (QoS 0, clean session)."""

    def __init__(
        self,
        app_guid: str,
        *,
        host: str = MQTT_HOST,
        port: int = MQTT_PORT,
        on_message: Callable[[str, bytes], None] | None = None,
    ) -> None:
        self.app_guid = app_guid
        self.host = host
        self.port = port
        self.on_message = on_message

        self._topics: set[str] = set()
        self._task: asyncio.Task[None] | None = None
        self._connected = asyncio.Event()
        self._closing = False
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._write_lock = asyncio.Lock()
        self._last_rx = 0.0
        self._ping_task: asyncio.Task[None] | None = None

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def add_topics(self, topics: list[str] | set[str]) -> None:
        """Remember topics; they are (re)subscribed on every connection."""
        self._topics.update(topics)

    async def async_start(self) -> None:
        if self._task is None or self._task.done():
            self._closing = False
            self._task = asyncio.create_task(self._async_run(), name="roki-mqtt")

    async def async_wait_connected(self, timeout: float = 10.0) -> None:
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
        except TimeoutError as err:
            raise RokiMqttError("MQTT connection timed out") from err

    async def async_stop(self) -> None:
        self._closing = True
        self._connected.clear()
        for task in (self._ping_task, self._task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("MQTT task ended: %s", err)
        self._ping_task = None
        self._task = None
        await self._async_close_writer()

    async def async_publish(self, topic: str, payload: bytes, timeout: float = 10.0) -> None:
        await self.async_wait_connected(timeout)
        # The body is: 2-byte topic length + topic + payload.  Forgetting the
        # length prefix here silently corrupts the stream.
        raw_topic = topic.encode("ascii")
        body = struct.pack(">H", len(raw_topic)) + raw_topic + payload
        packet = b"\x30" + _remaining_length(len(body)) + body
        try:
            async with self._write_lock:
                if self._writer is None:
                    raise RokiMqttError("MQTT is not connected")
                self._writer.write(packet)
                await self._writer.drain()
        except (OSError, RuntimeError) as err:
            raise RokiMqttError(f"MQTT publish failed: {err}") from err

    # -- internals --------------------------------------------------------

    async def _async_run(self) -> None:
        backoff = 2.0
        while not self._closing:
            try:
                await self._async_connect_once()
                backoff = 2.0
                self._connected.set()
                _LOGGER.debug("ROKI MQTT connected to %s:%s", self.host, self.port)
                self._ping_task = asyncio.create_task(self._async_ping())
                await self._async_read_loop()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - keep reconnecting
                _LOGGER.debug("ROKI MQTT connection problem: %s", err)
            finally:
                self._connected.clear()
                if self._ping_task is not None:
                    self._ping_task.cancel()
                    try:
                        await self._ping_task
                    except asyncio.CancelledError:
                        pass
                    self._ping_task = None
                await self._async_close_writer()
            if self._closing:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    async def _async_connect_once(self) -> None:
        client_id, username, password = mqtt_credentials(self.app_guid)
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), 10
        )
        variable = _mqtt_string("MQTT") + bytes([4, 0xC2]) + struct.pack(
            ">H", MQTT_KEEPALIVE
        )
        payload = (
            _mqtt_string(client_id) + _mqtt_string(username) + _mqtt_string(password)
        )
        packet = b"\x10" + _remaining_length(len(variable) + len(payload)) + variable + payload
        self._writer.write(packet)
        await self._writer.drain()

        packet_type, body = await asyncio.wait_for(self._read_packet(), 10)
        if packet_type != 2 or len(body) < 2:
            raise RokiMqttError("MQTT CONNACK missing")
        if body[1] != 0:
            raise RokiMqttError(f"MQTT broker rejected the client (code {body[1]})")

        self._last_rx = time.monotonic()
        if self._topics:
            await self._async_subscribe(sorted(self._topics))

    async def _async_subscribe(self, topics: list[str]) -> None:
        if self._writer is None:
            raise RokiMqttError("MQTT is not connected")
        entries = b"".join(_mqtt_string(topic) + b"\x00" for topic in topics)
        packet = (
            b"\x82"
            + _remaining_length(2 + len(entries))
            + struct.pack(">H", 1)
            + entries
        )
        async with self._write_lock:
            self._writer.write(packet)
            await self._writer.drain()

    async def _async_read_loop(self) -> None:
        assert self._reader is not None
        while not self._closing:
            packet_type, body = await self._read_packet()
            self._last_rx = time.monotonic()
            if packet_type == 3:  # PUBLISH
                self._dispatch_publish(body)

    async def _async_ping(self) -> None:
        while not self._closing:
            await asyncio.sleep(MQTT_KEEPALIVE / 2)
            if time.monotonic() - self._last_rx > MQTT_KEEPALIVE * 2.5:
                _LOGGER.debug("ROKI MQTT keepalive expired")
                await self._async_close_writer()
                return
            try:
                async with self._write_lock:
                    if self._writer is not None:
                        self._writer.write(b"\xc0\x00")
                        await self._writer.drain()
            except (OSError, RuntimeError):
                return

    async def _read_packet(self) -> tuple[int, bytes]:
        assert self._reader is not None
        first = await self._reader.readexactly(1)
        multiplier = 1
        length = 0
        while True:
            byte = (await self._reader.readexactly(1))[0]
            length += (byte & 0x7F) * multiplier
            if not byte & 0x80:
                break
            multiplier *= 128
            if multiplier > 128**3:
                raise RokiMqttError("malformed MQTT length")
        body = await self._reader.readexactly(length) if length else b""
        return first[0] >> 4, body

    def _dispatch_publish(self, body: bytes) -> None:
        if len(body) < 2:
            return
        topic_length = struct.unpack(">H", body[:2])[0]
        start = 2 + topic_length
        if len(body) < start:
            return
        topic = body[2:start].decode("ascii", "replace")
        payload = body[start:]
        if self.on_message is None:
            return
        try:
            self.on_message(topic, payload)
        except Exception:
            _LOGGER.exception("ROKI MQTT message handler failed")

    async def _async_close_writer(self) -> None:
        writer, self._writer = self._writer, None
        self._reader = None
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Closing the MQTT socket failed: %s", err)
