#!/usr/bin/env python3
"""Promote an exported profile bundle into the repository.

The export (`robam_roki.export_profile`) is one Markdown file with the
profile YAML and the fixture JSON inside two marker blocks.  This tool reads
those two blocks -- no general Markdown parser -- validates them, then writes

    custom_components/robam_roki/profiles/<name>.yaml
    tests/fixtures/devices/<dc>-<dt>.json      (source: user)

and prints the follow-up checks a maintainer runs before committing.

    python tools/promote_profile.py ~/config/robam_roki/exports/RYYJ-R8229-*.md
    python tools/promote_profile.py bundle.md --name my-oven --dry-run

Naming: a profile whose match has exactly one dt becomes `<dc>-<dt>.yaml`,
a category-wide match (no dt, or a dt list) becomes `<dc>.yaml`; `--name`
overrides both.  The fixture file name follows the fixture's own match.

`--repo-root` exists for the tests: it redirects the two output directories
without touching the real checkout.
"""

from __future__ import annotations

import argparse
import difflib
import importlib.util
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
EXPORT_PATH = REPO_ROOT / "custom_components" / "robam_roki" / "export.py"
PROFILES_DIR = Path("custom_components") / "robam_roki" / "profiles"
FIXTURES_DIR = Path("tests") / "fixtures" / "devices"

#: The schemas this tool understands.
PROFILE_SCHEMA = "roki-profile/1"
FIXTURE_SCHEMA = "roki-fixture/1"

#: CommandSpec kinds a control may use, mirrored from devices/base.py.
CONTROL_KINDS = ("property", "invoke", "mqtt")

#: The largest raw frame a control may send (mirrors MAX_MQTT_BODY_BYTES).
MAX_BODY_BYTES = 64

_HEX_BODY = re.compile(r"(?:[0-9a-fA-F]{2})*")
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")

#: PyYAML turns the bare words on/off into boolean keys.  The profile loader
#: normalises them back, so this tool has to as well or it would reject a
#: profile the integration happily accepts.
_YAML_BOOL_KEYS: dict[bool, str] = {True: "on", False: "off"}


class PromotionError(Exception):
    """The export cannot be promoted as it stands."""


@dataclass
class Promotion:
    """Everything one export contributes, already validated and rendered."""

    export_path: Path
    profile_name: str
    profile_text: str
    profile_target: Path
    fixture_text: str
    fixture_target: Path
    dc: str
    dt: str
    control_count: int


# -- reading the export --------------------------------------------------


def _load_export_module() -> Any:
    """Load the component export module from its file (no HA needed)."""
    spec = importlib.util.spec_from_file_location(
        "roki_export_promote", EXPORT_PATH
    )
    if spec is None or spec.loader is None:  # pragma: no cover - broken checkout
        raise PromotionError(f"cannot load {EXPORT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def parse_export(markdown: str) -> tuple[str, dict[str, Any]]:
    """The profile YAML text and the fixture document of one export.

    Only the two documented marker blocks are read; the surrounding Markdown
    (tables, prose) is ignored on purpose.
    """
    export = _load_export_module()
    profile_text = export.find_marked_block(
        markdown, export.PROFILE_MARKER_START, export.PROFILE_MARKER_END
    )
    if not profile_text:
        raise PromotionError(
            "no profile block found: the file must contain "
            f"{export.PROFILE_MARKER_START} ... {export.PROFILE_MARKER_END}"
        )
    fixture_text = export.find_marked_block(
        markdown, export.FIXTURE_MARKER_START, export.FIXTURE_MARKER_END
    )
    if not fixture_text:
        raise PromotionError(
            "no fixture block found: the file must contain "
            f"{export.FIXTURE_MARKER_START} ... {export.FIXTURE_MARKER_END}"
        )
    try:
        fixture = json.loads(fixture_text)
    except ValueError as err:
        raise PromotionError(f"the fixture block is not valid JSON: {err}") from err
    if not isinstance(fixture, dict):
        raise PromotionError("the fixture block is not a JSON object")
    return profile_text, fixture


# -- validation ----------------------------------------------------------


def _match_of(document: dict[str, Any], where: str) -> dict[str, Any]:
    match = document.get("match")
    if not isinstance(match, dict):
        raise PromotionError(f"{where}: a match mapping is required")
    return match


def _dc_of(match: dict[str, Any], where: str) -> str:
    dc = str(match.get("dc") or "").strip()
    if not dc:
        raise PromotionError(f"{where}: match.dc is required")
    return dc


def _dt_list(match: dict[str, Any]) -> list[str]:
    """The dt values of a match block, normalised to a list of strings."""
    raw = match.get("dt")
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    if isinstance(raw, (list, tuple)):
        return [str(item).strip() for item in raw if str(item).strip()]
    return []


def _normalise_entry(entry: dict[Any, Any]) -> dict[str, Any]:
    """Map the YAML boolean keys back to the words the user wrote."""
    normalised: dict[str, Any] = {}
    for key, value in entry.items():
        name = _YAML_BOOL_KEYS.get(key, key) if isinstance(key, bool) else key
        normalised[str(name)] = value
    return normalised


def _validate_control(key: str, entry: Any) -> None:
    where = f"controls.{key}"
    if not isinstance(entry, dict):
        raise PromotionError(f"{where}: entry must be a mapping")
    entry = _normalise_entry(entry)
    kind = entry.get("kind")
    if kind not in CONTROL_KINDS:
        allowed = ", ".join(CONTROL_KINDS)
        raise PromotionError(
            f"{where}.kind: {kind!r} is not one of {allowed}"
        )
    if kind == "mqtt":
        command = entry.get("command")
        if isinstance(command, bool) or not isinstance(command, int):
            raise PromotionError(f"{where}.command: an integer 0-255 is required")
        if not 0 <= command <= 255:
            raise PromotionError(f"{where}.command: an integer 0-255 is required")
        body = entry.get("body_hex")
        if not isinstance(body, str) or not _HEX_BODY.fullmatch(body.strip()):
            raise PromotionError(
                f"{where}.body_hex: an even-length hex string is required"
            )
        if len(body.strip()) // 2 > MAX_BODY_BYTES:
            raise PromotionError(
                f"{where}.body_hex: at most {MAX_BODY_BYTES} bytes"
            )
        return
    if kind == "invoke":
        if entry.get("function") in (None, ""):
            raise PromotionError(f"{where}.function: required for kind=invoke")
        return
    for field in ("on", "off"):
        if not isinstance(entry.get(field), dict) or not entry[field]:
            raise PromotionError(f"{where}.{field}: required for kind=property")


def validate_profile(document: Any) -> None:
    """Refuse a profile the integration itself would reject."""
    if not isinstance(document, dict):
        raise PromotionError("the profile block is not a YAML mapping")
    if document.get("schema") != PROFILE_SCHEMA:
        raise PromotionError(
            f"schema must be {PROFILE_SCHEMA!r}, "
            f"got {document.get('schema')!r}"
        )
    match = _match_of(document, "profile")
    _dc_of(match, "profile")
    controls = document.get("controls") or {}
    if not isinstance(controls, dict):
        raise PromotionError("controls must be a mapping")
    for key, entry in controls.items():
        _validate_control(str(key), entry)


def validate_fixture(document: dict[str, Any], profile: dict[str, Any]) -> None:
    """Refuse a fixture that does not describe the same device as the profile."""
    if document.get("schema") != FIXTURE_SCHEMA:
        raise PromotionError(
            f"fixture schema must be {FIXTURE_SCHEMA!r}, "
            f"got {document.get('schema')!r}"
        )
    fixture_dc = _dc_of(_match_of(document, "fixture"), "fixture")
    profile_dc = _dc_of(_match_of(profile, "profile"), "profile")
    if fixture_dc.upper() != profile_dc.upper():
        raise PromotionError(
            f"the fixture matches dc {fixture_dc} but the profile matches "
            f"{profile_dc}"
        )
    properties = document.get("properties")
    if properties is not None and not isinstance(properties, dict):
        raise PromotionError("the fixture properties must be a mapping")


# -- naming and paths ----------------------------------------------------


def _slug(text: Any) -> str:
    cleaned = _SAFE_NAME.sub("-", str(text or "").strip()).strip("-.")
    return cleaned or "profile"


def profile_name_for(profile: dict[str, Any], override: str | None = None) -> str:
    """<dc>-<dt> for one model, <dc> for a category-wide match."""
    if override:
        return _slug(override)
    match = _match_of(profile, "profile")
    dc = _dc_of(match, "profile")
    dts = _dt_list(match)
    base = f"{dc}-{dts[0]}" if len(dts) == 1 else dc
    # Built-in profiles are lower-case file names; fixtures keep the
    # device spelling (RYYJ-R8229.json), which is what the CI gate expects.
    return base.lower()


def fixture_name_for(fixture: dict[str, Any]) -> str:
    """<dc>-<dt>.json for one model, <dc>.json otherwise."""
    match = _match_of(fixture, "fixture")
    dc = _dc_of(match, "fixture")
    dts = _dt_list(match)
    return f"{dc}-{dts[0]}.json" if len(dts) == 1 else f"{dc}.json"


def plan(
    export_path: Path,
    repo_root: Path = REPO_ROOT,
    name: str | None = None,
) -> Promotion:
    """Read, validate and resolve one export into a Promotion."""
    try:
        markdown = export_path.read_text(encoding="utf-8")
    except OSError as err:
        raise PromotionError(f"cannot read {export_path}: {err}") from err
    profile_text, fixture = parse_export(markdown)
    try:
        profile = yaml.safe_load(profile_text)
    except yaml.YAMLError as err:
        raise PromotionError(f"the profile block is not valid YAML: {err}") from err
    validate_profile(profile)
    validate_fixture(fixture, profile)

    profile_name = profile_name_for(profile, name)
    promoted_fixture = dict(fixture)
    promoted_fixture["source"] = "user"
    match = _match_of(profile, "profile")
    dts = _dt_list(match)
    controls = profile.get("controls") or {}
    return Promotion(
        export_path=export_path,
        profile_name=profile_name,
        profile_text=profile_text.rstrip() + "\n",
        profile_target=repo_root / PROFILES_DIR / f"{profile_name}.yaml",
        fixture_text=json.dumps(
            promoted_fixture, ensure_ascii=False, indent=2
        )
        + "\n",
        fixture_target=repo_root / FIXTURES_DIR / fixture_name_for(promoted_fixture),
        dc=_dc_of(match, "profile"),
        dt=dts[0] if len(dts) == 1 else "",
        control_count=len(controls) if isinstance(controls, dict) else 0,
    )


# -- writing -------------------------------------------------------------


def diff_for(target: Path, text: str) -> str:
    """A unified diff against the file on disk (or an all-new block)."""
    if target.is_file():
        try:
            old = target.read_text(encoding="utf-8")
        except OSError as err:  # pragma: no cover - permissions only
            return f"cannot read {target}: {err}"
        old_lines = old.splitlines()
    else:
        old_lines = []
    diff = difflib.unified_diff(
        old_lines,
        text.splitlines(),
        fromfile=str(target) if old_lines else "/dev/null",
        tofile=f"{target} (promoted)",
        lineterm="",
    )
    return "\n".join(diff) or f"{target}: unchanged"


def render_targets(promotion: Promotion) -> list[tuple[Path, str]]:
    """The (path, content) pairs this promotion writes."""
    return [
        (promotion.profile_target, promotion.profile_text),
        (promotion.fixture_target, promotion.fixture_text),
    ]


def apply(promotion: Promotion, dry_run: bool = False, *, force: bool = False) -> None:
    """Write both files, or only print what would change."""
    targets = render_targets(promotion)
    if dry_run:
        print("（dry-run）将写入：")
        for target, text in targets:
            print(f"  {target}")
            print(diff_for(target, text))
        return
    # Refuse before writing anything: a half-applied promotion is worse than a
    # refused one.
    if not force and promotion.fixture_target.exists():
        try:
            existing = json.loads(
                promotion.fixture_target.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            existing = {}
        if str(existing.get("source", "")).lower() == "user":
            raise PromotionError(
                f"{promotion.fixture_target} already holds a source=user fixture",
                " (real-device evidence); re-run with --force to overwrite it",
            )
    for target, text in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        print(f"写入：{target}")


def follow_up(promotion: Promotion) -> list[str]:
    """The checklist a maintainer runs after promoting."""
    controls = (
        f"{promotion.control_count} 个 controls（verified 见文件）"
        if promotion.control_count
        else "只读遥测（没有 controls）"
    )
    dt = promotion.dt or "—"
    return [
        "",
        "下一步：",
        "  1) python -m pytest tests/ -q --no-header",
        "  2) python tools/check_fixtures.py --base HEAD~1 --head HEAD",
        "  3) 隐私复核：fixture 里没有真实手机号 / App GUID / token",
        "  4) README 的「支持范围」表加一行（品类列按实际填写）：",
        f"     | 待填 | {promotion.dc} | {dt} | {controls} |",
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Promote a robam_roki export .md into the repository.",
    )
    parser.add_argument("export", help="the exported .md file")
    parser.add_argument(
        "--name",
        default=None,
        help="profile file name (defaults to <dc>-<dt> or <dc>)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the target paths and the diff, write nothing",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing source=user fixture (refused by default)",
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        help="repository root to write into (defaults to this checkout)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = Path(args.repo_root) if args.repo_root else REPO_ROOT
    try:
        promotion = plan(Path(args.export), repo_root, args.name)
        apply(promotion, dry_run=args.dry_run, force=args.force)
    except PromotionError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    for line in follow_up(promotion):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
