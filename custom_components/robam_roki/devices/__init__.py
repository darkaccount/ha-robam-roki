"""Device driver registry and factory for ROBAM / ROKI appliances."""

from __future__ import annotations

import logging
from typing import Any

from ..profiles import Profile, match_profile
from .base import BinarySensorSpec, RokiDeviceDriver, SensorSpec
from .dishwasher import DishwasherDriver
from .generic import GenericDriver
from .hood import HoodDriver
from .profile import ProfileDriver
from .stove import StoveDriver
from .thermal import (
    MicrowaveDriver,
    OvenDriver,
    SteamOvenDriver,
    SterilizerDriver,
    ThermalCookerDriver,
)
from .water_purifier import WaterPurifierDriver

_LOGGER = logging.getLogger(__name__)

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


# Driver names a profile may use in "extends: python:<name>".  A profile
# inherits the named Python driver's entity specs the same way it inherits a
# YAML fragment, which is how a user can tweak a built-in driver for one model.
PYTHON_DRIVERS: dict[str, type[RokiDeviceDriver]] = {
    "hood": HoodDriver,
    "thermal": ThermalCookerDriver,
    "steam_oven": SteamOvenDriver,
    "oven": OvenDriver,
    "microwave": MicrowaveDriver,
    "sterilizer": SterilizerDriver,
    "stove": StoveDriver,
    "dishwasher": DishwasherDriver,
    "water_purifier": WaterPurifierDriver,
    "generic": GenericDriver,
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


def _match_device_profile(
    coordinator: Any, device: dict[str, Any]
) -> Profile | None:
    """The YAML profile describing this device, if any."""
    profiles = getattr(coordinator, "profiles", None)
    if not profiles:
        return None
    try:
        return match_profile(device, profiles)
    except Exception as err:  # noqa: BLE001 - a bad profile must not break setup
        _LOGGER.warning(
            "profile matching failed for device %s: %s",
            device.get("guid"),
            err,
        )
        return None


def _profile_driver(
    coordinator: Any, device: dict[str, Any], profile: Profile
) -> ProfileDriver:
    """Build the profile driver, inheriting a Python driver when asked."""
    base: RokiDeviceDriver | None = None
    python_base = getattr(profile, "python_base", None)
    if python_base:
        driver_cls = PYTHON_DRIVERS.get(python_base)
        if driver_cls is None:
            _LOGGER.warning(
                "profile %s: unknown python driver %r, profile-only driver used",
                profile.id,
                python_base,
            )
        else:
            base = driver_cls(coordinator, device)
    return ProfileDriver(coordinator, device, profile, base)


def get_driver_for_device(coordinator: Any, device: dict[str, Any]) -> RokiDeviceDriver:
    """Instantiate and return the appropriate driver for a device.

    A matching YAML profile wins over the built-in resolution: it is layered on
    top of a Python driver when the profile extends python:<name>, or replaces
    the built-in driver when it does not (a new, read-only category).  The
    profile never adds controls in P1.

    Otherwise the old rules apply: defaults to HoodDriver if dc is missing, or
    GenericDriver if dc is unlisted.
    Before giving up on an unknown/missing ``dc``, the cached properties are
    inspected: a hood reports ``level``, a cooking appliance reports
    ``workState``/``setTemp``/``curTopTemp``.
    """
    profile = _match_device_profile(coordinator, device)
    if profile is not None:
        return _profile_driver(coordinator, device, profile)

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
    "PYTHON_DRIVERS",
    "BinarySensorSpec",
    "DishwasherDriver",
    "GenericDriver",
    "HoodDriver",
    "MicrowaveDriver",
    "OvenDriver",
    "ProfileDriver",
    "RokiDeviceDriver",
    "SensorSpec",
    "SteamOvenDriver",
    "SterilizerDriver",
    "StoveDriver",
    "WaterPurifierDriver",
    "get_driver_for_device",
]
