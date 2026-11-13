"""Diagnostics support for the ROBAM / ROKI integration."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from .coordinator import RokiCoordinator, mask_phone
from .redact import REDACTED, SENSITIVE_KEYS, mask_guid, redact_secrets

# Kept as a public name for compatibility.  The policy itself lives in
# redact.py so the diagnostics export, the CLI bundle and the fixtures all use
# exactly one set of rules.
TO_REDACT = SENSITIVE_KEYS


def _mask_guid(guid: str | None) -> str | None:
    """Mask a hardware GUID keeping only model prefix and trailing chars."""
    return mask_guid(guid)


def _sanitize_device_data(raw_device: dict[str, Any]) -> dict[str, Any]:
    """Sanitize raw device representation."""
    cleaned = dict(raw_device)
    if "guid" in cleaned:
        cleaned["guid_masked"] = _mask_guid(str(cleaned["guid"]))
        del cleaned["guid"]
    if "mac" in cleaned:
        cleaned["mac"] = REDACTED
    return redact_secrets(cleaned)


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator: RokiCoordinator = entry.runtime_data.coordinator

    # Sanitize devices first (guid -> guid_masked, mac -> REDACTED), then let
    # the shared redaction policy remove the remaining credential values.  The
    # order keeps the published fields identical to the previous implementation.
    entry_data = dict(entry.data)
    if "devices" in entry_data:
        entry_data["devices"] = [
            _sanitize_device_data(d) if isinstance(d, dict) else d
            for d in entry_data["devices"]
        ]
    entry_data = redact_secrets(entry_data)

    # Redact live coordinator cache
    sanitized_coordinator_data: dict[str, Any] = {}
    if coordinator.data:
        for guid, state in coordinator.data.items():
            masked_key = _mask_guid(guid) or "unknown"
            sanitized_coordinator_data[masked_key] = redact_secrets(state)

    # Sanitize known registered devices
    sanitized_devices = [
        _sanitize_device_data(d) for d in coordinator.devices
    ]

    return {
        "entry": {
            "title": mask_phone(entry.title),
            "data": entry_data,
            "options": dict(entry.options),
        },
        "coordinator": {
            "sse_connected": coordinator._sse_connected,
            "mqtt_connected": coordinator._mqtt_connected,
            "enable_sse": coordinator.enable_sse,
            "data": sanitized_coordinator_data,
        },
        "devices": sanitized_devices,
    }


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry, device: DeviceEntry
) -> dict[str, Any]:
    """Return diagnostics for a specific device."""
    diagnostics_data = await async_get_config_entry_diagnostics(hass, entry)
    diagnostics_data["device_entry"] = {
        "identifiers": [
            (domain, _mask_guid(ident))
            for domain, ident in device.identifiers
        ],
        "model": device.model,
        "manufacturer": device.manufacturer,
        "sw_version": device.sw_version,
    }
    return diagnostics_data
