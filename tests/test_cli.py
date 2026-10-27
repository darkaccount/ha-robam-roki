"""Tests for the standalone CLI dispatch contract."""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import types
from pathlib import Path

import pytest

CLI_PATH = Path(__file__).resolve().parents[1] / "tools" / "roki_cli.py"


def _load_cli():
    spec = importlib.util.spec_from_file_location("roki_cli_test", CLI_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = _load_cli()


def test_commands_accept_client_and_args():
    # Regression: with_client used to call action(client) while every command
    # expected (client, args), which broke `devices` with a TypeError.
    for name, func in cli.COMMANDS.items():
        params = list(inspect.signature(func).parameters.values())
        assert [param.name for param in params[:2]] == ["client", "args"], name


def test_with_client_passes_args(monkeypatch):
    seen = {}

    class FakeClient:
        def __init__(self, session, phone, password, base_url=None):
            self.phone = phone

        async def async_login(self):
            return None

    async def action(client, args):
        seen["args"] = args
        return "ok"

    monkeypatch.setattr(cli.api, "RokiClient", FakeClient)
    monkeypatch.setattr(cli, "get_credentials", lambda args: ("138", "secret"))

    args = types.SimpleNamespace(phone="138", password="secret", base_url=cli.api.BASE_URL)

    assert asyncio.run(cli.with_client(args, action)) == "ok"
    assert seen["args"] is args


# -- countdown -----------------------------------------------------------


def test_timing_is_registered():
    assert "timing" in cli.COMMANDS
    assert "levels" in cli.COMMANDS


def _timing_args(minutes: int) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        device="R8229000000000000", minutes=minutes, transport="mqtt"
    )


class _TimingClient:
    def __init__(self, level: int) -> None:
        self.user_id = "1234567890"
        self._level = level
        self.published: list[bytes] = []
        self.timing_calls: list[int] = []

    async def async_get_app_guid(self):
        return "RKDRD000000000000"

    async def async_get_user(self):
        return None

    async def async_bind_app_guid(self):
        return None

    async def async_get_devices(self):
        return [{"guid": "R8229000000000000", "dt": "R8229", "deviceSource": 0}]

    async def async_get_properties(self, device_id):
        return {"workStatus": 1, "level": self._level, "timing": 0}


def test_timing_refuses_to_run_when_the_hood_is_off():
    # Command 144 carries the level to run on; with level 0 it would stop the
    # fan instead of arming a timer.
    client = _TimingClient(level=0)
    with pytest.raises(SystemExit):
        asyncio.run(cli.cmd_timing(client, _timing_args(30)))


def test_timing_clamps_the_minutes_to_the_app_range():
    client = _TimingClient(level=2)
    published: list[bytes] = []

    class _FakeSession:
        app_guid = "RKDRD000000000000"

        async def async_publish(self, topic, payload):
            published.append(payload)

        async def async_stop(self):
            pass

    async def fake_session(*args, **kwargs):
        return _FakeSession()

    async def fake_properties(device_id):
        return {"workStatus": 1, "level": 2, "timing": 0}

    client.async_get_properties = fake_properties
    monkeypatch_session(cli, fake_session)

    asyncio.run(cli.cmd_timing(client, _timing_args(999)))
    # 999 minutes clamps to the 240-minute ceiling.
    assert published[-1][-1] == 240


def monkeypatch_session(module, factory):
    """Point the CLI's MQTT session helper at a fake."""

    async def _session(client, device_ids, *, verbose=False):
        return await factory(client, device_ids, verbose=verbose)

    module._mqtt_session = _session
    return module


def test_levels_command_is_read_only():
    # The `levels` subcommand must never publish a write command: sending a
    # level would start the motor.
    published: list[bytes] = []

    class _FakeClient:
        user_id = "1234567890"

        async def async_get_app_guid(self):
            return "RKDRD000000000000"

        async def async_get_user(self):
            return None

    class _FakeSession:
        app_guid = "RKDRD000000000000"

        def add_topics(self, topics):
            pass

        async def async_start(self):
            pass

        async def async_wait_connected(self, timeout):
            pass

        async def async_publish(self, topic, payload):
            published.append(payload)

        async def async_stop(self):
            pass

    # Long enough for the poll loop to run at least once.
    asyncio.run(
        cli._watch_mqtt_levels(
            _FakeClient(),
            ["R8229000000000000"],
            2.5,
            session_factory=lambda guid, on_message: _FakeSession(),
        )
    )
    # Only 130 polls were sent, never a 132/134/136/144.
    assert published
    for payload in published:
        assert payload[17] == cli.mqtt.CMD_FAN_POLL


# -- dispatch contract ---------------------------------------------------


def test_every_subcommand_has_a_handler():
    # Regression: `dump` was registered in build_parser but missing from
    # COMMANDS, so the documented diagnostics command died with KeyError.
    parser = cli.build_parser()
    command_action = next(action for action in parser._actions if action.dest == "command")
    assert set(command_action.choices) == set(cli.COMMANDS)
