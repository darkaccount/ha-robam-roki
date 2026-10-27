"""Driver for ROBAM / ROKI Water Purifiers (净水器, dc: RJSQ)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorStateClass
from homeassistant.const import EntityCategory

from .base import BinarySensorSpec, RokiDeviceDriver, SensorSpec


class WaterPurifierDriver(RokiDeviceDriver):
    """Driver managing Water Purifiers (RJSQ)."""

    category_code = "RJSQ"
    category_name = "Water Purifier"
    is_controllable = False  # Telemetry only

    def sensors(self) -> list[SensorSpec]:
        return [
            SensorSpec(
                key="input_tds",
                translation_key="input_tds",
                icon="mdi:water-opacity",
                state_class=SensorStateClass.MEASUREMENT,
                unit_of_measurement="ppm",
                value_fn=lambda p: p.get("input_tds", p.get("inTds")),
            ),
            SensorSpec(
                key="output_tds",
                translation_key="output_tds",
                icon="mdi:water-check",
                state_class=SensorStateClass.MEASUREMENT,
                unit_of_measurement="ppm",
                value_fn=lambda p: p.get("output_tds", p.get("outTds", p.get("currentTDSValue"))),
            ),
            SensorSpec(
                key="filter_life_1",
                translation_key="filter_life_1",
                icon="mdi:filter-outline",
                unit_of_measurement="%",
                value_fn=lambda p: p.get("filter1", p.get("filterLife1")),
            ),
            SensorSpec(
                key="filter_life_2",
                translation_key="filter_life_2",
                icon="mdi:filter-outline",
                unit_of_measurement="%",
                value_fn=lambda p: p.get("filter2", p.get("filterLife2")),
            ),
        ]

    def binary_sensors(self) -> list[BinarySensorSpec]:
        return [
            BinarySensorSpec(
                key="purifying",
                translation_key="purifying",
                device_class=BinarySensorDeviceClass.RUNNING,
                is_on_fn=lambda p: bool(int(p.get("purifying", p.get("workStatus", 0))))
                if ("purifying" in p or "workStatus" in p)
                else None,
            ),
            BinarySensorSpec(
                key="leak_alarm",
                translation_key="leak_alarm",
                device_class=BinarySensorDeviceClass.MOISTURE,
                entity_category=EntityCategory.DIAGNOSTIC,
                is_on_fn=lambda p: bool(int(p.get("leakAlarm", 0))) if "leakAlarm" in p else None,
            ),
        ]
