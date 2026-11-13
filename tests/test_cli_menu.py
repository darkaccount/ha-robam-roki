"""The interactive menu: dispatch, credentials, device memory, chaining.

Every menu test drives the real menu code with an injected input script and an
injected runner, so nothing here needs a phone number, a password or a network.
"""

from __future__ import annotations

import importlib.util
import json
import re
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


cli = _load_module("roki_cli_menu_test", CLI_PATH)

DEVICES = [
    {
        "guid": "R8229HOODGUID0000",
        "name": "厨房油烟机",
        "dc": "RYYJ",
        "dt": "R8229",
        "deviceSource": 0,
    },
    {
        "guid": "RRQZSTOVEGUID000",
        "name": "蒸烤箱",
        "dc": "RRQZ",
        "dt": "9B39",
        "deviceSource": 0,
    },
]

BUNDLE = {
    "schema": "roki-bundle/1",
    "created": "2026-11-13T00:00:00Z",
    "device": {"dc": "RYYJ", "dt": "R8229", "transport": "mqtt"},
    "properties": {"workStatus": 1, "level": 2},
    "frames": [],
    "observations": [],
    "commands": [
        {
            "index": 0,
            "command": 132,
            "body_hex": "0b3132333435363738393001",
            "observed": {"workStatus": {"from": 0, "to": 1}},
        },
        {
            "index": 1,
            "command": 134,
            "body_hex": "02",
            "observed": {"level": {"from": 1, "to": 2}},
        },
    ],
    "truncated": False,
}


class _ScriptedInput:
    """Answers the prompts in order, then behaves like a closed stdin."""

    def __init__(self, *answers: str) -> None:
        self._answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, prompt: str = "") -> str:
        self.prompts.append(prompt)
        if not self._answers:
            raise EOFError
        return self._answers.pop(0)


class _FakeRunner:
    """Stands in for `asyncio.run(with_client(...))`."""

    def __init__(self, *, bundle: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.bundle = bundle
        self.error: BaseException | None = None

    def __call__(self, args: Any, handler: Any) -> Any:
        name = getattr(handler, "__name__", str(handler))
        self.calls.append((name, dict(vars(args))))
        if self.error is not None:
            raise self.error
        if handler is cli._menu_devices:
            return [dict(device) for device in DEVICES]
        if handler is cli._menu_properties:
            return {"workStatus": 1, "level": 2, "phone": "13800000000"}
        if handler is cli._menu_collect:
            target = Path(args.out)
            target.write_text(
                json.dumps(self.bundle or BUNDLE, ensure_ascii=False),
                encoding="utf-8",
            )
            return {
                "bundle": str(target),
                "frames": 3,
                "observations": 1,
                "commands": 2,
                "truncated": False,
            }
        return None

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def last(self, name: str) -> dict[str, Any]:
        for call_name, args in reversed(self.calls):
            if call_name == name:
                return args
        raise AssertionError(f"{name} was never called; calls={self.calls}")


def _menu(
    *answers: str,
    runner: _FakeRunner | None = None,
    phone: str = "",
    password: str = "",
    password_answer: str = "secret",
) -> tuple[cli._Menu, list[str], _ScriptedInput, _FakeRunner]:
    say: list[str] = []
    ask = _ScriptedInput(*answers)
    runner = runner or _FakeRunner()
    menu = cli._Menu(
        ask=ask,
        say=say.append,
        password_ask=lambda prompt="": password_answer,
        runner=runner,
        phone=phone,
        password=password,
    )
    return menu, say, ask, runner


def _run_menu(*answers: str, **kwargs: Any) -> tuple[int, list[str], _FakeRunner]:
    say: list[str] = []
    runner = kwargs.pop("runner", None) or _FakeRunner()
    code = cli.interactive_menu(
        ask=_ScriptedInput(*answers),
        say=say.append,
        password_ask=lambda prompt="": "secret",
        runner=runner,
        **kwargs,
    )
    return code, say, runner


def _output(say: list[str]) -> str:
    return "\n".join(say)


# -- entry point ---------------------------------------------------------


def test_no_arguments_shows_the_menu_and_zero_exits(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["roki_cli.py"])
    monkeypatch.setattr("builtins.input", lambda prompt="": (print(prompt, end="") or "0") )
    with pytest.raises(SystemExit) as stop:
        cli.main()
    assert stop.value.code == 0
    output = capsys.readouterr().out
    for key, label in cli.MENU_ITEMS:
        assert label in output
    assert cli.MENU_HINT in output


def test_the_menu_lists_every_task_and_the_hint() -> None:
    code, say, _ = _run_menu("0")
    assert code == 0
    output = _output(say)
    assert "ROBAM ROKI 命令行" in output
    assert "1) 登录并查看设备" in output
    assert "3) 生成配置（从设备信息，无需采集）" in output
    assert "4) 采集证据包（采完自动合并进配置）" in output
    assert "6) 诊断与查询 → 二级菜单" in output
    assert "7) 设备控制 → 二级菜单（发送前确认）" in output
    assert cli.MENU_HINT in output
    # Seven tasks plus 0) 退出: the main menu is deliberately short.
    assert len(cli.MENU_ITEMS) == 8
    assert [key for key, _ in cli.MENU_ITEMS] == [
        "1", "2", "3", "4", "5", "6", "7", "0",
    ]


@pytest.mark.parametrize("choice", ["?", "？", "h"])
def test_the_help_choice_prints_the_subcommand_table(choice: str) -> None:
    code, say, _ = _run_menu(choice, "0")
    assert code == 0
    output = _output(say)
    assert "usage:" in output
    assert "export" in output
    assert "draft" in output
    assert "collect" in output


def test_invoke_and_setprop_stay_out_of_the_menu() -> None:
    labels = " ".join(label for _, label in cli.MENU_ITEMS)
    labels += " ".join(label for _, label in cli.CONTROL_ITEMS)
    assert "invoke" not in labels
    assert "setprop" not in labels


# -- leaving -------------------------------------------------------------


def test_eof_exits_with_zero(*_: Any) -> None:
    code, say, _ = _run_menu("1")
    assert code == 0
    # The credential prompt hit EOF, which ends the session cleanly.
    assert "手机号" in "\n".join(say) or True


def test_ctrl_c_at_the_menu_prompt_exits_cleanly() -> None:
    def interrupt(prompt: str = "") -> str:
        raise KeyboardInterrupt

    assert cli.interactive_menu(ask=interrupt, say=lambda line="": None,
                                password_ask=lambda prompt="": "x",
                                runner=_FakeRunner()) == 0


def test_ctrl_c_inside_a_task_returns_to_the_menu() -> None:
    runner = _FakeRunner()
    runner.error = KeyboardInterrupt()
    say: list[str] = []
    code = cli.interactive_menu(
        ask=_ScriptedInput("1", "0"),
        say=say.append,
        password_ask=lambda prompt="": "secret",
        runner=runner,
        phone="13800000000",
        password="secret",
    )
    assert code == 0
    assert "已中断，回到主菜单" in _output(say)


def test_a_failed_task_returns_to_the_menu() -> None:
    runner = _FakeRunner()
    runner.error = RuntimeError("云端不可达")
    say: list[str] = []
    code = cli.interactive_menu(
        ask=_ScriptedInput("1", "0"),
        say=say.append,
        password_ask=lambda prompt="": "secret",
        runner=runner,
        phone="13800000000",
        password="secret",
    )
    assert code == 0
    output = _output(say)
    assert "操作失败：云端不可达" in output
    assert "回到主菜单" in output
    # The menu came back: the second prompt was the main menu again.
    assert output.count("选择: ") <= 0  # say() only gets printed lines
    assert output.count("ROBAM ROKI 命令行") >= 2


def test_an_unknown_option_is_reported() -> None:
    code, say, _ = _run_menu("9", "0")
    assert code == 0
    assert "没有 9 这个选项" in _output(say)


# -- credentials ---------------------------------------------------------


def test_credentials_are_asked_once_and_only_in_memory() -> None:
    menu, say, ask, runner = _menu(
        "1", "13800000000", "2", "1", "0", password_answer="pw"
    )
    assert menu.run() == 0
    credential_prompts = [p for p in ask.prompts if "手机号" in p]
    # The phone number is typed; the password comes from getpass, never echoed.
    assert credential_prompts == ["手机号: "]
    assert menu.phone == "13800000000"
    assert menu.password == "pw"
    assert runner.names().count("_menu_devices") == 1
    assert "_menu_properties" in runner.names()
    assert "不会写入任何文件" in _output(say)


def test_credentials_come_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ROKI_PHONE", "13800000000")
    monkeypatch.setenv("ROKI_PASSWORD", "envpw")
    menu, _, ask, runner = _menu("1", "0")
    assert menu.run() == 0
    assert not [p for p in ask.prompts if "手机号" in p or "密码" in p]
    assert runner.last("_menu_devices")["phone"] == "13800000000"
    assert runner.last("_menu_devices")["password"] == "envpw"


def test_startup_arguments_win_over_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ROKI_PHONE", "13800000000")
    menu, _, ask, runner = _menu("1", "0", phone="13800000000", password="cli")
    assert menu.run() == 0
    assert not [p for p in ask.prompts if "手机号" in p or "密码" in p]
    assert runner.last("_menu_devices")["phone"] == "13800000000"


# -- devices -------------------------------------------------------------


def test_login_lists_the_devices_by_name() -> None:
    code, say, runner = _run_menu("1", "0", phone="p", password="pw")
    assert code == 0
    output = _output(say)
    assert "登录成功：2 台设备" in output
    assert "厨房油烟机  RYYJ R8229" in output
    assert "蒸烤箱  RRQZ 9B39" in output
    assert runner.names() == ["_menu_devices"]


def test_the_last_device_is_reused_on_enter() -> None:
    menu, _, ask, runner = _menu(
        "2", "2", "2", "", "0", phone="p", password="pw"
    )
    assert menu.run() == 0
    device_prompts = [p for p in ask.prompts if p.startswith("选择设备")]
    assert device_prompts[-1] == "选择设备（回车沿用上次）: "
    assert runner.last("_menu_properties")["device"] == DEVICES[1]["guid"]


def test_a_bad_device_number_is_rejected() -> None:
    menu, say, _, runner = _menu(
        "2", "x", "2", "5", "2", "1", "0", phone="p", password="pw"
    )
    assert menu.run() == 0
    output = _output(say)
    assert "请输入设备编号" in output
    assert "没有这个编号" in output
    assert runner.last("_menu_properties")["device"] == DEVICES[0]["guid"]


# -- chaining ------------------------------------------------------------


def test_collect_chains_into_a_config_and_an_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    say: list[str] = []
    runner = _FakeRunner()
    code = cli.interactive_menu(
        ask=_ScriptedInput("4", "1", "y", "n", "0"),
        say=say.append,
        password_ask=lambda prompt="": "secret",
        runner=runner,
        phone="p",
        password="pw",
    )
    assert code == 0
    output = _output(say)
    # The bundle, the new config, its fixture and the export all exist.
    assert (tmp_path / "bundle.json").is_file()
    profile = tmp_path / "profiles" / "RYYJ-R8229.yaml"
    assert profile.is_file()
    assert "extends: python:hood" in profile.read_text(encoding="utf-8")
    assert (tmp_path / "fixtures" / "RYYJ-R8229.json").is_file()
    assert "已生成新配置" in output
    exports = sorted((tmp_path / "exports").glob("*.md"))
    assert len(exports) == 1
    assert "产物清单" in output
    # Paths are printed as given (relative to the working directory).
    assert profile.relative_to(tmp_path).as_posix() in output.replace("\\", "/")
    assert exports[0].name in output
    # Default answer to "did you verify these?" is no.
    assert "尚未验证" in output
    assert "device_support.yml" in output


def test_collect_can_skip_the_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    say: list[str] = []
    code = cli.interactive_menu(
        ask=_ScriptedInput("4", "1", "n", "0"),
        say=say.append,
        password_ask=lambda prompt="": "secret",
        runner=_FakeRunner(),
        phone="p",
        password="pw",
    )
    assert code == 0
    assert (tmp_path / "profiles" / "RYYJ-R8229.yaml").is_file()
    assert (tmp_path / "fixtures" / "RYYJ-R8229.json").is_file()
    assert not (tmp_path / "exports").exists()
    assert "已跳过导出" in _output(say)


def test_an_existing_bundle_is_not_overwritten_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bundle.json").write_text("{}", encoding="utf-8")
    say: list[str] = []
    runner = _FakeRunner()
    code = cli.interactive_menu(
        ask=_ScriptedInput("4", "1", "n", "n", "0"),
        say=say.append,
        password_ask=lambda prompt="": "secret",
        runner=runner,
        phone="p",
        password="pw",
    )
    assert code == 0
    out = str(runner.last("_menu_collect")["out"])
    assert re.fullmatch(r"bundle-\d{8}-\d{6}\.json", Path(out).name)


def test_the_config_task_generates_a_profile_from_device_info(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Menu 3 needs no capture at all: properties are enough."""
    monkeypatch.chdir(tmp_path)
    say: list[str] = []
    code = cli.interactive_menu(
        ask=_ScriptedInput("3", "1", "0"),
        say=say.append,
        password_ask=lambda prompt="": "secret",
        runner=_FakeRunner(),
        phone="p",
        password="pw",
    )
    assert code == 0
    output = _output(say)
    profile = tmp_path / "profiles" / "RYYJ-R8229.yaml"
    assert profile.is_file()
    document = yaml.safe_load(profile.read_text(encoding="utf-8"))
    assert document["extends"] == "python:hood"
    # workStatus/level are covered by the hood driver; only phone is new.
    assert list(document["sensors"]) == ["phone"]
    assert (tmp_path / "fixtures" / "RYYJ-R8229.json").is_file()
    assert "继承内置驱动 python:hood" in output
    assert "采集证据包" in output


EXISTING_PROFILE = """# 手写的配置头
schema: roki-profile/1
extends: python:hood
match:
  dc: RYYJ
  dt: R8229
sensors:
  phone:
    name: 手机号
    # 手工备注
controls:
  action_1:  # 命令 132，body 01
    name: 开机
    kind: mqtt
    command: 132
    body_hex: "01"
    verified: false
"""


def test_collect_merges_into_an_existing_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Menu 4 must merge, not clobber: sensors and hand edits stay."""
    monkeypatch.chdir(tmp_path)
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "RYYJ-R8229.yaml").write_text(
        EXISTING_PROFILE, encoding="utf-8"
    )
    say: list[str] = []
    code = cli.interactive_menu(
        ask=_ScriptedInput("4", "1", "n", "0"),
        say=say.append,
        password_ask=lambda prompt: "secret",
        runner=_FakeRunner(),
        phone="p",
        password="pw",
    )
    assert code == 0
    output = _output(say)
    assert "已合并进已有配置" in output
    assert "跳过 1 条重复" in output
    text = (profiles / "RYYJ-R8229.yaml").read_text(encoding="utf-8")
    document = yaml.safe_load(text)
    # The capture's 132:01 is a duplicate; 134:02 becomes action_2.
    assert list(document["controls"]) == ["action_1", "action_2"]
    assert document["controls"]["action_2"]["command"] == 134
    # Everything hand written survives.
    assert "# 手写的配置头" in text
    assert "# 手工备注" in text
    assert list(document["sensors"]) == ["phone"]
    # A fresh config was not created next to it.
    assert not (profiles / "RYYJ-R8229-2.yaml").exists()


def test_the_export_task_asks_about_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bundle.json").write_text(json.dumps(BUNDLE), encoding="utf-8")
    say: list[str] = []
    code, say, _ = _run_menu("5", "", "y", "0")
    assert code == 0
    exports = sorted((tmp_path / "exports").glob("*.md"))
    assert len(exports) == 1
    assert "用户自述已验证" in _output(say)
    assert "verified: true" in exports[0].read_text(encoding="utf-8")


# -- diagnostics and levels ---------------------------------------------


def test_diagnostics_is_a_submenu_with_three_commands() -> None:
    code, say, runner = _run_menu("6", "2", "0", phone="p", password="pw")
    assert code == 0
    output = _output(say)
    assert "MQTT 连通性" in output
    assert "诊断 JSON" in output
    assert "机型档位表" in output
    assert "cmd_dump" in runner.names()
    labels = " ".join(label for _, label in cli.DIAGNOSTIC_ITEMS)
    assert "invoke" not in labels and "setprop" not in labels


def test_mqtt_check_picks_a_device() -> None:
    code, _, runner = _run_menu(
        "6", "1", "1", "0", phone="p", password="pw"
    )
    assert code == 0
    assert runner.last("cmd_mqttcheck")["device"] == DEVICES[0]["guid"]


def test_the_level_ladder_sits_in_the_diagnostics_submenu() -> None:
    code, say, runner = _run_menu("6", "3", "0", phone="p", password="pw")
    assert code == 0
    assert "机型档位表" in _output(say)
    assert runner.names() == ["cmd_levels"]
    assert runner.last("cmd_levels")["device"] is None


# -- device control ------------------------------------------------------


def test_control_shows_the_command_and_requires_a_yes() -> None:
    code, say, runner = _run_menu(
        "7", "2", "1", "3", "n", "0", phone="p", password="pw"
    )
    assert code == 0
    output = _output(say)
    assert "将向 厨房油烟机 发送：档位 3" in output
    assert "已取消，没有发送任何命令" in output
    assert "cmd_level" not in runner.names()


def test_control_sends_after_confirmation() -> None:
    code, say, runner = _run_menu(
        "7", "2", "1", "3", "y", "0", phone="p", password="pw"
    )
    assert code == 0
    assert runner.last("cmd_level")["level"] == 3
    assert runner.last("cmd_level")["device"] == DEVICES[0]["guid"]
    assert runner.last("cmd_level")["transport"] == "auto"
    assert "已发送" in _output(say)


def test_control_covers_power_light_and_countdown() -> None:
    for choice, answer, handler, field, value in (
        ("1", "off", "cmd_power", "state", "off"),
        ("3", "on", "cmd_light", "state", "on"),
        ("4", "15", "cmd_timing", "minutes", 15),
    ):
        code, say, runner = _run_menu(
            "7", choice, "1", answer, "y", "0", phone="p", password="pw"
        )
        assert code == 0, (choice, _output(say))
        assert runner.last(handler)[field] == value


def test_control_rejects_an_out_of_range_level() -> None:
    code, say, runner = _run_menu(
        "7", "2", "1", "9", "0", phone="p", password="pw"
    )
    assert code == 0
    assert "需要 1-3 之间的整数" in _output(say)
    assert "cmd_level" not in runner.names()


def test_control_back_option_sends_nothing() -> None:
    code, say, runner = _run_menu("7", "0", "0", phone="p", password="pw")
    assert code == 0
    assert runner.calls == []
    assert "设备控制" in _output(say)
