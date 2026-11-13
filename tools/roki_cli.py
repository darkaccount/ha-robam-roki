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
import queue
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

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

BUILTIN_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "robam_roki"
    / "builtin.py"
)

EXPORT_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "robam_roki"
    / "export.py"
)

MANIFEST_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "robam_roki"
    / "manifest.json"
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


_BUILTIN_MODULE: Any = None
_EXPORT_MODULE: Any = None


def load_export():
    """The contribution-pack composer (the same module the service uses)."""
    global _EXPORT_MODULE
    if _EXPORT_MODULE is None:
        _EXPORT_MODULE = _load_standalone("roki_export", EXPORT_PATH)
    return _EXPORT_MODULE


def manifest_version() -> str:
    """The integration version stamped into an export document."""
    try:
        return str(
            json.loads(MANIFEST_PATH.read_text(encoding="utf-8")).get("version")
            or ""
        )
    except (OSError, ValueError):
        return ""


def load_builtin():
    """The shared built-in driver coverage helper (also used by the service).

    Loaded from its file so the CLI keeps working outside Home Assistant; it
    loads the real driver modules with a small enum shim, so the extends line,
    the skipped keys and the fixture expect match what the integration does.
    """
    global _BUILTIN_MODULE
    if _BUILTIN_MODULE is None:
        _BUILTIN_MODULE = _load_standalone("roki_builtin", BUILTIN_PATH)
    return _BUILTIN_MODULE


def coverage_for(device: dict[str, Any]):
    """(extends name, covered keys, expect function) for one device.

    All three are None when the helper cannot be loaded (missing optional
    dependency); the caller then covers every captured property.
    """
    try:
        builtin = load_builtin()
        return builtin.coverage(dict(device))
    except Exception as err:  # noqa: BLE001 - coverage is optional
        print(
            f"warning: built-in driver coverage unavailable ({err}); "
            "every captured property becomes a sensor",
            file=sys.stderr,
        )
        return None, None, None


def _builtin_resolver():
    """A resolver the drafter calls to learn what the built-in driver covers."""
    def resolve(device: dict[str, Any]):
        return coverage_for(device)

    return resolve


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


#: Read-only commands (report query / status poll): never user actions.
IGNORED_COMMAND_FRAMES = frozenset({128, 130})

#: A new command group starts after this long without a frame.  Mirrors
#: const.LEARN_GAP_SECONDS, the gap the HA learning mode groups with.
LEARN_GAP_SECONDS = 2.5

#: The largest body a command frame may carry (mirrors the drafter's cap).
MAX_COMMAND_BODY_BYTES = 64

#: A hard cap on recorded command frames, so a noisy bus cannot grow a bundle.
MAX_COMMAND_FRAMES = 200


class _Collector:
    """Accumulate decoded frames into one compressed, redacted bundle.

    A hood repeats the same status frame on every poll, so only frames whose
    properties actually changed are kept; the raw body is stored compactly as
    [offset_ms, command, body_hex] (the 17-byte source address is dropped so a
    bundle can be shared without leaking the account/app identifier).

    It also records the *external* command frames it sees on the bus: anything
    the official App (or another client) sends to the device.  That is the
    same evidence the Home Assistant learning mode collects, with the same
    filters, and it is what lets a plain `collect` run produce controls
    without the integration.  The raw frame body still carries the envelope
    (terminal type + account user id); the collector strips it before the body
    the profile, exactly like it does for an HA learning session.
    """

    def __init__(
        self,
        guid: str,
        *,
        app_guid: str = "",
        max_frames: int = 5000,
        max_bytes: int = 5 * 1024 * 1024,
        max_commands: int = MAX_COMMAND_FRAMES,
        auto_observations: bool = True,
    ) -> None:
        self.guid = guid
        self.app_guid = str(app_guid or "")
        self.max_frames = max(1, int(max_frames))
        self.max_bytes = max(1, int(max_bytes))
        self.max_commands = max(1, int(max_commands))
        self.auto_observations = auto_observations
        self.started = time.monotonic()
        self.properties: dict[str, Any] = {}
        self.frames: list[list[Any]] = []
        self.observations: list[dict[str, Any]] = []
        #: External command frames, grouped the way the HA learning mode does.
        self.commands: list[dict[str, Any]] = []
        self.truncated = False
        self._bytes = 0
        # Baseline for the first labelled mark: the state when capture started.
        self._mark_snapshot: dict[str, Any] | None = None
        # Command-group bookkeeping (see handle_command_frame).
        self._command_group_open = False
        self._command_group_start = 0
        self._command_group_snapshot: dict[str, Any] = {}
        self._last_command_at: float | None = None

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

    def handle_command_frame(self, payload: bytes) -> None:
        """Record one external command frame, if it passes the learning filter.

        Filters, identical to the HA learning mode: the device's own frames and
        our own app's frames are dropped, and so are the read-only poll/query
        commands.  Frames closer together than LEARN_GAP_SECONDS form one action
        group; the property diff observed when the group closes is attached to
        every frame in it, which is what lets the drafter name the control.
        """
        if len(payload) < 18:
            return
        try:
            source = payload[:17].decode("ascii")
        except UnicodeDecodeError:
            return
        if source == self.guid:
            return  # the appliance reporting its own state
        if self.app_guid and source == self.app_guid:
            return  # our own command (poll, auto-off, an entity click)
        command = int(payload[17])
        if command in IGNORED_COMMAND_FRAMES:
            return

        now = time.monotonic()
        if (
            self._command_group_open
            and self._last_command_at is not None
            and now - self._last_command_at > LEARN_GAP_SECONDS
        ):
            self._close_command_group()
        if not self._command_group_open:
            self._command_group_open = True
            self._command_group_start = len(self.commands)
            self._command_group_snapshot = dict(self.properties)

        if self.truncated or len(self.commands) >= self.max_commands:
            self.truncated = True
            return
        body = payload[18:]
        if self._bytes + len(body) > self.max_bytes:
            self.truncated = True
            return
        self._bytes += len(body)
        self._last_command_at = now
        self.commands.append(
            {
                "index": len(self.commands),
                "command": command,
                "body_hex": load_draft().strip_frame_envelope(
                    body[:MAX_COMMAND_BODY_BYTES].hex()
                ),
                "observed": {},
            }
        )

    def _close_command_group(self) -> None:
        """Attach the property diff seen at the end of the current group."""
        if not self._command_group_open:
            return
        self._command_group_open = False
        changes = self._diff(self._command_group_snapshot, self.properties)
        for entry in self.commands[self._command_group_start :]:
            entry["observed"] = changes
        self._command_group_start = len(self.commands)
        self._command_group_snapshot = dict(self.properties)

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
        # The last group has no following frame to close it: close it now.
        self._close_command_group()
        commands = [
            {
                "index": int(entry["index"]),
                "command": int(entry["command"]),
                "body_hex": str(entry["body_hex"]),
                "observed": redact.redact_properties(entry["observed"]),
            }
            for entry in self.commands
        ]
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
            "commands": commands,
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
        app_guid=str(getattr(client, "app_guid", "") or ""),
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
            # Also keep the App's command frames: that is the evidence the
            # learning mode collects, and what the draft turns into controls.
            collector.handle_command_frame(payload)

        session = await _collect_session(client, handler, guid)
        collector.app_guid = str(
            getattr(session, "app_guid", "") or collector.app_guid
        )
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
    print(f"  commands    : {len(bundle['commands'])}")
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
                "commands": len(bundle["commands"]),
                "truncated": bundle["truncated"],
                "frames_unavailable": bundle.get("frames_unavailable"),
            }
        )


def _extends_of(profile_yaml: str) -> str | None:
    """The extends line of a drafted profile, if it inherited a driver."""
    for line in profile_yaml.splitlines():
        if line.startswith("extends: python:"):
            return line.split(":", 1)[1].strip()
    return None


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


class _CliAbort(Exception):
    """A user-facing abort carrying the exit code the CLI should return."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


def _load_json_input(source: Path) -> Any:
    """Read one JSON input file, reporting the same errors every command uses."""
    try:
        return json.loads(source.read_text(encoding="utf-8"))
    except OSError as err:
        raise _CliAbort(f"cannot read {source}: {err}", 2) from err
    except json.JSONDecodeError as err:
        raise _CliAbort(f"{source} is not valid JSON: {err}", 2) from err


class _DraftInfo:
    """What a draft produced, so commands and the menu can report it."""

    def __init__(
        self,
        *,
        profile: Path,
        fixture: Path,
        controls: int,
        extends: str | None,
        redacted: int,
        warnings: list[str],
    ) -> None:
        self.profile = profile
        self.fixture = fixture
        self.controls = controls
        self.extends = extends
        self.redacted = redacted
        self.warnings = warnings


def _draft_to_dir(
    payload: Any,
    *,
    out_dir: Path,
    learned: Any,
    dc: str | None = None,
    dt: str | None = None,
) -> _DraftInfo:
    """Draft and write the profile + fixture; raises _CliAbort on failure.

    The rule is the service's rule: the profile inherits the built-in driver,
    the keys that driver already covers are skipped and the fixture carries the
    driver's expectations.  That is what makes a CLI draft comparable evidence
    to an HA draft.
    """
    drafter = load_draft()
    resolver = _builtin_resolver()
    if isinstance(payload, dict) and payload.get("schema") == drafter.BUNDLE_SCHEMA:
        result = drafter.draft_from_bundle(
            payload,
            learned_controls=learned,
            builtin_resolver=resolver,
        )
    elif isinstance(payload, dict) and (
        "entry" in payload or "coordinator" in payload
    ):
        result = drafter.draft_from_diagnostics(
            payload,
            dc=dc,
            dt=dt,
            learned_controls=learned,
            builtin_resolver=resolver,
        )
    else:
        raise _CliAbort(
            "unrecognised input: expected a roki-bundle/1 capture or a "
            "Home Assistant diagnostics JSON",
            2,
        )
    if not result.profile_yaml:
        raise _CliAbort("draft failed: nothing was generated", 1)

    match = result.fixture.get("match") or {}
    dc_value = str(match.get("dc") or "unknown")
    dt_value = str(match.get("dt") or "unknown")
    out_dir.mkdir(parents=True, exist_ok=True)
    profile_path = out_dir / f"{dc_value}-{dt_value}.yaml"
    fixture_path = out_dir / f"{dc_value}-{dt_value}.json"
    profile_path.write_text(result.profile_yaml, encoding="utf-8")
    fixture_path.write_text(
        json.dumps(result.fixture, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    redacted = _count_redactions(
        payload.get("properties") if isinstance(payload, dict) else None,
        result.fixture.get("properties"),
    )
    return _DraftInfo(
        profile=profile_path,
        fixture=fixture_path,
        controls=len(drafter.controls_from_actions(learned or [])),
        extends=_extends_of(result.profile_yaml),
        redacted=redacted,
        warnings=list(result.warnings),
    )


def _pair_paths(
    out_dir: Path,
    fixtures_dir: Path,
    dc: str,
    dt: str,
    *,
    ask: Callable[[str], str] | None,
) -> tuple[Path, Path]:
    """The profile/fixture pair to write, never silently overwriting.

    An interactive caller is asked first; a machine caller (--json, a pipe)
    gets the next free -N suffix instead.
    """
    stem = f"{dc}-{dt}"
    profile_path = out_dir / f"{stem}.yaml"
    fixture_path = fixtures_dir / f"{stem}.json"
    if not profile_path.exists() and not fixture_path.exists():
        return profile_path, fixture_path
    if ask is not None:
        try:
            answer = ask(f"{profile_path} 已存在，覆盖吗？(y/N) ")
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer.strip().lower() in {"y", "yes"}:
            return profile_path, fixture_path
    index = 2
    while (
        (out_dir / f"{stem}-{index}.yaml").exists()
        or (fixtures_dir / f"{stem}-{index}.json").exists()
    ):
        index += 1
    return (
        out_dir / f"{stem}-{index}.yaml",
        fixtures_dir / f"{stem}-{index}.json",
    )


def config_from_properties(
    device: dict[str, Any],
    properties: dict[str, Any],
    *,
    out_dir: Path,
    fixtures_dir: Path,
    ask: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Render a profile + fixture from what the cloud already knows.

    Read-only by construction: the caller passes the properties it fetched and
    nothing here talks to the device.  The profile inherits the built-in driver
    the same way an HA draft does, so a config-init file and an HA draft of the
    same capture stay interchangeable.
    """
    drafter = load_draft()
    name, keys, expect_fn = coverage_for(device)
    result = drafter.draft_from_properties(
        device,
        properties,
        expect_fn=expect_fn,
        builtin_driver=name,
        builtin_keys=keys,
    )
    if not result.profile_yaml:
        raise _CliAbort("配置生成失败：没有可写入的内容", 1)
    match = result.fixture.get("match") or {}
    dc = str(match.get("dc") or "unknown")
    dt = str(match.get("dt") or "unknown")
    out_dir.mkdir(parents=True, exist_ok=True)
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    profile_path, fixture_path = _pair_paths(
        out_dir, fixtures_dir, dc, dt, ask=ask
    )
    profile_path.write_text(result.profile_yaml, encoding="utf-8")
    fixture_path.write_text(
        json.dumps(result.fixture, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    document = yaml.safe_load(result.profile_yaml) or {}
    return {
        "name": str(device.get("name") or ""),
        "dc": dc,
        "dt": dt,
        "properties": len(properties),
        "builtin": name,
        "covered": drafter.covered_properties(properties, keys),
        "sensors": [str(key) for key in (document.get("sensors") or {})],
        "profile": str(profile_path),
        "fixture": str(fixture_path),
        "warnings": list(result.warnings),
    }


def draft_bundle_to_dirs(
    payload: Any,
    *,
    profile_dir: Path,
    fixtures_dir: Path,
    learned: Any,
) -> dict[str, Any]:
    """Draft a capture into separate profile/fixture directories."""
    drafter = load_draft()
    result = drafter.draft_from_bundle(
        payload,
        learned_controls=learned,
        builtin_resolver=_builtin_resolver(),
    )
    if not result.profile_yaml:
        raise _CliAbort("draft failed: nothing was generated", 1)
    match = result.fixture.get("match") or {}
    dc = str(match.get("dc") or "unknown")
    dt = str(match.get("dt") or "unknown")
    profile_dir.mkdir(parents=True, exist_ok=True)
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    profile_path, fixture_path = _pair_paths(
        profile_dir, fixtures_dir, dc, dt, ask=None
    )
    profile_path.write_text(result.profile_yaml, encoding="utf-8")
    fixture_path.write_text(
        json.dumps(result.fixture, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    document = yaml.safe_load(result.profile_yaml) or {}
    return {
        "dc": dc,
        "dt": dt,
        "profile": profile_path,
        "fixture": fixture_path,
        "controls": len(document.get("controls") or {}),
        "extends": _extends_of(result.profile_yaml),
        "warnings": list(result.warnings),
        "profile_yaml": result.profile_yaml,
        "fixture_data": result.fixture,
    }


def merge_bundle_into_profile(
    profile_path: Path,
    payload: Any,
    learned: Any,
    *,
    fixtures_dir: Path,
) -> dict[str, Any]:
    """Merge a capture into an existing profile and refresh its fixture.

    The profile keeps its sensors, comments and hand edits; only new control
    entries and the fixture are written.
    """
    drafter = load_draft()
    merged = drafter.merge_controls(
        profile_path.read_text(encoding="utf-8"), learned
    )
    profile_path.write_text(merged.profile_yaml, encoding="utf-8")
    result = drafter.draft_from_bundle(
        payload,
        learned_controls=learned,
        builtin_resolver=_builtin_resolver(),
    )
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    fixture_path = fixtures_dir / profile_path.with_suffix(".json").name
    fixture_data = result.fixture or {}
    if fixture_data:
        fixture_path.write_text(
            json.dumps(fixture_data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    document = yaml.safe_load(merged.profile_yaml) or {}
    return {
        "profile": profile_path,
        "fixture": fixture_path,
        "kept": list(merged.kept),
        "added": list(merged.added),
        "skipped": list(merged.skipped),
        "warnings": list(merged.warnings),
        "controls": len(document.get("controls") or {}),
        "profile_yaml": merged.profile_yaml,
        "fixture_data": fixture_data,
    }


async def cmd_config_init(client, args):
    """Generate a profile config from the device's current properties."""
    device = await _find_device(client, args.guid)
    if device is None:
        raise SystemExit(f"Device {args.guid} is not on this account")
    properties = dict(await client.async_get_properties(args.guid))
    summary = config_from_properties(
        device,
        properties,
        out_dir=Path(args.out),
        fixtures_dir=Path(args.fixtures_dir),
        ask=None if args.json else input,
    )
    for warning in summary["warnings"]:
        print(f"warning: {warning}", file=sys.stderr)
    if args.json:
        dump(summary)
        return
    print(
        f"device   : {summary['name'] or '-'}  "
        f"{summary['dc']}-{summary['dt']}"
    )
    print(f"properties: {summary['properties']}")
    builtin = summary["builtin"]
    covered = summary["covered"]
    if builtin:
        if covered:
            detail = f"（覆盖 {len(covered)} 个键：{', '.join(covered)}）"
        else:
            detail = "（没有属性被覆盖）"
        print(f"builtin  : python:{builtin}{detail}")
    else:
        print("builtin  : 无（这份配置是独立定义）")
    print(f"sensors  : {len(summary['sensors'])} 条")
    print(f"profile  : {summary['profile']}")
    print(f"fixture  : {summary['fixture']}")
    print(
        "next     : 只读设备到此为止；要控制证据 → 菜单 4) 采集证据包"
        "（或 collect 子命令），采完会合并进这份配置"
    )


def _merge_into_profile(path: Path, payload: Any, learned: Any) -> int:
    """Merge captured controls into an existing profile (or draft it fresh).

    The profile text is edited in place by the drafter, so sensors, comments and
    hand edits survive; a missing file is simply a new config at that path.
    """
    drafter = load_draft()
    if not path.is_file():
        result = drafter.draft_from_bundle(
            payload,
            learned_controls=learned,
            builtin_resolver=_builtin_resolver(),
        )
        for warning in result.warnings:
            print(f"warning: {warning}", file=sys.stderr)
        if not result.profile_yaml:
            raise _CliAbort("draft failed: nothing was generated", 1)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(result.profile_yaml, encoding="utf-8")
        fixture_path = path.with_suffix(".json")
        fixture_path.write_text(
            json.dumps(result.fixture, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"profile : {path}（新建，原本不存在）")
        print(f"fixture : {fixture_path}")
        return 0

    merged = drafter.merge_controls(path.read_text(encoding="utf-8"), learned)
    for warning in merged.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    path.write_text(merged.profile_yaml, encoding="utf-8")
    print(f"profile : {path}")
    print(f"kept    : {len(merged.kept)} 条已有 controls")
    if merged.added:
        print(f"added   : {len(merged.added)} 条（{', '.join(merged.added)}）")
    else:
        print("added   : 0 条")
    if merged.skipped:
        pairs = ", ".join(
            f"{command}:{body or '(空)'}" for command, body in merged.skipped
        )
        print(f"skipped : {len(merged.skipped)} 条重复（{pairs}）")
    if not merged.added and not merged.skipped:
        print("nothing to merge: 这次采集没有带回命令帧")
    print("note    : fixture 未改动；要刷新请再跑一次 draft <bundle> --out <目录>")
    return 0


def cmd_draft(client, args) -> int:
    """Draft a profile + fixture from a bundle or HA diagnostics JSON."""
    source = Path(args.input)
    try:
        payload = _load_json_input(source)
        learned, learned_error = _resolve_learned(
            payload, getattr(args, "learned", None)
        )
        if learned_error is not None:
            raise _CliAbort(learned_error, 2)
        merge_path = getattr(args, "merge", None)
        if merge_path:
            # Merge into the config that already exists (or create it there):
            # the profile is edited in place so hand edits survive.
            return _merge_into_profile(Path(merge_path), payload, learned)
        info = _draft_to_dir(
            payload,
            out_dir=Path(args.out),
            learned=learned,
            dc=getattr(args, "dc", None),
            dt=getattr(args, "dt", None),
        )
    except _CliAbort as abort:
        print(str(abort), file=sys.stderr)
        return abort.code
    for warning in info.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    print(f"profile : {info.profile}")
    print(f"fixture : {info.fixture}")
    if info.extends:
        print(f"builtin : {info.extends} (covered keys skipped)")
    print(f"privacy : {info.redacted} sensitive value(s) redacted")
    if info.controls:
        print(
            f"controls: {info.controls} learned action(s) "
            "(verified: false)"
        )
    if args.json:
        dump(
            {
                "profile": str(info.profile),
                "fixture": str(info.fixture),
                "redacted_values": info.redacted,
                "learned_actions": info.controls,
                "extends": info.extends,
                "warnings": info.warnings,
            }
        )
    return 0


def _resolve_learned(
    payload: Any, learned_path: str | None
) -> tuple[Any, str | None]:
    """The learned actions for a draft/export, and an error message if any.

    An explicit --learned file wins; otherwise a collect bundle supplies its own
    command frames (that is what makes the CLI self-sufficient).
    """
    learned: Any = None
    if learned_path:
        try:
            learned_payload = json.loads(
                Path(learned_path).read_text(encoding="utf-8")
            )
        except OSError as err:
            return None, f"cannot read {learned_path}: {err}"
        except json.JSONDecodeError as err:
            return None, f"{learned_path} is not valid JSON: {err}"
        if isinstance(learned_payload, dict):
            learned = learned_payload.get("actions") or learned_payload.get(
                "groups"
            )
        elif isinstance(learned_payload, list):
            learned = learned_payload
        if learned is None:
            return None, (
                "--learned must be a JSON list, or an object with an "
                "actions/groups list"
            )
    if learned is None and isinstance(payload, dict):
        bundled_commands = payload.get("commands")
        if isinstance(bundled_commands, list) and bundled_commands:
            learned = bundled_commands
    return learned, None


def _unique_path(directory: Path, filename: str) -> Path:
    """<filename>, or -2/-3 before the suffix when it already exists."""
    path = directory / filename
    if not path.exists():
        return path
    stem, suffix = Path(filename).stem, Path(filename).suffix
    index = 2
    while (directory / f"{stem}-{index}{suffix}").exists():
        index += 1
    return directory / f"{stem}-{index}{suffix}"


def _ask_verified(args: Any) -> bool:
    """Did the user verify the controls on the appliance?  Default: no."""
    if getattr(args, "verified", False):
        return True
    if getattr(args, "unverified", False):
        return False
    try:
        answer = input("这些控制是否已在真机上逐个验证过？(y/N) ")
    except EOFError:
        return False
    return answer.strip().lower() in {"y", "yes"}


def _mark_verification(markdown: str, verified: bool) -> str:
    """Record the user's answer at the top of the checklist section.

    export.py composes the document and has no note hook (the service never
    needs one: an HA draft is always unverified), so the CLI states the answer
    explicitly -- a maintainer must be able to tell a self-reported claim from
    a default.
    """
    note = (
        "- 验证状态：用户自述已验证（CLI 作答 y）"
        if verified
        else "- 验证状态：尚未验证（CLI 默认 N）"
    )
    heading = "## 2. 验证清单\n"
    position = markdown.find(heading)
    if position < 0:  # pragma: no cover - the composer always writes it
        return markdown
    insert_at = position + len(heading)
    return markdown[:insert_at] + "\n" + note + "\n" + markdown[insert_at:]


class _ExportInfo:
    """The files one export writes, plus what the checklist records."""

    def __init__(
        self,
        *,
        profile: Path,
        fixture: Path,
        export: Path,
        controls: int,
        verified: bool,
        extends: str | None,
        warnings: list[str],
        issue_template: str,
    ) -> None:
        self.profile = profile
        self.fixture = fixture
        self.export = export
        self.controls = controls
        self.verified = verified
        self.extends = extends
        self.warnings = warnings
        self.issue_template = issue_template
        self.promote_command = f"python tools/promote_profile.py {export}"


def _export_from_payload(
    payload: Any,
    *,
    learned: Any,
    verified: bool,
    out_dir: Path,
    profiles_dir: Path,
    profile_yaml: str | None = None,
    fixture: dict[str, Any] | None = None,
    written: dict[str, Any] | None = None,
) -> _ExportInfo:
    """Draft, then compose the contribution Markdown; raises _CliAbort.

    profile_yaml/fixture/written let the caller export an already merged config
    (the menu's collect -> merge -> export chain): the text is the merged file
    and the fixture is the fresh capture evidence, so nothing is rewritten.
    """
    drafter = load_draft()
    result = drafter.draft_from_bundle(
        payload,
        learned_controls=learned,
        builtin_resolver=_builtin_resolver(),
    )
    fixture_data = dict(fixture) if fixture is not None else result.fixture
    if profile_yaml is None and not result.profile_yaml:
        raise _CliAbort("draft failed: nothing to export", 1)

    match = fixture_data.get("match") or {}
    dc = str(match.get("dc") or "unknown")
    dt = str(match.get("dt") or "unknown")
    profile_yaml = profile_yaml or result.profile_yaml
    if verified:
        # Flip the drafter's unverified controls; the checklist is generated
        # from the same document, so the two can never disagree.
        profile_yaml = profile_yaml.replace(
            "    verified: false", "    verified: true"
        )
    document = yaml.safe_load(profile_yaml) or {}
    controls = document.get("controls") or {}
    extends = _extends_of(profile_yaml)

    if written is not None:
        # The caller already wrote the config (merged profile + fresh fixture);
        # rewriting it here would drop hand edits made between the two steps.
        profile_path = Path(written["profile"])
        fixture_path = Path(written["fixture"])
    else:
        profiles_dir.mkdir(parents=True, exist_ok=True)
        profile_path = profiles_dir / f"{dc}-{dt}.yaml"
        fixture_path = profiles_dir / f"{dc}-{dt}.json"
        profile_path.write_text(profile_yaml, encoding="utf-8")
        fixture_path.write_text(
            json.dumps(fixture_data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    exporter = load_export()
    generated_at = datetime.now()
    markdown = exporter.build_export_markdown(
        device_name=f"{dc}-{dt}",
        dc=dc,
        dt=dt,
        transport=str(
            (payload.get("device") or {}).get("transport") or "mqtt"
        ),
        extends=extends,
        profile_yaml=profile_yaml,
        fixture=fixture_data,
        fixture_local_path=str(fixture_path),
        controls=controls,
        learned=None,
        generated_at=generated_at,
        version=manifest_version(),
    )
    markdown = _mark_verification(markdown, verified)

    out_dir.mkdir(parents=True, exist_ok=True)
    export_path = _unique_path(
        out_dir, exporter.export_filename(dc, dt, generated_at)
    )
    export_path.write_text(markdown, encoding="utf-8")
    return _ExportInfo(
        profile=profile_path,
        fixture=fixture_path,
        export=export_path,
        controls=len(controls),
        verified=verified,
        extends=extends,
        warnings=list(result.warnings),
        issue_template=exporter.ISSUE_TEMPLATE_URL,
    )


def cmd_export(client, args) -> int:
    """Draft from a capture and write the contribution Markdown."""
    source = Path(args.input)
    try:
        payload = _load_json_input(source)
        learned, error = _resolve_learned(
            payload, getattr(args, "learned", None)
        )
        if error is not None:
            raise _CliAbort(error, 2)
        verified = _ask_verified(args)
        info = _export_from_payload(
            payload,
            learned=learned,
            verified=verified,
            out_dir=Path(args.out),
            profiles_dir=Path(args.profiles_dir),
        )
    except _CliAbort as abort:
        print(str(abort), file=sys.stderr)
        return abort.code
    for warning in info.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    status = "用户自述已验证" if info.verified else "尚未验证（默认）"
    print(f"profile : {info.profile}")
    print(f"fixture : {info.fixture}")
    print(f"export  : {info.export}")
    print(
        f"controls: {info.controls} 条"
        f"（verified: {str(info.verified).lower()}，{status}）"
    )
    print(f"issue   : {info.issue_template}")
    print(f"promote : {info.promote_command}")
    if args.json:
        dump(
            {
                "profile": str(info.profile),
                "fixture": str(info.fixture),
                "export": str(info.export),
                "controls": info.controls,
                "verified": info.verified,
                "extends": info.extends,
                "issue_template": info.issue_template,
                "promote_command": info.promote_command,
                "warnings": info.warnings,
            }
        )
    return 0


# -- interactive menu ----------------------------------------------------

#: The main menu: read the device, make a config, capture, export.
MENU_ITEMS: tuple[tuple[str, str], ...] = (
    ("1", "登录并查看设备"),
    ("2", "查看设备状态（原始属性）"),
    ("3", "生成配置（从设备信息，无需采集）"),
    ("4", "采集证据包（采完自动合并进配置）"),
    ("5", "生成贡献包（.md）"),
    ("6", "诊断与查询 → 二级菜单"),
    ("7", "设备控制 → 二级菜单（发送前确认）"),
    ("0", "退出"),
)

DIAGNOSTIC_ITEMS: tuple[tuple[str, str], ...] = (
    ("1", "MQTT 连通性检查"),
    ("2", "导出脱敏诊断 JSON"),
    ("3", "机型档位表（只读）"),
    ("0", "返回"),
)

CONTROL_ITEMS: tuple[tuple[str, str], ...] = (
    ("1", "开关（on / off）"),
    ("2", "档位（1 / 2 / 3）"),
    ("3", "灯光（on / off）"),
    ("4", "倒计时（0-240 分钟）"),
    ("0", "返回"),
)

MENU_HINT = "输入 ? 查看全部子命令帮助"

DEFAULT_BUNDLE = "bundle.json"
DEFAULT_PROFILES_DIR = "profiles"
DEFAULT_FIXTURES_DIR = "fixtures"
DEFAULT_EXPORTS_DIR = "exports"
#: The menu stops collecting after this long, even without an Enter.
MENU_COLLECT_SECONDS = 120.0


class _MenuExit(Exception):
    """The user left the menu (EOF, Ctrl+C at a prompt, or 0)."""


class _EnterWaiter:
    """Detect one line of input without blocking the event loop."""

    def __init__(self, stream: Any = None) -> None:
        self._stream = stream if stream is not None else sys.stdin
        self._queue: queue.Queue[str] = queue.Queue()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        try:
            self._queue.put(self._stream.readline())
        except Exception:  # noqa: BLE001 - a closed stdin simply ends the wait
            self._queue.put("")

    @property
    def done(self) -> bool:
        return not self._queue.empty()


async def _menu_devices(client, args):
    """The account's devices, so the menu can offer a numbered list."""
    return list(await client.async_get_devices())


async def _menu_properties(client, args):
    """The cloud properties of one device."""
    return dict(await client.async_get_properties(args.device))


def _bundle_summary(target: Path, bundle: dict[str, Any]) -> dict[str, Any]:
    return {
        "bundle": str(target),
        "frames": len(bundle.get("frames") or []),
        "observations": len(bundle.get("observations") or []),
        "commands": len(bundle.get("commands") or []),
        "truncated": bool(bundle.get("truncated")),
    }


async def _menu_collect(client, args):
    """Collect until Enter is pressed, with a hard time cap.

    Same read-only rule as the collect subcommand: the only frame ever
    published is the status poll (130).
    """
    device = await _find_device(client, args.device)
    if device is None:
        raise SystemExit(f"Device {args.device} is not on this account")
    guid = str(device.get("guid"))
    transport = _transport(device, "auto")
    collector = _Collector(
        guid,
        app_guid=str(getattr(client, "app_guid", "") or ""),
        auto_observations=True,
    )
    if transport == "rest":
        collector.set_properties(await client.async_get_properties(guid))
        bundle = collector.bundle(device, transport)
        return _bundle_summary(_write_bundle(args.out, bundle), bundle)

    def handler(topic: str, payload: bytes) -> None:
        collector.handle_payload(payload)
        # The App's own command frames are the evidence the draft needs.
        collector.handle_command_frame(payload)

    session = await _collect_session(client, handler, guid)
    collector.app_guid = str(
        getattr(session, "app_guid", "") or collector.app_guid
    )
    waiter = _EnterWaiter()
    waiter.start()
    print("正在采集：在官方 App 里操作设备，完成后回来按回车结束…")
    cap = float(getattr(args, "duration", MENU_COLLECT_SECONDS) or 0.0)
    deadline = time.monotonic() + max(1.0, cap)
    try:
        await _poll_once(session, guid)
        while time.monotonic() < deadline and not collector.truncated:
            if waiter.done:
                break
            await asyncio.sleep(0.25)
        if not waiter.done:
            print(f"已达 {cap:.0f} 秒上限，自动结束采集。")
    finally:
        await session.async_stop()
    bundle = collector.bundle(device, transport)
    return _bundle_summary(_write_bundle(args.out, bundle), bundle)


class _Menu:
    """The interactive entry point, with every input and output injected.

    Network work goes through ``runner(args, handler)``, so a test can drive the
    whole menu without a phone number, a password or a network.
    """

    def __init__(
        self,
        *,
        ask: Callable[[str], str] = input,
        say: Callable[[str], None] = print,
        password_ask: Callable[[str], str] = getpass.getpass,
        runner: Callable[[Any, Any], Any] | None = None,
        phone: str = "",
        password: str = "",
    ) -> None:
        self._ask = ask
        self._say = say
        self._password_ask = password_ask
        self._runner = runner or (
            lambda args, handler: asyncio.run(with_client(args, handler))
        )
        self.phone = phone or os.environ.get("ROKI_PHONE", "")
        self.password = password or os.environ.get("ROKI_PASSWORD", "")
        self.devices: list[dict[str, Any]] = []
        self.last_guid = ""

    # -- plumbing ------------------------------------------------------

    def run(self) -> int:
        """Show the menu until the user leaves; always returns 0."""
        while True:
            choice = self._ask_menu()
            if choice is None or choice == "0":
                return 0
            if choice in {"?", "？", "h"}:
                self._print_help()
                continue
            handler = self._handlers().get(choice)
            if handler is None:
                self._say(f"没有 {choice} 这个选项。{MENU_HINT}。")
                continue
            try:
                handler()
            except _MenuExit:
                return 0
            except KeyboardInterrupt:
                self._say("")
                self._say("已中断，回到主菜单。")
            except _CliAbort as abort:
                self._say(f"未完成：{abort}（回到主菜单）")
            except SystemExit as stop:
                reason = (
                    stop.code
                    if isinstance(stop.code, str)
                    else f"命令退出（{stop.code}）"
                )
                self._say(f"操作失败：{reason}（回到主菜单）")
            except Exception as err:  # noqa: BLE001 - one failure must not exit
                self._say(f"操作失败：{err}（回到主菜单）")

    def _handlers(self) -> dict[str, Callable[[], None]]:
        return {
            "1": self.action_login,
            "2": self.action_status,
            "3": self.action_config,
            "4": self.action_collect,
            "5": self.action_export,
            "6": self.action_diagnostics,
            "7": self.action_control,
        }

    def _read(self, prompt: str) -> str:
        try:
            return self._ask(prompt)
        except (EOFError, KeyboardInterrupt):
            raise _MenuExit from None

    def _ask_menu(self) -> str | None:
        self._say("")
        self._say("ROBAM ROKI 命令行")
        current = self._current_device()
        if current is not None:
            self._say(f"当前设备：{_device_label(current)}")
        for key, label in MENU_ITEMS:
            self._say(f"  {key}) {label}")
        self._say(MENU_HINT)
        try:
            choice = self._ask("选择: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        # Full-width question mark and h are aliases: typing a Chinese IME's ？
        # is the expected accident, not a wrong option.
        if choice.lower() in {"?", "？", "h", "help"}:
            return "?"
        return choice

    def _confirm(self, prompt: str) -> bool:
        answer = self._read(prompt).strip().lower()
        return answer in {"y", "yes"}

    def _print_help(self) -> None:
        self._say("")
        self._say(build_parser().format_help().rstrip())
        self._say("")

    def _args(self, **extra: Any) -> argparse.Namespace:
        values: dict[str, Any] = {
            "phone": self.phone,
            "password": self.password,
            "base_url": api.BASE_URL,
            "json": False,
            "device": None,
        }
        values.update(extra)
        return argparse.Namespace(**values)

    def _call(self, handler: Any, **extra: Any) -> Any:
        return self._runner(self._args(**extra), handler)

    # -- credentials and devices ---------------------------------------

    def _ensure_credentials(self) -> bool:
        if self.phone and self.password:
            return True
        phone = self.phone or os.environ.get("ROKI_PHONE", "")
        if not phone:
            phone = self._read("手机号: ").strip()
        password = self.password or os.environ.get("ROKI_PASSWORD", "")
        if not password:
            try:
                password = self._password_ask("密码: ")
            except (EOFError, KeyboardInterrupt):
                raise _MenuExit from None
        if not phone or not password:
            self._say("缺少手机号或密码，取消这次操作。")
            return False
        self.phone, self.password = phone, password
        self._say("凭据只用于本次会话，不会写入任何文件。")
        return True

    def _fetch_devices(self) -> list[dict[str, Any]]:
        if not self._ensure_credentials():
            return []
        devices = self._call(_menu_devices) or []
        self.devices = [dict(device) for device in devices]
        if not self.devices:
            self._say("这个账号下没有设备。")
        return self.devices

    def _current_device(self) -> dict[str, Any] | None:
        for device in self.devices:
            if str(device.get("guid")) == self.last_guid:
                return device
        return None

    def _pick_device(self) -> dict[str, Any] | None:
        if not self.devices and not self._fetch_devices():
            return None
        self._say("")
        self._say("可选的设备：")
        for index, device in enumerate(self.devices, start=1):
            self._say(f"  {index}) {_device_label(device)}")
        current = self._current_device()
        hint = "（回车沿用上次）" if current is not None else ""
        answer = self._read(f"选择设备{hint}: ").strip()
        if not answer and current is not None:
            return current
        try:
            index = int(answer)
        except ValueError:
            self._say("请输入设备编号。")
            return None
        if not 1 <= index <= len(self.devices):
            self._say("没有这个编号。")
            return None
        device = self.devices[index - 1]
        self.last_guid = str(device.get("guid") or "")
        return device

    def _input_file(self, label: str, default: str) -> Path:
        answer = self._read(f"{label} 路径（回车用 {default}）: ").strip()
        path = Path(answer or default)
        if not path.is_file():
            raise _CliAbort(f"找不到文件：{path}", 2)
        return path

    def _bundle_target(self) -> Path:
        target = Path(DEFAULT_BUNDLE)
        if not target.exists():
            return target
        if self._confirm(f"{target} 已存在，覆盖吗？(y/N) "):
            return target
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return Path(f"bundle-{stamp}.json")

    # -- the tasks -----------------------------------------------------

    def action_login(self) -> None:
        devices = self._fetch_devices()
        if not devices:
            return
        self._say(f"登录成功：{len(devices)} 台设备")
        for index, device in enumerate(devices, start=1):
            self._say(f"  {index}) {_device_label(device)}")
        self._say("其它任务会从这里选设备，本次会话记住上次的选择。")

    def action_status(self) -> None:
        device = self._pick_device()
        if device is None:
            return
        properties = self._call(
            _menu_properties, device=str(device.get("guid") or "")
        ) or {}
        self._say(f"{_device_label(device)} 的当前属性：")
        self._say(json.dumps(properties, ensure_ascii=False, indent=2))

    def action_collect(self) -> None:
        """Capture, then merge into the existing config (or make a new one)."""
        device = self._pick_device()
        if device is None:
            return
        target = self._bundle_target()
        summary = self._call(
            _menu_collect,
            device=str(device.get("guid") or ""),
            out=str(target),
            duration=MENU_COLLECT_SECONDS,
        ) or {}
        bundle_path = Path(str(summary.get("bundle") or target))
        self._say(f"证据包：{bundle_path}")
        self._say(
            f"  帧 {summary.get('frames', 0)}，"
            f"观察 {summary.get('observations', 0)}，"
            f"命令 {summary.get('commands', 0)}"
        )
        if summary.get("truncated"):
            self._say("  注意：达到采集预算上限，证据包已截断。")

        payload = json.loads(bundle_path.read_text(encoding="utf-8"))
        learned, error = _resolve_learned(payload, None)
        if error is not None:
            raise _CliAbort(error, 2)
        profile_dir = Path(DEFAULT_PROFILES_DIR)
        fixtures_dir = Path(DEFAULT_FIXTURES_DIR)
        dc = str((payload.get("device") or {}).get("dc") or "unknown")
        dt = str((payload.get("device") or {}).get("dt") or "unknown")
        existing = profile_dir / f"{dc}-{dt}.yaml"
        written: dict[str, Any]
        if existing.is_file():
            # The config is the source of truth: merge the new commands in and
            # keep sensors, comments and hand edits untouched.
            merged = merge_bundle_into_profile(
                existing, payload, learned, fixtures_dir=fixtures_dir
            )
            self._say(f"已合并进已有配置：{merged['profile']}")
            self._say(f"  保留 {len(merged['kept'])} 条已有 controls")
            if merged["added"]:
                self._say(
                    f"  新增 {len(merged['added'])} 条"
                    f"（{', '.join(merged['added'])}）"
                )
            else:
                self._say("  新增 0 条")
            if merged["skipped"]:
                self._say(f"  跳过 {len(merged['skipped'])} 条重复（同 command+body）")
            self._say(f"  fixture：{merged['fixture']}")
            written = {
                "profile": merged["profile"],
                "fixture": merged["fixture"],
            }
            export_profile = str(merged["profile_yaml"])
            export_fixture = merged["fixture_data"] or None
        else:
            drafted = draft_bundle_to_dirs(
                payload,
                profile_dir=profile_dir,
                fixtures_dir=fixtures_dir,
                learned=learned,
            )
            self._say(f"已生成新配置：{drafted['profile']}")
            self._say(f"      fixture：{drafted['fixture']}")
            if drafted["extends"]:
                self._say(f"      继承内置驱动：{drafted['extends']}")
            if not drafted["controls"]:
                self._say("      这次没有捕获到控制命令，配置里只有只读 sensors。")
            written = {
                "profile": drafted["profile"],
                "fixture": drafted["fixture"],
            }
            export_profile = str(drafted["profile_yaml"])
            export_fixture = drafted["fixture_data"] or None
        if not self._confirm("现在生成贡献包（.md）吗？(y/N) "):
            self._say("已跳过导出。产物清单：")
            self._say(f"  bundle  : {bundle_path}")
            self._say(f"  profile : {written['profile']}")
            self._say(f"  fixture : {written['fixture']}")
            self._say("之后可以选 5) 生成贡献包，或运行 export 子命令。")
            return
        verified = self._confirm("这些控制是否已在真机上逐个验证过？(y/N) ")
        info = _export_from_payload(
            payload,
            learned=learned,
            verified=verified,
            out_dir=Path(DEFAULT_EXPORTS_DIR),
            profiles_dir=profile_dir,
            profile_yaml=export_profile,
            fixture=export_fixture,
            written=written,
        )
        self._say("贡献包已生成，产物清单：")
        self._say(f"  bundle  : {bundle_path}")
        self._say(f"  profile : {info.profile}")
        self._say(f"  fixture : {info.fixture}")
        self._say(f"  export  : {info.export}")
        status = "用户自述已验证" if info.verified else "尚未验证（默认）"
        self._say(
            f"  verified: {str(info.verified).lower()}（{status}）"
        )
        self._say("下一步二选一：")
        self._say(f"  1) 把 {info.export} 整份贴到 issue：{info.issue_template}")
        self._say(f"  2) 运行 {info.promote_command}")

    def action_config(self) -> None:
        """Generate a config from the device's properties (no capture at all)."""
        device = self._pick_device()
        if device is None:
            return
        properties = self._call(
            _menu_properties, device=str(device.get("guid") or "")
        ) or {}
        summary = config_from_properties(
            device,
            dict(properties),
            out_dir=Path(DEFAULT_PROFILES_DIR),
            fixtures_dir=Path(DEFAULT_FIXTURES_DIR),
            ask=self._ask,
        )
        for warning in summary["warnings"]:
            self._say(f"warning: {warning}")
        self._say(f"配置：{summary['profile']}")
        self._say(f"      fixture：{summary['fixture']}")
        self._say(
            f"      属性 {summary['properties']} 个，"
            f"sensors {len(summary['sensors'])} 条"
        )
        if summary["builtin"]:
            covered = summary["covered"]
            detail = f"，覆盖 {len(covered)} 个键" if covered else ""
            self._say(
                f"      继承内置驱动 python:{summary['builtin']}{detail}"
            )
        self._say(
            "下一步：只读设备到此为止；要控制证据 → 4) 采集证据包，"
            "采完会自动合并进这份配置"
        )

    def action_export(self) -> None:
        payload = _load_json_input(self._input_file("证据包 JSON", DEFAULT_BUNDLE))
        learned, error = _resolve_learned(payload, None)
        if error is not None:
            raise _CliAbort(error, 2)
        verified = self._confirm("这些控制是否已在真机上逐个验证过？(y/N) ")
        info = _export_from_payload(
            payload,
            learned=learned,
            verified=verified,
            out_dir=Path(DEFAULT_EXPORTS_DIR),
            profiles_dir=Path(DEFAULT_PROFILES_DIR),
        )
        self._say(f"profile : {info.profile}")
        self._say(f"fixture : {info.fixture}")
        self._say(f"export  : {info.export}")
        status = "用户自述已验证" if info.verified else "尚未验证（默认）"
        self._say(
            f"controls: {info.controls} 条"
            f"（verified: {str(info.verified).lower()}，{status}）"
        )
        self._say(f"issue   : {info.issue_template}")
        self._say(f"promote : {info.promote_command}")

    def action_diagnostics(self) -> None:
        self._say("")
        self._say("诊断与查询")
        for key, label in DIAGNOSTIC_ITEMS:
            self._say(f"  {key}) {label}")
        choice = self._read("选择: ").strip()
        if choice in ("", "0"):
            return
        if choice == "1":
            device = self._pick_device()
            if device is None:
                return
            self._call(cmd_mqttcheck, device=str(device.get("guid") or ""))
            return
        if choice == "2":
            self._call(cmd_dump)
            return
        if choice == "3":
            self._call(cmd_levels)
            return
        self._say("没有这个选项。")

    def action_control(self) -> None:
        self._say("")
        self._say("设备控制（只发送你确认过的命令）")
        for key, label in CONTROL_ITEMS:
            self._say(f"  {key}) {label}")
        choice = self._read("选择: ").strip()
        if choice in ("", "0"):
            return
        device = self._pick_device()
        if device is None:
            return
        if choice == "1":
            state = self._on_off("开还是关？(on/off): ")
            self._send(
                device, f"电源 {state}", cmd_power, state=state, transport="auto"
            )
        elif choice == "2":
            level = self._number("档位（1-3）: ", 1, 3)
            self._send(
                device, f"档位 {level}", cmd_level, level=level, transport="auto"
            )
        elif choice == "3":
            state = self._on_off("灯光开还是关？(on/off): ")
            self._send(
                device, f"灯光 {state}", cmd_light, state=state, transport="auto"
            )
        elif choice == "4":
            minutes = self._number("倒计时分钟数（0-240，0 表示取消）: ", 0, 240)
            self._send(
                device,
                f"倒计时 {minutes} 分钟",
                cmd_timing,
                minutes=minutes,
                transport="auto",
            )
        else:
            self._say("没有这个选项。")

    def _on_off(self, prompt: str) -> str:
        answer = self._read(prompt).strip().lower()
        if answer not in {"on", "off"}:
            raise _CliAbort("只能输入 on 或 off。")
        return answer

    def _number(self, prompt: str, low: int, high: int) -> int:
        raw = self._read(prompt).strip()
        try:
            value = int(raw)
        except ValueError:
            raise _CliAbort(f"需要 {low}-{high} 之间的整数。") from None
        if not low <= value <= high:
            raise _CliAbort(f"需要 {low}-{high} 之间的整数。")
        return value

    def _send(
        self, device: dict[str, Any], description: str, handler: Any, **extra: Any
    ) -> None:
        name = str(device.get("name") or device.get("dt") or "该设备")
        self._say(f"将向 {name} 发送：{description}")
        if not self._confirm("确认发送？(y/N) "):
            self._say("已取消，没有发送任何命令。")
            return
        self._call(handler, device=str(device.get("guid") or ""), **extra)
        self._say("已发送。")


def _device_label(device: dict[str, Any]) -> str:
    """A device as the menu shows it: name plus model, never the guid."""
    name = str(device.get("name") or "(未命名)")
    model = " ".join(
        str(part)
        for part in (device.get("dc"), device.get("dt"))
        if part
    )
    return f"{name}  {model}".rstrip()


def interactive_menu(
    *,
    phone: str = "",
    password: str = "",
    ask: Callable[[str], str] | None = None,
    say: Callable[[str], None] | None = None,
    password_ask: Callable[[str], str] | None = None,
    runner: Callable[[Any, Any], Any] | None = None,
) -> int:
    """Run the interactive menu and return the process exit code."""
    return _Menu(
        phone=phone,
        password=password,
        ask=ask or input,
        say=say or print,
        password_ask=password_ask or getpass.getpass,
        runner=runner,
    ).run()


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

    # Credentials on the top-level parser: they are only used by the menu.
    parser.add_argument("--phone", help="account phone number (menu mode)")
    parser.add_argument("--password", help="account password (menu mode)")
    # No subcommand: the interactive menu (also what a first-time user runs).
    sub = parser.add_subparsers(dest="command")

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
        "timing", parents=[common], help="set the countdown in minutes (0-240)"
    )
    p_timing.add_argument("--device", required=True)
    p_timing.add_argument("minutes", type=int, help="0-240, 0 cancels")
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
    p_draft.add_argument(
        "--merge",
        metavar="PROFILE",
        help=(
            "merge the captured commands into an existing profile (sensors, "
            "comments and hand edits are kept; duplicates by command+body are "
            "skipped); a missing profile is created at that path instead"
        ),
    )
    p_draft.add_argument("--json", action="store_true", help="machine readable output")

    p_export = sub.add_parser(
        "export",
        parents=[common],
        help="draft from a bundle and write the contribution .md",
        description=(
            "Draft a profile from a captured bundle (built-in driver coverage "
            "included) and compose the five-section contribution Markdown the "
            "integration produces, so it can be pasted into an issue or "
            "promoted with tools/promote_profile.py."
        ),
        epilog=(
            "Only one .md is written (plus the draft profile/fixture). "
            "The checklist records whether you said the controls were verified "
            "on the appliance; the default answer is no."
        ),
    )
    p_export.add_argument(
        "input", help="bundle JSON (a roki-bundle/1 capture)"
    )
    p_export.add_argument(
        "--out", default="exports", help="directory for the .md"
    )
    p_export.add_argument(
        "--profiles-dir",
        default="profiles",
        help="directory for the drafted profile and fixture",
    )
    p_export.add_argument(
        "--learned",
        metavar="JSON",
        help="explicit learned actions file (defaults to the bundle's commands)",
    )
    verification = p_export.add_mutually_exclusive_group()
    verification.add_argument(
        "--verified",
        action="store_true",
        help="the controls were verified on the appliance (skips the prompt)",
    )
    verification.add_argument(
        "--unverified",
        action="store_true",
        help="the controls are not verified yet (skips the prompt; default)",
    )

    p_config = sub.add_parser(
        "config",
        parents=[common],
        help="generate a profile config from device information",
    )
    config_sub = p_config.add_subparsers(dest="config_command", required=True)
    p_init = config_sub.add_parser(
        "init",
        parents=[common],
        help="write a profile + fixture from the current properties",
        description=(
            "Log in, read the device's cached cloud properties and write a "
            "profile + fixture from them. Never sends a control command: a "
            "read-only appliance is fully served by this one step."
        ),
        epilog=(
            "An existing profile is never overwritten silently: you are asked, "
            "and a non-interactive run gets the next free -2 suffix. "
            "The profile inherits the built-in driver (extends) and skips the "
            "properties that driver already exposes."
        ),
    )
    p_init.add_argument("guid", help="device guid (see the devices subcommand)")
    p_init.add_argument("--out", default="profiles", help="profile directory")
    p_init.add_argument(
        "--fixtures-dir", default="fixtures", help="fixture directory"
    )

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
    "export": cmd_export,
    "config": cmd_config_init,
}


def main() -> None:
    args = build_parser().parse_args()
    if args.command is None:
        # No subcommand: the interactive menu, which needs no credentials up
        # front -- they are asked for when a task first needs the network.
        raise SystemExit(
            interactive_menu(
                phone=getattr(args, "phone", "") or "",
                password=getattr(args, "password", "") or "",
            )
        )
    if args.command in {"draft", "export"}:
        # Drafting and exporting are offline: they must work without a phone
        # number, a password or a network.
        raise SystemExit(COMMANDS[args.command](None, args))
    try:
        asyncio.run(with_client(args, COMMANDS[args.command]))
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
