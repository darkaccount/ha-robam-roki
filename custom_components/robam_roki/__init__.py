"""Set up the ROBAM / ROKI cloud integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import RokiAuthError, RokiClient, RokiError
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_APP_GUID,
    CONF_DEVICE_TOKEN,
    CONF_DEVICES,
    CONF_ENABLE_SSE,
    CONF_PASSWORD,
    CONF_PHONE,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL,
    CONF_TOKEN_EXPIRES_AT,
    CONF_TOKEN_TYPE,
    CONF_USER_ID,
    DEFAULT_ENABLE_SSE,
    DEFAULT_SCAN_INTERVAL,
    PLATFORMS,
)
from .coordinator import RokiCoordinator

_LOGGER = logging.getLogger(__name__)


@dataclass
class RokiRuntimeData:
    """Objects shared by the platforms."""

    client: RokiClient
    coordinator: RokiCoordinator
    options: dict[str, Any]


RokiConfigEntry = ConfigEntry


async def async_setup_entry(hass: HomeAssistant, entry: RokiConfigEntry) -> bool:
    session = async_get_clientsession(hass)
    client = RokiClient(
        session,
        entry.data.get(CONF_PHONE),
        entry.data.get(CONF_PASSWORD),
    )
    client.restore(
        access_token=entry.data.get(CONF_ACCESS_TOKEN),
        token_type=entry.data.get(CONF_TOKEN_TYPE),
        refresh_token=entry.data.get(CONF_REFRESH_TOKEN),
        expires_at=entry.data.get(CONF_TOKEN_EXPIRES_AT, 0.0),
        user_id=entry.data.get(CONF_USER_ID),
        device_token=entry.data.get(CONF_DEVICE_TOKEN),
        app_guid=entry.data.get(CONF_APP_GUID),
    )

    try:
        if not client.token_valid:
            await client.async_login()
        if not client.user_id:
            await client.async_get_user()
        if not client.app_guid:
            await client.async_get_app_guid()
    except RokiAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except RokiError as err:
        raise ConfigEntryNotReady(str(err)) from err

    _store_session(hass, entry, client)

    devices: list[dict[str, Any]] = list(entry.data.get(CONF_DEVICES) or [])
    if not devices:
        try:
            devices = [
                _device_to_dict(device)
                for device in await client.async_get_devices()
            ]
        except RokiAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except RokiError as err:
            raise ConfigEntryNotReady(str(err)) from err
        if not devices:
            raise ConfigEntryNotReady("No devices found on this account")
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_DEVICES: devices}
        )

    options = entry.options
    coordinator = RokiCoordinator(
        hass,
        entry,
        client,
        devices,
        scan_interval=int(options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)),
        enable_sse=bool(options.get(CONF_ENABLE_SSE, DEFAULT_ENABLE_SSE)),
    )

    await coordinator.async_config_entry_first_refresh()
    coordinator.async_start_push()

    entry.runtime_data = RokiRuntimeData(
        client=client,
        coordinator=coordinator,
        options=dict(entry.options),
    )
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))

    await hass.config_entries.async_forward_entry_setups(
        entry, [Platform(platform) for platform in PLATFORMS]
    )
    return True


async def _async_entry_updated(hass: HomeAssistant, entry: RokiConfigEntry) -> None:
    """Reload when the user changes the options, ignore token updates."""
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        return
    if dict(entry.options) != runtime.options:
        await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: RokiConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, [Platform(platform) for platform in PLATFORMS]
    )
    if unload_ok and hasattr(entry, "runtime_data") and entry.runtime_data:
        await entry.runtime_data.coordinator.async_stop_push()
    return unload_ok


def _store_session(hass: HomeAssistant, entry: ConfigEntry, client: RokiClient) -> None:
    data = {
        **entry.data,
        CONF_ACCESS_TOKEN: client.access_token,
        CONF_TOKEN_TYPE: client.token_type,
        CONF_REFRESH_TOKEN: client.refresh_token,
        CONF_TOKEN_EXPIRES_AT: client.expires_at,
        CONF_USER_ID: client.user_id,
        CONF_DEVICE_TOKEN: client.device_token,
        CONF_APP_GUID: client.app_guid,
    }
    if data != dict(entry.data):
        hass.config_entries.async_update_entry(entry, data=data)


def _device_to_dict(device: dict[str, Any]) -> dict[str, Any]:
    """Keep only the device fields the integration needs."""
    return {
        "guid": device["guid"],
        "name": device.get("name") or device["guid"],
        "dt": device.get("dt"),
        "dc": device.get("dc"),
        "deviceSource": device.get("deviceSource"),
        "isConnected": device.get("isConnected"),
    }
