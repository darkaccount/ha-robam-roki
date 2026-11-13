"""Config flow contract tests, run without a Home Assistant install.

The regression these cover: the device picker's ``default`` was a ``dict``
(``{guid: True}``) while a multi_select field's default has to be a *list of
the selected values*.  Home Assistant serialises a flow schema with
``probatio.to_field_list``, which copies each marker's ``default`` verbatim
into the field description it hands the frontend; it never runs the field's
validator.  A dict therefore reached the multi_select widget, which reads its
selection as a list, matched nothing, and rendered an empty picker -- the user
saw "选择设备" with no devices in it.

Neither Home Assistant nor voluptuous/probatio is installed in the offline
venv, so both are stubbed.  The stub reproduces the two behaviours these tests
depend on: a marker remembering its ``default``, and ``Schema`` exposing the
markers as its keys with the validator as the value.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import types
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

COMPONENT = (
    Path(__file__).resolve().parents[1] / "custom_components" / "robam_roki"
)

# -- minimal voluptuous stand-in -----------------------------------------

vol = types.ModuleType("voluptuous")


class _Invalid(Exception):
    pass


_UNSET = object()


class _Undefined:
    """Sentinel for "this marker has no default"."""


_UNDEFINED = _Undefined()


class multi_select:
    """Copied from homeassistant/helpers/config_validation.py."""

    def __init__(self, options: dict | list) -> None:
        self.options = options

    def __call__(self, selected: list) -> list:
        if not isinstance(selected, list):
            raise _Invalid("Not a list")
        for value in selected:
            if value not in self.options:
                raise _Invalid(f"{value} is not a valid option")
        return selected


class _Marker:
    def __init__(self, schema: Any, default: Any = _UNSET) -> None:
        self.schema = schema
        self.default = default

    def __hash__(self) -> int:
        return hash(self.schema)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Marker) and other.schema == self.schema


class Required(_Marker):
    pass


class Optional(_Marker):
    pass


class Coerce:
    def __init__(self, type_: type) -> None:
        self.type = type_

    def __call__(self, value: Any) -> Any:
        return self.type(value)


class Range:
    def __init__(self, min: Any = None, max: Any = None) -> None:
        self.min = min
        self.max = max

    def __call__(self, value: Any) -> Any:
        return value


class All:
    def __init__(self, *validators: Any) -> None:
        self.validators = validators

    def __call__(self, value: Any) -> Any:
        for validator in self.validators:
            value = validator(value)
        return value


class In:
    def __init__(self, container: Any) -> None:
        self.container = container

    def __call__(self, value: Any) -> Any:
        if value not in self.container:
            raise _Invalid(f"{value} is not in {self.container}")
        return value


class Schema:
    """Stands in for voluptuous.Schema, keeping the markers as keys."""

    def __init__(self, schema: Any) -> None:
        self.schema = schema

    def __call__(self, data: Any) -> Any:
        return data


vol.Required = Required
vol.Optional = Optional
vol.Coerce = Coerce
vol.Range = Range
vol.All = All
vol.In = In
vol.Schema = Schema
vol.Invalid = _Invalid
sys.modules["voluptuous"] = vol


def to_field_list(schema: Schema) -> list[dict[str, Any]]:
    """Mirror ``probatio.to_field_list`` for the shapes we build.

    This is the step that decides what the frontend sees: each marker's
    ``default`` is copied into the field verbatim, and the value validator is
    *not* run.

    ``config_flow`` imports ``cv.multi_select`` from the shared conftest stub,
    so the check is by class name rather than identity with the local copy.
    """
    fields: list[dict[str, Any]] = []
    for key, value in schema.schema.items():
        name = key.schema if isinstance(key, _Marker) else key
        field: dict[str, Any] = {
            "name": name,
            "required": isinstance(key, Required),
            "optional": isinstance(key, Optional),
        }
        if key.default is not _UNSET:
            field["default"] = key.default
        if type(value).__name__ == "multi_select":
            field["type"] = "multi_select"
            field["options"] = value.options
        fields.append(field)
    return fields


def _load_module(name: str, relative: str) -> types.ModuleType:
    """Load a component module under a real package so relative imports work."""
    package = types.ModuleType("roki_cf_pkg_test")
    package.__path__ = [str(COMPONENT)]
    sys.modules["roki_cf_pkg_test"] = package
    path = COMPONENT / relative
    spec = importlib.util.spec_from_file_location(
        f"roki_cf_pkg_test.{name}", path
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"roki_cf_pkg_test.{name}"] = module
    spec.loader.exec_module(module)
    return module


api = _load_module("roki_cf_api_test", "api.py")
const = _load_module("roki_cf_const_test", "const.py")
config_flow = _load_module("roki_cf_test", "config_flow.py")

# The device registry stub is shared with the other test modules; the options
# flow resolves a DeviceSelector value (a registry id) through it.
_DEVICE_REGISTRY = sys.modules["homeassistant.helpers.device_registry"]
_DEVICE_REGISTRY.async_get = lambda hass: hass.device_registry


DEVICE = {
    "guid": "R8229000000000000",
    "name": "油烟机8229",
    "dt": "R8229",
    "deviceSource": 0,
}


def _run(coroutine):
    import asyncio

    return asyncio.run(coroutine)


def _device_schema(devices: list[dict[str, Any]]):
    """Run the flow's device step and return ``(marker, validator)``.

    The validator is the ``cv.multi_select`` instance the schema holds as the
    value for the CONF_DEVICES marker.
    """
    flow = config_flow.RobamRokiConfigFlow()
    flow._devices = devices
    flow._pending = {config_flow.CONF_PHONE: "13800000000"}
    result = _run(flow.async_step_devices())
    schema = result["data_schema"].schema
    marker = next(iter(schema))
    return marker, schema[marker]


def _device_field(devices: list[dict[str, Any]]) -> dict[str, Any]:
    """The field the frontend receives for the device picker."""
    flow = config_flow.RobamRokiConfigFlow()
    flow._devices = devices
    flow._pending = {config_flow.CONF_PHONE: "13800000000"}
    schema = _run(flow.async_step_devices())["data_schema"]
    return to_field_list(schema)[0]


# -- the regression ------------------------------------------------------


def test_the_device_picker_default_is_a_list():
    # A dict default reached the multi_select widget, which reads its
    # selection as a list, matched nothing, and rendered an empty picker.
    marker, _ = _device_schema([dict(DEVICE)])
    assert isinstance(marker.default, list)


def test_the_serialized_field_carries_a_list_default():
    # probatio.to_field_list copies the marker default into the field verbatim
    # and never runs the validator, so this is what the frontend actually got.
    field = _device_field([dict(DEVICE)])
    assert field["type"] == "multi_select"
    assert isinstance(field["default"], list)


def test_the_serialized_default_selects_a_real_option():
    field = _device_field([dict(DEVICE)])
    # Every defaulted value has to be one of the options, or the widget shows
    # nothing selected.
    assert field["default"] == [DEVICE["guid"]]
    assert DEVICE["guid"] in field["options"]


def test_the_serialized_default_survives_the_validator():
    marker, validator = _device_schema([dict(DEVICE)])
    assert validator(marker.default) == marker.default


def test_a_single_device_is_preselected():
    # One hood on the account: defaulting to it means a single click finishes
    # the setup.
    marker, _ = _device_schema([dict(DEVICE)])
    assert marker.default == [DEVICE["guid"]]


def test_several_devices_start_unselected():
    devices = [
        dict(DEVICE),
        {**DEVICE, "guid": "R9700abcdef01234", "name": "油烟机9700"},
    ]
    marker, _ = _device_schema(devices)
    assert marker.default == []


def test_every_device_guid_is_an_option():
    devices = [
        dict(DEVICE),
        {**DEVICE, "guid": "R9700abcdef01234", "name": "油烟机9700"},
    ]
    _, validator = _device_schema(devices)
    assert set(validator.options) == {DEVICE["guid"], "R9700abcdef01234"}


def test_option_labels_show_the_model_prefix():
    _, validator = _device_schema([dict(DEVICE)])
    label = validator.options[DEVICE["guid"]]
    assert "油烟机8229" in label
    assert DEVICE["guid"][:5] in label


def test_submitting_the_default_creates_an_entry():
    flow = config_flow.RobamRokiConfigFlow()
    flow._devices = [dict(DEVICE)]
    flow._pending = {
        config_flow.CONF_PHONE: "13800000000",
        config_flow.CONF_PASSWORD: "secret",
        "title": "测试账号",
    }
    result = _run(
        flow.async_step_devices({config_flow.CONF_DEVICES: [DEVICE["guid"]]})
    )
    assert result["type"] == "create_entry"
    assert result["data"][config_flow.CONF_DEVICES] == [dict(DEVICE)]
    assert result["title"] == "测试账号"


def test_submitting_nothing_reports_an_error():
    flow = config_flow.RobamRokiConfigFlow()
    flow._devices = [dict(DEVICE)]
    flow._pending = {config_flow.CONF_PHONE: "13800000000"}
    result = _run(flow.async_step_devices({config_flow.CONF_DEVICES: []}))
    assert result["type"] == "form"
    assert result["errors"] == {"base": "no_devices_selected"}


# -- options flow: menu, learning mode and draft cleanup ------------------


@pytest.fixture
def tmp_path() -> Iterator[Path]:
    """A temporary directory that can actually be written to.

    The sandbox on this machine denies writes to the user Temp directory, so
    the builtin fixture falls back to the repository root and leaves
    pytest-of-* directories in the working tree.  This override keeps the
    directory inside the workspace and always removes it.
    """
    base = Path(__file__).resolve().parents[1] / ".pytest-profile-tmp"
    path = base / f"{os.getpid()}-{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
        try:
            base.rmdir()
        except OSError:
            pass


DOMAIN = "robam_roki"
HA_DEVICE_ID = "6f1c2f0a9b2c4d5e8f7a"
OPTION_FIELDS = [
    "levels",
    "max_level",
    "scan_interval",
    "enable_sse",
    "countdown_auto_off",
    "allow_unverified_controls",
]


class _FakeConfig:
    def __init__(self, base: Path) -> None:
        self._base = base

    def path(self, *parts: str) -> str:
        return str(self._base.joinpath(*parts))


class _FakeDeviceRegistry:
    def __init__(self) -> None:
        self.entries: dict[str, Any] = {}

    def add(self, device_id: str, identifiers: set[tuple[str, str]]) -> None:
        self.entries[device_id] = types.SimpleNamespace(
            identifiers=set(identifiers)
        )

    def async_get(self, device_id: str) -> Any:
        return self.entries.get(device_id)


class _FakeHass:
    """hass with a config dir and an executor that records blocking work."""

    def __init__(self, base: Path) -> None:
        self.config = _FakeConfig(base)
        self.device_registry = _FakeDeviceRegistry()
        self.executor_calls: list[str] = []

    async def async_add_executor_job(self, target: Any, *args: Any) -> Any:
        self.executor_calls.append(getattr(target, "__name__", repr(target)))
        return target(*args)


class _FakeCoordinator:
    def __init__(self, name: str = DEVICE["name"]) -> None:
        self.name = name
        self.learn_calls: list[tuple[Any, ...]] = []
        self.learning = False
        self.failure: Exception | None = None

    def learn_active(self) -> bool:
        return self.learning

    def device_info_for(self, guid: str) -> dict[str, Any]:
        return {"guid": guid, "name": self.name}

    def learn_start(self, device_id: str, duration: int) -> dict[str, Any]:
        if self.failure is not None:
            raise self.failure
        self.learn_calls.append(("start", device_id, duration))
        self.learning = True
        return {"device_id": device_id}

    async def learn_stop(self, device_id: str | None = None) -> dict[str, Any]:
        if self.failure is not None:
            raise self.failure
        self.learn_calls.append(("stop", device_id))
        self.learning = False
        return {"device_id": device_id, "actions": []}


class _FakeEntry:
    def __init__(
        self, options: dict[str, Any] | None = None, coordinator: Any = None
    ) -> None:
        self.options = dict(options or {})
        self.runtime_data = types.SimpleNamespace(coordinator=coordinator)


def _options_flow(
    base: Path,
    *,
    options: dict[str, Any] | None = None,
    coordinator: Any = None,
) -> tuple[Any, _FakeHass, _FakeCoordinator]:
    hass = _FakeHass(base)
    hass.device_registry.add(HA_DEVICE_ID, {(DOMAIN, DEVICE["guid"])})
    coordinator = coordinator or _FakeCoordinator()
    flow = config_flow.RobamRokiOptionsFlow()
    flow.hass = hass
    flow.config_entry = _FakeEntry(options, coordinator)
    return flow, hass, coordinator


def _field(schema: Any, name: str) -> Any:
    """The marker of one named field in a flow schema."""
    for marker in schema.schema:
        if getattr(marker, "schema", None) == name:
            return marker
    raise AssertionError(f"field {name} not in the schema")


def _field_names(schema: Any) -> list[str]:
    return [getattr(marker, "schema", marker) for marker in schema.schema]


# -- the menu -----------------------------------------------------------


def test_the_options_flow_opens_on_a_menu(tmp_path: Path) -> None:
    flow, _, _ = _options_flow(tmp_path)
    result = _run(flow.async_step_init())
    assert result["type"] == "menu"
    assert result["step_id"] == "init"
    assert result["menu_options"] == ["general", "learn", "cleanup"]


def test_every_menu_entry_has_a_step(tmp_path: Path) -> None:
    flow, _, _ = _options_flow(tmp_path)
    for option in _run(flow.async_step_init())["menu_options"]:
        assert callable(getattr(flow, f"async_step_{option}", None)), option


def test_the_menu_options_are_translated_in_all_three_files() -> None:
    strings = json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))
    english = json.loads(
        (COMPONENT / "translations" / "en.json").read_text(encoding="utf-8")
    )
    chinese = json.loads(
        (COMPONENT / "translations" / "zh-Hans.json").read_text(encoding="utf-8")
    )
    assert strings == chinese
    options = strings["options"]
    assert set(options["step"]) == {"init", "general", "learn", "cleanup"}
    assert set(options["step"]["init"]["menu_options"]) == {
        "general",
        "learn",
        "cleanup",
    }
    assert set(options["abort"]) >= {
        "learn_started",
        "learn_stopped",
        "learn_failed",
        "drafts_removed",
        "drafts_kept",
    }
    assert "device_required" in options["error"]
    # The English file must describe exactly the same steps and fields.
    assert set(options["step"]) == set(english["options"]["step"])
    for step in ("learn", "cleanup"):
        assert set(options["step"][step]["data"]) == set(
            english["options"]["step"][step]["data"]
        )


# -- general settings ---------------------------------------------------


def test_the_general_step_keeps_every_option_field(tmp_path: Path) -> None:
    flow, _, _ = _options_flow(tmp_path, options={"scan_interval": 90})
    result = _run(flow.async_step_general())
    assert result["type"] == "form"
    assert result["step_id"] == "general"
    assert _field_names(result["data_schema"]) == OPTION_FIELDS
    assert _field(result["data_schema"], "scan_interval").default == 90


def test_submitting_the_general_form_normalises_the_levels(tmp_path: Path) -> None:
    flow, _, _ = _options_flow(tmp_path)
    result = _run(
        flow.async_step_general({"levels": "6, 1, 3", "scan_interval": 60})
    )
    assert result["type"] == "create_entry"
    assert result["data"]["levels"] == "1,3,6"
    assert result["data"]["scan_interval"] == 60


def test_an_invalid_level_list_keeps_the_form_open(tmp_path: Path) -> None:
    flow, _, _ = _options_flow(tmp_path)
    result = _run(flow.async_step_general({"levels": "1,x"}))
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_levels"}


def test_a_blank_level_list_falls_back_to_the_model_table(tmp_path: Path) -> None:
    flow, _, _ = _options_flow(tmp_path)
    result = _run(flow.async_step_general({"levels": "  ", "scan_interval": 30}))
    assert result["type"] == "create_entry"
    assert "levels" not in result["data"]


# -- learning mode ------------------------------------------------------


def test_the_learn_step_offers_a_device_selector_and_an_action(
    tmp_path: Path,
) -> None:
    flow, _, _ = _options_flow(tmp_path)
    result = _run(flow.async_step_learn())
    assert result["type"] == "form"
    assert result["step_id"] == "learn"
    assert result["errors"] == {}
    schema = result["data_schema"]
    selector = schema.schema[_field(schema, "device_id")]
    assert type(selector).__name__ == "DeviceSelector"
    assert selector.config["integration"] == DOMAIN
    assert _field(schema, "duration").default == 300
    assert _field(schema, "action").default == "start"
    assert result["description_placeholders"]["status"] == "当前没有学习会话"


def test_starting_a_session_uses_the_roki_guid_behind_the_registry_id(
    tmp_path: Path,
) -> None:
    flow, _, coordinator = _options_flow(tmp_path)
    result = _run(
        flow.async_step_learn(
            {"device_id": HA_DEVICE_ID, "duration": 120, "action": "start"}
        )
    )
    assert result["type"] == "abort"
    assert result["reason"] == "learn_started"
    assert coordinator.learn_calls == [("start", DEVICE["guid"], 120)]
    # Learning is not a setting: the options must be left untouched.
    assert flow.config_entry.options == {}


def test_the_duration_is_clamped_to_the_protocol_window(tmp_path: Path) -> None:
    flow, _, coordinator = _options_flow(tmp_path)
    _run(
        flow.async_step_learn(
            {"device_id": HA_DEVICE_ID, "duration": 5000, "action": "start"}
        )
    )
    assert coordinator.learn_calls == [("start", DEVICE["guid"], 600)]
    assert config_flow._clamp_duration(None) == 300
    assert config_flow._clamp_duration(0) == 1
    assert config_flow._clamp_duration("60") == 60


def test_stopping_a_session_passes_the_device(tmp_path: Path) -> None:
    flow, _, coordinator = _options_flow(tmp_path)
    coordinator.learning = True
    result = _run(
        flow.async_step_learn({"device_id": HA_DEVICE_ID, "action": "stop"})
    )
    assert result["reason"] == "learn_stopped"
    assert coordinator.learn_calls == [("stop", DEVICE["guid"])]


def test_stopping_without_a_device_ends_whatever_is_running(
    tmp_path: Path,
) -> None:
    flow, _, coordinator = _options_flow(tmp_path)
    coordinator.learning = True
    result = _run(flow.async_step_learn({"action": "stop"}))
    assert result["reason"] == "learn_stopped"
    assert coordinator.learn_calls == [("stop", None)]


def test_starting_without_a_device_asks_for_one(tmp_path: Path) -> None:
    flow, _, coordinator = _options_flow(tmp_path)
    result = _run(flow.async_step_learn({"action": "start"}))
    assert result["type"] == "form"
    assert result["errors"] == {"base": "device_required"}
    assert coordinator.learn_calls == []


def test_a_failing_learning_action_aborts(tmp_path: Path) -> None:
    flow, _, coordinator = _options_flow(tmp_path)
    coordinator.failure = config_flow.RokiApiError("learning already running")
    result = _run(
        flow.async_step_learn(
            {"device_id": HA_DEVICE_ID, "action": "start"}
        )
    )
    assert result["type"] == "abort"
    assert result["reason"] == "learn_failed"


def test_the_learn_step_shows_the_running_session(tmp_path: Path) -> None:
    flow, _, coordinator = _options_flow(tmp_path)
    coordinator.learning = True
    coordinator._learn = {
        "guid": DEVICE["guid"],
        "deadline": datetime.now(UTC) + timedelta(seconds=180),
    }
    status = _run(flow.async_step_learn())["description_placeholders"]["status"]
    assert DEVICE["name"] in status
    assert "剩余" in status
    remaining = int(status.rsplit(" ", 2)[-2])
    assert 170 <= remaining <= 180


def test_remaining_seconds_are_exact_for_a_fixed_clock() -> None:
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    state = {"deadline": now + timedelta(seconds=90)}
    assert config_flow._learn_remaining_seconds(state, now) == 90
    assert config_flow._learn_remaining_seconds({}, now) is None
    later = {"deadline": now - timedelta(seconds=5)}
    assert config_flow._learn_remaining_seconds(later, now) == 0


# -- draft cleanup ------------------------------------------------------


def _drafts_dir(base: Path) -> Path:
    directory = base / "robam_roki" / "profiles"
    directory.mkdir(parents=True)
    return directory


def test_the_cleanup_step_lists_only_draft_profiles(tmp_path: Path) -> None:
    directory = _drafts_dir(tmp_path)
    (directory / "draft-RYYJ-R8229.yaml").write_text("a", encoding="utf-8")
    (directory / "draft-RYYJ-R8229-2.yaml").write_text("b", encoding="utf-8")
    (directory / "mine.yaml").write_text("c", encoding="utf-8")
    flow, hass, _ = _options_flow(tmp_path)
    result = _run(flow.async_step_cleanup())
    assert result["step_id"] == "cleanup"
    drafts = result["description_placeholders"]["drafts"]
    assert "draft-RYYJ-R8229.yaml" in drafts
    assert "draft-RYYJ-R8229-2.yaml" in drafts
    assert "mine.yaml" not in drafts
    # The directory scan runs in the executor, never on the event loop.
    assert hass.executor_calls == ["_list_drafts"]


def test_removing_drafts_keeps_hand_written_profiles(tmp_path: Path) -> None:
    directory = _drafts_dir(tmp_path)
    (directory / "draft-RYYJ-R8229.yaml").write_text("a", encoding="utf-8")
    (directory / "mine.yaml").write_text("c", encoding="utf-8")
    flow, hass, _ = _options_flow(tmp_path)
    result = _run(flow.async_step_cleanup({"remove_drafts": True}))
    assert result["type"] == "abort"
    assert result["reason"] == "drafts_removed"
    assert not (directory / "draft-RYYJ-R8229.yaml").exists()
    assert (directory / "mine.yaml").exists()
    assert hass.executor_calls == ["_list_drafts", "_remove_drafts"]


def test_keeping_drafts_deletes_nothing(tmp_path: Path) -> None:
    directory = _drafts_dir(tmp_path)
    draft = directory / "draft-RYYJ-R8229.yaml"
    draft.write_text("a", encoding="utf-8")
    flow, _, _ = _options_flow(tmp_path)
    result = _run(flow.async_step_cleanup({"remove_drafts": False}))
    assert result["reason"] == "drafts_kept"
    assert draft.exists()


def test_the_cleanup_step_copes_without_a_profiles_directory(
    tmp_path: Path,
) -> None:
    flow, _, _ = _options_flow(tmp_path)
    result = _run(flow.async_step_cleanup())
    assert result["description_placeholders"]["drafts"] == "（无）"
    result = _run(flow.async_step_cleanup({"remove_drafts": True}))
    assert result["reason"] == "drafts_removed"
