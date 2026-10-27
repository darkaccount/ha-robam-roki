"""Test diagnostics for the ROBAM / ROKI integration."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.robam_roki.diagnostics import (
    async_get_config_entry_diagnostics,
    async_get_device_diagnostics,
)


@pytest.mark.asyncio
async def test_diagnostics_redacts_credentials():
    hass = MagicMock()

    coordinator = MagicMock()
    coordinator._sse_connected = True
    coordinator._mqtt_connected = False
    coordinator.enable_sse = True
    coordinator.devices = [
        {
            "guid": "R8229000000000000",
            "name": "油烟机",
            "dt": "R8229",
            "mac": "AA:BB:CC:DD:EE:FF",
            "deviceSource": 0,
            "deviceToken": "secret_device_token",
        }
    ]
    coordinator.data = {
        "R8229000000000000": {
            "workStatus": 1,
            "fanLevel": 2,
            "light": 0,
            "token": "sensitive_session_token",
        }
    }

    entry = MagicMock()
    entry.title = "13800000000"
    entry.data = {
        "phone": "13800000000",
        "password": "secret_md5_password",
        "access_token": "secret_access_token",
        "refresh_token": "secret_refresh_token",
        "app_guid": "12345678901234567",
        "user_id": 99999,
        "devices": coordinator.devices,
    }
    entry.options = {"scan_interval": 30}
    entry.runtime_data = MagicMock()
    entry.runtime_data.coordinator = coordinator

    diag = await async_get_config_entry_diagnostics(hass, entry)

    # Validate that sensitive data in entry.data is redacted
    assert diag["entry"]["data"]["phone"] == "**REDACTED**"
    assert diag["entry"]["data"]["password"] == "**REDACTED**"
    assert diag["entry"]["data"]["access_token"] == "**REDACTED**"
    assert diag["entry"]["data"]["refresh_token"] == "**REDACTED**"
    assert diag["entry"]["data"]["app_guid"] == "**REDACTED**"
    assert diag["entry"]["data"]["user_id"] == "**REDACTED**"
    assert diag["entry"]["title"] == "138****0000"

    # Validate device masking
    sanitized_device = diag["devices"][0]
    assert "guid" not in sanitized_device
    assert sanitized_device["guid_masked"] == "R822...0000"
    assert sanitized_device["mac"] == "**REDACTED**"
    assert sanitized_device["deviceToken"] == "**REDACTED**"

    # Validate coordinator data masking
    assert "R8229000000000000" not in diag["coordinator"]["data"]
    assert "R822...0000" in diag["coordinator"]["data"]
    assert diag["coordinator"]["data"]["R822...0000"]["token"] == "**REDACTED**"
    assert diag["coordinator"]["data"]["R822...0000"]["fanLevel"] == 2


@pytest.mark.asyncio
async def test_device_diagnostics():
    hass = MagicMock()
    coordinator = MagicMock()
    coordinator._sse_connected = False
    coordinator._mqtt_connected = True
    coordinator.enable_sse = False
    coordinator.devices = []
    coordinator.data = {}

    entry = MagicMock()
    entry.title = "ROBAM ROKI"
    entry.data = {}
    entry.options = {}
    entry.runtime_data = MagicMock()
    entry.runtime_data.coordinator = coordinator

    device_entry = MagicMock()
    device_entry.identifiers = {("robam_roki", "R8229000000000000")}
    device_entry.model = "R8229"
    device_entry.manufacturer = "ROBAM"
    device_entry.sw_version = "1.0.0"

    device_diag = await async_get_device_diagnostics(hass, entry, device_entry)

    assert "device_entry" in device_diag
    assert device_diag["device_entry"]["model"] == "R8229"
    assert device_diag["device_entry"]["identifiers"] == [("robam_roki", "R822...0000")]
