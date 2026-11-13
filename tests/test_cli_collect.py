"""Tests for the collect / draft CLI additions (P2-B).

Everything runs offline: the cloud client and the MQTT session are fakes, and
the draft command only touches local JSON files.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest
import yaml

CLI_PATH = Path(__file__).resolve().parents[1] / "tools" / "roki_cli.py"
PROFILES_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "robam_roki"
    / "profiles.py"
)


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cli = _load_module("roki_cli_collect_test", CLI_PATH)
profiles = _load_module("roki_profiles_cli_test", PROFILES_PATH)

DEVICE_GUID = "R8229000000000000"
APP_GUID = "RKDRD000000000000"
DEVICE = {
    "guid": DEVICE_GUID,
    "name": "厨房油烟机",
    "dc": "RYYJ",
    "dt": "R8229",
    "deviceSource": 0,
}
REST_DEVICE = {**DEVICE, "deviceSource": 1}


def _report(
    work_status: int = 1,
    level: int = 2,
    light: int = 1,
    clean: int = 0,
    timing: int = 0,
) -> bytes:
    """A synthetic 131 status report (17-byte guid + command + body)."""
    body = bytes([work_status, level, light, clean, timing & 0xFF])
    return (
        DEVICE_GUID.encode("ascii")
        + bytes([cli.mqtt.REPORT_FAN_STATUS])
        + body
    )


def _collect_args(tmp_path: Path, **overrides: Any) -> types.SimpleNamespace:
    values: dict[str, Any] = {
        "device": DEVICE_GUID,
        "out": str(tmp_path / "bundle.json"),
        "duration": 0.01,
        "mark": False,
        "max_frames": 5000,
        "max_bytes": 5 * 1024 * 1024,
        "json": False,
    }
    values.update(overrides)
    return types.SimpleNamespace(**values)


def _draft_args(tmp_path: Path, source: Path, **overrides: Any) -> types.SimpleNamespace:
    values: dict[str, Any] = {
        "input": str(source),
        "out": str(tmp_path / "out"),
        "dc": None,
        "dt": None,
        "json": False,
    }
    values.update(overrides)
    return types.SimpleNamespace(**values)


class _FakeClient:
    user_id = "1234567890"

    def __init__(self, devices: list[dict[str, Any]]) -> None:
        self.devices = devices

    async def async_get_app_guid(self) -> str:
        return APP_GUID

    async def async_get_user(self) -> None:
        return None

    async def async_bind_app_guid(self) -> None:
        return None

    async def async_get_devices(self) -> list[dict[str, Any]]:
        return self.devices

    async def async_get_properties(self, device_id: str) -> dict[str, Any]:
        return {
            "workStatus": 1,
            "level": 2,
            "phone": "13800000000",
            "guid": "0123456789abcdef0123456789abcdef",
        }


class _FakeSession:
    app_guid = APP_GUID

    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []
        self.stopped = False

    async def async_publish(self, topic: str, payload: bytes) -> None:
        self.published.append((topic, payload))

    async def async_stop(self) -> None:
        self.stopped = True


def _install_fake_session(monkeypatch: pytest.MonkeyPatch, frames: list[bytes]):
    """Replace the MQTT session with one that replays frames then idles."""
    session = _FakeSession()

    async def fake_collect_session(client, handler, device_guid):
        for payload in frames:
            handler("topic", payload)
        return session

    monkeypatch.setattr(cli, "_collect_session", fake_collect_session)
    return session


# -- parser --------------------------------------------------------------


def test_collect_parser_defaults_and_required() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["collect", "--device", DEVICE_GUID])
    assert args.out == "bundle.json"
    assert args.duration == 120.0
    assert args.mark is False
    assert args.max_frames == 5000
    assert args.max_bytes == 5 * 1024 * 1024
    with pytest.raises(SystemExit):
        parser.parse_args(["collect"])


def test_draft_parser_defaults() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["draft", "in.json"])
    assert args.input == "in.json"
    assert args.out == "."
    assert args.dc is None and args.dt is None
    assert args.json is False


def test_both_commands_are_registered() -> None:
    assert "collect" in cli.COMMANDS
    assert "draft" in cli.COMMANDS


def test_collect_help_documents_the_read_only_and_hex_rules() -> None:
    parser = cli.build_parser()
    command_action = next(
        action for action in parser._actions if action.dest == "command"
    )
    collect = command_action.choices["collect"]
    text = " ".join(
        part for part in (collect.description, collect.epilog) if part
    )
    assert "command 130" in text
    assert "no control command" in text
    assert "does not contain the sender guid" in text


# -- collect: REST -------------------------------------------------------


def test_collect_rest_marks_frames_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_session(client, handler, device_guid):
        raise AssertionError("REST devices must not open an MQTT session")

    monkeypatch.setattr(cli, "_collect_session", no_session)
    client = _FakeClient([REST_DEVICE])
    asyncio.run(cli.cmd_collect(client, _collect_args(tmp_path)))

    bundle = json.loads((tmp_path / "bundle.json").read_text(encoding="utf-8"))
    assert bundle["schema"] == "roki-bundle/1"
    assert bundle["device"] == {"dc": "RYYJ", "dt": "R8229", "transport": "rest"}
    assert bundle["frames"] == []
    assert bundle["frames_unavailable"] == "rest"
    assert bundle["truncated"] is False
    assert bundle["created"].endswith("Z")
    # Properties are redacted before they ever reach the file.
    assert bundle["properties"]["phone"] == "**REDACTED**"
    assert bundle["properties"]["guid"] == "**REDACTED**"
    assert bundle["properties"]["level"] == 2


# -- collect: MQTT -------------------------------------------------------


def test_collect_compresses_identical_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frames = [
        _report(level=2),
        _report(level=2),  # identical: must not be stored again
        _report(level=2),
        _report(level=3, timing=15),  # real change: stored with an observation
    ]
    session = _install_fake_session(monkeypatch, frames)
    client = _FakeClient([DEVICE])
    asyncio.run(cli.cmd_collect(client, _collect_args(tmp_path)))

    bundle = json.loads((tmp_path / "bundle.json").read_text(encoding="utf-8"))
    assert bundle["device"]["transport"] == "mqtt"
    assert len(bundle["frames"]) == 2
    for frame in bundle["frames"]:
        assert isinstance(frame[0], int)  # offset_ms
        assert frame[1] == cli.mqtt.REPORT_FAN_STATUS
        assert isinstance(frame[2], str) and frame[2]  # body hex
    # The source guid is not part of the stored body hex.
    assert APP_GUID.encode().hex() not in bundle["frames"][0][2]
    assert DEVICE_GUID.encode().hex() not in bundle["frames"][0][2]
    assert bundle["properties"]["level"] == 3
    assert bundle["properties"]["timing"] == 15
    assert len(bundle["observations"]) == 2
    assert bundle["observations"][-1]["label"] == "auto"
    assert bundle["observations"][-1]["changes"]["level"] == {"from": 2, "to": 3}
    assert session.stopped is True


def test_collect_only_ever_publishes_the_read_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _install_fake_session(monkeypatch, [_report()])
    client = _FakeClient([DEVICE])
    asyncio.run(cli.cmd_collect(client, _collect_args(tmp_path)))
    # The only frame the collector publishes is the read-only 130 poll.
    assert session.published
    for _topic, payload in session.published:
        assert payload[17] == cli.mqtt.CMD_FAN_POLL


def test_collect_truncates_at_the_frame_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frames = [_report(level=1), _report(level=2), _report(level=3)]
    _install_fake_session(monkeypatch, frames)
    client = _FakeClient([DEVICE])
    asyncio.run(cli.cmd_collect(client, _collect_args(tmp_path, max_frames=2)))

    bundle = json.loads((tmp_path / "bundle.json").read_text(encoding="utf-8"))
    assert len(bundle["frames"]) == 2
    assert bundle["truncated"] is True


# -- collector internals -------------------------------------------------


def test_mark_observation_only_holds_changed_keys() -> None:
    collector = cli._Collector(DEVICE_GUID, auto_observations=False)
    collector.set_properties({"workStatus": 1, "level": 2, "timing": 0})
    collector.mark("开机")
    collector.set_properties({"workStatus": 1, "level": 3, "timing": 0})
    collector.mark("强档")

    assert [entry["label"] for entry in collector.observations] == ["开机", "强档"]
    # The baseline is the state capture started from, so the first mark only
    # reports what changed after it.
    assert collector.observations[0]["changes"] == {}
    # Only the level changed between the two marks.
    assert collector.observations[1]["changes"] == {
        "level": {"from": 2, "to": 3}
    }


def test_collect_mark_mode_records_labelled_observations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _FakeSession()
    handler_box: dict[str, Any] = {}

    async def fake_collect_session(client, handler, device_guid):
        handler_box["handler"] = handler
        handler("topic", _report(level=2))
        return session

    monkeypatch.setattr(cli, "_collect_session", fake_collect_session)
    labels = iter(["按下强档", ""])

    def fake_input(prompt: str = "") -> str:
        label = next(labels)
        if label:
            handler_box["handler"]("topic", _report(level=3))
        return label

    monkeypatch.setattr("builtins.input", fake_input)
    client = _FakeClient([DEVICE])
    asyncio.run(cli.cmd_collect(client, _collect_args(tmp_path, mark=True)))

    bundle = json.loads((tmp_path / "bundle.json").read_text(encoding="utf-8"))
    assert bundle["observations"][0]["label"] == "按下强档"
    assert bundle["observations"][0]["changes"]["level"] == {"from": 2, "to": 3}
    # Mark mode does not also emit the automatic change observations.
    assert [entry["label"] for entry in bundle["observations"]] == ["按下强档"]


# -- draft ---------------------------------------------------------------

BUNDLE_INPUT: dict[str, Any] = {
    "schema": "roki-bundle/1",
    "created": "2026-11-12T00:00:00Z",
    "device": {"dc": "RYYJ", "dt": "R8229", "transport": "mqtt"},
    "properties": {
        "level": 2,
        "workStatus": 1,
        "phone": "13800000000",
        "guid": "0123456789abcdef0123456789abcdef",
    },
    "frames": [],
    "observations": [],
    "truncated": False,
}

DIAGNOSTICS_INPUT: dict[str, Any] = {
    "entry": {"title": "ROKI", "data": {}, "options": {}},
    "coordinator": {"data": {"RYYJ...0001": {"level": 2}}},
    "devices": [
        {
            "guid_masked": "RYYJ...0001",
            "name": "RYYJ-R8229",
            "dc": "RYYJ",
            "dt": "R8229",
            "deviceSource": 0,
        }
    ],
}


def _write_json(tmp_path: Path, name: str, payload: Any) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_draft_detects_a_bundle_and_writes_both_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _write_json(tmp_path, "bundle.json", BUNDLE_INPUT)
    rc = cli.cmd_draft(None, _draft_args(tmp_path, source))
    output = capsys.readouterr().out

    assert rc == 0
    profile_path = tmp_path / "out" / "RYYJ-R8229.yaml"
    fixture_path = tmp_path / "out" / "RYYJ-R8229.json"
    assert profile_path.is_file()
    assert fixture_path.is_file()
    assert f"profile : {profile_path}" in output
    assert f"fixture : {fixture_path}" in output
    assert "privacy : 2 sensitive value(s) redacted" in output

    document = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    assert document["schema"] == "roki-profile/1"
    assert document["match"] == {"dc": "RYYJ", "dt": "R8229"}
    assert list(document["sensors"]) == ["guid", "level", "phone", "workStatus"]
    assert "# source: bundle" in profile_path.read_text(encoding="utf-8")

    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert fixture["schema"] == "roki-fixture/1"
    assert fixture["source"] == "bundle"
    assert fixture["properties"]["phone"] == "**REDACTED**"
    assert fixture["properties"]["guid"] == "**REDACTED**"


def test_draft_detects_diagnostics(tmp_path: Path) -> None:
    source = _write_json(tmp_path, "diag.json", DIAGNOSTICS_INPUT)
    rc = cli.cmd_draft(None, _draft_args(tmp_path, source))
    assert rc == 0
    profile_path = tmp_path / "out" / "RYYJ-R8229.yaml"
    assert profile_path.is_file()
    document = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    assert list(document["sensors"]) == ["level"]
    assert "# source: auto" in profile_path.read_text(encoding="utf-8")


def test_draft_output_loads_in_the_profile_loader(tmp_path: Path) -> None:
    source = _write_json(tmp_path, "bundle.json", BUNDLE_INPUT)
    assert cli.cmd_draft(None, _draft_args(tmp_path, source)) == 0
    profile_path = tmp_path / "out" / "RYYJ-R8229.yaml"
    profile = profiles.load_profile_file(profile_path, profiles.SOURCE_USER)
    assert profile is not None
    matched = profiles.match_profile({"dc": "RYYJ", "dt": "R8229"}, [profile])
    assert matched is not None
    assert matched.id == "RYYJ-R8229"


def test_draft_picks_a_device_by_dc(tmp_path: Path) -> None:
    payload = json.loads(json.dumps(DIAGNOSTICS_INPUT))
    payload["devices"].append(
        {
            "guid_masked": "RZKY...0002",
            "name": "RZKY-98AD3",
            "dc": "RZKY",
            "dt": "98AD3",
        }
    )
    payload["coordinator"]["data"]["RZKY...0002"] = {"workState": 3}
    source = _write_json(tmp_path, "diag.json", payload)

    assert cli.cmd_draft(None, _draft_args(tmp_path, source)) == 1
    rc = cli.cmd_draft(None, _draft_args(tmp_path, source, dc="RZKY"))
    assert rc == 0
    assert (tmp_path / "out" / "RZKY-98AD3.yaml").is_file()


def test_draft_rejects_unknown_and_invalid_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    unknown = _write_json(tmp_path, "unknown.json", {"hello": "world"})
    assert cli.cmd_draft(None, _draft_args(tmp_path, unknown)) == 2
    assert "unrecognised input" in capsys.readouterr().err

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert cli.cmd_draft(None, _draft_args(tmp_path, broken)) == 2
    assert "not valid JSON" in capsys.readouterr().err

    missing = tmp_path / "missing.json"
    assert cli.cmd_draft(None, _draft_args(tmp_path, missing)) == 2


def test_draft_runs_offline_without_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _write_json(tmp_path, "bundle.json", BUNDLE_INPUT)
    out_dir = tmp_path / "out"
    monkeypatch.setattr(
        sys,
        "argv",
        ["roki_cli.py", "draft", str(source), "--out", str(out_dir)],
    )

    async def no_cloud(*args, **kwargs):
        raise AssertionError("draft must not touch the cloud client")

    monkeypatch.setattr(cli, "with_client", no_cloud)
    with pytest.raises(SystemExit) as excinfo:
        cli.main()
    assert excinfo.value.code == 0
    assert (out_dir / "RYYJ-R8229.yaml").is_file()


def test_draft_json_flag_prints_a_summary(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = _write_json(tmp_path, "bundle.json", BUNDLE_INPUT)
    assert cli.cmd_draft(None, _draft_args(tmp_path, source, json=True)) == 0
    output = capsys.readouterr().out
    assert '"redacted_values": 2' in output
    assert '"schema"' not in output  # the fixture schema lives in the file
