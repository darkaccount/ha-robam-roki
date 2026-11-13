
"""Draft a YAML profile and a fixture from captured device properties.

The drafter never guesses protocol semantics: property values become sensors
with an inferred scalar type, while anything semantic (device_class, unit,
enum maps) is only emitted as a commented suggestion for the user to review.
The controls block is only rendered when a learning session produced real
commands; otherwise it stays a commented template.

Like redact.py this module is pure Python.  It is importable both as part of
the integration package and stand-alone from tools/roki_cli.py, which is why
the redact import has a file based fallback below.
"""

from __future__ import annotations

import importlib.util
import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

try:  # normal package import (integration and its test suite)
    from .redact import redact_properties
except ImportError:  # loaded straight from the file system by the CLI
    _REDACT_PATH = Path(__file__).with_name("redact.py")
    _REDACT_SPEC = importlib.util.spec_from_file_location(
        "roki_redact_standalone", _REDACT_PATH
    )
    if _REDACT_SPEC is None or _REDACT_SPEC.loader is None:  # pragma: no cover
        raise
    _REDACT_MODULE = importlib.util.module_from_spec(_REDACT_SPEC)
    _REDACT_SPEC.loader.exec_module(_REDACT_MODULE)
    redact_properties = _REDACT_MODULE.redact_properties

#: Schema ids shared with profiles.py and the fixture regression tests.
PROFILE_SCHEMA = "roki-profile/1"
FIXTURE_SCHEMA = "roki-fixture/1"
BUNDLE_SCHEMA = "roki-bundle/1"

#: A callable computes the expected entity values for a fixture from the
#: captured properties; it is usually the real driver in a dict comprehension.
ExpectFn = Callable[[dict[str, Any]], dict[str, Any]]

#: A callable that answers "what does the built-in driver cover?", returning
#: (extends name, covered keys, expect function).  It lets the CLI hand the
#: drafter the driver coverage without the drafter importing Home Assistant.
BuiltinResolver = Callable[
    [dict[str, Any]],
    tuple[
        str | None,
        Iterable[str] | None,
        Callable[[dict[str, Any]], dict[str, Any]] | None,
    ]
    | None,
]

_TEMPERATURE_KEY = re.compile(r"temp|温度", re.IGNORECASE)

_PLAIN_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_PLAIN_VALUE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
_RESERVED_WORDS = frozenset(
    {"true", "false", "yes", "no", "on", "off", "null", "nan", "inf"}
)

#: The always-commented controls skeleton.  It shows every CommandSpec field
#: (kind/on/off/function/param/params/verified) so a user can fill it in later.
CONTROL_TEMPLATE: tuple[str, ...] = (
    "# controls:  # P2 reserved: uncomment only after the protocol is verified",
    "#   power:",
    "#     kind: property",
    "#     on: {working_status: 1}",
    "#     off: {working_status: 0}",
    "#     verified: false",
    "#   level:",
    "#     kind: invoke",
    '#     function: "134"',
    "#     param: level",
    "#     params: {level: 2}",
    "#     verified: false",
)

def strip_frame_envelope(body_hex: str) -> str:
    """Drop the terminal byte and the ascii user id from a captured body.

    The learning session records the whole marshalled body because that is
    what the wire carries: 0x0b (terminal type) + the App account user id +
    the payload.  The executor prepends its own terminal type and user id,
    so keeping the captured ones would double them and the appliance would
    ignore the frame.  Only the payload belongs in body_hex.
    """
    try:
        raw = bytes.fromhex(str(body_hex))
    except ValueError:
        return str(body_hex).lower()
    if not raw or raw[0] != TERMINAL_TYPE_APP:
        return raw.hex()
    rest = raw[1:]
    cut = 0
    while cut < len(rest) and 0x30 <= rest[cut] <= 0x39:
        cut += 1
    return rest[cut:].hex()


def _normalise_learned(learned: Any) -> list[dict[str, Any]]:
    """Accept the grouped coordinator form or the flat learn_stop actions."""
    if not learned:
        return []
    groups: list[dict[str, Any]] = []
    for item in learned:
        if not isinstance(item, dict):
            continue
        if "frames" in item:
            frames = [
                frame
                for frame in item.get("frames") or []
                if isinstance(frame, dict)
            ]
        else:
            frames = [item]
        observed = item.get("observed") or {}
        usable = [
            {
                **frame,
                "body_hex": strip_frame_envelope(
                    str(frame.get("body_hex") or "")
                ),
            }
            for frame in frames
            if frame.get("command") is not None
        ]
        if usable:
            groups.append({"frames": usable, "observed": observed})
    return groups


#: Command numbers the protocol already documents, used to name a learned
#: control when no property change was observed around its frame.

def _payload_byte(body_hex: str) -> int | None:
    """The last payload byte, which is the value most commands carry."""
    try:
        raw = bytes.fromhex(str(body_hex or ""))
    except ValueError:
        return None
    return raw[-1] if raw else None

FALLBACK_NAME_BY_COMMAND: dict[int, str] = {
    132: "开机/关机",
    134: "设置档位",
    136: "灯光开关",
    144: "设置倒计时",
}


def _observed_to(observed: dict[str, Any], key: str) -> Any:
    """The value a property changed to inside one learned group, or None."""
    change = observed.get(key)
    if not isinstance(change, dict):
        return None
    return change.get("to")


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def suggest_control_name(entry: dict[str, Any]) -> str:
    """A first-guess display name for one learned control.

    The only evidence available without probing the appliance is what the
    property snapshots showed around the frame, so the name is derived from
    that effect: workStatus/level/lightStatus changes describe the common hood
    buttons.  Without an observable effect the documented meaning of the
    command number is used, and finally a plain "动作 N".  The drafter writes
    it as an editable suggestion, never as a fact.
    """
    observed = entry.get("observed")
    if not isinstance(observed, dict):
        observed = {}

    work_status = _as_int(_observed_to(observed, "workStatus"))
    level = _as_int(_observed_to(observed, "level"))
    light = _as_int(_observed_to(observed, "lightStatus"))

    if work_status == 1:
        if level is not None and level > 0:
            return f"开机（档位 {level}）"
        return "开机"
    if work_status == 0:
        return "关机"
    if level is not None and level > 0:
        return f"设置档位 {level}"
    if light == 1:
        return "开灯"
    if light == 0:
        return "关灯"

    # The payload byte is protocol evidence, so it names the control even
    # when no property change was observed in time for the group.
    command = _as_int(entry.get("command"))
    value = _payload_byte(str(entry.get("body_hex") or ""))
    if command == 134 and value is not None and value > 0:
        return f"设置档位 {value}"
    if command == 132 and value is not None:
        return "关机" if value == 0 else "开机"
    if command == 136 and value is not None:
        return "关灯" if value == 0 else "开灯"

    if command is not None and command in FALLBACK_NAME_BY_COMMAND:
        return FALLBACK_NAME_BY_COMMAND[command]
    return f"动作 {int(entry.get('index') or 1)}"


def controls_from_actions(learned: Any) -> list[dict[str, Any]]:
    """Turn learned groups into profile controls entries.

    A frame is identified by (command, body): duplicates collapse inside one
    group (the App retransmits and the capture interleaves read frames) and
    across groups (the same button pressed twice must not become two entries).
    The first occurrence wins, so the entry keeps the effect it produced.
    Groups whose frames are all duplicates are not rendered at all.  The HA
    service and the CLI both call this one generator, so their controls match
    byte for byte.
    """
    entries: list[dict[str, Any]] = []
    index = 0
    emitted: set[tuple[int, str]] = set()
    for group_index, group in enumerate(_normalise_learned(learned)):
        runs: list[list[Any]] = []
        seen: dict[tuple[int, str], int] = {}
        for frame in group["frames"]:
            command = int(frame["command"])
            body_hex = str(frame.get("body_hex") or "").lower()
            key = (command, body_hex)
            if key in seen:
                runs[seen[key]][2] += 1
                continue
            seen[key] = len(runs)
            runs.append([command, body_hex, 1])
        fresh = [run for run in runs if (run[0], run[1]) not in emitted]
        if not fresh:
            continue
        for command, body_hex, _repeats in fresh:
            emitted.add((command, body_hex))
            index += 1
            entries.append(
                {
                    "index": index,
                    "name": f"action_{index}",
                    "command": command,
                    "body_hex": body_hex,
                    "group_index": group_index,
                    "group_frame_count": len(group["frames"]),
                    "group_entry_count": len(fresh),
                    "observed": group.get("observed") or {},
                }
            )
    return entries

def covered_properties(
    properties: dict[str, Any], builtin_keys: Iterable[str] | None
) -> list[str]:
    """The captured property keys the built-in driver already exposes.

    The CLI lists these so a contributor can see why a key did not become a
    sensor: the driver already publishes it as an entity.
    """
    if not builtin_keys:
        return []
    covered = {_canonical_key(key) for key in builtin_keys}
    return sorted(
        str(key) for key in properties if _canonical_key(key) in covered
    )


def _learned_control_body_lines(entries: list[dict[str, Any]]) -> list[str]:
    """Render only the learned control entries (no controls: header)."""
    lines: list[str] = []
    previous_group: int | None = None
    for entry in entries:
        if entry["group_index"] != previous_group:
            lines.append(
                f"  # 本组 {entry['group_frame_count']} 帧，去重后 "
                f"{entry['group_entry_count']} 条"
            )
            previous_group = entry["group_index"]
        effect = ""
        observed = entry.get("observed") or {}
        if observed:
            effect = "；观察到 " + ", ".join(
                f"{key} {observed[key].get('from')} → {observed[key].get('to')}"
                for key in sorted(observed, key=str)
            )
        body = entry["body_hex"] or "(空)"
        lines.append(
            f"  {_yaml_key(entry['name'])}:  # 命令 {entry['command']}，"
            f"body {body}{effect}"
        )
        lines.append(
            "    # 名称是按设备变化推测的，建议改成 App 上显示的名字"
        )
        lines.append(f"    name: {_yaml_string(suggest_control_name(entry))}")
        lines.append("    kind: mqtt")
        lines.append(f"    command: {int(entry['command'])}")
        lines.append(f"    body_hex: {_yaml_string(entry['body_hex'])}")
        lines.append("    verified: false")
    return lines


def _learned_control_lines(entries: list[dict[str, Any]]) -> list[str]:
    """Render the learned controls section with its review comments."""
    return [
        "# learned controls: verified is false; check each one with the",
        "# experimental-controls option before trusting it.",
        "controls:",
        *_learned_control_body_lines(entries),
    ]


#: A control key the drafter generated: action_1, action_2, ...
_ACTION_KEY = re.compile(r"^action_(d+)$")


@dataclass
class MergeResult:
    """One merge: the new profile YAML plus what changed."""

    profile_yaml: str
    added: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    skipped: list[tuple[int, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _control_pairs(controls: dict[str, Any]) -> set[tuple[int, str]]:
    """The (command, body) identities a profile's controls already hold."""
    pairs: set[tuple[int, str]] = set()
    for entry in controls.values():
        if not isinstance(entry, dict):
            continue
        command = _as_int(entry.get("command"))
        if command is None:
            continue
        body = strip_frame_envelope(str(entry.get("body_hex") or "")).lower()
        pairs.add((command, body))
    return pairs


def _controls_block_bounds(lines: list[str]) -> tuple[int, int] | None:
    """(first body line, end) of the top-level controls mapping, or None."""
    start = None
    for index, line in enumerate(lines):
        if line.strip() == "controls:" and not line[:1].isspace():
            start = index + 1
            break
    if start is None:
        return None
    end = len(lines)
    for index in range(start, len(lines)):
        if not lines[index].strip():
            continue
        if not lines[index][:1].isspace():
            end = index
            break
    while end > start and not lines[end - 1].strip():
        end -= 1
    return start, end


def merge_controls(profile_yaml: str, learned: Any) -> MergeResult:
    """Merge learned controls into an existing profile, keeping everything else.

    The profile text is edited in place -- it is never re-rendered -- so the
    header comments, sensors, binary_sensors and any hand edits survive
    untouched.  A frame is identified by (command, body) after the capture
    envelope is stripped, the same identity the drafter uses, so re-capturing
    a button does not duplicate its entry; a new button appends action_N after
    the highest action_N already present.
    """
    result = MergeResult(profile_yaml=profile_yaml)
    try:
        document = yaml.safe_load(profile_yaml)
    except yaml.YAMLError as err:
        result.warnings.append(
            f"profile is not valid YAML ({err}); nothing was merged"
        )
        return result
    if not isinstance(document, dict):
        result.warnings.append("profile is not a mapping; nothing was merged")
        return result
    controls = document.get("controls")
    if not isinstance(controls, dict):
        controls = {}
    result.kept = [str(key) for key in controls]
    known = _control_pairs(controls)

    entries = controls_from_actions(learned)
    if not entries:
        return result

    numbers = [
        int(match.group(1))
        for key in controls
        if (match := _ACTION_KEY.match(str(key))) is not None
    ]
    next_index = max(numbers, default=0) + 1
    taken = {str(key) for key in controls}
    fresh: list[dict[str, Any]] = []
    for entry in entries:
        body = strip_frame_envelope(str(entry.get("body_hex") or "")).lower()
        pair = (int(entry["command"]), body)
        if pair in known:
            result.skipped.append(pair)
            continue
        known.add(pair)
        while f"action_{next_index}" in taken:
            next_index += 1
        entry = dict(entry, index=next_index, name=f"action_{next_index}")
        taken.add(entry["name"])
        result.added.append(entry["name"])
        fresh.append(entry)
        next_index += 1
    if not fresh:
        return result

    body_lines = _learned_control_body_lines(fresh)
    lines = profile_yaml.splitlines()
    bounds = _controls_block_bounds(lines)
    if bounds is None:
        merged = [*lines, "", "controls:", *body_lines]
    else:
        _start, end = bounds
        merged = [*lines[:end], *body_lines, *lines[end:]]
    result.profile_yaml = "\n".join(merged) + "\n"
    return result


def merge_controls_into_profile(profile_yaml: str, learned: Any) -> str:
    """The profile YAML with the learned controls merged in (comments kept)."""
    return merge_controls(profile_yaml, learned).profile_yaml


_HEADER: tuple[str, ...] = (
    "# ROKI profile draft (generated by the profile drafter).",
    "# Read-only: the sensors below only read coordinator properties.",
    "# Controls are only rendered from a learning session and stay hidden",
    "# until the experimental-controls option is enabled.",
)

#: Protocol bookkeeping keys that never describe the appliance state.
PROTOCOL_METADATA_KEYS = frozenset(
    {"lastupdatetime", "responsemessagetype", "msgid", "timestamp"}
)

#: Mirrors mqtt.TERMINAL_TYPE_APP; local so this module stays HA-free.
TERMINAL_TYPE_APP = 0x0B

#: Word level synonyms between the REST (camelCase) and older spellings.
_ALIAS_WORDS = (("working", "work"),)


@dataclass
class DraftResult:
    """One draft: the profile YAML, its fixture and the warnings to show."""

    profile_yaml: str
    fixture: dict[str, Any]
    warnings: list[str] = field(default_factory=list)


# -- YAML rendering helpers ----------------------------------------------


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _looks_numeric(text: str) -> bool:
    try:
        float(text)
    except (TypeError, ValueError):
        return False
    return True


def _yaml_key(key: Any) -> str:
    text = str(key)
    if _PLAIN_KEY.match(text) and text.lower() not in _RESERVED_WORDS:
        return text
    return json.dumps(text, ensure_ascii=False)


def _yaml_string(value: Any) -> str:
    text = str(value)
    if (
        _PLAIN_VALUE.match(text)
        and text.lower() not in _RESERVED_WORDS
        and not _looks_numeric(text)
    ):
        return text
    return json.dumps(text, ensure_ascii=False)


def _infer_type(value: Any) -> str | None:
    """One of int/float/bool/str, or None for values a sensor cannot read."""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    return None


def _canonical_key(key: Any) -> str:
    """Collapse separators/case and known synonyms so aliases group together."""
    text = re.sub(r"[^0-9a-z]", "", str(key).lower())
    for source, target in _ALIAS_WORDS:
        text = text.replace(source, target)
    return text


def _group_keys(properties: dict[str, Any]) -> list[tuple[str, list[str]]]:
    """Group property spellings that describe the same field.

    Returns [(canonical, [primary, *aliases])] sorted by the primary spelling.
    camelCase wins as the primary key, then snake_case/kebab-case, so the
    output order stays stable whatever order the payload used.
    """
    groups: dict[str, list[str]] = {}
    for key in sorted(properties, key=str):
        groups.setdefault(_canonical_key(key), []).append(key)
    grouped: list[tuple[str, list[str]]] = []
    for canonical, keys in groups.items():
        keys.sort(
            key=lambda item: ("_" in str(item) or "-" in str(item), str(item))
        )
        grouped.append((canonical, keys))
    grouped.sort(key=lambda item: str(item[1][0]))
    return grouped


def _first_value(properties: dict[str, Any], keys: list[str]) -> Any:
    """The first non-null value among the alias spellings."""
    for key in keys:
        value = properties.get(key)
        if value is not None:
            return value
    return properties.get(keys[0]) if keys else None


def _split_properties(
    properties: dict[str, Any], warnings: list[str]
) -> tuple[dict[str, Any], list[str]]:
    """Drop protocol metadata keys and report which ones were skipped."""
    kept: dict[str, Any] = {}
    skipped: list[str] = []
    for key, value in properties.items():
        if _canonical_key(key) in PROTOCOL_METADATA_KEYS:
            skipped.append(str(key))
            continue
        kept[key] = value
    skipped.sort()
    if skipped:
        warnings.append("skipped protocol metadata: " + ", ".join(skipped))
    return kept, skipped


def _sensor_lines(
    properties: dict[str, Any],
    warnings: list[str],
    *,
    covered: frozenset[str] = frozenset(),
) -> list[str]:
    """Render one sorted sensor entry per property group, hints as comments."""
    lines: list[str] = []
    for canonical, keys in _group_keys(properties):
        if canonical in covered:
            # Already exposed by the built-in driver this draft extends.
            continue
        primary = keys[0]
        value = _first_value(properties, keys)
        rendered_keys = ", ".join(_yaml_key(key) for key in keys)
        lines.append(f"  {_yaml_key(primary)}:")
        lines.append(f"    keys: [{rendered_keys}]")
        type_name = _infer_type(value)
        if type_name is None:
            warnings.append(
                f"property {primary!r} is a {type(value).__name__}, not a "
                "scalar; no type was suggested"
            )
        else:
            lines.append(f"    type: {type_name}")

        # Semantic hints are comments only -- the drafter never writes a
        # device_class/unit/map it cannot prove.
        if _TEMPERATURE_KEY.search(primary):
            lines.append("    # device_class: temperature")
            lines.append("    # unit: °C")
        if type_name == "int" and 0 <= int(value) <= 9:
            upper = max(int(value), 1)
            pairs = ", ".join(f"{index}: 含义{index}" for index in range(upper + 1))
            lines.append(f"    # map: {{{pairs}}}")
    return lines


def _render_profile(
    device: dict[str, Any],
    properties: dict[str, Any],
    source: str,
    *,
    builtin_driver: str | None = None,
    covered: frozenset[str] = frozenset(),
    covered_property_keys: list[str] | None = None,
    skipped_metadata: list[str] | None = None,
    learned_controls: Any | None = None,
) -> tuple[str, list[str]]:
    warnings: list[str] = []
    dc = _clean(device.get("dc"))
    dt = _clean(device.get("dt"))
    transport = _clean(device.get("transport"))

    lines: list[str] = list(_HEADER)
    lines.append(f"# source: {source}")
    if transport:
        lines.append(f"# transport: {transport}")
    if builtin_driver:
        lines.append(
            f"# extends python:{builtin_driver}: keys that driver already "
            "exposes were not drafted again"
        )
        if covered_property_keys:
            lines.append(
                "# covered and skipped: " + ", ".join(covered_property_keys)
            )
    if skipped_metadata:
        lines.append(
            "# skipped protocol metadata: " + ", ".join(skipped_metadata)
        )
    lines.append(f"schema: {PROFILE_SCHEMA}")
    if builtin_driver:
        lines.append(f"extends: python:{builtin_driver}")
    lines.append("match:")
    lines.append(f"  dc: {_yaml_string(dc)}")
    if dt:
        lines.append(f"  dt: {_yaml_string(dt)}")

    sensors = _sensor_lines(properties, warnings, covered=covered)
    if sensors:
        lines.append("sensors:")
        lines.extend(sensors)
    elif properties:
        lines.append(
            "# sensors: every captured property is already covered by the "
            "built-in driver."
        )
    else:
        lines.append("# sensors: no properties were captured; add keys by hand.")
    lines.append("")
    learned_entries = controls_from_actions(learned_controls)
    if learned_entries:
        lines.extend(_learned_control_lines(learned_entries))
    else:
        lines.extend(CONTROL_TEMPLATE)
    return "\n".join(lines) + "\n", warnings


def _builtin_coverage(driver_name: str) -> frozenset[str]:
    """Keys a built-in Python driver already exposes (best effort).

    The HA service passes the keys explicitly; this fallback only runs when a
    caller knows the driver name and is inside the integration package.
    """
    try:
        from .devices import PYTHON_DRIVERS
    except ImportError:  # standalone CLI: no driver registry available
        return frozenset()
    driver_cls = PYTHON_DRIVERS.get(driver_name)
    if driver_cls is None:
        return frozenset()
    try:
        probe = driver_cls(None, {})
        keys = {spec.key for spec in probe.sensors()}
        keys |= {spec.key for spec in probe.binary_sensors()}
    except Exception:  # noqa: BLE001 - coverage is only an optimisation
        return frozenset()
    return frozenset(_canonical_key(key) for key in keys)


# -- fixture rendering ---------------------------------------------------


def _fixture_properties(properties: dict[str, Any]) -> dict[str, Any]:
    """Drop the protocol bookkeeping: it is capture metadata, not state."""
    return {
        key: value
        for key, value in properties.items()
        if _canonical_key(key) not in PROTOCOL_METADATA_KEYS
    }


def _build_fixture(
    device: dict[str, Any],
    properties: dict[str, Any],
    *,
    source: str,
    expect_fn: ExpectFn | None,
    warnings: list[str],
) -> dict[str, Any]:
    match: dict[str, Any] = {"dc": _clean(device.get("dc"))}
    dt = _clean(device.get("dt"))
    if dt:
        match["dt"] = dt

    expect: dict[str, Any] = {}
    if expect_fn is None:
        warnings.append(
            "expect is empty: no expect_fn was supplied, so the fixture "
            "cannot be checked automatically"
        )
    else:
        try:
            computed = expect_fn(properties)
        except Exception as err:  # noqa: BLE001 - a draft never raises
            warnings.append(f"expect_fn failed ({err}); expect left empty")
        else:
            if isinstance(computed, dict):
                expect = dict(computed)
            else:
                warnings.append(
                    f"expect_fn returned {type(computed).__name__}, not a "
                    "mapping; expect left empty"
                )

    return {
        "schema": FIXTURE_SCHEMA,
        "source": source,
        "match": match,
        "properties": redact_properties(_fixture_properties(properties)),
        "expect": expect,
    }


def _draft(
    device: Any,
    properties: Any,
    *,
    source: str,
    expect_fn: ExpectFn | None,
    builtin_driver: str | None = None,
    builtin_keys: Iterable[str] | None = None,
    learned_controls: Any | None = None,
    builtin_resolver: BuiltinResolver | None = None,
) -> DraftResult:
    warnings: list[str] = []
    if builtin_resolver is not None and isinstance(device, dict):
        # Explicit arguments win; the resolver only fills what the caller did
        # not already know (the CLI passes the resolver, the HA service the
        # explicit values).
        resolved = None
        try:
            resolved = builtin_resolver(device)
        except Exception as err:  # noqa: BLE001 - coverage is an optimisation
            warnings.append(f"built-in driver coverage failed ({err}); ignored")
        if resolved:
            name, keys, resolved_expect = resolved
            if builtin_driver is None:
                builtin_driver = name
            if builtin_keys is None:
                builtin_keys = keys
            if expect_fn is None:
                expect_fn = resolved_expect
    if not isinstance(device, dict):
        return DraftResult(
            "", {}, ["device is not a mapping; nothing was drafted"]
        )
    if not isinstance(properties, dict):
        return DraftResult(
            "", {}, ["properties are not a mapping; nothing was drafted"]
        )
    if not _clean(device.get("dc")):
        return DraftResult(
            "",
            {},
            ["device has no dc; a profile needs the category code to match on"],
        )
    if not properties:
        warnings.append(
            "no properties were captured; the draft only pins the match block"
        )
    sensor_properties, skipped_metadata = _split_properties(properties, warnings)
    covered: set[str] = set()
    if builtin_driver:
        if builtin_keys is not None:
            covered.update(_canonical_key(key) for key in builtin_keys)
        if not covered:
            covered.update(_builtin_coverage(builtin_driver))
    covered_keys = frozenset(covered)
    covered_property_keys = sorted(
        str(key)
        for key in sensor_properties
        if _canonical_key(key) in covered_keys
    )
    profile_yaml, render_warnings = _render_profile(
        device,
        sensor_properties,
        source,
        builtin_driver=builtin_driver,
        covered=covered_keys,
        covered_property_keys=covered_property_keys,
        skipped_metadata=skipped_metadata,
        learned_controls=learned_controls,
    )
    warnings.extend(render_warnings)
    # The fixture keeps every captured state key, including the ones the
    # built-in driver already covers (it is the regression evidence), but
    # drops protocol bookkeeping such as lastUpdateTime.
    fixture = _build_fixture(
        device, properties, source=source, expect_fn=expect_fn, warnings=warnings
    )
    return DraftResult(profile_yaml, fixture, warnings)


# -- public entry points -------------------------------------------------


def draft_from_properties(
    device: dict[str, Any],
    properties: dict[str, Any],
    *,
    expect_fn: ExpectFn | None = None,
    builtin_driver: str | None = None,
    builtin_keys: Iterable[str] | None = None,
    learned_controls: Any | None = None,
) -> DraftResult:
    """Draft a profile + fixture from one device and its cached properties.

    When builtin_driver names an existing Python driver (for example "hood"),
    the YAML inherits it with extends: python:<name> and only the properties
    that driver does not already expose become sensor entries.  builtin_keys
    is the explicit key set of that driver; when omitted, it is resolved from
    the driver registry if that is importable.
    """
    return _draft(
        device,
        properties,
        source="auto",
        expect_fn=expect_fn,
        builtin_driver=builtin_driver,
        builtin_keys=builtin_keys,
        learned_controls=learned_controls,
    )


def draft_from_bundle(
    bundle: dict[str, Any],
    *,
    expect_fn: ExpectFn | None = None,
    learned_controls: Any | None = None,
    builtin_resolver: BuiltinResolver | None = None,
) -> DraftResult:
    """Draft from a roki-bundle/1 capture produced by the CLI collector."""
    if not isinstance(bundle, dict):
        return DraftResult("", {}, ["bundle is not a mapping; nothing was drafted"])
    warnings: list[str] = []
    schema = bundle.get("schema")
    if schema is not None and schema != BUNDLE_SCHEMA:
        warnings.append(
            f"unexpected bundle schema {schema!r} (expected {BUNDLE_SCHEMA})"
        )
    result = _draft(
        bundle.get("device"),
        bundle.get("properties") or {},
        source="bundle",
        expect_fn=expect_fn,
        learned_controls=learned_controls,
        builtin_resolver=builtin_resolver,
    )
    return DraftResult(
        result.profile_yaml, result.fixture, [*warnings, *result.warnings]
    )


def _diagnostics_devices(diagnostics: dict[str, Any]) -> list[dict[str, Any]]:
    devices = diagnostics.get("devices")
    if not isinstance(devices, list) or not devices:
        entry = diagnostics.get("entry")
        if isinstance(entry, dict):
            data = entry.get("data")
            if isinstance(data, dict) and isinstance(data.get("devices"), list):
                devices = data["devices"]
    if not isinstance(devices, list):
        return []
    return [device for device in devices if isinstance(device, dict)]


def _device_label(device: dict[str, Any]) -> str:
    dc = _clean(device.get("dc")) or "?"
    dt = _clean(device.get("dt")) or "?"
    guid = _clean(device.get("guid_masked")) or "?"
    return f"{dc}-{dt} ({guid})"


def _select_device(
    devices: list[dict[str, Any]], *, dc: Any, dt: Any
) -> tuple[dict[str, Any] | None, list[str]]:
    wanted_dc = _clean(dc).upper()
    wanted_dt = _clean(dt).upper()

    def matches(device: dict[str, Any]) -> bool:
        if wanted_dc and _clean(device.get("dc")).upper() != wanted_dc:
            return False
        return not (wanted_dt and _clean(device.get("dt")).upper() != wanted_dt)

    candidates = [device for device in devices if matches(device)]
    if not candidates:
        return None, [
            f"no diagnostics device matches dc={_clean(dc)!r} dt={_clean(dt)!r}"
        ]
    if len(candidates) > 1:
        labels = ", ".join(_device_label(device) for device in candidates)
        if wanted_dc or wanted_dt:
            hint = "still more than one device matches; narrow it down further"
        else:
            hint = "pass dc/dt to choose one of them"
        message = (
            f"diagnostics contain {len(candidates)} matching devices "
            f"({labels}); {hint}"
        )
        return None, [message]
    return candidates[0], []


def _diagnostics_properties(
    diagnostics: dict[str, Any], device: dict[str, Any]
) -> dict[str, Any]:
    coordinator = diagnostics.get("coordinator")
    if not isinstance(coordinator, dict):
        return {}
    data = coordinator.get("data")
    if not isinstance(data, dict):
        return {}
    guid = _clean(device.get("guid_masked"))
    if guid:
        state = data.get(guid)
        if isinstance(state, dict):
            return state
    if len(data) == 1:
        only = next(iter(data.values()))
        if isinstance(only, dict):
            return only
    return {}


def draft_from_diagnostics(
    diagnostics: dict[str, Any],
    *,
    dc: Any = None,
    dt: Any = None,
    expect_fn: ExpectFn | None = None,
    learned_controls: Any | None = None,
    builtin_resolver: BuiltinResolver | None = None,
) -> DraftResult:
    """Draft from a Home Assistant diagnostics export.

    The export masks/redacts credentials already; a device is picked by dc/dt
    (their masked GUIDs cannot be typed by a user) and its cached properties
    are read from coordinator.data under the same masked key.
    """
    if not isinstance(diagnostics, dict):
        return DraftResult(
            "", {}, ["diagnostics is not a mapping; nothing was drafted"]
        )
    devices = _diagnostics_devices(diagnostics)
    if not devices:
        return DraftResult(
            "", {}, ["diagnostics contain no devices; nothing was drafted"]
        )
    selected, warnings = _select_device(devices, dc=dc, dt=dt)
    if selected is None:
        return DraftResult("", {}, warnings)

    properties = _diagnostics_properties(diagnostics, selected)
    if not properties:
        warnings.append(
            "no cached properties for "
            f"{_device_label(selected)} in coordinator.data; the draft only "
            "pins the match block"
        )
    result = _draft(
        {"dc": selected.get("dc"), "dt": selected.get("dt")},
        properties,
        source="auto",
        expect_fn=expect_fn,
        learned_controls=learned_controls,
        builtin_resolver=builtin_resolver,
    )
    return DraftResult(
        result.profile_yaml, result.fixture, [*warnings, *result.warnings]
    )
