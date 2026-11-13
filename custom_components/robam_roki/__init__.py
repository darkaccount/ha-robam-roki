"""Set up the ROBAM / ROKI cloud integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
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
    DOMAIN,
    PLATFORMS,
)
from .coordinator import RokiCoordinator
from .profiles import (
    SOURCE_BUILTIN,
    SOURCE_USER,
    FullProfile,
    load_profile_dir,
    resolve_extends,
)

_LOGGER = logging.getLogger(__name__)

#: ``async_setup`` registers the integration-wide services, but the integration
#: is configured through the UI only: YAML setup is rejected by this schema.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

#: Profiles shipped with the integration; replaced on every update.
_BUILTIN_PROFILES_DIR = Path(__file__).parent / "profiles"
#: User profiles, relative to the Home Assistant configuration directory.
_USER_PROFILES_PATH = ("robam_roki", "profiles")


@dataclass
class RokiRuntimeData:
    """Objects shared by the platforms."""

    client: RokiClient
    coordinator: RokiCoordinator
    options: dict[str, Any]


RokiConfigEntry = ConfigEntry


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Register the integration-wide services.

    Imported lazily so merely importing the package (as the offline tests do)
    does not require the Home Assistant service/notification modules.
    """
    from .services import async_setup_services

    await async_setup_services(hass)
    return True


def _load_profiles(hass: HomeAssistant) -> list[FullProfile]:
    """Load the built-in and user profiles and resolve their extends chains.

    Runs in the executor because it reads the filesystem.  The profile layer
    only ever logs and skips a broken file, and this function still guards the
    whole load, so adding a profile can never stop the integration from setting
    up.
    """
    builtin_dir = _BUILTIN_PROFILES_DIR
    user_dir = Path(hass.config.path(*_USER_PROFILES_PATH))
    try:
        builtin = load_profile_dir(builtin_dir, SOURCE_BUILTIN)
        user = load_profile_dir(user_dir, SOURCE_USER)
        resolved = resolve_extends([*builtin, *user])
    except Exception as err:  # noqa: BLE001 - profiles must never break setup
        _LOGGER.warning(
            "profiles: could not be loaded (%s); continuing without", err
        )
        return []
    # INFO on purpose: a user who put the directory in the wrong place can see
    # where the integration actually looked and that nothing was found there.
    user_label = str(user_dir) if user_dir.is_dir() else f"{user_dir} (missing)"
    _LOGGER.info(
        "profiles: builtin=%d user=%d (builtin=%s, user=%s)",
        len(builtin),
        len(user),
        builtin_dir,
        user_label,
    )
    return resolved


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

    coordinator.profiles = await hass.async_add_executor_job(_load_profiles, hass)

    # Surface fixable profile problems and the experimental-controls warning
    # as repair issues.  A failure here must never stop the setup.
    try:
        from . import repairs

        await repairs.async_refresh_issues(
            hass,
            coordinator,
            user_dir=Path(hass.config.path(*_USER_PROFILES_PATH)),
        )
    except Exception as err:  # noqa: BLE001 - repairs are advisory
        _LOGGER.debug("repairs: could not refresh issues: %s", err)

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
