"""Driver for ROBAM / ROKI Dishwashers (洗碗机, dc: RXWJ)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import EntityCategory, UnitOfTime

from .base import BinarySensorSpec, RokiDeviceDriver, SensorSpec

DISHWASHER_STATUS_MAP: dict[int, str] = {
    0: "关机",
    1: "待机",
    2: "工作中",
    3: "暂停",
    4: "预约中",
    5: "完成",
    6: "故障",
}


class DishwasherDriver(RokiDeviceDriver):
    """Driver managing Dishwashers (RXWJ)."""

    category_code = "RXWJ"
    category_name = "Dishwasher"
    is_controllable = False  # Read-only until verified

    def sensors(self) -> list[SensorSpec]:
        return [
            SensorSpec(
                key="work_status",
                translation_key="work_status",
                icon="mdi:dishwasher",
                value_fn=lambda p: DISHWASHER_STATUS_MAP.get(
                    int(p.get("workStatus", p.get("DishWasherStatus", 0))),
                    "未知",
                ),
            ),
            SensorSpec(
                key="remaining_time",
                translation_key="remaining_time",
                icon="mdi:timer-sand",
                device_class=SensorDeviceClass.DURATION,
                unit_of_measurement=UnitOfTime.MINUTES,
                value_fn=lambda p: p.get("remainTime", p.get("DishWasherRemainingWorkingTime")),
            ),
            SensorSpec(
                key="work_mode",
                translation_key="work_mode",
                icon="mdi:silverware-clean",
                value_fn=lambda p: p.get("workMode", p.get("DishWasherWorkMode")),
            ),
        ]

    def binary_sensors(self) -> list[BinarySensorSpec]:
        return [
            BinarySensorSpec(
                key="door",
                translation_key="door",
                device_class=BinarySensorDeviceClass.DOOR,
                is_on_fn=lambda p: bool(int(p.get("doorOpen", p.get("DishWasherDoorValue", 0))))
                if ("doorOpen" in p or "DishWasherDoorValue" in p)
                else None,
            ),
            BinarySensorSpec(
                key="lack_salt",
                translation_key="lack_salt",
                device_class=BinarySensorDeviceClass.PROBLEM,
                entity_category=EntityCategory.DIAGNOSTIC,
                is_on_fn=lambda p: bool(int(p.get("lackSalt", 0))) if "lackSalt" in p else None,
            ),
            BinarySensorSpec(
                key="lack_rinse",
                translation_key="lack_rinse",
                device_class=BinarySensorDeviceClass.PROBLEM,
                entity_category=EntityCategory.DIAGNOSTIC,
                is_on_fn=lambda p: bool(int(p.get("lackRinse", 0))) if "lackRinse" in p else None,
            ),
        ]
