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
import sys
import types
from pathlib import Path
from typing import Any

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

