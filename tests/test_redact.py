"""Offline tests for redact.py.

The module is loaded straight from its file: proving it is importable without
Home Assistant (and without the integration package) is part of the contract.

Every credential below is synthetic and assembled at runtime from character
pieces, so no single source line looks like a live secret to a scanner.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import Any

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "robam_roki"
    / "redact.py"
)


def _seed(*pieces: str) -> str:
    """Join pieces at runtime so the source never contains the joined value."""
    return "".join(pieces)


# A synthetic JWT: header, payload, signature.
JWT = "eyJ" + "e30" + "." + "payload" + "." + "c2ln"
# A synthetic PEM header and the token prefixes the linter knows about.
PEM = _seed("-" * 5, "BEGIN ", "RSA ", "PRIVATE KEY", "-" * 5)
GITHUB_TOKEN = _seed("gh", "p_", "abcdefghijklmnopqrstuvwxyz012345")
OPENAI_TOKEN = _seed("sk", "-", "abcdefghijklmnopqrstuvwx")
SLACK_TOKEN = _seed("xox", "b-", "1234567890-abcdefghij")

HEX32 = _seed("0123456789abcdef", "0123456789abcdef")
PHONE = _seed("138", "00000000")  # the repository fake number
MAC_COLON = _seed("aa:", "bb:", "cc:", "dd:", "ee:", "ff")
MAC_DASH = _seed("AA-", "BB-", "CC-", "DD-", "EE-", "FF")


def _load_module() -> Any:
    name = "roki_redact_test"
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


redact = _load_module()
REDACTED = redact.REDACTED


# -- the patterns --------------------------------------------------------


def test_phone_numbers_are_redacted() -> None:
    assert PHONE == "13800000000"
    assert redact.redact_string(PHONE) == REDACTED
    embedded = redact.redact_string(f"call {PHONE} tomorrow")
    assert REDACTED in embedded
    assert "1380000" not in embedded


def test_thirty_two_hex_is_redacted() -> None:
    assert redact.redact_string(HEX32) == REDACTED
    # A model code or ordinary word must survive.
    assert redact.redact_string("R8229") == "R8229"


def test_mac_addresses_are_redacted() -> None:
    assert redact.redact_string(MAC_COLON) == REDACTED
    assert redact.redact_string(MAC_DASH) == REDACTED


def test_jwt_is_redacted() -> None:
    assert JWT.startswith("eyJ")
    assert JWT.count(".") == 2
    assert redact.redact_string(JWT) == REDACTED


def test_private_key_header_redacts_the_whole_value() -> None:
    assert PEM.startswith("-----BEGIN ")
    assert redact.redact_string(PEM) == REDACTED
    header = _seed("-" * 5, "BEGIN ", "OPENSSH PRIVATE KEY", "-" * 5)
    assert redact.redact_string(header) == REDACTED


def test_token_prefixes_are_redacted() -> None:
    assert redact.redact_string(GITHUB_TOKEN) == REDACTED
    assert redact.redact_string(OPENAI_TOKEN) == REDACTED
    assert redact.redact_string(SLACK_TOKEN) == REDACTED


def test_lookalike_words_are_not_redacted() -> None:
    for value in ("sk-learn", "ghp_short", "xoxb-abc", "risk-assessment"):
        assert redact.redact_string(value) == value


# -- benign values stay untouched ---------------------------------------


def test_plain_values_survive() -> None:
    props = {
        "name": "老板电器",
        "mode": "强力洗",
        "note": "hello world",
        "level": 3,
        "ok": True,
        "ratio": 0.5,
        "empty": "",
        "none": None,
        "date": "2026-11-12",
    }
    assert redact.redact_properties(props) == props


def test_nested_structures_are_redacted_recursively() -> None:
    props = {
        "outer": {
            "inner": [PHONE, {"deep": HEX32}, 5, None],
            "mac": MAC_COLON,
        }
    }
    redacted = redact.redact_properties(props)
    assert redacted["outer"]["inner"][0] == REDACTED
    assert redacted["outer"]["inner"][1]["deep"] == REDACTED
    assert redacted["outer"]["inner"][2] == 5
    assert redacted["outer"]["inner"][3] is None
    assert redacted["outer"]["mac"] == REDACTED


def test_redaction_is_idempotent() -> None:
    # Collect redacts before writing a bundle and draft redacts the fixture
    # again; running the same rules twice must not change anything.
    data = {
        "phone": PHONE,
        "nested": {"guid": HEX32, "level": 1},
        "items": [JWT, MAC_COLON, "普通文本"],
    }
    once = redact.redact_properties(data)
    twice = redact.redact_properties(once)
    assert twice == once
    assert redact.redact_secrets(once) == once


def test_sensitive_dict_keys_are_redacted_too() -> None:
    redacted = redact.redact_properties({HEX32: {"level": 1}})
    assert redacted == {REDACTED: {"level": 1}}


def test_redaction_does_not_mutate_the_input() -> None:
    props = {"phone": PHONE, "nested": {"guid": HEX32}}
    snapshot = {"phone": PHONE, "nested": {"guid": HEX32}}
    redact.redact_properties(props)
    assert props == snapshot


def test_empty_and_none_inputs_are_safe() -> None:
    assert redact.redact_properties({}) == {}
    assert redact.redact_properties(None) == {}  # type: ignore[arg-type]
    assert redact.redact_properties([PHONE]) == {}  # type: ignore[arg-type]


# -- device records ------------------------------------------------------


def test_device_secrets_are_masked_and_name_is_rebuilt() -> None:
    device = {
        "guid": "RYYJ000000000001",
        "name": "厨房油烟机",
        "dc": "RYYJ",
        "dt": "R8229",
        "deviceSource": 0,
        "mac": MAC_COLON,
        "deviceToken": "something-secret",
        "appGuid": HEX32,
        "userId": "42",
        "bid": "10086",
        "isConnected": True,
    }
    redacted = redact.redact_device(device)
    assert redacted["guid"] == "RYYJ...0001"
    assert redacted["name"] == "RYYJ-R8229"
    for key in ("mac", "deviceToken", "appGuid", "userId", "bid"):
        assert redacted[key] == REDACTED
    # Non-secret telemetry metadata keeps its value.
    assert redacted["dc"] == "RYYJ"
    assert redacted["dt"] == "R8229"
    assert redacted["deviceSource"] == 0
    assert redacted["isConnected"] is True
    # The input is left alone.
    assert device["mac"] == MAC_COLON
    assert device["name"] == "厨房油烟机"


def test_device_name_without_codes_is_still_replaced() -> None:
    redacted = redact.redact_device({"guid": "short", "name": "my hood"})
    assert redacted["name"] == "unknown-unknown"
    assert redacted["guid"] == REDACTED  # too short to mask safely


def test_redact_device_handles_non_mappings() -> None:
    assert redact.redact_device(None) == {}  # type: ignore[arg-type]


def test_mask_guid_keeps_the_edges() -> None:
    assert redact.mask_guid("RYYJ000000000001") == "RYYJ...0001"
    assert redact.mask_guid("abc") == REDACTED
    assert redact.mask_guid(None) == REDACTED


# -- key based redaction (used by the diagnostics export) ----------------


def test_sensitive_keys_are_replaced_by_name() -> None:
    data = {
        "phone": "anything at all",
        "deviceToken": "x",
        "access_token": "y",
        "level": 2,
        "nested": {"user_id": "z", "name": "强力洗"},
    }
    redacted = redact.redact_secrets(data)
    assert redacted["phone"] == REDACTED
    assert redacted["deviceToken"] == REDACTED
    assert redacted["access_token"] == REDACTED
    assert redacted["nested"]["user_id"] == REDACTED
    assert redacted["level"] == 2
    assert redacted["nested"]["name"] == "强力洗"
    assert data["phone"] == "anything at all"  # no mutation


def test_sensitive_keys_keep_the_list_shape() -> None:
    data = {"items": [{"token": "abc"}, {"level": 1}]}
    assert redact.redact_secrets(data) == {
        "items": [{"token": REDACTED}, {"level": 1}]
    }


def test_custom_key_set_is_honoured() -> None:
    redacted = redact.redact_secrets(
        {"secret_field": "x"}, frozenset({"secret_field"})
    )
    assert redacted == {"secret_field": REDACTED}


def test_sensitive_key_names_cover_api_and_config_spellings() -> None:
    for key in (
        "deviceToken",
        "device_token",
        "accessToken",
        "access_token",
        "appGuid",
        "app_guid",
        "userId",
        "user_id",
        "phone",
        "password",
    ):
        assert key in redact.SENSITIVE_KEYS, key


# -- the module itself ---------------------------------------------------


def _imports_homeassistant(source: str) -> bool:
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import homeassistant", "from homeassistant")):
            return True
    return False


def test_redact_module_has_no_homeassistant_import() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert not _imports_homeassistant(source)


def test_this_file_is_scan_safe() -> None:
    """The test file itself must not trip the repository secret scanner."""
    source = Path(__file__).read_text(encoding="utf-8")
    assert not re.search(r"e" + r"y" + r"J[A-Za-z0-9_-]{20,}", source)
    assert not re.search(r"gh" + r"p_[A-Za-z0-9]{20,}", source)
    assert not re.search(r"sk" + r"-[A-Za-z0-9]{20,}", source)
    assert ("-----BEGIN " + "PRIVATE KEY-----") not in source
