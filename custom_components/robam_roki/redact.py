"""Privacy redaction helpers shared by diagnostics, fixtures and the CLI.

Everything here is pure Python: the CLI loads this module straight from the
file system and the tests import it without Home Assistant, so it must stay
free of homeassistant imports (and of any third-party dependency).

The same rules are used in three places on purpose -- the HA diagnostics
export, the CLI collection bundle and the draft fixture writer -- so the
privacy bar cannot drift apart between them.
"""

from __future__ import annotations

import re
from typing import Any

#: The marker every redacted value is replaced with.
REDACTED = "**REDACTED**"

#: Key names whose values are replaced by name, whatever the value looks like.
#: Both the raw config-entry spellings (snake_case) and the cloud API spellings
#: (camelCase) are listed so the integration and the CLI cannot drift apart.
SENSITIVE_KEYS = frozenset(
    {
        "guid",
        "mac",
        "bid",
        "token",
        "password",
        "phone",
        "deviceToken",
        "appGuid",
        "userId",
        "accessToken",
        "refreshToken",
        "ownerId",
        "device_token",
        "app_guid",
        "user_id",
        "access_token",
        "refresh_token",
    }
)

#: Backwards-friendly alias: device records use the same policy.
DEVICE_SECRET_KEYS = SENSITIVE_KEYS

#: Ordered from the most specific to the most generic pattern so a JWT is not
#: half-eaten by the 32-hex rule first.
_PATTERNS: tuple[re.Pattern[str], ...] = (
    # JWTs: three dot separated base64url segments, always starting with eyJ.
    re.compile(r"(?<![\w-])eyJ[\w-]*\.[\w-]+\.[\w-]+(?![\w-])"),
    # GitHub, OpenAI and Slack style tokens.
    re.compile(r"(?<![\w-])gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"(?<![\w-])sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"(?<![\w-])xox[baprs]-[A-Za-z0-9-]{10,}"),
    # 32 hex characters: hardware GUIDs, device codes and most API secrets.
    re.compile(r"(?<![0-9A-Za-z])[0-9A-Fa-f]{32}(?![0-9A-Za-z])"),
    # MAC addresses with colons or dashes.
    re.compile(r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:])"),
    re.compile(r"(?<![0-9A-Fa-f-])(?:[0-9A-Fa-f]{2}-){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f-])"),
    # Mainland Chinese mobile numbers.
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
)


def mask_guid(guid: Any) -> str:
    """Mask a hardware GUID keeping only the first and last four characters.

    This mirrors what the HA diagnostics export has always done, so switching
    it over to this module does not change the published JSON.
    """
    if not guid:
        return REDACTED
    text = str(guid)
    if len(text) < 8:
        return REDACTED
    return f"{text[:4]}...{text[-4:]}"


def redact_string(value: str) -> str:
    """Replace every sensitive substring of one string.

    A private key header redacts the whole string: a PEM body must never be
    partially shared, and nobody can use half a key.
    """
    if "-----BEGIN " in value:
        return REDACTED
    for pattern in _PATTERNS:
        value = pattern.sub(REDACTED, value)
    return value


def _redact_value(value: Any) -> Any:
    """Recursively redact one value, keeping the container shape."""
    if isinstance(value, str):
        return redact_string(value)
    if isinstance(value, dict):
        return {
            _redact_value(key) if isinstance(key, str) else key: _redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    return value


def redact_properties(props: dict[str, Any]) -> dict[str, Any]:
    """Redact a property mapping, recursing through nested dicts and lists.

    Returns a new mapping; the input is never mutated.  Anything that is not a
    dict is treated as an empty property set instead of raising, so a broken
    capture can be dropped without aborting a draft.
    """
    if not isinstance(props, dict):
        return {}
    redacted = _redact_value(props)
    return redacted if isinstance(redacted, dict) else {}


def redact_secrets(
    data: Any, keys: frozenset[str] = SENSITIVE_KEYS
) -> Any:
    """Replace every value stored under a sensitive key, recursively.

    This is the key-name half of the policy (the diagnostics export uses it to
    redact credentials whose shape is unknown); redact_properties is the
    value-pattern half.  Lists keep their shape, everything else is masked.
    """
    if isinstance(data, dict):
        redacted: dict[Any, Any] = {}
        for key, value in data.items():
            if key in keys:
                redacted[key] = REDACTED
            else:
                redacted[key] = redact_secrets(value, keys)
        return redacted
    if isinstance(data, (list, tuple)):
        return [redact_secrets(item, keys) for item in data]
    return data


def redact_device(device: dict[str, Any]) -> dict[str, Any]:
    """Redact one device record: secrets masked, name replaced by model codes.

    The name is rebuilt from dc/dt because vendor names often carry the room or
    the owner's nickname; the model pair is what a profile actually matches on.
    """
    if not isinstance(device, dict):
        return {}
    cleaned = redact_secrets(redact_properties(device))
    if "guid" in device:
        cleaned["guid"] = mask_guid(device.get("guid"))
    dc = str(device.get("dc") or "").strip() or "unknown"
    dt = str(device.get("dt") or "").strip() or "unknown"
    cleaned["name"] = f"{dc}-{dt}"
    return cleaned
