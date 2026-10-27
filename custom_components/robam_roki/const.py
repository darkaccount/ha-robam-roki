"""Constants for the ROBAM / ROKI integration."""

from __future__ import annotations

DOMAIN = "robam_roki"

CONF_PHONE = "phone"
CONF_PASSWORD = "password"
CONF_DEVICES = "devices"
CONF_USER_ID = "user_id"
CONF_DEVICE_TOKEN = "device_token"
CONF_APP_GUID = "app_guid"
CONF_ACCESS_TOKEN = "access_token"
CONF_TOKEN_TYPE = "token_type"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_TOKEN_EXPIRES_AT = "token_expires_at"

CONF_MAX_LEVEL = "max_level"
CONF_LEVELS = "levels"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_ENABLE_SSE = "enable_sse"

DEFAULT_MAX_LEVEL = 3
DEFAULT_SCAN_INTERVAL = 30
DEFAULT_ENABLE_SSE = True

# Countdown range in minutes.  The protocol carries FanTime as one unsigned
# byte, so 255 is the ceiling; the app's hour+minute picker tops out at
# 3 h 59 min (239), and 240 is used here to land on a round four hours.
# FanCtr8229View.onStartClock passes 0-60 to its NumberPicker, but that is
# only the hood panel entry point, not the app-wide limit.
MAX_COUNTDOWN_MINUTES = 240

# A hood is considered offline after this many unanswered polls.  The app uses
# six consecutive failures (AbsDevice.onCheckConnection) at a 2 s foreground /
# 10 s background poll, i.e. roughly 12-60 s.
OFFLINE_AFTER_POLLS = 6

# Fallback level ladder for models the table below does not know.
DEFAULT_LEVELS = [1, 2, 3]

# Fan level ladders, taken verbatim from the official app.  Each entry maps a
# model code (``dt``) to the ordered list of levels its gear buttons offer.
#
# Sources in com.robam.roki 6.4.0:
#   * FanCtr8229View.init()          -> tags 1, 2, 3, 6  (R.layout.view_8229)
#   * FanCtr9700View.init()          -> tags 1, 2, 3, 6  (R.layout.view_9700)
#   * FanCtr66A2View.onClickLow/High -> 2 / 6
#   * FanCtr5610View.onClickLow/High -> 1 / 6
#   * FanCtr8700View.onClickHigh     -> 6 (two-button 弱档/爆炒 layout)
#   * SpeechDeviceCmdManage.levelList -> {1, 3, 6}
#
# These are static lookups on purpose: probing the device by sending levels
# would start the motor, which is exactly what we must never do on setup.
#
# ``LEVEL_SOURCES`` records how much each row is trusted.  "apk" means the
# numbers were read out of the app's control view but never confirmed on a
# device.  The fan entity publishes this so a user can tell the difference.
#
# R8229 is marked "hardware" on the strength of the hood's owner confirming
# that the panel offers exactly these four settings.  Note the limits of that
# evidence: the owner's confirmation is second-hand for the *protocol* -- only
# level 0 and level 2 have actually been observed coming back in a 131 report.
# Levels 1, 3 and 6 are still app-side knowledge until a `watch --levels` run
# captures them from the device.
MODEL_LEVELS: dict[str, list[int]] = {
    "R8229": [1, 2, 3, 6],
    "R9700": [1, 2, 3, 6],
    "R66A2": [2, 6],
    # The app lists this one without the R prefix (IPlatRokiFamily.all).
    "66A2H": [2, 6],
    "R5610": [1, 6],
    "R8700": [2, 6],
}

LEVEL_SOURCES: dict[str, str] = {
    # Owner-confirmed panel settings; 131-frame observation still pending.
    "R8229": "hardware",
}

# Models whose hood exposes only two gears (a low one and 爆炒).
TWO_GEAR_MODELS = frozenset({"R66A2", "66A2H", "R8700", "R5610"})

# The model codes com.legent.plat.constant.IPlatRokiFamily.all lists.  Used by
# the tests to catch typos in the table above: a row that is not a real model
# code can never match a device.
#
# The list is not always prefixed with R (``66A2H``, ``90B8Z``, ``8230S``...),
# and a device's ``dt`` can be either spelling, so both forms are accepted.
KNOWN_MODEL_CODES: frozenset[str] = frozenset(
    {
        "R8700", "R9700", "R8230", "8230S", "5910S", "8231S", "8230C",
        "R8229", "R66A2", "R5610", "R9W70", "R9B12", "R9B37", "R9B39",
        "RR829", "RR826", "RR075", "RR039", "RR028", "RM509", "RS209",
        "RS226", "RS228", "RS275", "RJ312", "RJ321", "RJ320", "RM526",
        "RC906", "66A2H", "R0003", "90B8Z", "90B8X", "KDC01", "9B30C",
        "HI704", "HS906", "HK906", "R826L", "Q30i1",
    }
)


def levels_for_model(model: str | None) -> list[int]:
    """Return the gear ladder for a hood model.

    Falls back to :data:`DEFAULT_LEVELS` for unknown models.  The table is
    static on purpose -- see the comment above :data:`MODEL_LEVELS`.
    """
    return known_levels_for_model(model) or list(DEFAULT_LEVELS)


def known_levels_for_model(model: str | None) -> list[int]:
    """Return the ladder for a model in :data:`MODEL_LEVELS`, else ``[]``.

    Unlike :func:`levels_for_model` this never invents a ladder, so callers can
    tell "we know this model" from "we are guessing".
    """
    code = (model or "").strip().upper()
    return list(MODEL_LEVELS.get(code, []))


def levels_source_for_model(model: str | None) -> str:
    """How much the ladder for ``model`` is trusted.

    ``hardware`` means a real hood's owner confirmed the settings on the panel,
    ``apk`` means the numbers were only read out of the app, ``option`` is a
    ladder the user typed in, and ``fallback`` is the linear guess for unknown
    models.  See the comment above :data:`MODEL_LEVELS` for what "confirmed"
    does and does not cover.
    """
    code = (model or "").strip().upper()
    if code in LEVEL_SOURCES:
        return LEVEL_SOURCES[code]
    if code in MODEL_LEVELS:
        return "apk"
    return "fallback"

PLATFORMS = ["fan", "light", "sensor", "binary_sensor", "number"]

# Cloud endpoints (see api.py).
BASE_URL = "https://api.myroki.com"
