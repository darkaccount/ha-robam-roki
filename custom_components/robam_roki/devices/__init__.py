"""Device driver registry and factory for ROBAM / ROKI appliances."""

from __future__ import annotations

from typing import Any

from .base import BinarySensorSpec, RokiDeviceDriver, SensorSpec
from .dishwasher import DishwasherDriver
from .generic import GenericDriver
from .hood import HoodDriver
from .stove import StoveDriver
from .thermal import (
    MicrowaveDriver,
    OvenDriver,
    SteamOvenDriver,
    SterilizerDriver,
    ThermalCookerDriver,
)
from .water_purifier import WaterPurifierDriver

# Registry mapping device category code (dc) to Driver classes
DRIVER_REGISTRY: dict[str, type[RokiDeviceDriver]] = {
    "RYYJ": HoodDriver,
    "RRQZ": StoveDriver,
    "RXWJ": DishwasherDriver,
    "RJSQ": WaterPurifierDriver,
    "RZQL": SteamOvenDriver,
    "RZKY": SteamOvenDriver,
    "RDKX": OvenDriver,
    "RWBL": MicrowaveDriver,
    "RXDG": SterilizerDriver,
}


# Property keys a cooking appliance reports but a hood never does.  Used when
# the cloud omits ``dc`` (or sends a code we do not know) so the appliance still
# gets temperature telemetry instead of an empty generic device.
THERMAL_HINT_KEYS = (
    "workState",
    "setTemp",
    "targetTemp",
    "curTopTemp",
    "curButtomTemp",
    "remainTime",
    "doorOpen",
    "OvenTemp",
    "setOvenTemp",
    "steameOvenTemp",
)


def _cached_properties(coordinator: Any, device: dict[str, Any]) -> dict[str, Any]:
    """Properties already fetched by the coordinator, or an empty dict."""
    try:
        cached = (coordinator.data or {}).get(str(device.get("guid")), {})
    except (AttributeError, TypeError):
        return {}
    return cached if isinstance(cached, dict) else {}


def get_driver_for_device(coordinator: Any, device: dict[str, Any]) -> RokiDeviceDriver:
    """Instantiate and return the appropriate driver for a device.

    Defaults to HoodDriver if dc is missing, or GenericDriver if dc is unlisted.
    Before giving up on an unknown/missing ``dc``, the cached properties are
    inspected: a hood reports ``level``, a cooking appliance reports
    ``workState``/``setTemp``/``curTopTemp``.
    """
    raw_dc = device.get("dc")
    dc = str(raw_dc).upper().strip() if raw_dc else ""
    driver_cls = DRIVER_REGISTRY.get(dc)
    if driver_cls is None:
        properties = _cached_properties(coordinator, device)
        if "level" in properties:
            driver_cls = HoodDriver
        elif any(key in properties for key in THERMAL_HINT_KEYS):
            driver_cls = ThermalCookerDriver
        else:
            driver_cls = GenericDriver if dc else HoodDriver
    return driver_cls(coordinator, device)


__all__ = [
    "BinarySensorSpec",
    "DishwasherDriver",
    "GenericDriver",
    "HoodDriver",
    "MicrowaveDriver",
    "OvenDriver",
    "RokiDeviceDriver",
    "SensorSpec",
    "SteamOvenDriver",
    "SterilizerDriver",
    "StoveDriver",
    "WaterPurifierDriver",
    "get_driver_for_device",
]
