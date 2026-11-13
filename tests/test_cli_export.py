"""The CLI export command: bundle -> draft -> contribution Markdown.

These tests drive the real CLI over a synthetic capture and check the document
it composes: the five sections, the two marker blocks (parsed with the promote
tool, so a maintainer can actually use the file), the verification branches and
the default paths.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[1]
CLI_PATH = REPO / "tools" / "roki_cli.py"
PROMOTE_PATH = REPO / "tools" / "promote_profile.py"


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cli = _load_module("roki_cli_export_test", CLI_PATH)
promote = _load_module("roki_promote_export_test", PROMOTE_PATH)

BUNDLE = {
    "schema": "roki-bundle/1",
    "created": "2026-11-13T00:00:00Z",
    "device": {"dc": "RYYJ", "dt": "R8229", "transport": "mqtt"},
    "properties": {
        "workStatus": 1,
        "level": 2,
        "phone": "13800000000",
    },
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


def _run(
    tmp_path: Path,
    monkeypatch: Any,
    *extra: str,
    answer: str | None = None,
) -> int:
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(
        json.dumps(BUNDLE, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    if answer is not None:
        monkeypatch.setattr("builtins.input", lambda prompt="": answer)
    args = cli.build_parser().parse_args(["export", str(bundle_path), *extra])
    return cli.cmd_export(None, args)


def _markdown(tmp_path: Path) -> tuple[Path, str]:
    exports = sorted((tmp_path / "exports").glob("*.md"))
    assert len(exports) >= 1, "no .md was written"
    path = exports[-1]
    return path, path.read_text(encoding="utf-8")


def test_export_writes_exactly_one_markdown(
    tmp_path: Path, monkeypatch: Any
) -> None:
    assert _run(tmp_path, monkeypatch, answer="") == 0
    files = sorted((tmp_path / "exports").iterdir())
    assert len(files) == 1
    assert re.fullmatch(r"RYYJ-R8229-\d{8}-\d{4}\.md", files[0].name)
    # The draft artifacts travel next to it, so section 4 can name the file.
    profiles = tmp_path / "profiles"
    assert (profiles / "RYYJ-R8229.yaml").is_file()
    assert (profiles / "RYYJ-R8229.json").is_file()


def test_the_markdown_has_the_five_sections_and_the_markers(
    tmp_path: Path, monkeypatch: Any
) -> None:
    assert _run(tmp_path, monkeypatch, answer="") == 0
    _, markdown = _markdown(tmp_path)
    for heading in ("## 1. 设备与匹配", "## 2. 验证清单", "## 3. profile YAML",
                    "## 4. fixture", "## 5. 如何贡献"):
        assert heading in markdown, heading
    assert "## 6." not in markdown
    assert markdown.count("<!-- roki:profile-yaml -->") == 1
    assert markdown.count("<!-- roki:fixture-json -->") == 1
    assert "已脱敏：不含设备 GUID、手机号、token、App GUID" in markdown
    # The phone number never reaches the document.
    assert "13800000000" not in markdown


def test_the_markdown_is_promotable(tmp_path: Path, monkeypatch: Any) -> None:
    assert _run(tmp_path, monkeypatch, answer="") == 0
    path, markdown = _markdown(tmp_path)
    profile_text, fixture = promote.parse_export(markdown)
    profile = yaml.safe_load(profile_text)
    assert profile["extends"] == "python:hood"
    assert list(profile["controls"]) == ["action_1", "action_2"]
    assert fixture["schema"] == "roki-fixture/1"
    assert fixture["source"] == "bundle"

    # The promote tool can plan (and apply) the file as it stands.
    repo = tmp_path / "repo"
    promotion = promote.plan(path, repo)
    promote.apply(promotion)
    assert promotion.profile_target.is_file()
    assert promotion.fixture_target.is_file()
    assert json.loads(
        promotion.fixture_target.read_text(encoding="utf-8")
    )["source"] == "user"


def test_the_default_answer_keeps_the_controls_unverified(
    tmp_path: Path, monkeypatch: Any
) -> None:
    # An empty answer is the documented default: no verification claim.
    assert _run(tmp_path, monkeypatch, answer="") == 0
    _, markdown = _markdown(tmp_path)
    profile_text, _ = promote.parse_export(markdown)
    controls = yaml.safe_load(profile_text)["controls"]
    assert [entry["verified"] for entry in controls.values()] == [False, False]
    assert "- 验证状态：尚未验证（CLI 默认 N）" in markdown
    assert "| false |" in markdown
    assert "| true |" not in markdown


def test_a_verified_answer_flips_the_controls(
    tmp_path: Path, monkeypatch: Any
) -> None:
    assert _run(tmp_path, monkeypatch, answer="y") == 0
    _, markdown = _markdown(tmp_path)
    profile_text, _ = promote.parse_export(markdown)
    controls = yaml.safe_load(profile_text)["controls"]
    assert [entry["verified"] for entry in controls.values()] == [True, True]
    assert "- 验证状态：用户自述已验证（CLI 作答 y）" in markdown
    # The checklist column comes from the same document, so it cannot disagree.
    assert "| true |" in markdown
    assert "| false |" not in markdown


def test_the_flags_skip_the_prompt(tmp_path: Path, monkeypatch: Any) -> None:
    def refuse(prompt: str = "") -> str:
        raise AssertionError("the prompt must not be shown with --verified")

    assert _run(tmp_path, monkeypatch, "--verified", answer=None) == 0
    monkeypatch.setattr("builtins.input", refuse)
    assert _run(tmp_path, monkeypatch, "--unverified", answer=None) == 0
    files = sorted((tmp_path / "exports").glob("*.md"))
    assert len(files) == 2
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert "已脱敏" in text


def test_a_second_export_in_the_same_minute_gets_a_suffix(
    tmp_path: Path, monkeypatch: Any
) -> None:
    assert _run(tmp_path, monkeypatch, answer="") == 0
    assert _run(tmp_path, monkeypatch, answer="") == 0
    names = sorted(path.name for path in (tmp_path / "exports").glob("*.md"))
    assert len(names) == 2
    assert any(
        re.fullmatch(r"RYYJ-R8229-\d{8}-\d{4}\.md", name) for name in names
    )
    assert any(
        re.fullmatch(r"RYYJ-R8229-\d{8}-\d{4}-2\.md", name) for name in names
    )


def test_the_command_prints_the_paths_and_the_next_step(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    assert _run(tmp_path, monkeypatch, answer="") == 0
    output = capsys.readouterr().out
    path, _ = _markdown(tmp_path)
    # Paths are printed relative to the working directory the user ran from.
    assert f"export  : {path.relative_to(tmp_path)}" in output
    assert "profile : profiles" in output
    assert "issues/new?template=device_support.yml" in output
    assert f"python tools/promote_profile.py {path.relative_to(tmp_path)}" in output
    assert "尚未验证" in output


def test_json_output_carries_the_paths(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    assert _run(tmp_path, monkeypatch, "--unverified", "--json", answer=None) == 0
    output = capsys.readouterr().out
    # The human summary comes first, then the machine-readable object.
    payload = json.loads(output[output.index("{") :])
    assert payload["verified"] is False
    assert payload["extends"] == "python:hood"
    assert Path(payload["export"]).is_file()
    assert payload["promote_command"].startswith(
        "python tools/promote_profile.py"
    )
