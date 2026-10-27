"""Driver for ROBAM / ROKI Gas / Induction Stoves (燃气灶 / 电磁灶, dc: RRQZ)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import EntityCategory, UnitOfTime

from .base import BinarySensorSpec, RokiDeviceDriver, SensorSpec

STOVE_STATUS_MAP: dict[int, str] = {
    0: "关火",
    1: "待机",
    2: "工作中",
    3: "定时中",
}


class StoveDriver(RokiDeviceDriver):
    """Driver managing Gas / Induction Stoves (RRQZ)."""

    category_code = "RRQZ"
    category_name = "Stove"
    is_controllable = False  # Read-only for safety until verified

    def sensors(self) -> list[SensorSpec]:
        return [
            SensorSpec(
                key="left_head_level",
                translation_key="left_head_level",
                icon="mdi:fire",
                value_fn=lambda p: p.get("stoveHeadLeftPower", p.get("leftHeadLevel", p.get("ih1Level"))),
            ),
            SensorSpec(
                key="right_head_level",
                translation_key="right_head_level",
                icon="mdi:fire",
                value_fn=lambda p: p.get("stoveHeadRightPower", p.get("rightHeadLevel", p.get("ih2Level"))),
            ),
            SensorSpec(
                key="left_head_remaining_time",
                translation_key="left_head_remaining_time",
                icon="mdi:timer-outline",
                device_class=SensorDeviceClass.DURATION,
                unit_of_measurement=UnitOfTime.MINUTES,
                value_fn=lambda p: p.get("stoveHeadLeftRemainingTime", p.get("leftHeadTime", p.get("ih1Time"))),
            ),
            SensorSpec(
                key="right_head_remaining_time",
                translation_key="right_head_remaining_time",
                icon="mdi:timer-outline",
                device_class=SensorDeviceClass.DURATION,
                unit_of_measurement=UnitOfTime.MINUTES,
                value_fn=lambda p: p.get("stoveHeadRightRemainingTime", p.get("rightHeadTime", p.get("ih2Time"))),
            ),
            SensorSpec(
                key="left_head_status",
                translation_key="left_head_status",
                icon="mdi:stove",
                value_fn=lambda p: STOVE_STATUS_MAP.get(
                    int(p.get("stoveHeadLeftWorkStatus", p.get("leftHeadStatus", p.get("ih1Status", 0)))),
                    "未知",
                ),
            ),
            SensorSpec(
                key="right_head_status",
                translation_key="right_head_status",
                icon="mdi:stove",
                value_fn=lambda p: STOVE_STATUS_MAP.get(
                    int(p.get("stoveHeadRightWorkStatus", p.get("rightHeadStatus", p.get("ih2Status", 0)))),
                    "未知",
                ),
            ),
        ]

    def binary_sensors(self) -> list[BinarySensorSpec]:
        return [
            BinarySensorSpec(
                key="left_head_alarm",
                translation_key="left_head_alarm",
                device_class=BinarySensorDeviceClass.PROBLEM,
                entity_category=EntityCategory.DIAGNOSTIC,
                is_on_fn=lambda p: bool(int(p.get("stoveHeadLeftAlarmStatus", p.get("leftHeadAlarm", 0))))
                if ("stoveHeadLeftAlarmStatus" in p or "leftHeadAlarm" in p)
                else None,
            ),
            BinarySensorSpec(
                key="right_head_alarm",
                translation_key="right_head_alarm",
                device_class=BinarySensorDeviceClass.PROBLEM,
                entity_category=EntityCategory.DIAGNOSTIC,
                is_on_fn=lambda p: bool(int(p.get("stoveHeadRightAlarmStatus", p.get("rightHeadAlarm", 0))))
                if ("stoveHeadRightAlarmStatus" in p or "rightHeadAlarm" in p)
                else None,
            ),
        ]
