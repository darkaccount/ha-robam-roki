"""Config flow for the ROBAM / ROKI integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import RokiAuthError, RokiClient, RokiError
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_APP_GUID,
    CONF_DEVICE_TOKEN,
    CONF_DEVICES,
    CONF_ENABLE_SSE,
    CONF_LEVELS,
    CONF_MAX_LEVEL,
    CONF_PASSWORD,
    CONF_PHONE,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL,
    CONF_TOKEN_EXPIRES_AT,
    CONF_TOKEN_TYPE,
    CONF_USER_ID,
    DEFAULT_ENABLE_SSE,
    DEFAULT_LEVELS,
    DEFAULT_MAX_LEVEL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_LEVELS_TEXT = ",".join(str(level) for level in DEFAULT_LEVELS)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PHONE): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class RobamRokiConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the UI setup."""

    VERSION = 1

    def __init__(self) -> None:
        self._client: RokiClient | None = None
        self._pending: dict[str, Any] = {}
        self._devices: list[dict[str, Any]] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            phone = user_input[CONF_PHONE].strip()
            password = user_input[CONF_PASSWORD]
            await self.async_set_unique_id(phone)
            self._abort_if_unique_id_configured()

            session = async_get_clientsession(self.hass)
            client = RokiClient(session, phone, password)
            try:
                await client.async_login()
                user = await client.async_get_user()
                devices = await client.async_get_devices()
                supported = await self._detect_devices(client, devices)
            except RokiAuthError:
                errors["base"] = "invalid_auth"
            except RokiError as err:
                _LOGGER.debug("ROKI setup failed: %s", err)
                errors["base"] = "cannot_connect"
            else:
                if not supported:
                    return self.async_abort(reason="no_supported_devices")

                self._client = client
                self._devices = supported
                self._pending = {
                    CONF_PHONE: phone,
                    CONF_PASSWORD: password,
                    CONF_USER_ID: client.user_id,
                    CONF_DEVICE_TOKEN: client.device_token,
                    CONF_APP_GUID: client.app_guid,
                    CONF_ACCESS_TOKEN: client.access_token,
                    CONF_TOKEN_TYPE: client.token_type,
                    CONF_REFRESH_TOKEN: client.refresh_token,
                    CONF_TOKEN_EXPIRES_AT: client.expires_at,
                    "title": user.get("nickName") or user.get("name") or phone,
                }
                return await self.async_step_devices()

        return self.async_show_form(
            step_id="user", data_schema=USER_SCHEMA, errors=errors
        )

    async def async_step_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            selected = set(user_input[CONF_DEVICES])
            devices = [
                device for device in self._devices if device["guid"] in selected
            ]
            if not devices:
                errors["base"] = "no_devices_selected"
            else:
                data = {
                    **self._pending,
                    CONF_DEVICES: devices,
                }
                data.pop("title", None)
                title = self._pending.get("title") or data[CONF_PHONE]
                return self.async_create_entry(title=title, data=data)

        choices = {
            device["guid"]: f"{device['name']} ({device['guid'][:5]})"
            for device in self._devices
        }
        # The default has to be a list of selected values.  Home Assistant
        # serialises this schema with probatio.to_field_list, which copies each
        # marker's default into the field verbatim and never runs the
        # validator, so a dict here reached the multi_select widget, matched
        # none of its options, and the picker rendered empty.
        first = next(iter(choices))
        default = [first] if len(choices) == 1 else []
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_DEVICES, default=default
                ): cv.multi_select(choices)
            }
        )
        return self.async_show_form(
            step_id="devices", data_schema=schema, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        assert entry is not None
        errors: dict[str, str] = {}

        if user_input is not None:
            session = async_get_clientsession(self.hass)
            client = RokiClient(
                session, entry.data[CONF_PHONE], user_input[CONF_PASSWORD]
            )
            try:
                await client.async_login()
                await client.async_get_user()
            except RokiAuthError:
                errors["base"] = "invalid_auth"
            except RokiError:
                errors["base"] = "cannot_connect"
            else:
                self.hass.config_entries.async_update_entry(
                    entry,
                    data={
                        **entry.data,
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_USER_ID: client.user_id,
                        CONF_ACCESS_TOKEN: client.access_token,
                        CONF_TOKEN_TYPE: client.token_type,
                        CONF_REFRESH_TOKEN: client.refresh_token,
                        CONF_TOKEN_EXPIRES_AT: client.expires_at,
                    },
                )
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            description_placeholders={"phone": entry.data[CONF_PHONE]},
            errors=errors,
        )

    @staticmethod
    async def _detect_devices(
        client: RokiClient, devices: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Query each device and keep the ones that expose hood properties."""
        supported: list[dict[str, Any]] = []
        for device in devices:
            guid = str(device["guid"])
            try:
                properties = await client.async_get_properties(guid)
            except RokiError as err:
                _LOGGER.debug("Skipping %s: %s", guid, err)
                continue
            if not properties:
                continue
            # Hoods expose `level` plus the motor/light state; ovens and
            # heaters only have `powerState`/`workState`, so this keeps them
            # out of the fan entities.
            if "level" not in properties:
                continue
            if not any(
                key in properties
                for key in (
                    "workStatus",
                    "working_status",
                    "lightStatus",
                    "light_status",
                )
            ):
                continue
            supported.append(
                {
                    "guid": guid,
                    "name": device.get("name") or guid,
                    "dt": device.get("dt"),
                    "dc": device.get("dc"),
                    "deviceSource": device.get("deviceSource"),
                    "isConnected": device.get("isConnected"),
                }
            )
        # Prefer cloud (SSE) devices, they are the ones the REST write API serves.
        supported.sort(key=lambda item: item.get("deviceSource") != 1)
        return supported

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return RobamRokiOptionsFlow()


class RobamRokiOptionsFlow(OptionsFlow):
    """Tune polling, fan levels and push updates."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            # A blank level list means "use the model table"; anything else
            # must be a clean ascending ladder.
            raw_levels = str(user_input.get(CONF_LEVELS) or "").strip()
            if raw_levels:
                levels = _parse_levels(raw_levels)
                if not levels:
                    errors["base"] = "invalid_levels"
                else:
                    user_input = {
                        **user_input,
                        CONF_LEVELS: ",".join(str(level) for level in levels),
                    }
                    return self.async_create_entry(title="", data=user_input)
            else:
                user_input = {k: v for k, v in user_input.items() if k != CONF_LEVELS}
                return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_LEVELS,
                    default=options.get(CONF_LEVELS, DEFAULT_LEVELS_TEXT),
                ): str,
                vol.Optional(
                    CONF_MAX_LEVEL,
                    default=int(options.get(CONF_MAX_LEVEL, DEFAULT_MAX_LEVEL)),
                ): vol.All(vol.Coerce(int), vol.Range(min=2, max=6)),
                vol.Optional(
                    CONF_SCAN_INTERVAL,
                    default=int(
                        options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=10, max=600)),
                vol.Optional(
                    CONF_ENABLE_SSE,
                    default=bool(options.get(CONF_ENABLE_SSE, DEFAULT_ENABLE_SSE)),
                ): bool,
            }
        )
        return self.async_show_form(
            step_id="init", data_schema=schema, errors=errors
        )


def _parse_levels(text: Any) -> list[int]:
    """Parse a comma separated ladder such as ``1,2,3,6``.

    Empty means "use the model table", so a blank field is accepted.
    """
    if text is None:
        return []
    if isinstance(text, (list, tuple)):
        chunks: list[Any] = list(text)
    else:
        chunks = str(text).replace("，", ",").split(",")
    levels: list[int] = []
    for chunk in chunks:
        chunk = str(chunk).strip()
        if not chunk:
            continue
        try:
            number = int(chunk)
        except ValueError:
            return []
        if number <= 0 or number in levels:
            return []
        levels.append(number)
    return sorted(levels)
