"""Driver for ROBAM / ROKI Ovens, Steamers, Microwaves and Sterilizers."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import EntityCategory, UnitOfTemperature, UnitOfTime

from .base import BinarySensorSpec, RokiDeviceDriver, SensorSpec

# ``workState`` codes. 0 is deliberately not mapped here: it only means "not
# cooking", and whether that is 关机 or 待机 depends on ``powerState``.
THERMAL_STATUS_MAP: dict[int, str] = {
    1: "待机",
    2: "预热中",
    3: "工作中",
    4: "暂停",
    5: "完成",
    6: "预约中",
}

_STAGE_WORD = {1: "One", 2: "Two", 3: "Three"}

# The cloud sends two different payload shapes depending on the appliance
# generation.  The C98A-D3P micro/steam/air-fry oven reports ``workState`` +
# ``curTopTemp``/``curButtomTemp`` (sic) + ``totalRemainSeonds`` (sic), while the
# older shape used ``workStatus`` + ``temp`` + ``remainTime``.  Both are accepted
# so a cloud-side payload change does not blank every entity.
CAVITY_TOP_KEYS = ("curTopTemp",)
CAVITY_BOTTOM_KEYS = ("curButtomTemp",)
CAVITY_LEGACY_KEYS = ("temp", "currentTemp", "OvenTemp", "steameOvenTemp")
TARGET_LEGACY_KEYS = ("setTemp", "targetTemp", "setOvenTemp")
REMAIN_SECONDS_KEYS = ("totalRemainSeonds", "totalRemainSeconds")
REMAIN_MINUTE_KEYS = ("remainTime", "time", "leftTime")

# Measured on a C98A-D3P at the main breaker: 2833 W while preheating (nameplate
# 3450 W).  Holding a temperature is nowhere near that: the elements cycle, so a
# provisional 46 % duty is used until a full bake is measured.
PREHEAT_POWER_W = 2833
HOLD_POWER_W = 1300
# After shutdown the cavity fan keeps running while the box is hot.
COOLDOWN_FAN_W = 30
COOLDOWN_ABOVE_C = 60


def _num(props: dict[str, Any], *keys: str, default: float | None = None) -> float | None:
    """Read the first numeric property, tolerating strings and blanks."""
    for key in keys:
        value = props.get(key)
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return default


def _int(props: dict[str, Any], *keys: str, default: int = 0) -> int:
    value = _num(props, *keys)
    return int(value) if value is not None else default


def _work_code(props: dict[str, Any]) -> int:
    return _int(props, "workState", "workStatus", "status", default=-1)


def _cavity_temp(props: dict[str, Any]) -> float | None:
    readings = [
        value
        for value in (
            _num(props, *CAVITY_TOP_KEYS),
            _num(props, *CAVITY_BOTTOM_KEYS),
        )
        if value is not None and value > 0
    ]
    if readings:
        return max(readings)
    return _num(props, *CAVITY_LEGACY_KEYS)


def _target_temp(props: dict[str, Any]) -> float | None:
    stage = _int(props, "curStageNum", default=1)
    if stage not in _STAGE_WORD:
        stage = 1
    word = _STAGE_WORD[stage]
    readings = [
        value
        for value in (
            _num(props, f"stage{word}SetTopTemp"),
            _num(props, f"stage{word}SetButtomTemp"),
        )
        if value is not None and value > 0
    ]
    if readings:
        return max(readings)
    return _num(props, *TARGET_LEGACY_KEYS)


def _remaining_min(props: dict[str, Any]) -> int | None:
    if any(key in props for key in REMAIN_SECONDS_KEYS):
        seconds = _num(props, *REMAIN_SECONDS_KEYS)
        if seconds is None:
            return None
        return round(seconds / 60) if seconds > 1 else 0
    minutes = _num(props, *REMAIN_MINUTE_KEYS)
    return int(minutes) if minutes is not None else None


def _is_powered(props: dict[str, Any]) -> bool:
    """``powerState`` is 0 (mains off) or 2 (powered); older payloads omit it."""
    if "powerState" not in props:
        return True
    return _int(props, "powerState", default=0) != 0


def _work_text(props: dict[str, Any]) -> str | None:
    """``workState`` alone cannot tell 关机 / 散热中 / 待机.

    A powered but idle oven still reports ``workState`` 0, and while the cavity
    is hot the cooling/deodorising fan keeps running -- that is 散热中 and it
    still draws power.  Only once the box has cooled down is it really idle.
    """
    code = _work_code(props)
    if code < 0:
        return None
    if code == 0:
        if not _is_powered(props):
            return "关机"
        cavity = _cavity_temp(props)
        if cavity is not None and cavity > COOLDOWN_ABOVE_C:
            return "散热中"
        return "待机"
    return THERMAL_STATUS_MAP.get(code, f"状态{code}")


def _power_w(props: dict[str, Any]) -> int:
    """Estimated draw: preheat is full power, holding a temperature is not.

    Idle-but-hot is not zero either -- the cavity fan runs until the box cools.
    """
    code = _work_code(props)
    if code == 2:
        return PREHEAT_POWER_W
    if code == 3:
        return HOLD_POWER_W
    cavity = _cavity_temp(props)
    if cavity is not None and cavity > COOLDOWN_ABOVE_C:
        return COOLDOWN_FAN_W
    return 0


class ThermalCookerDriver(RokiDeviceDriver):
    """Driver managing Ovens, Steam Ovens, Microwaves and Sterilizers."""

    is_controllable = False  # Telemetry / monitoring until verified

    def sensors(self) -> list[SensorSpec]:
        return [
            SensorSpec(
                key="work_status",
                translation_key="work_status",
                icon="mdi:stove",
                value_fn=_work_text,
                attrs_fn=lambda p: {
                    "work_state": _work_code(p),
                    "power_state": _int(p, "powerState", default=-1),
                    "stage": _int(p, "curStageNum", default=0),
                    "stages": _int(p, "stageTotalNum", default=0),
                    "fault_code": _int(p, "faultCode", default=0),
                },
            ),
            SensorSpec(
                key="current_temperature",
                translation_key="current_temperature",
                icon="mdi:thermometer",
                device_class=SensorDeviceClass.TEMPERATURE,
                state_class=SensorStateClass.MEASUREMENT,
                unit_of_measurement=UnitOfTemperature.CELSIUS,
                value_fn=_cavity_temp,
            ),
            SensorSpec(
                key="top_temperature",
                translation_key="top_temperature",
                icon="mdi:thermometer-chevron-up",
                device_class=SensorDeviceClass.TEMPERATURE,
                state_class=SensorStateClass.MEASUREMENT,
                unit_of_measurement=UnitOfTemperature.CELSIUS,
                value_fn=lambda p: _num(p, *CAVITY_TOP_KEYS),
            ),
            SensorSpec(
                key="bottom_temperature",
                translation_key="bottom_temperature",
                icon="mdi:thermometer-chevron-down",
                device_class=SensorDeviceClass.TEMPERATURE,
                state_class=SensorStateClass.MEASUREMENT,
                unit_of_measurement=UnitOfTemperature.CELSIUS,
                value_fn=lambda p: _num(p, *CAVITY_BOTTOM_KEYS),
            ),
            SensorSpec(
                key="target_temperature",
                translation_key="target_temperature",
                icon="mdi:thermometer-chevron-up",
                device_class=SensorDeviceClass.TEMPERATURE,
                unit_of_measurement=UnitOfTemperature.CELSIUS,
                value_fn=_target_temp,
            ),
            SensorSpec(
                key="remaining_time",
                translation_key="remaining_time",
                icon="mdi:timer-outline",
                device_class=SensorDeviceClass.DURATION,
                unit_of_measurement=UnitOfTime.MINUTES,
                value_fn=_remaining_min,
            ),
            SensorSpec(
                key="stage",
                translation_key="stage",
                icon="mdi:tray-full",
                value_fn=lambda p: (
                    f"{_int(p, 'curStageNum')}/{_int(p, 'stageTotalNum')}"
                    if _int(p, "stageTotalNum") > 0
                    else "单段"
                ),
            ),
            SensorSpec(
                key="estimated_power",
                translation_key="estimated_power",
                icon="mdi:flash",
                device_class=SensorDeviceClass.POWER,
                state_class=SensorStateClass.MEASUREMENT,
                unit_of_measurement="W",
                value_fn=_power_w,
            ),
        ]

    def binary_sensors(self) -> list[BinarySensorSpec]:
        return [
            BinarySensorSpec(
                key="door",
                translation_key="door",
                device_class=BinarySensorDeviceClass.DOOR,
                is_on_fn=lambda p: bool(_int(p, "doorState", "doorOpen", "door"))
                if ("doorState" in p or "doorOpen" in p or "door" in p)
                else None,
            ),
            BinarySensorSpec(
                key="alarm",
                translation_key="alarm",
                device_class=BinarySensorDeviceClass.PROBLEM,
                entity_category=EntityCategory.DIAGNOSTIC,
                is_on_fn=lambda p: bool(_int(p, "faultCode", "alarm", "fault"))
                if ("faultCode" in p or "alarm" in p or "fault" in p)
                else None,
            ),
            BinarySensorSpec(
                key="light",
                translation_key="light",
                icon="mdi:lightbulb",
                entity_category=EntityCategory.DIAGNOSTIC,
                is_on_fn=lambda p: bool(_int(p, "lightSwitch"))
                if "lightSwitch" in p
                else None,
            ),
        ]


class SteamOvenDriver(ThermalCookerDriver):
    """Driver managing Steam Ovens (蒸箱/蒸烤一体机, dc: RZQL / RZKY)."""

    category_code = "RZQL"
    category_name = "Steam Oven"


class OvenDriver(ThermalCookerDriver):
    """Driver managing Electric Ovens (烤箱, dc: RDKX)."""

    category_code = "RDKX"
    category_name = "Oven"


class MicrowaveDriver(ThermalCookerDriver):
    """Driver managing Microwaves (微波炉, dc: RWBL)."""

    category_code = "RWBL"
    category_name = "Microwave"


class SterilizerDriver(ThermalCookerDriver):
    """Driver managing Sterilizers (消毒柜, dc: RXDG)."""

    category_code = "RXDG"
    category_name = "Sterilizer"
