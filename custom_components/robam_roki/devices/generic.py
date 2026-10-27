"""Generic fallback driver exposing transparent raw properties."""

from __future__ import annotations

from typing import Any

from homeassistant.const import EntityCategory

from .base import RokiDeviceDriver, SensorSpec


class GenericDriver(RokiDeviceDriver):
    """Driver managing any unlisted or unknown appliance category."""

    category_code = "GENERIC"
    category_name = "Generic Appliance"
    is_controllable = False

    def sensors(self) -> list[SensorSpec]:
        """Provide a diagnostic sensor exposing raw properties in attributes."""
        return [
            SensorSpec(
                key="raw_state",
                translation_key="raw_state",
                icon="mdi:information-outline",
                entity_category=EntityCategory.DIAGNOSTIC,
                value_fn=lambda props: "available" if props else "idle",
                attrs_fn=lambda props: dict(props) if props else {},
            )
        ]
