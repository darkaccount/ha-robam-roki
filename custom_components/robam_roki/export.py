"""Compose the export bundle a user attaches to an issue or a pull request.

The export is deliberately a single Markdown file.  The profile YAML and the
fixture JSON travel inside it as two machine-readable marker blocks, so a
maintainer can promote them with tools/promote_profile.py without asking the
user for a second file -- and the export never writes another .yaml or .json
into the user's configuration directory.

This module is free of Home Assistant imports: composing the document is pure
string work and is unit-tested offline.  Redaction is applied here as well as
in the drafter (idempotent), so a fixture that predates the current privacy
bar still cannot leak a phone number or a token.
"""

from __future__ import annotations

import importlib.util
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

try:  # normal package import (integration and its test suite)
    from .redact import redact_properties, redact_secrets, redact_string
except ImportError:  # loaded straight from the file system by a tool
    _REDACT_PATH = Path(__file__).with_name("redact.py")
    _REDACT_SPEC = importlib.util.spec_from_file_location(
        "roki_redact_export", _REDACT_PATH
    )
    if _REDACT_SPEC is None or _REDACT_SPEC.loader is None:  # pragma: no cover
        raise
    _REDACT_MODULE = importlib.util.module_from_spec(_REDACT_SPEC)
    _REDACT_SPEC.loader.exec_module(_REDACT_MODULE)
    redact_properties = _REDACT_MODULE.redact_properties
    redact_secrets = _REDACT_MODULE.redact_secrets
    redact_string = _REDACT_MODULE.redact_string

#: The marker blocks the promote tool parses.  Keep them byte stable.
PROFILE_MARKER_START = "<!-- roki:profile-yaml -->"
PROFILE_MARKER_END = "<!-- /roki:profile-yaml -->"
FIXTURE_MARKER_START = "<!-- roki:fixture-json -->"
FIXTURE_MARKER_END = "<!-- /roki:fixture-json -->"

#: Where a promoted fixture and profile belong in the repository.
REPO_FIXTURE_DIR = "tests/fixtures/devices"
REPO_PROFILE_DIR = "custom_components/robam_roki/profiles"

#: The issue template path the export tells people to use; the rest of the URL
#: is redundant because the document is pasted into this very repository.
ISSUE_TEMPLATE_PATH = "/issues/new?template=device_support.yml"
REPOSITORY_URL = "https://github.com/darkaccount/ha-robam-roki"

#: The one privacy promise the document makes, and what it must keep true.
REDACTION_NOTE = "> 已脱敏：不含设备 GUID、手机号、token、App GUID"

#: The document format version, so a future tool can tell exports apart.
EXPORT_FORMAT = "roki-export/1"

#: Characters that may stay in a file name segment.
_SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9_-]+")


def _slug(value: Any) -> str:
    """A filesystem-safe one-word form of one dc/dt segment."""
    text = _SAFE_SEGMENT.sub("-", str(value or "").strip()).strip("-")
    return text or "unknown"


def export_filename(dc: str, dt: str, generated_at: datetime) -> str:
    """<dc>-<dt>-<YYYYmmdd-HHMM>.md, exactly as documented to users."""
    return f"{_slug(dc)}-{_slug(dt)}-{generated_at:%Y%m%d-%H%M}.md"


def fixture_repo_path(dc: str, dt: str) -> str:
    return f"{REPO_FIXTURE_DIR}/{_slug(dc)}-{_slug(dt)}.json"


def profile_repo_path(dc: str, dt: str | None = None) -> str:
    """A generic category profile is <dc>.yaml, a single model one <dc>-<dt>."""
    name = _slug(dc) if not dt else f"{_slug(dc)}-{_slug(dt)}"
    return f"{REPO_PROFILE_DIR}/{name}.yaml"


def _cell(value: Any) -> str:
    """Escape one markdown table cell."""
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(":---" for _ in header) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(_cell(value) for value in row) + " |")
    return lines


def _observed_text(changes: Any) -> str:
    """Render one group's property diff: "workStatus 0 → 1, level 0 → 2"."""
    if not isinstance(changes, dict) or not changes:
        return "—"
    parts: list[str] = []
    for key in sorted(changes, key=str):
        change = changes[key]
        if not isinstance(change, dict):
            continue
        before = redact_string(str(change.get("from")))
        after = redact_string(str(change.get("to")))
        parts.append(f"{key} {before} → {after}")
    return ", ".join(parts) if parts else "—"


def observed_index(learned: Any) -> dict[str, dict[str, Any]]:
    """Map "command:body" to the property diff the session observed.

    The profile YAML cannot carry the observed changes (they are evidence, not
    configuration), so the checklist reads them from the last learning session
    when one is still in memory; anything missing renders as "—".
    """
    index: dict[str, dict[str, Any]] = {}
    if not learned:
        return index
    for group in learned:
        if not isinstance(group, dict):
            continue
        observed = group.get("observed") or {}
        for frame in group.get("frames") or []:
            if not isinstance(frame, dict) or frame.get("command") is None:
                continue
            key = f"{int(frame['command'])}:{str(frame.get('body_hex') or '').lower()}"
            index.setdefault(key, observed)
    return index


#: The drafter appends "；观察到 level 0 → 3, ..." to each control line.
_OBSERVED_COMMENT = re.compile(
    r"^\s*[A-Za-z0-9_]+:\s*#.*?；\s*观察到\s+(.+)$"
)
_OBSERVED_PART = re.compile(r"([A-Za-z0-9_]+)\s+(\S+)\s*→\s*(\S+)")


def _coerce(value: str) -> Any:
    """Numbers come back as numbers; everything else stays text."""
    try:
        return int(value)
    except ValueError:
        return value


def observed_from_profile_comments(
    profile_yaml: str,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Recover the property diffs the drafter wrote as YAML comments.

    The profile file is the only durable copy of a learning session (the
    in-memory groups are gone after a reload), so the checklist reads the
    evidence back from the comments the drafter emitted next to each control.
    """
    index: dict[str, dict[str, dict[str, Any]]] = {}
    current: str | None = None
    for line in profile_yaml.splitlines():
        if not line.startswith((" ", "\t")) and line.strip():
            current = None
        match = _OBSERVED_COMMENT.match(line)
        if match is None:
            continue
        key_match = re.match(r"^\s*([A-Za-z0-9_]+):", line)
        if key_match is None:
            continue
        current = key_match.group(1)
        changes: dict[str, Any] = {}
        for part in match.group(1).split(", "):
            piece = _OBSERVED_PART.match(part.strip())
            if piece is not None:
                changes[piece.group(1)] = {
                    "from": _coerce(piece.group(2)),
                    "to": _coerce(piece.group(3)),
                }
        if changes:
            index[current] = changes
    return index

def _control_rows(
    controls: dict[str, dict[str, Any]],
    observed: dict[str, dict[str, Any]],
    observed_by_key: dict[str, dict[str, Any]] | None = None,
) -> list[list[str]]:
    rows: list[list[str]] = []
    for key, entry in controls.items():
        if not isinstance(entry, dict):
            continue
        command = entry.get("command")
        body = entry.get("body_hex")
        if command is not None:
            changes = observed.get(f"{int(command)}:{str(body or '').lower()}")
        else:
            changes = None
        if not changes and observed_by_key:
            changes = observed_by_key.get(str(key))
        rows.append(
            [
                str(key),
                str(entry.get("name") or key),
                "—" if command is None else str(command),
                "—" if body is None else (str(body) or "(空)"),
                _observed_text(changes),
                "true" if entry.get("verified", True) else "false",
            ]
        )
    return rows


def _marker_block(marker: str, language: str, body: str) -> list[str]:
    end_marker = PROFILE_MARKER_END if marker == PROFILE_MARKER_START else FIXTURE_MARKER_END
    return [
        marker,
        f"```{language}",
        body.rstrip(),
        "```",
        end_marker,
    ]


def build_export_markdown(
    *,
    device_name: str,
    dc: str,
    dt: str,
    transport: str,
    extends: str | None,
    profile_yaml: str,
    fixture: dict[str, Any],
    fixture_local_path: str,
    controls: dict[str, dict[str, Any]] | None = None,
    learned: Any = None,
    generated_at: datetime,
    version: str,
) -> str:
    """Compose the whole export document (five sections, two marker blocks)."""
    observed = observed_index(learned)
    observed_by_key = (
        {} if observed else observed_from_profile_comments(profile_yaml)
    )
    rows = _control_rows(controls or {}, observed, observed_by_key)
    safe_fixture = dict(fixture or {})
    # Both halves of the privacy policy: value patterns (phone numbers, tokens,
    # 32-hex ids) and key names that are secrets by definition (appGuid,
    # accessToken...).  Idempotent, so an already-redacted fixture is unchanged.
    safe_fixture["properties"] = redact_secrets(
        redact_properties(safe_fixture.get("properties") or {})
    )

    lines: list[str] = [
        f"# ROBAM ROKI profile export — {_slug(dc)}-{_slug(dt)}",
        "",
        REDACTION_NOTE,
        "",
        f"- 生成时间：{generated_at:%Y-%m-%d %H:%M}",
        f"- 集成版本：{version or 'unknown'}",
        f"- 导出格式：{EXPORT_FORMAT}",
        f"- 设备名称：{redact_string(str(device_name or '')) or '(未命名)'}",
        "",
        "## 1. 设备与匹配",
        "",
    ]
    lines.extend(
        _table(
            ["字段", "值"],
            [
                ["设备", redact_string(str(device_name or "")) or "(未命名)"],
                ["dc", _slug(dc)],
                ["dt", _slug(dt)],
                ["通道", transport or "unknown"],
                ["extends", extends or "（无，本 profile 就是完整定义）"],
            ],
        )
    )
    lines.extend(["", "## 2. 验证清单", ""])
    if rows:
        lines.extend(
            _table(
                ["key", "名称", "命令", "body", "观察到的变化", "verified"],
                rows,
            )
        )
        lines.extend(
            [
                "",
                "验证方法：在集成选项里打开「允许未验证的控制」，重载后逐个按下",
                "这些 button 实体，确认它们和官方 App 上的按钮效果一致；确认后",
                "把 profile 里对应的 verified 改成 true。",
            ]
        )
    else:
        lines.append("（这条 profile 没有 controls 条目。）")
    lines.extend(["", "## 3. profile YAML", ""])
    lines.extend(_marker_block(PROFILE_MARKER_START, "yaml", profile_yaml))
    lines.extend(["", "## 4. fixture", ""])
    lines.extend(
        [
            f"- 本机路径：`{fixture_local_path}`",
            f"- 目标仓库路径：`{fixture_repo_path(dc, dt)}`",
            "",
        ]
    )
    fixture_json = json.dumps(safe_fixture, ensure_ascii=False, indent=2)
    lines.extend(_marker_block(FIXTURE_MARKER_START, "json", fixture_json))
    lines.extend(
        [
            "",
            "## 5. 如何贡献",
            "",
            f"1. 到 GitHub 提 issue（模板「新设备支持」）：{ISSUE_TEMPLATE_PATH}",
            "   把本文件整份贴进去即可，profile 与 fixture 都在里面。",
            "2. 或者直接提 PR：维护者（或你自己）运行",
            "   `python tools/promote_profile.py <本文件>`，它会按第 3、4 节把",
            f"   profile 与 fixture 落到 {REPO_PROFILE_DIR}/ 与 {REPO_FIXTURE_DIR}/。",
            f"3. 仓库地址：{REPOSITORY_URL}",
            "",
        ]
    )
    return "\n".join(lines)


def find_marked_block(markdown: str, start: str, end: str) -> str | None:
    """The fenced content between two markers, or None when absent.

    Kept here (not only in the promote tool) so the writer and every reader
    agree on the exact framing, including the code fence.
    """
    begin = markdown.find(start)
    if begin < 0:
        return None
    finish = markdown.find(end, begin + len(start))
    if finish < 0:
        return None
    block = markdown[begin + len(start) : finish].strip()
    if block.startswith("```"):
        block = block.split("\n", 1)[1] if "\n" in block else ""
        if block.rstrip().endswith("```"):
            block = block.rstrip()[: -len("```")]
    return block.strip()

