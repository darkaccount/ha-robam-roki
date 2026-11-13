"""Built-in driver coverage, shared by the HA services and the standalone CLI.

A draft that wants to sit at "built-in configuration level" needs three facts
about a device's driver: which Python driver serves it (the `extends` line),
the keys that driver already exposes (so the draft does not duplicate them) and
the expected entity values (the fixture's `expect`).  The Home Assistant
service and `tools/roki_cli.py` must answer those identically, so the logic
lives here once.

The module itself imports no Home Assistant symbol.  Inside Home Assistant it
imports the real `devices` package; loaded from the CLI it installs a minimal
shim (only the enum names the driver modules reference) and loads the same
package from this directory, so both paths execute the same driver code.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

COMPONENT_DIR = Path(__file__).resolve().parent
BUILTIN_PROFILES_DIR = COMPONENT_DIR / "profiles"

#: Synthetic package name used when this module runs outside the integration.
_STANDALONE_PACKAGE = "roki_builtin_runtime"

#: camelCase/snake_case aliases between the cloud properties and the driver.
PROPERTY_ALIASES = {
    "working_status": "workStatus",
    "light_status": "lightStatus",
}

_cache: dict[str, Any] = {}


# -- loading the device layer -------------------------------------------


def _loose_enum() -> type:
    """A stand-in for a Home Assistant StrEnum: value is the lower-case name."""

    class _LooseEnum(str):
        pass

    class _LooseEnumMeta(type):
        def __getattr__(cls, name: str) -> Any:
            if name.startswith("__"):
                raise AttributeError(name)
            return _LooseEnum(name.lower())

    return _LooseEnumMeta("LooseEnum", (_LooseEnum,), {})


def _install_ha_shim() -> None:
    """Give the driver modules the enum names they import, when HA is absent."""
    if _cache.get("ha_shim"):
        return
    _cache["ha_shim"] = True
    try:
        import homeassistant  # noqa: F401
    except ImportError:
        pass
    else:
        return

    ha = ModuleType("homeassistant")
    ha.__path__ = []
    components = ModuleType("homeassistant.components")
    components.__path__ = []
    binary_sensor = ModuleType("homeassistant.components.binary_sensor")
    binary_sensor.BinarySensorDeviceClass = _loose_enum()
    sensor = ModuleType("homeassistant.components.sensor")
    sensor.SensorDeviceClass = _loose_enum()
    sensor.SensorStateClass = _loose_enum()
    const = ModuleType("homeassistant.const")
    const.EntityCategory = _loose_enum()
    const.UnitOfTemperature = _loose_enum()
    const.UnitOfTime = _loose_enum()
    ha.components = components
    components.binary_sensor = binary_sensor
    components.sensor = sensor
    ha.const = const
    for name, module in (
        ("homeassistant", ha),
        ("homeassistant.components", components),
        ("homeassistant.components.binary_sensor", binary_sensor),
        ("homeassistant.components.sensor", sensor),
        ("homeassistant.const", const),
    ):
        sys.modules.setdefault(name, module)


def _ensure_standalone_package() -> ModuleType:
    """The synthetic package the device modules load under in the CLI."""
    package = sys.modules.get(_STANDALONE_PACKAGE)
    if package is not None:
        return package
    package = ModuleType(_STANDALONE_PACKAGE)
    package.__path__ = [str(COMPONENT_DIR)]
    sys.modules[_STANDALONE_PACKAGE] = package
    # devices/base.py imports the coordinator type; drivers only need its name.
    coordinator = ModuleType(f"{_STANDALONE_PACKAGE}.coordinator")
    coordinator.RokiCoordinator = type("RokiCoordinator", (), {})
    sys.modules[f"{_STANDALONE_PACKAGE}.coordinator"] = coordinator
    package.coordinator = coordinator
    return package


def _devices_module() -> ModuleType:
    """The devices package: the real one inside HA, the loaded one in the CLI."""
    cached = _cache.get("devices")
    if cached is not None:
        return cached
    try:
        from . import devices as module
    except ImportError:
        _install_ha_shim()
        _ensure_standalone_package()
        module = importlib.import_module(f"{_STANDALONE_PACKAGE}.devices")
    _cache["devices"] = module
    return module


def _profiles_module() -> ModuleType:
    """The YAML profile loader, from whichever package is in use."""
    cached = _cache.get("profiles_module")
    if cached is not None:
        return cached
    try:
        from . import profiles as module
    except ImportError:
        _ensure_standalone_package()
        module = importlib.import_module(f"{_STANDALONE_PACKAGE}.profiles")
    _cache["profiles_module"] = module
    return module


class _ProbeCoordinator:
    """The minimum a driver needs: no REST device, no cached properties."""

    def __init__(self, profiles: list[Any]) -> None:
        self.profiles = list(profiles)
        self.data: dict[str, Any] = {}

    def is_rest_device(self, device_id: str) -> bool:
        return False


# -- the shared answers --------------------------------------------------


def covered_control_properties(driver: Any) -> set[str]:
    """Property keys the driver already exposes as controls.

    A drafted sensor for one of these keys would duplicate the entity the
    control already writes (level, light, power, countdown), so the draft skips
    them the same way it skips the driver's own sensor keys.
    """
    keys: set[str] = set()
    try:
        commands = driver.commands() or {}
    except Exception:  # noqa: BLE001 - a broken driver must not kill the draft
        return keys
    for spec in commands.values():
        for payload in (spec.on, spec.off):
            if isinstance(payload, dict):
                for key in payload:
                    keys.add(str(key))
                    keys.add(PROPERTY_ALIASES.get(str(key), str(key)))
        if spec.param:
            keys.add(str(spec.param))
        if spec.params:
            keys.update(str(param) for param in spec.params)
    if "timing" in commands:
        keys.add("timing")
    return keys


def builtin_driver(driver: Any) -> tuple[str | None, frozenset[str] | None]:
    """Name and entity keys of the built-in Python driver behind this driver.

    A driver generated from a profile is not in the registry, so it gets no
    extends line: the draft itself is the replacement profile.
    """
    if driver is None:
        return None, None
    registry = getattr(_devices_module(), "PYTHON_DRIVERS", {})
    for name, driver_cls in registry.items():
        if type(driver) is driver_cls:
            keys = {spec.key for spec in driver.sensors()}
            keys |= {spec.key for spec in driver.binary_sensors()}
            keys |= covered_control_properties(driver)
            return name, frozenset(keys)
    return None, None


def expect_values(driver: Any, properties: dict[str, Any]) -> dict[str, Any]:
    """Expected entity values computed with the device's current driver."""
    expect: dict[str, Any] = {}
    if driver is None:
        return expect
    for spec in driver.sensors():
        if spec.value_fn is not None:
            try:
                expect[spec.key] = spec.value_fn(properties)
            except Exception:  # noqa: BLE001 - keep the rest of the fixture
                expect[spec.key] = None
        else:
            expect[spec.key] = properties.get(spec.key)
    for spec in driver.binary_sensors():
        if spec.is_on_fn is not None:
            try:
                expect[spec.key] = spec.is_on_fn(properties)
            except Exception:  # noqa: BLE001 - keep the rest of the fixture
                expect[spec.key] = None
        else:
            expect[spec.key] = properties.get(spec.key)
    return expect


def load_builtin_profiles() -> list[Any]:
    """The profiles shipped with the integration, resolved for matching."""
    cached = _cache.get("builtin_profiles")
    if cached is not None:
        return cached
    module = _profiles_module()
    try:
        loaded = module.load_profile_dir(BUILTIN_PROFILES_DIR, module.SOURCE_BUILTIN)
        profiles = module.resolve_extends(loaded)
    except Exception:  # noqa: BLE001 - coverage is an optimisation, never fatal
        profiles = []
    _cache["builtin_profiles"] = profiles
    return profiles


def driver_for_device(
    device: dict[str, Any], profiles: list[Any] | None = None
) -> Any | None:
    """Build the driver the integration would use for this device."""
    if not isinstance(device, dict) or not device.get("dc"):
        return None
    profile_list = load_builtin_profiles() if profiles is None else profiles
    factory = getattr(_devices_module(), "get_driver_for_device", None)
    if not callable(factory):
        return None
    try:
        return factory(_ProbeCoordinator(profile_list), dict(device))
    except Exception:  # noqa: BLE001 - a draft must not fail on a driver quirk
        return None


def coverage(
    device: dict[str, Any],
) -> tuple[
    str | None,
    frozenset[str] | None,
    Callable[[dict[str, Any]], dict[str, Any]] | None,
]:
    """(extends name, covered keys, expect function) for one device.

    All three are None when no driver can be built, so a caller can simply hand
    the tuple to the drafter: it degrades to a plain read-only draft.
    """
    try:
        driver = driver_for_device(device)
        if driver is None:
            return None, None, None
        name, keys = builtin_driver(driver)
        return (
            name,
            keys,
            lambda properties: expect_values(driver, properties),
        )
    except Exception:  # noqa: BLE001 - never break a draft over coverage
        return None, None, None
