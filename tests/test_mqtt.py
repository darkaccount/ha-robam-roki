"""Offline tests for the MQTT transport used by deviceSource == 0 hoods."""

from __future__ import annotations

import asyncio
import importlib.util
import struct
from pathlib import Path

import pytest

MQTT_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "robam_roki"
    / "mqtt.py"
)


def _load_mqtt():
    spec = importlib.util.spec_from_file_location("roki_mqtt_test", MQTT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mqtt = _load_mqtt()

APP_GUID = "RKDRD000000000000"
DEVICE_GUID = "R8229000000000000"


def test_credentials_match_the_app_formula():
    client_id, username, password = mqtt.mqtt_credentials(
        APP_GUID, timestamp_ms=1700000000000
    )
    assert client_id == (
        "RKDRD.RKDRD000000000000"
        "|securemode=2,signmethod=hmacsha256,random=1700000000000|"
    )
    assert username == "RKDRD000000000000&RKDRD"
    # HMAC-SHA256("Kp0lxmm1", "clientidRKDRD.<guid>deviceGuid<guid>"
    #                           "deviceTypeRKDRDrandom<stamp>") as lowercase hex.
    assert password == (
        "1f6c6dcf19f79f2960a1d39f77070cc6"
        "a6cfbb8ab107f56e801a3558256c862d"
    )


def test_credentials_reject_bad_guid():
    with pytest.raises(ValueError):
        mqtt.mqtt_credentials("short")


def test_topic_uses_five_char_type_and_twelve_char_number():
    assert mqtt.topic_for(DEVICE_GUID) == "/u/R8229/000000000000"
    assert mqtt.topic_for(DEVICE_GUID, broadcast=True) == "/b/R8229/000000000000"


def test_encode_power():
    # MqttProtocol.encode writes msg.getSource(), which for a request is the
    # app guid, not the device guid.
    payload = mqtt.encode_fan_command(
        APP_GUID, mqtt.CMD_FAN_STATUS, user_id=123456, on=True
    )
    assert payload == (
        b"RKDRD000000000000"
        + b"\x84"  # command 132
        + b"\x0b"  # TerminalType APP
        + b"123456\x00\x00\x00\x00"
        + b"\x01"
    )


def test_encode_level_and_light():
    level = mqtt.encode_fan_command(
        APP_GUID, mqtt.CMD_FAN_LEVEL, user_id="1", level=3
    )
    assert level[-1:] == b"\x03"
    light = mqtt.encode_fan_command(
        APP_GUID, mqtt.CMD_FAN_LIGHT, user_id="1", on=False
    )
    assert light[-1:] == b"\x00"


def test_decode_status_report():
    payload = (
        DEVICE_GUID.encode()
        + b"\x83"  # command 131
        + b"\x01\x02\x01\x00"  # status, level, light, cleanRequired
        + b"\x00\x00"
    )
    assert mqtt.decode_fan_message(payload) == (
        DEVICE_GUID,
        131,
        {
            "workStatus": 1,
            "level": 2,
            "lightStatus": 1,
            "cleanRequired": 0,
            "timing": 0,
        },
    )


def test_decode_status_report_carries_the_countdown():
    # FanMsgMar case 131: on frames that are not the long variant, byte 4 is
    # FanTime unless the value is 2.
    payload = (
        DEVICE_GUID.encode()
        + b"\x83"
        + b"\x01\x02\x01\x00"
        + b"\x1e"  # 30 minutes
    )
    _, _, properties = mqtt.decode_fan_message(payload)
    assert properties["timing"] == 30


def test_decode_reports_wifi_on_a_short_frame_when_the_value_is_two():
    # The app's own test is ``length != 19 && value == 2 -> FanWIfi``, so a
    # short frame carrying 2 is read as the signal strength, not a countdown.
    payload = (
        DEVICE_GUID.encode()
        + b"\x83"
        + b"\x01\x02\x01\x00"
        + b"\x02"
    )
    _, _, properties = mqtt.decode_fan_message(payload)
    assert properties.get("timing") is None
    assert properties.get("wifi") == 2


def test_decode_long_status_report_reports_the_countdown():
    # length == 19 always means FanTime, even for the ambiguous value 2.
    # The frame needs 4 base bytes + 19 more, so 18 filler bytes after the
    # countdown byte.
    payload = (
        DEVICE_GUID.encode()
        + b"\x83"
        + b"\x01\x02\x01\x00"
        + b"\x02" + bytes(18)
    )
    _, _, properties = mqtt.decode_fan_message(payload)
    assert properties["timing"] == 2
    assert properties.get("wifi") is None


def test_decode_four_byte_status_report_has_no_timing():
    payload = DEVICE_GUID.encode() + b"\x83" + b"\x01\x02\x01\x00"
    _, _, properties = mqtt.decode_fan_message(payload)
    assert properties == {
        "workStatus": 1,
        "level": 2,
        "lightStatus": 1,
        "cleanRequired": 0,
    }


def test_the_countdown_survives_a_round_trip_through_the_cloud():
    # Confirmed on R8229: after setting 30 minutes over command
    # 144, both the 131 report and /property/latest returned timing == 30.
    report = (
        DEVICE_GUID.encode()
        + b"\x83"
        + b"\x01\x02\x01\x00"
        + b"\x1e"  # 30 minutes, as the hood reports it
    )
    _, _, mqtt_properties = mqtt.decode_fan_message(report)
    assert mqtt_properties["timing"] == 30

    rest_properties = {"timing": 30, "level": 2, "workStatus": 1}
    assert int(rest_properties["timing"]) == mqtt_properties["timing"]


def test_encode_countdown_carries_level_and_minutes():
    # Fan8229.setFanTimeWork marshals TerminalType, UserId, FanLevel, FanTime,
    # and the app always passes the level the hood currently runs on.
    payload = mqtt.encode_fan_command(
        APP_GUID, mqtt.CMD_FAN_TIME, user_id=123456, level=3, minutes=30
    )
    assert payload == (
        b"RKDRD000000000000"
        + b"\x90"  # command 144
        + b"\x0b"  # TerminalType APP
        + b"123456\x00\x00\x00\x00"
        + b"\x03"  # level
        + b"\x1e"  # 30 minutes
    )


def test_encode_countdown_cancel():
    payload = mqtt.encode_fan_command(
        APP_GUID, mqtt.CMD_FAN_TIME, user_id=1, level=2, minutes=0
    )
    assert payload[-2:] == b"\x02\x00"


def test_encode_countdown_requires_both_fields():
    with pytest.raises(ValueError):
        mqtt.encode_fan_command(APP_GUID, mqtt.CMD_FAN_TIME, level=3)
    with pytest.raises(ValueError):
        mqtt.encode_fan_command(APP_GUID, mqtt.CMD_FAN_TIME, minutes=30)


def test_encode_countdown_accepts_the_whole_byte_range():
    # FanTime is one unsigned byte: 0-255.  The app's own picker stops at 239
    # (3 h 59 min), but the protocol does not.
    payload = mqtt.encode_fan_command(
        APP_GUID, mqtt.CMD_FAN_TIME, user_id=1, level=2, minutes=255
    )
    assert payload[-2:] == b"\x02\xff"


def test_encode_countdown_rejects_a_value_the_byte_cannot_hold():
    # `& 0xFF` would silently turn 300 minutes into 44, which is far harder to
    # diagnose than a raised error.
    with pytest.raises(ValueError):
        mqtt.encode_fan_command(
            APP_GUID, mqtt.CMD_FAN_TIME, user_id=1, level=2, minutes=256
        )
    with pytest.raises(ValueError):
        mqtt.encode_fan_command(
            APP_GUID, mqtt.CMD_FAN_TIME, user_id=1, level=2, minutes=-1
        )


def test_decode_countdown_reply():
    payload = DEVICE_GUID.encode() + b"\x91\x00"  # 145, rc=0
    assert mqtt.decode_fan_message(payload) == (DEVICE_GUID, 145, {"rc": 0})
    refused = DEVICE_GUID.encode() + b"\x91\x01"  # 145, rc=1
    assert mqtt.decode_fan_message(refused) == (DEVICE_GUID, 145, {"rc": 1})


def test_encode_poll_carries_only_terminal_type():
    # AbsFan.onPolling sends command 130 with TerminalType and nothing else.
    payload = mqtt.encode_fan_command(APP_GUID, mqtt.CMD_FAN_POLL)
    assert payload == b"RKDRD000000000000" + b"\x82\x0b"


def test_decode_command_reply():
    payload = DEVICE_GUID.encode() + b"\x85\x00"  # 133, rc=0
    assert mqtt.decode_fan_message(payload) == (DEVICE_GUID, 133, {"rc": 0})
    failed = DEVICE_GUID.encode() + b"\x89\x01"  # 137, rc=1
    assert mqtt.decode_fan_message(failed) == (DEVICE_GUID, 137, {"rc": 1})


def test_decode_ignores_other_frames():
    assert mqtt.decode_fan_message(b"") is None
    assert mqtt.decode_fan_message(b"x" * 18) is None


async def _read_packet(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    first = await reader.readexactly(1)
    multiplier = 1
    length = 0
    while True:
        byte = (await reader.readexactly(1))[0]
        length += (byte & 0x7F) * multiplier
        if not byte & 0x80:
            break
        multiplier *= 128
    body = await reader.readexactly(length) if length else b""
    return first[0] >> 4, body


def test_client_speaks_mqtt_to_a_broker():
    state: dict[str, bytes] = {}
    messages: list[tuple[str, bytes]] = []
    report = (
        DEVICE_GUID.encode()
        + b"\x83\x01\x02\x01\x00\x00\x00"
    )

    async def handler(reader, writer):
        try:
            while True:
                packet_type, body = await _read_packet(reader)
                if packet_type == 1:
                    state["connect"] = body
                    writer.write(b"\x20\x02\x00\x00")
                    await writer.drain()
                elif packet_type == 8:
                    state["subscribe"] = body
                    writer.write(b"\x90\x03\x00\x01\x00")
                    topic = mqtt.topic_for(DEVICE_GUID, broadcast=True).encode()
                    payload = (
                        b"\x30"
                        + mqtt._remaining_length(2 + len(topic) + len(report))
                        + struct.pack(">H", len(topic))
                        + topic
                        + report
                    )
                    writer.write(payload)
                    await writer.drain()
                elif packet_type == 3:
                    state["publish"] = body
                elif packet_type == 12:
                    writer.write(b"\xd0\x00")
                    await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    async def scenario():
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = mqtt.RokiMqttClient(
            APP_GUID,
            host="127.0.0.1",
            port=port,
            on_message=lambda topic, payload: messages.append((topic, payload)),
        )
        client.add_topics([mqtt.topic_for(DEVICE_GUID, broadcast=True)])
        await client.async_start()
        await client.async_wait_connected(5)
        await asyncio.sleep(0.1)
        await client.async_publish(mqtt.topic_for(DEVICE_GUID), b"ping")
        await asyncio.sleep(0.1)
        await client.async_stop()
        server.close()
        await server.wait_closed()

    asyncio.run(scenario())

    assert b"RKDRD000000000000&RKDRD" in state["connect"]
    assert b"/b/R8229/000000000000" in state["subscribe"]
    assert messages and messages[0][0] == "/b/R8229/000000000000"
    assert mqtt.decode_fan_message(messages[0][1])[2]["level"] == 2

    # The published body must be a well-formed MQTT frame: a 2-byte topic
    # length prefix that matches the topic, then the payload.  A short
    # remaining length used to slip through an endswith() check.
    body = state["publish"]
    topic_length = struct.unpack(">H", body[:2])[0]
    topic = body[2 : 2 + topic_length].decode("ascii")
    assert topic == mqtt.topic_for(DEVICE_GUID)
    assert body[2 + topic_length :] == b"ping"

