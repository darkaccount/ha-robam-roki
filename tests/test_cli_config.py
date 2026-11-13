"""The config init subcommand: a read-only config from cloud properties.

Everything here runs against a fake client, so the tests also prove the command
never reaches for a control API: the fake has no publish/invoke method at all.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
CLI_PATH = REPO / "tools" / "roki_cli.py"


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cli = _load_module("roki_cli_config_test", CLI_PATH)

DEVICE = {
    "guid": "R8229HOODGUID0000",
    "name": "厨房油烟机",
    "dc": "RYYJ",
    "dt": "R8229",
    "deviceSource": 0,
}
PROPERTIES = {
    "workStatus": 1,
    "level": 2,
    "lightStatus": 0,
    "timing": 15,
    "custom": 7,
    "phone": "13800000000",
}


class _FakeClient:
    """A client with no control API: only the read-only calls exist."""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def async_get_devices(self) -> list[dict[str, Any]]:
        self.calls.append("devices")
        return [dict(DEVICE)]

    async def async_get_properties(self, guid: str) -> dict[str, Any]:
        self.calls.append(("properties", guid))
        return dict(PROPERTIES)


def _args(tmp_path: Path, *extra: str) -> Any:
    return cli.build_parser().parse_args(
        [
            "config",
            "init",
            DEVICE["guid"],
            "--out",
            str(tmp_path / "profiles"),
            "--fixtures-dir",
            str(tmp_path / "fixtures"),
            *extra,
        ]
    )


def _run(
    tmp_path: Path,
    monkeypatch: Any,
    *extra: str,
    answer: str | None = None,
) -> _FakeClient:
    if answer is not None:
        monkeypatch.setattr("builtins.input", lambda prompt="": answer)
    client = _FakeClient()
    asyncio.run(cli.cmd_config_init(client, _args(tmp_path, *extra)))
    return client


def test_config_init_writes_a_profile_and_a_fixture(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _run(tmp_path, monkeypatch, answer="")
    profile_path = tmp_path / "profiles" / "RYYJ-R8229.yaml"
    fixture_path = tmp_path / "fixtures" / "RYYJ-R8229.json"
    assert profile_path.is_file()
    assert fixture_path.is_file()

    document = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    assert document["schema"] == "roki-profile/1"
    assert document["match"] == {"dc": "RYYJ", "dt": "R8229"}
    # The built-in driver is inherited and its keys are skipped.
    assert document["extends"] == "python:hood"
    assert list(document["sensors"]) == ["custom", "phone"]
    assert "controls" not in document

    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert fixture["schema"] == "roki-fixture/1"
    assert fixture["source"] == "auto"
    assert fixture["expect"]["work_status"] in {"开机", "延时关机", "关机"}
    # Privacy: the phone-like value never reaches the fixture.
    assert fixture["properties"]["phone"] == "**REDACTED**"
    assert "13800000000" not in profile_path.read_text(encoding="utf-8")


def test_config_init_never_sends_a_command(
    tmp_path: Path, monkeypatch: Any
) -> None:
    client = _run(tmp_path, monkeypatch, answer="")
    assert client.calls == ["devices", ("properties", DEVICE["guid"])]


def test_config_init_reports_what_it_did(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    _run(tmp_path, monkeypatch, answer="")
    output = capsys.readouterr().out
    assert "厨房油烟机  RYYJ-R8229" in output
    assert "properties: 6" in output
    assert "builtin  : python:hood（覆盖 4 个键" in output
    assert "profile  : " in output
    assert "fixture  : " in output
    assert "采集证据包" in output


def test_the_json_summary_is_machine_readable(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    _run(tmp_path, monkeypatch, "--json")
    summary = json.loads(capsys.readouterr().out)
    assert summary["dc"] == "RYYJ"
    assert summary["dt"] == "R8229"
    assert summary["properties"] == 6
    assert summary["builtin"] == "hood"
    assert summary["sensors"] == ["custom", "phone"]
    assert "workStatus" in summary["covered"]
    assert Path(summary["profile"]).is_file()
    assert Path(summary["fixture"]).is_file()
    assert summary["warnings"] == []


def test_an_existing_profile_is_not_overwritten_by_default(
    tmp_path: Path, monkeypatch: Any
) -> None:
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    original = profiles / "RYYJ-R8229.yaml"
    original.write_text("stale: true\n", encoding="utf-8")

    # Empty answer = the documented default: keep the old file.
    _run(tmp_path, monkeypatch, answer="")
    assert original.read_text(encoding="utf-8") == "stale: true\n"
    assert (profiles / "RYYJ-R8229-2.yaml").is_file()
    assert (tmp_path / "fixtures" / "RYYJ-R8229-2.json").is_file()


def test_overwriting_can_be_confirmed(
    tmp_path: Path, monkeypatch: Any
) -> None:
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    original = profiles / "RYYJ-R8229.yaml"
    original.write_text("stale: true\n", encoding="utf-8")

    _run(tmp_path, monkeypatch, answer="y")
    text = original.read_text(encoding="utf-8")
    assert "stale" not in text
    assert "extends: python:hood" in text
    assert not (profiles / "RYYJ-R8229-2.yaml").exists()


def test_json_mode_never_prompts_and_appends_a_suffix(
    tmp_path: Path, monkeypatch: Any
) -> None:
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "RYYJ-R8229.yaml").write_text("stale: true\n", encoding="utf-8")

    def refuse(prompt: str = "") -> str:
        raise AssertionError("--json must not prompt")

    monkeypatch.setattr("builtins.input", refuse)
    _run(tmp_path, monkeypatch, "--json")
    assert (profiles / "RYYJ-R8229-2.yaml").is_file()


def test_a_custom_fixtures_dir_is_honoured(
    tmp_path: Path, monkeypatch: Any
) -> None:
    args = cli.build_parser().parse_args(
        [
            "config",
            "init",
            DEVICE["guid"],
            "--out",
            str(tmp_path / "profiles"),
            "--fixtures-dir",
            str(tmp_path / "captured"),
        ]
    )
    asyncio.run(cli.cmd_config_init(_FakeClient(), args))
    assert (tmp_path / "captured" / "RYYJ-R8229.json").is_file()
    assert not (tmp_path / "fixtures").exists()


def test_an_unknown_guid_is_reported(tmp_path: Path) -> None:
    args = cli.build_parser().parse_args(
        ["config", "init", "NOPE", "--out", str(tmp_path / "profiles")]
    )
    with pytest.raises(SystemExit) as stop:
        asyncio.run(cli.cmd_config_init(_FakeClient(), args))
    assert "not on this account" in str(stop.value.code)


def test_the_subcommand_is_registered_and_offline_writes_are_separate() -> None:
    assert "config" in cli.COMMANDS
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["config"])
