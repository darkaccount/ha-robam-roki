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
}


def main() -> None:
    args = build_parser().parse_args()
    try:
        asyncio.run(with_client(args, COMMANDS[args.command]))
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
