"""Config flow for the ROBAM / ROKI integration."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
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

#: The three entries of the options menu (each one is also a step id).
OPTIONS_MENU_GENERAL = "general"
OPTIONS_MENU_LEARN = "learn"
OPTIONS_MENU_CLEANUP = "cleanup"

#: Fields of the learning step.
CONF_LEARN_DEVICE = "device_id"
CONF_LEARN_DURATION = "duration"
CONF_LEARN_ACTION = "action"
LEARN_ACTION_START = "start"
LEARN_ACTION_STOP = "stop"

#: Field of the cleanup step.
CONF_REMOVE_DRAFTS = "remove_drafts"

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
                    await coordinator.learn_stop(guid or None)
                    return self.async_abort(reason="learn_stopped")
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

    # -- draft cleanup ----------------------------------------------------

    async def async_step_cleanup(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Delete the generated draft profiles, and nothing else.

        Only files matching draft-*.yaml are ever touched: a profile the user
        wrote by hand shares the same directory and must survive.
        """
        directory = self._profile_dir()
        drafts = await _async_in_executor(self.hass, _list_drafts, directory)
        if user_input is not None:
            if not user_input.get(CONF_REMOVE_DRAFTS):
                return self.async_abort(reason="drafts_kept")
            removed = await _async_in_executor(
                self.hass, _remove_drafts, directory
            )
            _LOGGER.info(
                "options flow removed %d draft profile(s)", len(removed)
            )
            return self.async_abort(reason="drafts_removed")

        schema = vol.Schema(
            {vol.Optional(CONF_REMOVE_DRAFTS, default=bool(drafts)): bool}
        )
        return self.async_show_form(
            step_id="cleanup",
            data_schema=schema,
            description_placeholders={
                "drafts": "、".join(drafts) if drafts else "（无）"
            },
        )

    # -- small helpers ----------------------------------------------------

    def _options(self) -> dict[str, Any]:
        entry = getattr(self, "config_entry", None)
        return dict(getattr(entry, "options", None) or {})

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


def _list_drafts(directory: str) -> list[str]:
    """The draft profile file names (never any other profile), sorted."""
    base = Path(directory)
    if not base.is_dir():
        return []
    try:
        return [
            path.name
            for path in sorted(base.glob(DRAFT_GLOB))
            if path.is_file()
        ]
    except OSError as err:
        _LOGGER.debug("could not scan %s: %s", base, err)
        return []


def _remove_drafts(directory: str) -> list[str]:
    """Delete every draft-*.yaml in the directory; return what was removed."""
    removed: list[str] = []
    for name in _list_drafts(directory):
        try:
            (Path(directory) / name).unlink()
        except OSError as err:
            _LOGGER.warning("could not delete the draft %s: %s", name, err)
            continue
        removed.append(name)
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
