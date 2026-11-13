"""YAML profile definitions for the ROBAM / ROKI integration.

Profiles describe appliance telemetry without touching the Python drivers: a
user can add a category that has no driver yet, override an existing driver's
entity list, or share a fragment between several profiles.  This module is
deliberately free of Home Assistant imports -- parsing, validation, inheritance,
priority and device matching are pure logic and are unit-tested offline.

A profile file looks like this (schema roki-profile/1)::

    schema: roki-profile/1
    id: my-oven                  # optional, defaults to the file name stem
    extends: shared-base         # optional: fragment id or python:<driver>
    match:                       # required outside _shared/, dc is mandatory
      dc: RZQL
      dt: [C98A, C98B]           # optional, a string or a list of strings
    name: 我的蒸烤箱
    sensors:
      cavity_temp:
        keys: [curTopTemp, topTemp]
        type: int
        scale: 1
        map: {0: 关闭}
        default: 0
        name: 腔体温度
    binary_sensors:
      door:
        keys: [doorState, doorOpen]
        on_values: [1, "open"]
        name: 门
    controls: {}                 # reserved for P2; never activated in P1

Files under a _shared/ subdirectory are fragments: they may omit match and are
merged into the profiles that extend them.  A broken file only ever affects
that file and the profiles that extend it -- every failure is logged as a
warning and the offending file or field is skipped, never raised.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

_LOGGER = logging.getLogger(__name__)

#: The only schema version this module understands.
SCHEMA_ID = "roki-profile/1"

SOURCE_BUILTIN: Literal["builtin"] = "builtin"
SOURCE_USER: Literal["user"] = "user"

#: Subdirectory whose files are fragments instead of device profiles.
SHARED_DIR_NAME = "_shared"

#: Only .yaml files are picked up; .yml is deliberately not accepted.
PROFILE_GLOB = "*.yaml"

#: Allowed keys inside a match block.
MATCH_FIELDS = frozenset({"dc", "dt"})

#: Allowed top-level keys of a profile document.
TOP_LEVEL_FIELDS = frozenset(
    {
        "schema",
        "id",
        "extends",
        "match",
        "name",
        "sensors",
        "binary_sensors",
        "controls",
    }
)

#: Allowed per-entity keys, one set per section.  controls mirrors the
#: CommandSpec fields in devices/base.py so a YAML control can be lifted into a
#: CommandSpec without a second shape to translate.
SENSOR_FIELDS = frozenset(
    {
        "keys",
        "type",
        "scale",
        "map",
        "default",
        "name",
        "icon",
        "unit",
        "device_class",
        "state_class",
        "entity_category",
    }
)
BINARY_SENSOR_FIELDS = frozenset(
    {"keys", "on_values", "name", "icon", "device_class", "entity_category"}
)
CONTROL_FIELDS = frozenset(
    {"kind", "on", "off", "function", "param", "params", "verified"}
)

#: The only entity categories a profile may ask for.
ENTITY_CATEGORIES = frozenset({"diagnostic", "config"})

#: The CommandSpec kinds a control entry may declare.
CONTROL_KINDS = frozenset({"property", "invoke"})

#: Value converters a sensor/control entry may name in its type field.
TYPE_CONVERTERS: dict[str, Callable[[Any], Any]] = {
    "int": int,
    "float": float,
    "str": str,
    "bool": bool,
}


@dataclass
class Profile:
    """One parsed profile document, raw values exactly as written."""

    id: str
    source: str
    path: Path | None = None
    dc: str = ""
    dt: str | list[str] | None = None
    name: str | None = None
    sensors: dict[str, dict[str, Any]] = field(default_factory=dict)
    binary_sensors: dict[str, dict[str, Any]] = field(default_factory=dict)
    controls: dict[str, dict[str, Any]] = field(default_factory=dict)
    extends: str | None = None


@dataclass
class FullProfile(Profile):
    """A profile with its extends chain merged in.

    python_base holds the driver name from an extends: python:<name> entry;
    the driver layer merges that driver's specs under the profile's own
    entries, exactly like a YAML fragment is merged here.
    """

    python_base: str | None = None


# -- small helpers -------------------------------------------------------


def _warn(message: str, *args: Any) -> None:
    _LOGGER.warning(message, *args)


def _is_fragment(profile: Profile) -> bool:
    """Whether a profile was loaded from the _shared/ fragment directory."""
    path = profile.path
    return path is not None and path.parent.name == SHARED_DIR_NAME


def _has_match(profile: Profile) -> bool:
    return bool(str(profile.dc or "").strip())


def _clean_str(value: Any) -> str | None:
    """A non-empty trimmed string, or None."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


#: Conflict warnings already emitted, keyed by the device dc and the sorted
#: profile ids involved.  match_profile runs for every entity of every device,
#: so without this cache one ambiguous profile pair floods the log.
_EMITTED_CONFLICTS: set[tuple[str, tuple[str, ...]]] = set()


def reset_conflict_warnings() -> None:
    """Forget which conflicts were warned about (used by tests)."""
    _EMITTED_CONFLICTS.clear()


#: YAML 1.1 parses the bare words on/off/yes/no as booleans.  Profiles are
#: written by hand, so a control written as "on:" arrives as the True key and
#: "param: on" arrives as the True scalar.  Both are normalised back to the
#: words the user typed instead of forcing quotes on them.
_YAML_BOOL_TEXT: dict[bool, str] = {True: "on", False: "off"}


def _normalize_key(name: Any) -> Any:
    """Map a YAML boolean mapping key back to the word it was written as."""
    if isinstance(name, bool):
        return _YAML_BOOL_TEXT[name]
    return name


def _clean_text(value: Any) -> str | None:
    """A non-empty trimmed string; YAML booleans become "on"/"off"."""
    if isinstance(value, bool):
        return _YAML_BOOL_TEXT[value]
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _apply_text(
    spec: dict[str, Any],
    data: dict[str, Any],
    field: str,
    where: str,
    *,
    yaml_bools: bool = False,
) -> None:
    """Copy an optional non-empty string field into the validated spec.

    yaml_bools is only enabled for free-text values such as param, where a user
    may legitimately write the word on/off and get a YAML boolean back.
    """
    if field not in data:
        return
    value = (
        _clean_text(data[field]) if yaml_bools else _clean_str(data[field])
    )
    if value is None:
        _warn("%s.%s: non-empty string expected, ignored", where, field)
        return
    spec[field] = value


def _apply_entity_category(
    spec: dict[str, Any], data: dict[str, Any], where: str
) -> None:
    if "entity_category" not in data:
        return
    value = _clean_str(data["entity_category"])
    if value not in ENTITY_CATEGORIES:
        _warn(
            "%s.entity_category: only %s are allowed, %r ignored",
            where,
            "/".join(sorted(ENTITY_CATEGORIES)),
            data["entity_category"],
        )
        return
    spec["entity_category"] = value


def _normalize_keys(value: Any, where: str) -> list[str] | None:
    """Accept a key or a list of keys and return a clean list."""
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else None
    if isinstance(value, (list, tuple)):
        keys: list[str] = []
        for item in value:
            text = _clean_str(item)
            if text is None:
                _warn("%s: ignoring non-string key %r", where, item)
                continue
            keys.append(text)
        return keys or None
    _warn("%s: keys must be a string or a list of strings, got %r", where, value)
    return None


def _filter_fields(
    entry: dict[str, Any], allowed: frozenset[str], where: str
) -> dict[str, Any]:
    """Drop unknown fields with a warning."""
    filtered: dict[str, Any] = {}
    for name, value in entry.items():
        if name not in allowed:
            _warn("%s: unknown field %r ignored", where, name)
            continue
        filtered[name] = value
    return filtered


# -- validation ----------------------------------------------------------


def _validate_sensor(key: str, entry: Any, path: Path) -> dict[str, Any] | None:
    where = f"{path}: sensors.{key}"
    if not isinstance(entry, dict):
        _warn("%s: entry must be a mapping, got %r", where, entry)
        return None
    data = _filter_fields(entry, SENSOR_FIELDS, where)
    spec: dict[str, Any] = {}
    if "keys" in data:
        keys = _normalize_keys(data["keys"], f"{where}.keys")
        if keys is None:
            _warn("%s: sensor has no usable keys, skipped", where)
            return None
        spec["keys"] = keys
    if "type" in data:
        type_name = data["type"]
        if isinstance(type_name, str) and type_name in TYPE_CONVERTERS:
            spec["type"] = type_name
        else:
            _warn("%s.type: unknown type %r, ignored", where, type_name)
    if "scale" in data:
        scale = data["scale"]
        if isinstance(scale, (int, float)) and not isinstance(scale, bool):
            spec["scale"] = scale
        else:
            _warn("%s.scale: number expected, got %r, ignored", where, scale)
    if "map" in data:
        mapping = data["map"]
        if isinstance(mapping, dict):
            spec["map"] = dict(mapping)
        else:
            _warn("%s.map: mapping expected, got %r, ignored", where, mapping)
    if "default" in data:
        spec["default"] = data["default"]
    _apply_text(spec, data, "name", where)
    _apply_text(spec, data, "icon", where)
    _apply_text(spec, data, "unit", where)
    _apply_text(spec, data, "device_class", where)
    _apply_text(spec, data, "state_class", where)
    _apply_entity_category(spec, data, where)
    if not spec:
        _warn("%s: sensor has no usable fields, skipped", where)
        return None
    return spec


def _validate_binary_sensor(
    key: str, entry: Any, path: Path
) -> dict[str, Any] | None:
    where = f"{path}: binary_sensors.{key}"
    if not isinstance(entry, dict):
        _warn("%s: entry must be a mapping, got %r", where, entry)
        return None
    data = _filter_fields(entry, BINARY_SENSOR_FIELDS, where)
    spec: dict[str, Any] = {}
    if "keys" in data:
        keys = _normalize_keys(data["keys"], f"{where}.keys")
        if keys is None:
            _warn("%s: binary sensor has no usable keys, skipped", where)
            return None
        spec["keys"] = keys
    if "on_values" in data:
        on_values = data["on_values"]
        if isinstance(on_values, list):
            spec["on_values"] = list(on_values)
        else:
            _warn("%s.on_values: list expected, got %r, ignored", where, on_values)
    _apply_text(spec, data, "name", where)
    _apply_text(spec, data, "icon", where)
    _apply_text(spec, data, "device_class", where)
    _apply_entity_category(spec, data, where)
    if not spec:
        _warn("%s: binary sensor has no usable fields, skipped", where)
        return None
    return spec


def _validate_control(key: str, entry: Any, path: Path) -> dict[str, Any] | None:
    where = f"{path}: controls.{key}"
    if not isinstance(entry, dict):
        _warn("%s: entry must be a mapping, got %r", where, entry)
        return None

    # A bare "on:"/"off:" key arrives as True/False from PyYAML; normalise the
    # keys first so users do not have to quote the words.
    data: dict[str, Any] = {}
    for raw_name, value in entry.items():
        name = _normalize_key(raw_name)
        if name not in CONTROL_FIELDS:
            _warn("%s: unknown field %r ignored", where, raw_name)
            continue
        data[name] = value

    kind = _clean_text(data.get("kind"))
    if kind not in CONTROL_KINDS:
        _warn(
            "%s.kind: %r is not one of %s, control skipped",
            where,
            data.get("kind"),
            "/".join(sorted(CONTROL_KINDS)),
        )
        return None
    spec: dict[str, Any] = {"kind": kind}

    for mapping_field in ("on", "off", "params"):
        if mapping_field not in data:
            continue
        mapping = data[mapping_field]
        if not isinstance(mapping, dict):
            _warn(
                "%s.%s: mapping expected, got %r, ignored",
                where,
                mapping_field,
                mapping,
            )
            continue
        spec[mapping_field] = {
            _normalize_key(name): value for name, value in mapping.items()
        }

    if "function" in data:
        function = data["function"]
        if isinstance(function, bool):
            _warn("%s.function: string or integer expected, ignored", where)
        elif isinstance(function, int):
            spec["function"] = str(function)
        elif isinstance(function, str) and function.strip():
            spec["function"] = function.strip()
        else:
            _warn("%s.function: string or integer expected, ignored", where)

    _apply_text(spec, data, "param", where, yaml_bools=True)

    if "verified" in data:
        verified = data["verified"]
        if isinstance(verified, bool):
            spec["verified"] = verified
        else:
            _warn("%s.verified: boolean expected, got %r, ignored", where, verified)
    return spec


def _validate_section(
    document: dict[str, Any],
    section: str,
    validator: Callable[[str, Any, Path], dict[str, Any] | None],
    path: Path,
) -> dict[str, dict[str, Any]] | None:
    """Validate one entity section; None means the section itself is broken."""
    raw = document.get(section)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        _warn("%s: %s must be a mapping, ignored", path, section)
        return None
    entries: dict[str, dict[str, Any]] = {}
    for raw_key, entry in raw.items():
        key = _normalize_key(raw_key)
        if not isinstance(key, str) or not key.strip():
            _warn(
                "%s: %s key %r is not a usable name, skipped",
                path,
                section,
                raw_key,
            )
            continue
        spec = validator(key, entry, path)
        if spec is not None:
            entries[key] = spec
    return entries


def _parse_match(
    document: dict[str, Any], path: Path
) -> tuple[str, str | list[str] | None] | None:
    """Return (dc, dt) for the match block; None means the profile is broken."""
    raw = document.get("match")
    if raw is None:
        return "", None
    if not isinstance(raw, dict):
        _warn("%s: match must be a mapping, ignored", path)
        return "", None

    for name in raw:
        if name not in MATCH_FIELDS:
            _warn("%s: match: unknown field %r ignored", path, name)

    dc = _clean_str(raw.get("dc"))
    if dc is None:
        _warn("%s: match.dc is required for a device profile, skipped", path)
        return None

    dt: str | list[str] | None = None
    if "dt" in raw:
        raw_dt = raw["dt"]
        if isinstance(raw_dt, str):
            dt = _clean_str(raw_dt)
        elif isinstance(raw_dt, (list, tuple)):
            items: list[str] = []
            for item in raw_dt:
                text = _clean_str(item)
                if text is None:
                    _warn("%s: match.dt: ignoring non-string model %r", path, item)
                    continue
                items.append(text)
            dt = items or None
        else:
            _warn(
                "%s: match.dt must be a string or a list of strings, ignored",
                path,
            )
    return dc, dt


def _parse_profile(path: Path, document: Any, source: str) -> Profile | None:
    """Validate an already YAML-decoded document into a Profile."""
    if document is None:
        _LOGGER.debug("%s: empty profile file, skipped", path)
        return None
    if not isinstance(document, dict):
        _warn("%s: profile must be a mapping, skipped", path)
        return None

    schema = document.get("schema")
    if schema != SCHEMA_ID:
        _warn(
            "%s: unsupported schema %r (expected %r), skipped",
            path,
            schema,
            SCHEMA_ID,
        )
        return None

    for name in document:
        if name not in TOP_LEVEL_FIELDS:
            _warn("%s: unknown field %r ignored", path, name)

    profile_id = _clean_str(document.get("id")) or path.stem

    extends: str | None = None
    if "extends" in document:
        extends = _clean_str(document["extends"])
        if extends is None:
            _warn("%s: extends must be a non-empty string, ignored", path)

    name: str | None = None
    if "name" in document:
        name = _clean_str(document["name"])
        if name is None:
            _warn("%s: name must be a non-empty string, ignored", path)

    match = _parse_match(document, path)
    if match is None:
        return None
    dc, dt = match

    sensors = _validate_section(document, "sensors", _validate_sensor, path)
    binary_sensors = _validate_section(
        document, "binary_sensors", _validate_binary_sensor, path
    )
    controls = _validate_section(document, "controls", _validate_control, path)

    return Profile(
        id=profile_id,
        source=source,
        path=path,
        dc=dc,
        dt=dt,
        name=name,
        sensors=sensors or {},
        binary_sensors=binary_sensors or {},
        controls=controls or {},
        extends=extends,
    )


# -- loading -------------------------------------------------------------


def load_profile_file(path: Path, source: str = SOURCE_USER) -> Profile | None:
    """Load and validate one profile file.

    Never raises: an unreadable or malformed file is logged as a warning and
    yields None, so one bad YAML file cannot break the setup of the others.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as err:
        _warn("%s: cannot read profile (%s), skipped", path, err)
        return None
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as err:
        _warn("%s: invalid YAML (%s), skipped", path, err)
        return None
    try:
        return _parse_profile(path, document, source)
    except Exception as err:  # noqa: BLE001 - a profile must never break setup
        _warn("%s: unexpected error while loading (%s), skipped", path, err)
        return None


def load_profile_dir(directory: Path, source: str) -> list[Profile]:
    """Load every profile in a directory.

    Files under _shared/ are fragments and may omit match; every other file
    must be a device profile (dc present) or it is skipped with a warning.
    """
    directory = Path(directory)
    if not directory.is_dir():
        _LOGGER.debug("%s: profile directory does not exist", directory)
        return []

    profiles: list[Profile] = []
    try:
        shared = directory / SHARED_DIR_NAME
        shared_paths = sorted(shared.glob(PROFILE_GLOB)) if shared.is_dir() else []
        device_paths = [
            path for path in sorted(directory.glob(PROFILE_GLOB)) if path.is_file()
        ]
    except OSError as err:
        _warn("%s: cannot scan profile directory (%s)", directory, err)
        return []

    for path in [*shared_paths, *device_paths]:
        profile = load_profile_file(path, source)
        if profile is None:
            continue
        is_shared = path.parent.name == SHARED_DIR_NAME
        if not is_shared and not _has_match(profile):
            _warn(
                "%s: device profile needs match.dc, skipped "
                "(fragments belong in %s/)",
                path,
                SHARED_DIR_NAME,
            )
            continue
        profiles.append(profile)
    return profiles


# -- extends resolution --------------------------------------------------


def _merge_sections(
    target: dict[str, dict[str, Any]], source: dict[str, dict[str, Any]]
) -> None:
    """Shallow section merge: a same-named entry is replaced whole, not merged."""
    for key, entry in source.items():
        target[key] = dict(entry)


def _resolve_one(profile: Profile, by_id: dict[str, Profile]) -> FullProfile | None:
    """Resolve the extends chain of one device profile."""
    chain: list[Profile] = [profile]
    seen = {profile.id}
    python_base: str | None = None
    current = profile
    while current.extends:
        target = current.extends
        if target.startswith("python:"):
            python_base = _clean_str(target[len("python:") :])
            if python_base is None:
                _warn(
                    "%s: extends %r has no driver name, ignored",
                    profile.path,
                    target,
                )
            break
        parent = by_id.get(target)
        if parent is None:
            _warn(
                "%s: extends %r not found, profile skipped",
                profile.path,
                target,
            )
            return None
        if parent.id in seen:
            _warn(
                "%s: extends chain loops back to %r, profile skipped",
                profile.path,
                parent.id,
            )
            return None
        seen.add(parent.id)
        chain.append(parent)
        current = parent

    sensors: dict[str, dict[str, Any]] = {}
    binary_sensors: dict[str, dict[str, Any]] = {}
    controls: dict[str, dict[str, Any]] = {}
    name = profile.name
    for item in reversed(chain):
        _merge_sections(sensors, item.sensors)
        _merge_sections(binary_sensors, item.binary_sensors)
        _merge_sections(controls, item.controls)
        if name is None:
            name = item.name

    return FullProfile(
        id=profile.id,
        source=profile.source,
        path=profile.path,
        dc=profile.dc,
        dt=profile.dt,
        name=name,
        sensors=sensors,
        binary_sensors=binary_sensors,
        controls=controls,
        extends=profile.extends,
        python_base=python_base,
    )


def resolve_extends(profiles: list[Profile]) -> list[FullProfile]:
    """Merge every profile's extends chain and return the device profiles.

    extends: <fragment id> merges a fragment's sections underneath the
    profile's own entries; extends: python:<name> records the Python driver the
    driver layer should use as a base.  Fragments themselves are not returned,
    and a profile whose chain is broken or circular is skipped with a warning.
    """
    by_id: dict[str, Profile] = {}
    for profile in profiles:
        existing = by_id.get(profile.id)
        if existing is None:
            by_id[profile.id] = profile
            continue
        if existing.source == SOURCE_BUILTIN and profile.source == SOURCE_USER:
            # A user fragment deliberately shadows the builtin one.
            by_id[profile.id] = profile
            continue
        _warn(
            "%s: duplicate profile id %r (already defined by %s), ignored",
            profile.path,
            profile.id,
            existing.path,
        )

    resolved: list[FullProfile] = []
    for profile in profiles:
        if _is_fragment(profile) or not _has_match(profile):
            continue
        if by_id.get(profile.id) is not profile:
            # A duplicate id: the winning profile is resolved instead, so the
            # same device is not matched twice.
            continue
        full = _resolve_one(profile, by_id)
        if full is not None:
            resolved.append(full)
    return resolved


# -- device matching -----------------------------------------------------


def _dt_precision(profile: Profile, device_dt: str) -> int:
    """How specifically a profile's dt matches: 2 exact, 1 list, 0 dc-only."""
    dt = profile.dt
    if dt is None:
        return 0
    if not device_dt:
        return -1
    if isinstance(dt, str):
        return 2 if dt.strip().upper() == device_dt else -1
    if isinstance(dt, (list, tuple)):
        models = {str(item).strip().upper() for item in dt if str(item).strip()}
        return 1 if device_dt in models else -1
    return -1


def match_profile(device: dict[str, Any], profiles: list[Profile]) -> Profile | None:
    """The best profile for a device, or None.

    Priority: user profiles beat builtin ones; within a source, an exact dt
    beats a dt list, which beats a dc-only profile.  Profiles tied on both win
    by file name (the first one alphabetically) and the conflict is logged.
    """
    device_dc = str(device.get("dc") or "").strip().upper()
    device_dt = str(device.get("dt") or "").strip().upper()
    if not device_dc:
        return None

    candidates: list[tuple[Profile, int]] = []
    for profile in profiles:
        if not isinstance(profile, Profile):
            continue
        if not str(profile.dc or "").strip():
            continue
        if str(profile.dc).strip().upper() != device_dc:
            continue
        precision = _dt_precision(profile, device_dt)
        if precision < 0:
            continue
        candidates.append((profile, precision))
    if not candidates:
        return None

    def sort_key(item: tuple[Profile, int]) -> tuple[int, int, str]:
        profile, precision = item
        source_rank = 0 if profile.source == SOURCE_USER else 1
        return (source_rank, -precision, str(profile.path or profile.id))

    candidates.sort(key=sort_key)
    best, best_precision = candidates[0]
    tied = [
        profile
        for profile, precision in candidates[1:]
        if precision == best_precision and profile.source == best.source
    ]
    if tied:
        conflict_key = (
            device_dc,
            tuple(sorted({best.id, *(profile.id for profile in tied)})),
        )
        if conflict_key not in _EMITTED_CONFLICTS:
            _EMITTED_CONFLICTS.add(conflict_key)
            _warn(
                "profile conflict: %s and %s match device %s equally well, "
                "using %s",
                best.id,
                ", ".join(profile.id for profile in tied),
                device_dc,
                best.id,
            )
    return best
