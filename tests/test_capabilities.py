"""Capability matrix for the ROBAM / ROKI driver layer.

The capability contract replaces the old per-platform category filter: a
platform asks the driver what a device can do and only generates entities for
the matching capabilities.  These tests pin that matrix and the protocol facts
the command table encodes, so a driver that gains a capability by accident --
or loses one during a refactor -- fails loudly here.
"""

from __future__ import annotations

from typing import Any, get_args

import pytest

from custom_components.robam_roki.devices import (
    DishwasherDriver,
    GenericDriver,
    HoodDriver,
    MicrowaveDriver,
    OvenDriver,
    SteamOvenDriver,
    SterilizerDriver,
    StoveDriver,
    ThermalCookerDriver,
    WaterPurifierDriver,
    get_driver_for_device,
)
from custom_components.robam_roki.devices.base import (
    Capability,
    CommandSpec,
    RokiDeviceDriver,
)

# Every capability one hood exposes: power switch, gear ladder, lamp and the
# countdown.  Nothing else in the registry is controllable.
HOOD_CAPABILITIES = frozenset({"power", "level", "light", "timing"})


class _FakeCoordinator:
    """Just enough coordinator for the drivers under test.

    is_rest_device mirrors the real coordinator: REST/SSE devices are the
    ones whose deviceSource is 1, everything else talks MQTT.
    """

    def __init__(self, devices: list[dict[str, Any]] | None = None) -> None:
        self.devices = devices or []
        self.data: dict[str, dict[str, Any]] = {}

    def is_rest_device(self, guid: str) -> bool:
        for device in self.devices:
            if str(device.get("guid")) == str(guid):
                return str(device.get("deviceSource")) == "1"
        return False


def _device(
    guid: str,
    dc: str,
    *,
    dt: str | None = "R8229",
    source: int = 0,
) -> dict[str, Any]:
    device: dict[str, Any] = {
        "guid": guid,
        "name": "device",
        "dc": dc,
        "deviceSource": source,
    }
    if dt is not None:
        device["dt"] = dt
    return device


def _build(
    driver_cls: type[RokiDeviceDriver],
    *,
    guid: str,
    dc: str,
    source: int = 0,
    dt: str | None = "R8229",
    cached: dict[str, Any] | None = None,
) -> tuple[RokiDeviceDriver, _FakeCoordinator]:
    device = _device(guid, dc, dt=dt, source=source)
    coordinator = _FakeCoordinator([device])
    if cached is not None:
        coordinator.data[guid] = cached
    return driver_cls(coordinator, device), coordinator


def _hood(source: int = 0) -> RokiDeviceDriver:
    driver, _ = _build(
        HoodDriver, guid="RYYJ000000000001", dc="RYYJ", source=source
    )
    return driver


# -- base contract -------------------------------------------------------


def test_capability_alias_lists_the_supported_capabilities():
    assert set(get_args(Capability)) == HOOD_CAPABILITIES


def test_base_driver_has_no_commands_and_no_capabilities():
    driver, _ = _build(RokiDeviceDriver, guid="BASE000000000001", dc="")
    assert driver.commands() == {}
    assert driver.capabilities == frozenset()
    assert isinstance(driver.capabilities, frozenset)


def test_command_spec_defaults_and_immutability():
    spec = CommandSpec(kind="property")
    assert spec.on is None
    assert spec.off is None
    assert spec.function is None
    assert spec.param is None
    assert spec.params is None
    assert spec.verified is True
    with pytest.raises(AttributeError):
        spec.verified = False  # frozen dataclass


class _PowerOnlyDriver(RokiDeviceDriver):
    def commands(self) -> dict[str, CommandSpec]:
        return {"power": CommandSpec(kind="property", on={"working_status": 1})}


def test_capabilities_are_derived_from_commands():
    driver, _ = _build(_PowerOnlyDriver, guid="POWER00000000001", dc="")
    assert driver.commands() == {
        "power": CommandSpec(kind="property", on={"working_status": 1})
    }
    assert driver.capabilities == frozenset({"power"})


# -- hood ----------------------------------------------------------------


def test_mqtt_hood_exposes_all_four_capabilities():
    assert _hood(source=0).capabilities == HOOD_CAPABILITIES


def test_rest_hood_still_declares_timing_until_p3():
    # TODO(P3) in hood.py: the REST/IoT countdown is not implemented and the
    # coordinator raises for it, so a REST hood should advertise no timing.
    # P0 keeps the capability so the existing number entity is not dropped
    # (zero behaviour change) and marks the command unverified instead.
    driver = _hood(source=1)
    assert driver.capabilities == HOOD_CAPABILITIES
    assert driver.commands()["timing"].verified is False
    assert driver.commands()["power"].verified is True


def test_hood_command_specs_describe_the_protocol():
    commands = _hood().commands()

    power = commands["power"]
    assert power.kind == "property"
    assert power.on == {"working_status": 1}
    assert power.off == {"working_status": 0}

    light = commands["light"]
    assert light.kind == "property"
    assert light.on == {"light_status": 1}
    assert light.off == {"light_status": 0}

    level = commands["level"]
    assert level.kind == "invoke"
    assert level.function == "134"
    assert level.param == "level"

    timing = commands["timing"]
    assert timing.kind == "invoke"
    assert timing.function == "144"
    assert timing.param == "minutes"
    assert timing.verified is True


# -- read-only drivers ---------------------------------------------------


@pytest.mark.parametrize(
    ("driver_cls", "dc", "guid"),
    [
        (StoveDriver, "RRQZ", "RRQZ000000000001"),
        (DishwasherDriver, "RXWJ", "RXWJ000000000001"),
        (WaterPurifierDriver, "RJSQ", "RJSQ000000000001"),
        (SteamOvenDriver, "RZQL", "RZQL000000000001"),
        (SteamOvenDriver, "RZKY", "RZKY000000000001"),
        (OvenDriver, "RDKX", "RDKX000000000001"),
        (MicrowaveDriver, "RWBL", "RWBL000000000001"),
        (SterilizerDriver, "RXDG", "RXDG000000000001"),
        (ThermalCookerDriver, "RZKX", "RZKX000000000001"),
        (GenericDriver, "SPECIAL_ROBOT", "GENERIC0000000001"),
    ],
)
def test_read_only_drivers_have_no_capabilities(
    driver_cls: type[RokiDeviceDriver], dc: str, guid: str
) -> None:
    driver, _ = _build(driver_cls, guid=guid, dc=dc, dt=None)
    assert driver.commands() == {}
    assert driver.capabilities == frozenset()


# -- factory -------------------------------------------------------------


def test_factory_hood_resolves_all_four_capabilities():
    device = _device("RYYJ000000000002", "RYYJ", dt="82-i1")
    coordinator = _FakeCoordinator([device])
    driver = get_driver_for_device(coordinator, device)
    assert isinstance(driver, HoodDriver)
    assert driver.capabilities == HOOD_CAPABILITIES


def test_factory_fallback_for_missing_dc_is_still_a_hood():
    # Behaviour preserved: a device without dc used to pass the platforms'
    # dc in ('', 'RYYJ') filter, so the factory still yields a hood driver
    # and the device keeps its four hood entities.
    device = {"guid": "UNKNOWN0000000001", "name": "unknown"}
    coordinator = _FakeCoordinator([device])
    driver = get_driver_for_device(coordinator, device)
    assert isinstance(driver, HoodDriver)
    assert driver.capabilities == HOOD_CAPABILITIES


def test_factory_thermal_payload_has_no_capabilities():
    # A cooking appliance is resolved from its properties, not its dc, and is
    # telemetry-only: no capability at all.
    device = {"guid": "UNKNOWN0000000002", "dc": "RZKX"}
    coordinator = _FakeCoordinator([device])
    coordinator.data[device["guid"]] = {"workState": 3, "curTopTemp": 120}
    driver = get_driver_for_device(coordinator, device)
    assert isinstance(driver, ThermalCookerDriver)
    assert driver.capabilities == frozenset()
