#!/usr/bin/env python3
"""Standalone ROKI cloud probe / control tool.

This talks to the same cloud API as the Home Assistant integration and is
meant for verifying an account and a device before relying on the integration:

    python tools/roki_cli.py login --phone 13800000000
    python tools/roki_cli.py devices --phone 13800000000
    python tools/roki_cli.py info --phone 13800000000 --device R1234...
    python tools/roki_cli.py watch --phone 13800000000 --device R1234...
    python tools/roki_cli.py power --phone 13800000000 --device R1234... on
    python tools/roki_cli.py level --phone 13800000000 --device R1234... 2
    python tools/roki_cli.py light --phone 13800000000 --device R1234... on

The password is read from ``--password``, ``ROKI_PASSWORD`` or an interactive
prompt (recommended).  Credentials are never written to disk.

``collect`` is read-only: the only frame it publishes is the status poll
(command 130), and it never sends a power/level/light/timing/setprop command.

Requires ``aiohttp`` and ``cryptography``.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import importlib.util
import json
import os
import platform
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

API_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "robam_roki"
    / "api.py"
)

MQTT_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "robam_roki"
    / "mqtt.py"
)

REDACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "robam_roki"
    / "redact.py"
)

DRAFT_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "robam_roki"
    / "draft.py"
)


def load_api():
    spec = importlib.util.spec_from_file_location("roki_api", API_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise RuntimeError(f"Cannot load {API_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_mqtt():
    spec = importlib.util.spec_from_file_location("roki_mqtt", MQTT_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise RuntimeError(f"Cannot load {MQTT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_standalone(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise RuntimeError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    # Register before executing: dataclasses resolves string annotations through
    # sys.modules on Python 3.13.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_redact():
    return _load_standalone("roki_redact", REDACT_PATH)


def load_draft():
    return _load_standalone("roki_draft", DRAFT_PATH)


try:
    api = load_api()
    mqtt = load_mqtt()
except ImportError as err:  # pragma: no cover - friendly CLI failure
    print(
        f"Missing dependency: {err}\n"
        "Install with: python -m pip install -r tools/requirements.txt",
        file=sys.stderr,
    )
    raise SystemExit(3) from err


def get_credentials(args: argparse.Namespace) -> tuple[str, str]:
    phone = args.phone or os.environ.get("ROKI_PHONE")
    if not phone:
        phone = input("Phone number: ").strip()
    password = args.password or os.environ.get("ROKI_PASSWORD")
    if not password:
        password = getpass.getpass("Password: ")
    if not phone or not password:
        raise SystemExit("Phone number and password are required")
    return phone, password


def dump(value: Any, as_json: bool = False) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


async def with_client(args: argparse.Namespace, action):
    import aiohttp

    phone, password = get_credentials(args)
    timeout = aiohttp.ClientTimeout(total=45)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        client = api.RokiClient(
            session, phone, password, base_url=args.base_url
        )
        # Keep the app guid stable between runs (the cloud creates it from this
        # token): the MQTT client id must stay the same across restarts if you
        # want one broker session instead of a new one every time.
        client.device_token = os.environ.get("ROKI_DEVICE_TOKEN") or hashlib.md5(
            f"roki-cli:{platform.node()}".encode()
        ).hexdigest()[:16]
        try:
            await client.async_login()
            return await action(client, args)
        except api.RokiAuthError as err:
            print(f"Authentication failed: {err}", file=sys.stderr)
            raise SystemExit(2)
        except api.RokiApiError as err:
            print(f"API error: {err}", file=sys.stderr)
            raise SystemExit(1)


async def _find_device(client, guid: str) -> dict[str, Any] | None:
    devices = await client.async_get_devices()
    for device in devices:
        if str(device.get("guid")) == guid:
            return device
    return None


def _transport(device: dict[str, Any], requested: str) -> str:
    if requested != "auto":
        return requested
    # The app uses the REST IoT API for deviceSource == 1 and MQTT otherwise.
    return "rest" if str(device.get("deviceSource")) == "1" else "mqtt"


async def _mqtt_session(client, device_ids: list[str], *, verbose: bool = False):
    app_guid = await client.async_get_app_guid()
    if not app_guid:
        raise SystemExit("The cloud did not return an app guid")
    if not client.user_id:
        await client.async_get_user()
    # The app binds the app guid to the account right after login; the broker
    # only routes device traffic to a bound guid.
    await client.async_bind_app_guid()

    def on_message(topic: str, payload: bytes) -> None:
        if not verbose:
            return
        decoded = mqtt.decode_fan_message(payload)
        if decoded:
            guid, command, props = decoded
            print(
                json.dumps(
                    {"deviceId": guid, "command": command, "data": props},
                    ensure_ascii=False,
                ),
                flush=True,
            )
        else:
            print(
                json.dumps({"topic": topic, "payload": payload.hex()}),
                flush=True,
            )

    session = mqtt.RokiMqttClient(app_guid, on_message=on_message)
    topics = {mqtt.topic_for(guid) for guid in device_ids}
    topics.update(mqtt.topic_for(guid, broadcast=True) for guid in device_ids)
    topics.add(mqtt.topic_for(app_guid))
    session.add_topics(topics)
    await session.async_start()
    await session.async_wait_connected(20)
    print(f"MQTT connected as {app_guid} (userId={client.user_id})")
    return session


async def cmd_mqttcheck(client, args):
    """Print everything needed to judge the MQTT channel in one shot."""
    devices = await client.async_get_devices()
    device = None
    for candidate in devices:
        if str(candidate.get("guid")) == args.device:
            device = candidate
            break
    if device is None:
        raise SystemExit(f"Device {args.device} is not on this account")

    props = await client.async_get_properties(args.device)
    last_update = props.get("lastUpdateTime")
    print("device      :", device.get("name"))
    print("guid        :", args.device)
    print("deviceSource:", device.get("deviceSource"))
    print("isConnected :", device.get("isConnected"))
    print("userId      :", client.user_id)
    print("appGuid     :", client.app_guid)
    print("properties  :", json.dumps(props, ensure_ascii=False))
    if last_update:
        age = int(time.time() * 1000) - int(last_update)
        print(f"cloud cache : last updated {age} ms ago")
        if age > 120_000:
            print("             ^ the cloud has not heard from the hood "
                  "recently: it is probably offline or in standby")

    session = await _mqtt_session(client, [args.device])
    try:
        await asyncio.sleep(5)
    finally:
        await session.async_stop()


async def _send_command(client, args, kind: str, *, on: bool | None = None,
                        level: int | None = None) -> None:
    device = await _find_device(client, args.device)
    if device is None:
        raise SystemExit(f"Device {args.device} is not on this account")
    transport = _transport(device, args.transport)
    print(f"transport: {transport}")
    if transport == "rest":
        if kind == "power":
            await client.async_set_power(args.device, bool(on))
        elif kind == "level":
            await client.async_set_level(args.device, int(level or 1))
        else:
            await client.async_set_light(args.device, bool(on))
        return

    commands = {"power": mqtt.CMD_FAN_STATUS, "level": mqtt.CMD_FAN_LEVEL,
                "light": mqtt.CMD_FAN_LIGHT}
    session = await _mqtt_session(client, [args.device], verbose=True)
    try:
        # The frame starts with the *app* guid: the device is addressed by the
        # topic, see MqttProtocol.encode.
        payload = mqtt.encode_fan_command(
            session.app_guid,
            commands[kind],
            user_id=client.user_id or "",
            level=level,
            on=on,
        )
        # AbsFan.onPolling: ask for the state first, the hood then accepts the
        # command (it answers 131 to the poll and 133/135/137 to the command).
        await session.async_publish(
            mqtt.topic_for(args.device),
            mqtt.encode_fan_command(session.app_guid, mqtt.CMD_FAN_POLL),
        )
        await session.async_publish(mqtt.topic_for(args.device), payload)
        print(f"published {len(payload)} bytes to {mqtt.topic_for(args.device)}")
        await asyncio.sleep(2)
    finally:
        await session.async_stop()


async def cmd_levels(client, args):
    """Report the ladder the integration would use, and what the hood reports.

    This is deliberately read-only: it sends the same status poll (130) the app
    already sends every couple of seconds and nothing else.  No level command
    is ever published here, so running it cannot start the motor.
    """
    const = _load_const()
    devices = await client.async_get_devices()
    if args.device:
        devices = [
            device
            for device in devices
            if str(device.get("guid")) == args.device
        ]
        if not devices:
            raise SystemExit(f"Device {args.device} is not on this account")

    report: list[dict[str, Any]] = []
    for device in devices:
        guid = str(device.get("guid"))
        model = str(device.get("dt") or "")
        ladder = const.levels_for_model(model)
        source = const.levels_source_for_model(model)
        try:
            props = await client.async_get_properties(guid)
        except api.RokiError as err:
            props = {"error": str(err)}
        entry = {
            "guid": guid,
            "name": device.get("name"),
            "dt": model,
            "dc": device.get("dc"),
            "deviceSource": device.get("deviceSource"),
            "levels": ladder,
            "levels_source": source,
            "known_model": model.upper() in const.MODEL_LEVELS,
            "properties": props,
        }
        report.append(entry)

    if args.json:
        dump(report)
        return

    for entry in report:
        print(f"{entry['guid']}  {entry['name']}")
        print(f"  dt={entry['dt']} dc={entry['dc']} "
              f"deviceSource={entry['deviceSource']}")
        print(f"  levels      : {entry['levels']}")
        print(f"  levels_from : {entry['levels_source']}")
        if not entry["known_model"]:
            print("  ! this model is not in the table; the linear fallback "
                  "is used")
        print(f"  properties  : "
              f"{json.dumps(entry['properties'], ensure_ascii=False)}")

    print()
    print("Passive verification:")
    print("  1. keep this running (or use `watch`)")
    print("  2. press the gear buttons on the hood itself / in the app")
    print("  3. compare the `level` values reported above with the table")
    print()
    print("To watch the reported levels for a while and print every distinct")
    print("value the hood uses:")
    print(f"  python {Path(__file__).name} watch --device <guid> --levels")


async def _watch_mqtt_levels(
    client,
    device_ids: list[str],
    seconds: float,
    *,
    session_factory=None,
) -> None:
    """Collect the levels a hood reports without ever writing to it.

    ``session_factory`` is only there so tests can inject a fake broker
    session; production code lets it default to the real MQTT client.
    """
    observed: set[int] = set()

    def handler(topic: str, payload: bytes) -> None:
        decoded = mqtt.decode_fan_message(payload)
        if not decoded or "rc" in decoded[2]:
            return
        guid, command, props = decoded
        if command == mqtt.REPORT_FAN_STATUS:
            level = props.get("level")
            if level is not None and level not in observed:
                observed.add(level)
                print(
                    json.dumps(
                        {"deviceId": guid, "new_level": level},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

    app_guid = await client.async_get_app_guid()
    if not app_guid:
        raise SystemExit("The cloud did not return an app guid")
    if not client.user_id:
        await client.async_get_user()
    factory = session_factory or mqtt.RokiMqttClient
    session = factory(app_guid, on_message=handler)
    topics = {mqtt.topic_for(guid) for guid in device_ids}
    topics.update(mqtt.topic_for(guid, broadcast=True) for guid in device_ids)
    topics.add(mqtt.topic_for(app_guid))
    session.add_topics(topics)
    await session.async_start()
    await session.async_wait_connected(20)
    print(f"MQTT connected as {app_guid}; collecting levels for {seconds:.0f}s")
    print("Press the gear buttons on the hood now. Ctrl+C stops early.")
    try:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            # Read-only: the same 130 poll AbsFan.onPolling sends.
            for guid in device_ids:
                payload = mqtt.encode_fan_command(
                    session.app_guid, mqtt.CMD_FAN_POLL
                )
                await session.async_publish(mqtt.topic_for(guid), payload)
            await asyncio.sleep(2)
    finally:
        await session.async_stop()
        print(f"levels observed: {sorted(observed)}")


def _load_const():
    const_path = (
        Path(__file__).resolve().parents[1]
        / "custom_components"
        / "robam_roki"
        / "const.py"
    )
    spec = importlib.util.spec_from_file_location("roki_const", const_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def cmd_timing(client, args):
    """Set or clear the countdown in minutes (MQTT command 144)."""
    device = await _find_device(client, args.device)
    if device is None:
        raise SystemExit(f"Device {args.device} is not on this account")
    transport = _transport(device, args.transport)
    # 0-240: the protocol's FanTime is one byte (255 max) and the app's own
    # hour+minute picker stops at 239.  240 is verified on real hardware.
    minutes = max(0, min(240, int(args.minutes)))
    print(f"transport: {transport}; countdown: {minutes} min")
    if transport == "rest":
        raise SystemExit(
            "Countdown over REST is not verified; the app calls set_air_fresh "
            "on IoT devices, which is not a countdown"
        )

    session = await _mqtt_session(client, [args.device], verbose=True)
    try:
        # 144 carries the level to run on, and the app always sends the level
        # the hood is currently on (FanCtr8229View.onStartClock).  Sending it
        # with the wrong level would restart the fan at another gear.
        props = await client.async_get_properties(args.device)
        level = int(props.get("level") or 0)
        if level <= 0:
            raise SystemExit(
                "The hood is not running (level 0); the countdown would stop it"
            )
        payload = mqtt.encode_fan_command(
            session.app_guid,
            mqtt.CMD_FAN_TIME,
            user_id=client.user_id or "",
            level=level,
            minutes=minutes,
        )
        # The app polls (130) before every command.
        await session.async_publish(
            mqtt.topic_for(args.device),
            mqtt.encode_fan_command(session.app_guid, mqtt.CMD_FAN_POLL),
        )
        await session.async_publish(mqtt.topic_for(args.device), payload)
        print(f"published {len(payload)} bytes to {mqtt.topic_for(args.device)}")
        await asyncio.sleep(3)
    finally:
        await session.async_stop()

    props = await client.async_get_properties(args.device)
    print(json.dumps(props, ensure_ascii=False, indent=2))


async def cmd_poll(client, args):
    """Ask the hood for its state over MQTT (command 130)."""
    device = await _find_device(client, args.device)
    if device is None:
        raise SystemExit(f"Device {args.device} is not on this account")
    transport = _transport(device, args.transport)
    print(f"transport: {transport}")
    if transport == "rest":
        props = await client.async_get_properties(args.device)
        print(json.dumps(props, ensure_ascii=False, indent=2))
        return

    session = await _mqtt_session(client, [args.device], verbose=True)
    try:
        payload = mqtt.encode_fan_command(session.app_guid, mqtt.CMD_FAN_POLL)
        await session.async_publish(mqtt.topic_for(args.device), payload)
        print(f"published {len(payload)} bytes to {mqtt.topic_for(args.device)}")
        await asyncio.sleep(3)
    finally:
        await session.async_stop()


async def cmd_dump(client, args):
    """Dump sanitized diagnostics for issue reporting."""
    user_id = client.user_id or await client.async_get_user()
    app_guid = client.app_guid or await client.async_get_app_guid()
    devices = await client.async_get_devices()

    sanitized_devices = []
    for d in devices:
        item = dict(d)
        guid = str(item.get("guid", ""))
        item["guid_masked"] = f"{guid[:4]}...{guid[-4:]}" if len(guid) >= 8 else "**REDACTED**"
        item["guid"] = item["guid_masked"]
        if "mac" in item:
            item["mac"] = "**REDACTED**"
        if "deviceToken" in item:
            item["deviceToken"] = "**REDACTED**"
        
        # Try fetching properties
        try:
            props = await client.async_get_properties(guid)
            item["properties"] = props
        except Exception as err:
            item["properties_error"] = str(err)
        sanitized_devices.append(item)

    payload = {
        "user_id_masked": f"***{str(user_id)[-2:]}" if user_id else None,
        "app_guid_masked": f"{str(app_guid)[:4]}...{str(app_guid)[-4:]}" if app_guid else None,
        "device_count": len(devices),
        "devices": sanitized_devices,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


async def cmd_login(client, args):
    await client.async_get_app_guid()
    devices = await client.async_get_devices()
    print("Login OK")
    print(f"  user id   : {client.user_id}")
    print(f"  token type: {client.token_type}")
    print(f"  app guid  : {client.app_guid}")
    print(f"  devices   : {len(devices)}")
    for device in devices:
        print(
            "  - {guid}  {name}  dt={dt} source={src} online={online}".format(
                guid=device.get("guid"),
                name=device.get("name"),
                dt=device.get("dt"),
                src=device.get("deviceSource"),
                online=device.get("isConnected"),
            )
        )
    if args.json:
        dump(
            {
                "user_id": client.user_id,
                "app_guid": client.app_guid,
                "devices": devices,
            }
        )


async def cmd_devices(client, args):
    devices = await client.async_get_devices()
    if args.json:
        dump(devices)
        return
    for device in devices:
        guid = str(device.get("guid"))
        print(f"{guid}  {device.get('name')}")
        print(f"  dt={device.get('dt')} dc={device.get('dc')} "
              f"deviceSource={device.get('deviceSource')} "
              f"isConnected={device.get('isConnected')}")
        try:
            props = await client.async_get_properties(guid)
        except api.RokiError as err:
            print(f"  properties: ERROR {err}")
            continue
        print(f"  properties: {json.dumps(props, ensure_ascii=False)}")


async def cmd_info(client, args):
    device = await _find_device(client, args.device)
    if device is None:
        raise SystemExit(f"Device {args.device} is not on this account")
    props = await client.async_get_properties(args.device)
    result = {"device": device, "properties": props}
    if args.json:
        dump(result)
        return
    print(json.dumps(result, ensure_ascii=False, indent=2))


async def cmd_power(client, args):
    on = args.state.lower() in ("on", "1", "true")
    await _send_command(client, args, "power", on=on)
    props = await client.async_get_properties(args.device)
    print(json.dumps(props, ensure_ascii=False, indent=2))


async def cmd_level(client, args):
    await _send_command(client, args, "level", level=args.level)
    props = await client.async_get_properties(args.device)
    print(json.dumps(props, ensure_ascii=False, indent=2))


async def cmd_light(client, args):
    on = args.state.lower() in ("on", "1", "true")
    await _send_command(client, args, "light", on=on)
    props = await client.async_get_properties(args.device)
    print(json.dumps(props, ensure_ascii=False, indent=2))


async def cmd_invoke(client, args):
    params = json.loads(args.params) if args.params else {}
    result = await client.async_invoke(args.device, args.function, params)
    print(json.dumps(result, ensure_ascii=False, indent=2))


async def cmd_setprop(client, args):
    try:
        value = json.loads(args.value)
    except json.JSONDecodeError:
        value = args.value
    result = await client.async_set_properties(args.device, {args.key: value})
    print(json.dumps(result, ensure_ascii=False, indent=2))


async def _watch_mqtt(client, device_ids: list[str]) -> None:
    def handler(topic: str, payload: bytes) -> None:
        decoded = mqtt.decode_fan_message(payload)
        if decoded:
            guid, command, props = decoded
            if "rc" in props:
                # 133/135/137: the hood's answer to a published command.
                print(
                    json.dumps(
                        {
                            "deviceId": guid,
                            "command": command,
                            "rc": props["rc"],
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                return
            print(
                json.dumps(
                    {"deviceId": guid, "command": command, "data": props},
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return
        print(
            json.dumps({"topic": topic, "payload": payload.hex()}),
            flush=True,
        )

    app_guid = await client.async_get_app_guid()
    if not app_guid:
        raise SystemExit("The cloud did not return an app guid")
    if not client.user_id:
        await client.async_get_user()
    session = mqtt.RokiMqttClient(app_guid, on_message=handler)
    topics = {mqtt.topic_for(guid, broadcast=True) for guid in device_ids}
    topics.update(mqtt.topic_for(guid) for guid in device_ids)
    topics.add(mqtt.topic_for(app_guid))
    session.add_topics(topics)
    await session.async_start()
    await session.async_wait_connected(20)
    print(f"MQTT connected as {app_guid}; topics: {', '.join(sorted(topics))}")
    print("Press Ctrl+C to stop.\n")
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await session.async_stop()


async def _watch_sse(client, device_ids: list[str]) -> None:
    connect_id = await client.async_get_app_guid()
    print(f"Watching {', '.join(device_ids)} via SSE (connectId={connect_id})")
    async for event in client.async_sse_events(
        device_ids, connect_id=connect_id or "", uid=client.user_id or ""
    ):
        print(json.dumps(event, ensure_ascii=False, default=str), flush=True)


async def cmd_watch(client, args):
    devices = await client.async_get_devices()
    if args.device:
        selected = [
            device
            for device in devices
            if str(device.get("guid")) == args.device
        ]
        if not selected:
            raise SystemExit(f"Device {args.device} is not on this account")
    else:
        selected = devices
    if not selected:
        raise SystemExit("No devices to watch")

    rest_ids = [
        str(device["guid"])
        for device in selected
        if _transport(device, "auto") == "rest"
    ]
    mqtt_ids = [
        str(device["guid"])
        for device in selected
        if _transport(device, "auto") != "rest"
    ]

    if args.levels is not None:
        if not mqtt_ids:
            raise SystemExit(
                "--levels only applies to MQTT devices (deviceSource == 0); "
                "REST devices report their level through SSE already"
            )
        try:
            await _watch_mqtt_levels(client, mqtt_ids, args.levels)
        except KeyboardInterrupt:
            pass
        return

    tasks = []
    if mqtt_ids:
        tasks.append(_watch_mqtt(client, mqtt_ids))
    if rest_ids:
        tasks.append(_watch_sse(client, rest_ids))
    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        pass


# -- profile collection --------------------------------------------------


class _Collector:
    """Accumulate decoded frames into one compressed, redacted bundle.

    A hood repeats the same status frame on every poll, so only frames whose
    properties actually changed are kept; the raw body is stored compactly as
    [offset_ms, command, body_hex] (the 17-byte source address is dropped so a
    bundle can be shared without leaking the account/app identifier).
    """

    def __init__(
        self,
        guid: str,
        *,
        max_frames: int = 5000,
        max_bytes: int = 5 * 1024 * 1024,
        auto_observations: bool = True,
    ) -> None:
        self.guid = guid
        self.max_frames = max(1, int(max_frames))
        self.max_bytes = max(1, int(max_bytes))
        self.auto_observations = auto_observations
        self.started = time.monotonic()
        self.properties: dict[str, Any] = {}
        self.frames: list[list[Any]] = []
        self.observations: list[dict[str, Any]] = []
        self.truncated = False
        self._bytes = 0
        # Baseline for the first labelled mark: the state when capture started.
        self._mark_snapshot: dict[str, Any] | None = None

    def offset_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)

    @staticmethod
    def _diff(
        before: dict[str, Any], after: dict[str, Any]
    ) -> dict[str, dict[str, Any]]:
        changes: dict[str, dict[str, Any]] = {}
        for key, value in after.items():
            if before.get(key) != value:
                changes[key] = {"from": before.get(key), "to": value}
        for key, value in before.items():
            if key not in after:
                changes[key] = {"from": value, "to": None}
        return changes

    def handle_payload(self, payload: bytes) -> None:
        decoded = mqtt.decode_fan_message(payload)
        if decoded is None:
            return
        guid, command, props = decoded
        if str(guid) != self.guid or "rc" in props:
            return
        changes = self._diff(self.properties, props)
        if not changes:
            # Structural compression: the same snapshot is only stored once.
            return
        if self.truncated:
            return
        body = payload[18:] if len(payload) > 18 else b""
        if (
            len(self.frames) >= self.max_frames
            or self._bytes + len(body) > self.max_bytes
        ):
            self.truncated = True
            return
        offset = self.offset_ms()
        self.frames.append([offset, command, body.hex()])
        self._bytes += len(body)
        self.properties.update(props)
        if self._mark_snapshot is None:
            self._mark_snapshot = dict(self.properties)
        if self.auto_observations:
            self.observations.append(
                {"t": offset, "label": "auto", "changes": changes}
            )

    def set_properties(self, props: dict[str, Any]) -> None:
        self.properties = dict(props)
        if self._mark_snapshot is None:
            self._mark_snapshot = dict(self.properties)

    def mark(self, label: str) -> None:
        """Record one labelled observation holding only the changed keys."""
        before = self._mark_snapshot or {}
        changes = self._diff(before, self.properties)
        self.observations.append(
            {"t": self.offset_ms(), "label": label, "changes": changes}
        )
        self._mark_snapshot = dict(self.properties)

    def bundle(self, device: dict[str, Any], transport: str) -> dict[str, Any]:
        redact = load_redact()
        observations = [
            {
                "t": entry["t"],
                "label": entry["label"],
                "changes": redact.redact_properties(entry["changes"]),
            }
            for entry in self.observations
        ]
        data: dict[str, Any] = {
            "schema": "roki-bundle/1",
            "created": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "device": {
                "dc": device.get("dc"),
                "dt": device.get("dt"),
                "transport": transport,
            },
            "properties": redact.redact_properties(self.properties),
            "frames": self.frames,
            "observations": observations,
            "truncated": self.truncated,
        }
        if transport == "rest":
            data["frames_unavailable"] = "rest"
        return data


async def _collect_session(client, handler, device_guid: str):
    """Open the read-only MQTT subscription used by collect."""
    app_guid = await client.async_get_app_guid()
    if not app_guid:
        raise SystemExit("The cloud did not return an app guid")
    if not client.user_id:
        await client.async_get_user()
    await client.async_bind_app_guid()
    session = mqtt.RokiMqttClient(app_guid, on_message=handler)
    topics = {
        mqtt.topic_for(device_guid),
        mqtt.topic_for(device_guid, broadcast=True),
        mqtt.topic_for(app_guid),
    }
    session.add_topics(topics)
    await session.async_start()
    await session.async_wait_connected(20)
    return session


async def _poll_once(session, device_guid: str) -> None:
    """Publish exactly one read-only status poll (command 130)."""
    payload = mqtt.encode_fan_command(session.app_guid, mqtt.CMD_FAN_POLL)
    await session.async_publish(mqtt.topic_for(device_guid), payload)


async def _collect_for(collector, session, device_guid: str, duration: float) -> None:
    deadline = time.monotonic() + max(0.0, float(duration))
    while not collector.truncated:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(min(2.0, remaining))
        if collector.truncated:
            break
        await _poll_once(session, device_guid)


async def _collect_with_marks(collector, session, device_guid: str) -> None:
    while not collector.truncated:
        label = await asyncio.to_thread(
            input, "mark label (empty to finish): "
        )
        if not label.strip():
            break
        await _poll_once(session, device_guid)
        await asyncio.sleep(0.2)
        collector.mark(label.strip())
    print()


def _write_bundle(path: str, bundle: dict[str, Any]) -> Path:
    target = Path(path)
    if target.parent and str(target.parent) not in ("", "."):
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


async def cmd_collect(client, args):
    """Capture a read-only evidence bundle for profile drafting.

    The only frame ever published is the read-only status poll (130); no
    control command is sent.  REST devices cannot stream frames, so only their
    property snapshot is kept and the bundle says so.
    """
    device = await _find_device(client, args.device)
    if device is None:
        raise SystemExit(f"Device {args.device} is not on this account")
    guid = str(device.get("guid"))
    transport = _transport(device, "auto")
    collector = _Collector(
        guid,
        max_frames=args.max_frames,
        max_bytes=args.max_bytes,
        auto_observations=not args.mark,
    )
    print(f"device   : {device.get('name')}  ({guid})")
    print(f"transport: {transport}")
    if transport == "rest":
        collector.set_properties(await client.async_get_properties(guid))
    else:
        def handler(topic: str, payload: bytes) -> None:
            collector.handle_payload(payload)

        session = await _collect_session(client, handler, guid)
        try:
            await _poll_once(session, guid)
            if args.mark:
                await _collect_with_marks(collector, session, guid)
            else:
                await _collect_for(collector, session, guid, args.duration)
        finally:
            await session.async_stop()
    bundle = collector.bundle(device, transport)
    target = _write_bundle(args.out, bundle)
    print(f"bundle written: {target}")
    print(f"  frames      : {len(bundle['frames'])}")
    print(f"  observations: {len(bundle['observations'])}")
    print(f"  truncated   : {str(bundle['truncated']).lower()}")
    if transport == "rest":
        print("  frames unavailable over REST; snapshot only")
    if args.json:
        dump(
            {
                "out": str(target),
                "schema": bundle["schema"],
                "frames": len(bundle["frames"]),
                "observations": len(bundle["observations"]),
                "truncated": bundle["truncated"],
                "frames_unavailable": bundle.get("frames_unavailable"),
            }
        )


def _count_redactions(original: Any, redacted: Any) -> int:
    """How many values the drafter's redaction replaced (privacy report)."""
    redact = load_redact()
    if original is None:
        return 0
    count = 0
    if isinstance(original, dict) and isinstance(redacted, dict):
        for key, value in original.items():
            after = redacted.get(key)
            if after == redact.REDACTED and value != redact.REDACTED:
                count += 1
            else:
                count += _count_redactions(value, after)
    elif isinstance(original, list) and isinstance(redacted, list):
        for before, after in zip(original, redacted):
            count += _count_redactions(before, after)
    return count


def cmd_draft(client, args) -> int:
    """Draft a profile + fixture from a bundle or HA diagnostics JSON."""
    source = Path(args.input)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except OSError as err:
        print(f"cannot read {source}: {err}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as err:
        print(f"{source} is not valid JSON: {err}", file=sys.stderr)
        return 2

    learned: Any = None
    learned_path = getattr(args, "learned", None)
    if learned_path:
        try:
            learned_payload = json.loads(
                Path(learned_path).read_text(encoding="utf-8")
            )
        except OSError as err:
            print(f"cannot read {learned_path}: {err}", file=sys.stderr)
            return 2
        except json.JSONDecodeError as err:
            print(
                f"{learned_path} is not valid JSON: {err}", file=sys.stderr
            )
            return 2
        if isinstance(learned_payload, dict):
            learned = learned_payload.get("actions") or learned_payload.get(
                "groups"
            )
        elif isinstance(learned_payload, list):
            learned = learned_payload
        if learned is None:
            print(
                "--learned must be a JSON list, or an object with an "
                "actions/groups list",
                file=sys.stderr,
            )
            return 2

    drafter = load_draft()
    if isinstance(payload, dict) and payload.get("schema") == drafter.BUNDLE_SCHEMA:
        result = drafter.draft_from_bundle(
            payload, learned_controls=learned
        )
    elif isinstance(payload, dict) and (
        "entry" in payload or "coordinator" in payload
    ):
        result = drafter.draft_from_diagnostics(
            payload,
            dc=args.dc,
            dt=args.dt,
            learned_controls=learned,
        )
    else:
        print(
            "unrecognised input: expected a roki-bundle/1 capture or a "
            "Home Assistant diagnostics JSON",
            file=sys.stderr,
        )
        return 2

    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if not result.profile_yaml:
        print("draft failed: nothing was generated", file=sys.stderr)
        return 1

    match = result.fixture.get("match") or {}
    dc = str(match.get("dc") or "unknown")
    dt = str(match.get("dt") or "unknown")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    profile_path = out_dir / f"{dc}-{dt}.yaml"
    fixture_path = out_dir / f"{dc}-{dt}.json"
    profile_path.write_text(result.profile_yaml, encoding="utf-8")
    fixture_path.write_text(
        json.dumps(result.fixture, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    redacted = _count_redactions(
        payload.get("properties") if isinstance(payload, dict) else None,
        result.fixture.get("properties"),
    )
    learned_entries = drafter.controls_from_actions(learned or [])
    print(f"profile : {profile_path}")
    print(f"fixture : {fixture_path}")
    print(f"privacy : {redacted} sensitive value(s) redacted")
    if learned_entries:
        print(
            f"controls: {len(learned_entries)} learned action(s) "
            "(verified: false)"
        )
    if args.json:
        dump(
            {
                "profile": str(profile_path),
                "fixture": str(fixture_path),
                "redacted_values": redacted,
                "learned_actions": len(learned_entries),
                "warnings": result.warnings,
            }
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Probe and control ROBAM / ROKI devices",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    # Options are attached to every subcommand so the documented examples
    # (``roki_cli.py login --phone ...``) work.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--phone", help="account phone number")
    common.add_argument("--password", help="account password (or ROKI_PASSWORD)")
    common.add_argument("--base-url", default=api.BASE_URL, help="cloud base URL")
    common.add_argument("--json", action="store_true", help="machine readable output")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login", parents=[common], help="log in and list devices")

    sub.add_parser(
        "devices", parents=[common], help="list devices and their properties"
    )

    sub.add_parser(
        "dump", parents=[common], help="dump sanitized diagnostics JSON for issue reporting"
    )

    p_info = sub.add_parser(
        "info", parents=[common], help="show one device and its properties"
    )
    p_info.add_argument("--device", required=True)

    p_check = sub.add_parser(
        "mqttcheck", parents=[common], help="MQTT channel diagnostic"
    )
    p_check.add_argument("--device", required=True)

    p_levels = sub.add_parser(
        "levels", parents=[common], help="show the level ladder for a model"
    )
    p_levels.add_argument("--device", help="device guid (default: all)")

    p_watch = sub.add_parser(
        "watch", parents=[common], help="stream property updates (SSE or MQTT)"
    )
    p_watch.add_argument("--device", help="device guid (default: all)")
    p_watch.add_argument(
        "--levels",
        type=float,
        metavar="SECONDS",
        help=(
            "instead of streaming, poll read-only for SECONDS and print every "
            "distinct fan level the hood reports (never writes a command)"
        ),
    )

    transport_help = "rest, mqtt, or auto (by deviceSource)"
    p_poll = sub.add_parser(
        "poll", parents=[common], help="ask the hood for its state (MQTT 130)"
    )
    p_poll.add_argument("--device", required=True)
    p_poll.add_argument("--transport", choices=["auto", "rest", "mqtt"],
                        default="auto", help=transport_help)

    p_power = sub.add_parser("power", parents=[common], help="turn the hood on or off")
    p_power.add_argument("--device", required=True)
    p_power.add_argument("state", choices=["on", "off"])
    p_power.add_argument("--transport", choices=["auto", "rest", "mqtt"],
                         default="auto", help=transport_help)

    p_level = sub.add_parser("level", parents=[common], help="set the fan level")
    p_level.add_argument("--device", required=True)
    p_level.add_argument("level", type=int)
    p_level.add_argument("--transport", choices=["auto", "rest", "mqtt"],
                         default="auto", help=transport_help)

    p_timing = sub.add_parser(
        "timing", parents=[common], help="set the countdown in minutes (0-60)"
    )
    p_timing.add_argument("--device", required=True)
    p_timing.add_argument("minutes", type=int, help="0-60, 0 cancels")
    p_timing.add_argument("--transport", choices=["auto", "rest", "mqtt"],
                          default="auto", help=transport_help)

    p_light = sub.add_parser("light", parents=[common], help="switch the hood light")
    p_light.add_argument("--device", required=True)
    p_light.add_argument("state", choices=["on", "off"])
    p_light.add_argument("--transport", choices=["auto", "rest", "mqtt"],
                         default="auto", help=transport_help)

    p_invoke = sub.add_parser(
        "invoke", parents=[common], help="invoke an IoT function"
    )
    p_invoke.add_argument("--device", required=True)
    p_invoke.add_argument("--function", required=True)
    p_invoke.add_argument("--params", default="{}", help="JSON object")

    p_setprop = sub.add_parser(
        "setprop", parents=[common], help="write an IoT property"
    )
    p_setprop.add_argument("--device", required=True)
    p_setprop.add_argument("--key", required=True)
    p_setprop.add_argument("--value", required=True)

    p_collect = sub.add_parser(
        "collect",
        parents=[common],
        help="capture a read-only bundle (status polls only, no commands)",
        description=(
            "Capture a read-only evidence bundle for the draft command.  The "
            "only frame ever published is the read-only status poll "
            "(command 130); no control command is ever sent."
        ),
        epilog=(
            "Bundle notes: properties and observation values are redacted "
            "before they are written; frame entries are "
            "[offset_ms, command, body_hex] and the hex does not contain the "
            "sender guid."
        ),
    )
    p_collect.add_argument("--device", required=True, help="device guid")
    p_collect.add_argument("--out", default="bundle.json", help="output file")
    p_collect.add_argument(
        "--duration",
        type=float,
        default=120.0,
        help="collect for this many seconds (ignored with --mark)",
    )
    p_collect.add_argument(
        "--mark",
        action="store_true",
        help=(
            "interactive: type a label and press Enter for each observation; "
            "an empty line ends the capture"
        ),
    )
    p_collect.add_argument(
        "--max-frames", type=int, default=5000, help="frame budget"
    )
    p_collect.add_argument(
        "--max-bytes", type=int, default=5 * 1024 * 1024, help="body byte budget"
    )

    p_draft = sub.add_parser(
        "draft",
        help="draft a profile + fixture from a bundle or diagnostics JSON",
    )
    p_draft.add_argument("input", help="bundle JSON or HA diagnostics JSON")
    p_draft.add_argument("--out", default=".", help="output directory")
    p_draft.add_argument("--dc", help="pick the device by category code")
    p_draft.add_argument("--dt", help="pick the device by model code")
    p_draft.add_argument(
        "--learned",
        metavar="JSON",
        help=(
            "JSON file with learned actions (the learn_stop result, a groups "
            "list, or a flat action list); generates the controls section "
            "with the same generator the HA service uses"
        ),
    )
    p_draft.add_argument("--json", action="store_true", help="machine readable output")

    return parser


COMMANDS = {
    "login": cmd_login,
    "devices": cmd_devices,
    "dump": cmd_dump,
    "info": cmd_info,
    "mqttcheck": cmd_mqttcheck,
    "levels": cmd_levels,
    "poll": cmd_poll,
    "watch": cmd_watch,
    "power": cmd_power,
    "level": cmd_level,
    "timing": cmd_timing,
    "light": cmd_light,
    "invoke": cmd_invoke,
    "setprop": cmd_setprop,
    "collect": cmd_collect,
    "draft": cmd_draft,
}


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "draft":
        # Drafting is offline: it must work without phone/password or network.
        raise SystemExit(cmd_draft(None, args))
    try:
        asyncio.run(with_client(args, COMMANDS[args.command]))
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
