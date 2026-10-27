"""Driver for ROBAM / ROKI Ovens, Steamers, Microwaves and Sterilizers."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import EntityCategory, UnitOfTemperature, UnitOfTime

from .base import BinarySensorSpec, RokiDeviceDriver, SensorSpec

THERMAL_STATUS_MAP: dict[int, str] = {
    0: "关机",
    1: "待机",
    2: "预热中",
    3: "工作中",
    4: "暂停",
    5: "完成",
    6: "预约中",
}


class ThermalCookerDriver(RokiDeviceDriver):
    """Driver managing Ovens, Steam Ovens, Microwaves and Sterilizers."""

    is_controllable = False  # Telemetry / monitoring until verified

    def sensors(self) -> list[SensorSpec]:
        return [
            SensorSpec(
                key="work_status",
                translation_key="work_status",
                icon="mdi:stove",
                value_fn=lambda p: THERMAL_STATUS_MAP.get(
                    int(p.get("workStatus", p.get("status", 0))), "未知"
                ),
            ),
            SensorSpec(
                key="current_temperature",
                translation_key="current_temperature",
                icon="mdi:thermometer",
                device_class=SensorDeviceClass.TEMPERATURE,
                state_class=SensorStateClass.MEASUREMENT,
                unit_of_measurement=UnitOfTemperature.CELSIUS,
                value_fn=lambda p: p.get("temp", p.get("currentTemp", p.get("OvenTemp", p.get("steameOvenTemp")))),
            ),
            SensorSpec(
                key="target_temperature",
                translation_key="target_temperature",
                icon="mdi:thermometer-chevron-up",
                device_class=SensorDeviceClass.TEMPERATURE,
                unit_of_measurement=UnitOfTemperature.CELSIUS,
                value_fn=lambda p: p.get("setTemp", p.get("targetTemp", p.get("setOvenTemp"))),
            ),
            SensorSpec(
                key="remaining_time",
                translation_key="remaining_time",
                icon="mdi:timer-outline",
                device_class=SensorDeviceClass.DURATION,
                unit_of_measurement=UnitOfTime.MINUTES,
                value_fn=lambda p: p.get("remainTime", p.get("time", p.get("leftTime"))),
            ),
        ]

    def binary_sensors(self) -> list[BinarySensorSpec]:
        return [
            BinarySensorSpec(
                key="door",
                translation_key="door",
                device_class=BinarySensorDeviceClass.DOOR,
                is_on_fn=lambda p: bool(int(p.get("doorOpen", p.get("door", 0))))
                if ("doorOpen" in p or "door" in p)
                else None,
            ),
            BinarySensorSpec(
                key="alarm",
                translation_key="alarm",
                device_class=BinarySensorDeviceClass.PROBLEM,
                entity_category=EntityCategory.DIAGNOSTIC,
                is_on_fn=lambda p: bool(int(p.get("alarm", p.get("fault", 0))))
                if ("alarm" in p or "fault" in p)
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
