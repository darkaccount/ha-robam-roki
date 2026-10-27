"""Device driver registry and factory for ROBAM / ROKI appliances."""

from __future__ import annotations

from typing import Any

from .base import BinarySensorSpec, RokiDeviceDriver, SensorSpec
from .dishwasher import DishwasherDriver
from .generic import GenericDriver
from .hood import HoodDriver
from .stove import StoveDriver
from .thermal import MicrowaveDriver, OvenDriver, SteamOvenDriver, SterilizerDriver
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


def get_driver_for_device(coordinator: Any, device: dict[str, Any]) -> RokiDeviceDriver:
    """Instantiate and return the appropriate driver for a device.

    Defaults to HoodDriver if dc is missing, or GenericDriver if dc is unlisted.
    """
    raw_dc = device.get("dc")
    if not raw_dc:
        return HoodDriver(coordinator, device)

    dc = str(raw_dc).upper().strip()
    driver_cls = DRIVER_REGISTRY.get(dc, GenericDriver)
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
