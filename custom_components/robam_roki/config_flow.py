"""Config flow for the ROBAM / ROKI integration."""

from __future__ import annotations

import logging
import types
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import DeviceSelector, DeviceSelectorConfig

from .api import RokiApiError, RokiAuthError, RokiClient, RokiError
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_ALLOW_UNVERIFIED_CONTROLS,
    CONF_APP_GUID,
    CONF_COUNTDOWN_AUTO_OFF,
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
    DEFAULT_ALLOW_UNVERIFIED_CONTROLS,
    DEFAULT_COUNTDOWN_AUTO_OFF,
    DEFAULT_ENABLE_SSE,
    DEFAULT_LEVELS,
    DEFAULT_MAX_LEVEL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    LEARN_DEFAULT_SECONDS,
    LEARN_MAX_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_LEVELS_TEXT = ",".join(str(level) for level in DEFAULT_LEVELS)

# -- options flow menu ---------------------------------------------------

#: The entries of the options menu (each one is also a step id).  Only the
#: self-service items are numbered, and their labels live in the translations.
OPTIONS_MENU_GENERAL = "general"
OPTIONS_MENU_LEARN = "learn"
OPTIONS_MENU_DRAFT = "draft"
OPTIONS_MENU_EXPORT = "export"
OPTIONS_MENU_CLEANUP = "cleanup"

#: Fields of the learning step.
CONF_LEARN_DEVICE = "device_id"
CONF_LEARN_DURATION = "duration"
CONF_LEARN_ACTION = "action"
LEARN_ACTION_START = "start"
LEARN_ACTION_STOP = "stop"

#: Device picker field shared by the learn, draft and export steps.
CONF_DRAFT_DEVICE = "device_id"

#: Field of the cleanup step.
CONF_REMOVE_DRAFTS = "remove_drafts"
CONF_REMOVE_FIXTURES = "remove_fixtures"
CONF_REMOVE_EXPORTS = "remove_exports"

#: Fields of the draft confirmation step.
CONF_ENABLE_UNVERIFIED = "enable_unverified_controls"
CONF_RELOAD_NOW = "reload_now"

#: Draft profiles live next to the hand-written ones; the same layout the
#: draft_profile service writes with its CONFIG_SUBDIR/PROFILES_SUBDIR.
_DRAFTS_PATH = ("robam_roki", "profiles")
DRAFT_GLOB = "draft-*.yaml"

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
        """Query each device and keep the ones that expose readable properties.

        The old gate required ``level``, which only range hoods report, so an
        account that owns nothing but an oven aborted the flow with
        ``no_supported_devices``.  Every device that answers with properties and
        identifies its category is accepted now; the fan/light/number platforms
        still filter on ``dc`` so a non-hood appliance cannot grow a fan entity,
        and devices without ``dc`` keep the old behaviour (accepted only when
        they look like a hood).
        """
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
            has_category = bool(str(device.get("dc") or "").strip())
            if not has_category and "level" not in properties:
                # No category and no hood markers: nothing we could drive.
                _LOGGER.debug(
                    "Skipping %s (%s): no dc and no level, keys=%s",
                    guid,
                    device.get("name"),
                    sorted(properties),
                )
                continue
            _LOGGER.debug(
                "Detected %s (%s) dc=%s dt=%s keys=%s",
                device.get("name"),
                guid,
                device.get("dc"),
                device.get("dt"),
                sorted(properties),
            )
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
    """Options menu: general settings, learning mode and draft cleanup.

    A single long form made the two most useful maintenance actions (learning
    a new device, throwing away stale drafts) invisible: both were services or
    plain filesystem chores.  The options flow is a menu now, one entry per job.
    """

    # -- menu -------------------------------------------------------------

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Top level: pick what to change."""
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                OPTIONS_MENU_GENERAL,
                OPTIONS_MENU_LEARN,
                OPTIONS_MENU_DRAFT,
                OPTIONS_MENU_EXPORT,
                OPTIONS_MENU_CLEANUP,
            ],
        )

    # -- general settings -------------------------------------------------

    async def async_step_general(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Polling, fan levels, push updates and the experimental gate."""
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
                user_input = {
                    key: value
                    for key, value in user_input.items()
                    if key != CONF_LEVELS
                }
                return self.async_create_entry(title="", data=user_input)

        options = self._options()
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
                vol.Optional(
                    CONF_COUNTDOWN_AUTO_OFF,
                    default=bool(
                        options.get(
                            CONF_COUNTDOWN_AUTO_OFF,
                            DEFAULT_COUNTDOWN_AUTO_OFF,
                        )
                    ),
                ): bool,
                vol.Optional(
                    CONF_ALLOW_UNVERIFIED_CONTROLS,
                    default=bool(
                        options.get(
                            CONF_ALLOW_UNVERIFIED_CONTROLS,
                            DEFAULT_ALLOW_UNVERIFIED_CONTROLS,
                        )
                    ),
                ): bool,
            }
        )
        return self.async_show_form(
            step_id="general", data_schema=schema, errors=errors
        )

    # -- learning mode ----------------------------------------------------

    async def async_step_learn(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Start or finish a passive learning session from the UI.

        Learning never touches the options: it only drives the coordinator.
        The flow aborts when it is done so no half-filled form stays open.
        """
        errors: dict[str, str] = {}
        coordinator = self._coordinator()
        if user_input is not None:
            action = str(user_input.get(CONF_LEARN_ACTION) or LEARN_ACTION_START)
            guid = self._guid_for_selection(
                str(user_input.get(CONF_LEARN_DEVICE) or "")
            )
            if coordinator is None:
                return self.async_abort(reason="learn_failed")
            try:
                if action == LEARN_ACTION_STOP:
                    # No device given stops whichever session is running.
                    result = await coordinator.learn_stop(guid or None)
                    actions = (result or {}).get("actions") or []
                    # Learning never writes files: the user decides in step 2
                    # whether the session should become a draft.
                    return self.async_abort(
                        reason="learn_stopped",
                        description_placeholders={"count": str(len(actions))},
                    )
                if not guid:
                    errors["base"] = "device_required"
                else:
                    duration = _clamp_duration(
                        user_input.get(CONF_LEARN_DURATION)
                    )
                    coordinator.learn_start(guid, duration)
                    return self.async_abort(reason="learn_started")
            except RokiApiError as err:
                _LOGGER.debug("learning from the options flow failed: %s", err)
                return self.async_abort(reason="learn_failed")

        schema = vol.Schema(
            {
                vol.Optional(CONF_LEARN_DEVICE): DeviceSelector(
                    DeviceSelectorConfig(integration=DOMAIN)
                ),
                vol.Optional(
                    CONF_LEARN_DURATION, default=LEARN_DEFAULT_SECONDS
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=1, max=LEARN_MAX_SECONDS),
                ),
                vol.Required(
                    CONF_LEARN_ACTION, default=LEARN_ACTION_START
                ): vol.In([LEARN_ACTION_START, LEARN_ACTION_STOP]),
            }
        )
        return self.async_show_form(
            step_id="learn",
            data_schema=schema,
            description_placeholders={"status": _learn_status_text(coordinator)},
            errors=errors,
        )

    # -- step 2: generate a profile draft ---------------------------------

    async def async_step_draft(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Generate a profile draft for one device, then confirm what to do."""
        errors: dict[str, str] = {}
        coordinator = self._coordinator()
        if user_input is not None:
            guid = self._guid_for_selection(
                str(user_input.get(CONF_DRAFT_DEVICE) or "")
            )
            if coordinator is None:
                return self.async_abort(reason="draft_failed")
            if not guid:
                errors["base"] = "device_required"
            else:
                draft = await self._async_create_draft(coordinator, guid)
                if draft is None:
                    return self.async_abort(reason="draft_failed")
                self._draft = draft
                return await self.async_step_draft_confirm()

        return self.async_show_form(
            step_id="draft",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_DRAFT_DEVICE): _device_selector(),
                }
            ),
            description_placeholders={"learned": _learn_summary(coordinator)},
            errors=errors,
        )

    async def async_step_draft_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm a draft; the form depends on the experimental switch."""
        if getattr(self, "_draft", None) is None:
            return await self.async_step_init()
        if user_input is not None:
            return await self._async_finish_draft(user_input)
        if self._allow_unverified():
            # Already allowed: only the reload decision is left.
            return await self.async_step_draft_confirm_enabled()
        fields: dict[Any, Any] = {
            vol.Optional(CONF_ENABLE_UNVERIFIED, default=False): bool,
            vol.Optional(CONF_RELOAD_NOW, default=True): bool,
        }
        return self.async_show_form(
            step_id="draft_confirm",
            data_schema=vol.Schema(fields),
            description_placeholders=self._draft_placeholders(),
        )

    async def async_step_draft_confirm_enabled(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm a draft for a user who already allows unverified controls."""
        if getattr(self, "_draft", None) is None:
            return await self.async_step_init()
        if user_input is not None:
            return await self._async_finish_draft(user_input)
        return self.async_show_form(
            step_id="draft_confirm_enabled",
            data_schema=vol.Schema(
                {vol.Optional(CONF_RELOAD_NOW, default=True): bool}
            ),
            description_placeholders=self._draft_placeholders(),
        )

    async def _async_create_draft(
        self, coordinator: Any, guid: str
    ) -> dict[str, Any] | None:
        """Build the draft with the service helpers and write it in the executor.

        The draft generation lives in services.py (it needs the driver to
        compute the fixture expectations); importing it here keeps this module
        import-light and avoids a second implementation.
        """
        services = _services()
        learned_fn = getattr(coordinator, "learned_controls", None)
        learned = list(learned_fn(guid) or []) if callable(learned_fn) else []
        info = getattr(coordinator, "device_info_for", None)
        device = info(guid) if callable(info) else {"guid": guid}
        device = dict(device or {"guid": guid})
        cached = (coordinator.data or {}).get(guid) or {}
        driver = services._build_driver(coordinator, device)
        builtin_driver, builtin_keys = services._builtin_driver(driver)

        def expect_fn(properties: dict[str, Any]) -> dict[str, Any]:
            return services._expect_values(driver, properties)

        result = services.draft_from_properties(
            device,
            cached,
            expect_fn=expect_fn,
            builtin_driver=builtin_driver,
            builtin_keys=builtin_keys,
            learned_controls=learned,
        )
        if not getattr(result, "profile_yaml", ""):
            return None
        match = (result.fixture or {}).get("match") or {}
        dc = str(match.get("dc") or "unknown")
        dt = str(match.get("dt") or "unknown")
        try:
            written = await _async_in_executor(
                self.hass,
                services._write_draft,
                str(self.hass.config.path()),
                dc,
                dt,
                result.profile_yaml,
                result.fixture,
                True,
            )
        except OSError as err:
            _LOGGER.warning("options flow could not write the draft: %s", err)
            return None
        profile_path = str(written[0])
        old_drafts = list(written[2] if len(written) > 2 else [])
        controls = services.controls_from_actions(learned)
        return {
            "file": Path(profile_path).name,
            "path": profile_path,
            "learned_groups": len(learned),
            "controls": len(controls),
            "old_drafts": old_drafts,
        }

    def _draft_placeholders(self) -> dict[str, str]:
        draft = getattr(self, "_draft", None) or {}
        controls = int(draft.get("controls") or 0)
        if controls:
            learned = f"学习结果已写入：{controls} 条 controls（verified: false）"
        else:
            learned = "没有可用的学习结果：这份草稿只有只读传感器"
        note = ""
        if draft.get("old_drafts"):
            note = (
                f"同名草稿已存在，本次另存为 {draft.get('file', '')}"
                "（旧文件不会被覆盖）"
            )
        return {
            "file": str(draft.get("file") or ""),
            "controls": learned,
            "note": note,
        }

    async def _async_finish_draft(
        self, user_input: dict[str, Any]
    ) -> ConfigFlowResult:
        """Apply the confirmation: maybe enable the switch, maybe reload."""
        draft = getattr(self, "_draft", None) or {}
        enable = bool(user_input.get(CONF_ENABLE_UNVERIFIED))
        reload_now = bool(user_input.get(CONF_RELOAD_NOW, True))
        placeholders = {"file": str(draft.get("file") or "")}
        entry = self._entry()
        if enable and entry is not None and not self._allow_unverified():
            options = dict(getattr(entry, "options", None) or {})
            options[CONF_ALLOW_UNVERIFIED_CONTROLS] = True
            self.hass.config_entries.async_update_entry(entry, options=options)
        if enable:
            # Updating the options already reloads the entry through its update
            # listener; only reload here when the entry has no listener (tests or
            # an older setup), otherwise HA would set up twice.
            listeners = getattr(entry, "update_listeners", ()) if entry else ()
            if not listeners:
                await self._async_reload_entry()
            return self.async_abort(
                reason="draft_enabled",
                description_placeholders=placeholders,
            )
        if reload_now:
            await self._async_reload_entry()
            return self.async_abort(
                reason="draft_reloaded",
                description_placeholders=placeholders,
            )
        return self.async_abort(
            reason="draft_written", description_placeholders=placeholders
        )

    # -- step 3: export a contribution package ----------------------------

    async def async_step_export(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Export the device's user profile as the Markdown contribution file."""
        errors: dict[str, str] = {}
        coordinator = self._coordinator()
        if user_input is not None:
            guid = self._guid_for_selection(
                str(user_input.get(CONF_DRAFT_DEVICE) or "")
            )
            if coordinator is None:
                return self.async_abort(reason="export_failed")
            if not guid:
                errors["base"] = "device_required"
            else:
                services = _services()
                info = getattr(coordinator, "device_info_for", None)
                device = (
                    info(guid) if callable(info) else {"guid": guid}
                )
                profile = services._active_profile(
                    coordinator, dict(device or {"guid": guid})
                )
                if profile is None:
                    return self.async_abort(reason="export_no_profile")
                if services._is_builtin_profile(profile):
                    return self.async_abort(reason="export_builtin")
                try:
                    await services._async_export_profile(
                        self.hass, types.SimpleNamespace(data={"guid": guid})
                    )
                except ServiceValidationError as err:
                    _LOGGER.debug("options flow could not export: %s", err)
                    return self.async_abort(reason="export_failed")
                return self.async_abort(reason="export_done")

        return self.async_show_form(
            step_id="export",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_DRAFT_DEVICE): _device_selector(),
                }
            ),
            errors=errors,
        )

    # -- draft cleanup ----------------------------------------------------

    async def async_step_cleanup(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Delete the generated artefacts the user selects.

        Drafts are self-contained files, but a learning session also leaves a
        fixture and the export bundle behind; each group is listed and only
        the ticked ones are removed, so nothing the user hand-wrote is ever
        touched (drafts match draft-*.yaml, and the other two live in their
        own directories).
        """
        directory = self._profile_dir()
        if user_input is not None:
            selected = any(
                bool(user_input.get(key))
                for key in (
                    CONF_REMOVE_DRAFTS,
                    CONF_REMOVE_FIXTURES,
                    CONF_REMOVE_EXPORTS,
                )
            )
            if not selected:
                return self.async_abort(reason="drafts_kept")
            removed = await _async_in_executor(
                self.hass,
                _remove_selected,
                directory,
                bool(user_input.get(CONF_REMOVE_DRAFTS)),
                bool(user_input.get(CONF_REMOVE_FIXTURES)),
                bool(user_input.get(CONF_REMOVE_EXPORTS)),
            )
            _LOGGER.info(
                "options flow removed %d draft(s), %d fixture(s), %d export(s)",
                removed["drafts"],
                removed["fixtures"],
                removed["exports"],
            )
            return self.async_abort(
                reason="drafts_removed",
                description_placeholders={
                    "drafts": str(removed["drafts"]),
                    "fixtures": str(removed["fixtures"]),
                    "exports": str(removed["exports"]),
                },
            )

        listings = await _async_in_executor(
            self.hass, _list_artefacts, directory
        )
        drafts = listings["drafts"]
        fixtures = listings["fixtures"]
        exports = listings["exports"]
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_REMOVE_DRAFTS, default=bool(drafts)
                ): bool,
                vol.Optional(
                    CONF_REMOVE_FIXTURES, default=False
                ): bool,
                vol.Optional(
                    CONF_REMOVE_EXPORTS, default=False
                ): bool,
            }
        )
        return self.async_show_form(
            step_id="cleanup",
            data_schema=schema,
            description_placeholders={
                "drafts": "、".join(drafts) if drafts else "（无）",
                "fixtures": "、".join(fixtures) if fixtures else "（无）",
                "exports": "、".join(exports) if exports else "（无）",
            },
        )

    # -- small helpers ----------------------------------------------------

    def _entry(self) -> Any | None:
        return getattr(self, "config_entry", None)

    def _options(self) -> dict[str, Any]:
        return dict(getattr(self._entry(), "options", None) or {})

    def _allow_unverified(self) -> bool:
        """Whether the experimental-controls option is already on."""
        return bool(
            self._options().get(
                CONF_ALLOW_UNVERIFIED_CONTROLS,
                DEFAULT_ALLOW_UNVERIFIED_CONTROLS,
            )
        )

    async def _async_reload_entry(self) -> bool:
        """Reload the entry so the freshly written profile takes effect."""
        entry = self._entry()
        reload = getattr(
            getattr(self.hass, "config_entries", None), "async_reload", None
        )
        if entry is None or not callable(reload):
            return False
        await reload(getattr(entry, "entry_id", ""))
        return True

    def _coordinator(self) -> Any | None:
        """The coordinator of the entry this options flow belongs to."""
        entry = getattr(self, "config_entry", None)
        runtime = getattr(entry, "runtime_data", None)
        return getattr(runtime, "coordinator", None)

    def _profile_dir(self) -> str:
        """The user profile directory (same layout as services.py)."""
        return self.hass.config.path(*_DRAFTS_PATH)

    def _guid_for_selection(self, value: str) -> str:
        """Map a device-registry id from the DeviceSelector to the ROKI guid.

        The selector returns Home Assistant device ids; the coordinator speaks
        ROKI guids.  The value is returned unchanged when it already is one (or
        when no registry is available, as in the offline tests).
        """
        selected = str(value or "").strip()
        if not selected:
            return ""
        getter = getattr(dr, "async_get", None)
        if not callable(getter):
            return selected
        device_entry = getter(self.hass).async_get(selected)
        identifiers = getattr(device_entry, "identifiers", None) or ()
        try:
            pairs = list(identifiers)
        except TypeError:  # a harness stub instead of a real device entry
            return selected
        for domain, identifier in pairs:
            if domain == DOMAIN:
                return str(identifier)
        return selected


# -- module helpers used by the options flow -----------------------------


def _device_selector() -> Any:
    """The limited device picker shared by the draft and export steps."""
    return DeviceSelector(DeviceSelectorConfig(integration=DOMAIN))


def _services() -> Any:
    """Import the service helpers lazily.

    The draft and export steps reuse services.py (the driver, the fixture
    expectations, the file writer, the export composition) instead of growing a
    second implementation.  The import stays inside the call so importing this
    module never drags the whole service layer in.
    """
    from . import services

    return services


def _learn_summary(coordinator: Any) -> str:
    """What step 2 will use, shown above the device picker."""
    active = getattr(coordinator, "learn_active", None)
    if callable(active) and active():
        return (
            "学习会话仍在进行：先在 1. 里选「结束」，草稿才会带上刚学到的动作"
        )
    groups = getattr(coordinator, "_learned_groups", None)
    if isinstance(groups, dict) and any(groups.values()):
        return "内存中已有学习结果：选中对应设备后草稿会带上 controls"
    return (
        "当前没有可用的学习结果：生成的是只读草稿"
        "（可先做 1. 学习新设备控制）"
    )


def _async_in_executor(hass: Any, target: Any, *args: Any) -> Any:
    """Run one blocking helper off the event loop (an awaitable is returned).

    A harness double without an executor calls the helper directly: there is
    no event loop there to block.
    """
    add_executor_job = getattr(hass, "async_add_executor_job", None)
    if callable(add_executor_job):
        return add_executor_job(target, *args)
    return _as_awaitable(target(*args))


async def _as_awaitable(value: Any) -> Any:
    """Wrap a direct helper result so the caller can always await it."""
    return value


def _artefact_dirs(directory: str) -> tuple[Path, Path]:
    """The fixtures/ and exports/ siblings of the profiles directory."""
    base = Path(directory)
    return base.parent / "fixtures", base.parent / "exports"


def _list_drafts(directory: str) -> list[str]:
    """The draft profile file names (never any other profile), sorted."""
    return _list_files(Path(directory), DRAFT_GLOB)


def _list_files(directory: Path, pattern: str) -> list[str]:
    if not directory.is_dir():
        return []
    try:
        return [
            path.name
            for path in sorted(directory.glob(pattern))
            if path.is_file()
        ]
    except OSError as err:
        _LOGGER.debug("could not scan %s: %s", directory, err)
        return []


def _remove_drafts(directory: str) -> list[str]:
    """Delete every draft-*.yaml in the directory; return what was removed."""
    drafts = _list_drafts(directory)
    _remove_artefacts(directory, drafts, [], [])
    return [name for name in drafts if not (Path(directory) / name).exists()]


def _list_artefacts(directory: str) -> dict[str, list[str]]:
    """Drafts, fixtures and export bundles, grouped for the cleanup form."""
    fixtures_dir, exports_dir = _artefact_dirs(directory)
    return {
        "drafts": _list_drafts(directory),
        "fixtures": _list_files(fixtures_dir, "*.json"),
        "exports": _list_files(exports_dir, "*.md"),
    }


def _remove_selected(
    directory: str,
    drafts: bool,
    fixtures: bool,
    exports: bool,
) -> dict[str, int]:
    """Delete the ticked groups; listing happens inside the executor."""
    listings = _list_artefacts(directory)
    return _remove_artefacts(
        directory,
        listings["drafts"] if drafts else [],
        listings["fixtures"] if fixtures else [],
        listings["exports"] if exports else [],
    )


def _remove_artefacts(
    directory: str,
    drafts: list[str],
    fixtures: list[str],
    exports: list[str],
) -> dict[str, int]:
    """Delete the listed files; return how many of each group went away."""
    fixtures_dir, exports_dir = _artefact_dirs(directory)
    removed = {"drafts": 0, "fixtures": 0, "exports": 0}
    targets = [
        ("drafts", Path(directory)),
        ("fixtures", fixtures_dir),
        ("exports", exports_dir),
    ]
    names = {"drafts": drafts, "fixtures": fixtures, "exports": exports}
    for group, base in targets:
        for name in names[group]:
            try:
                (base / name).unlink()
            except OSError as err:
                _LOGGER.warning("could not delete %s: %s", name, err)
                continue
            removed[group] += 1
    return removed


def _clamp_duration(value: Any) -> int:
    """A learning duration inside the protocol window, defaulting to 300 s."""
    try:
        duration = int(value)
    except (TypeError, ValueError):
        return LEARN_DEFAULT_SECONDS
    return max(1, min(LEARN_MAX_SECONDS, duration))


def _learn_remaining_seconds(
    state: dict[str, Any], now: datetime | None = None
) -> int | None:
    """Seconds left in a learning session, or None when it has no deadline."""
    deadline = state.get("deadline")
    if not isinstance(deadline, datetime):
        return None
    current = now or datetime.now(UTC)
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    return max(0, int((deadline - current).total_seconds()))


def _learn_status_text(coordinator: Any) -> str:
    """The session line the learn step shows: device name and time left."""
    active = getattr(coordinator, "learn_active", None)
    if not callable(active) or not active():
        return "当前没有学习会话"
    state = getattr(coordinator, "_learn", None)
    if not isinstance(state, dict):
        return "学习进行中"
    guid = str(state.get("guid") or "")
    name = guid
    device_info = getattr(coordinator, "device_info_for", None)
    if callable(device_info) and guid:
        name = str((device_info(guid) or {}).get("name") or guid)
    remaining = _learn_remaining_seconds(state)
    if remaining is None:
        return f"{name}：学习进行中"
    return f"{name}：剩余 {remaining} 秒"


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
