"""Driver for ROBAM / ROKI Range Hoods (油烟机, dc: RYYJ)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.const import EntityCategory

from ..const import known_levels_for_model
from .base import BinarySensorSpec, RokiDeviceDriver, SensorSpec

FAULT_DESCRIPTIONS: dict[int, str] = {
    0: "正常",
}


class HoodDriver(RokiDeviceDriver):
    """Driver managing Range Hoods (RYYJ)."""

    category_code = "RYYJ"
    category_name = "Range Hood"
    is_controllable = True

    def __init__(self, coordinator: Any, device: dict[str, Any]) -> None:
        super().__init__(coordinator, device)
        self.model_levels = known_levels_for_model(self.dt)

    def sensors(self) -> list[SensorSpec]:
        """Telemetry/diagnostic sensors for range hood."""
        return [
            SensorSpec(
                key="fault_code",
                translation_key="fault_code",
                icon="mdi:alert-circle-outline",
                entity_category=EntityCategory.DIAGNOSTIC,
                value_fn=lambda props: (
                    int(props.get("faultCode", props.get("fault_code", 0)))
                    if ("faultCode" in props or "fault_code" in props)
                    else None
                ),
                attrs_fn=lambda props: {
                    "description": FAULT_DESCRIPTIONS.get(
                        int(props.get("faultCode", props.get("fault_code", 0))),
                        "未知故障码",
                    )
                }
                if ("faultCode" in props or "fault_code" in props)
                else {},
            )
        ]

    def binary_sensors(self) -> list[BinarySensorSpec]:
        """Binary diagnostic sensors for range hood."""
        return [
            BinarySensorSpec(
                key="clean_required",
                translation_key="clean_required",
                device_class=BinarySensorDeviceClass.PROBLEM,
                entity_category=EntityCategory.DIAGNOSTIC,
                is_on_fn=lambda props: (
                    int(props.get("cleanRequired", props.get("clean_required", 0))) == 1
                    if ("cleanRequired" in props or "clean_required" in props)
                    else None
                ),
            )
        ]
