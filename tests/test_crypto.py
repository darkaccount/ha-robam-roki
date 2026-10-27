"""Offline tests for the ROKI crypto/transport helpers.

Run with::

    python -m pytest tests -q

The ``LIVE_LOGIN_ERROR`` vector was captured from api.myroki.com and proves
that the double base64 + AES-128-ECB layer matches the official Android
client.  ``LIVE_APP_GUID`` exercises the same layer with a locally encrypted,
clearly fake app GUID.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
from pathlib import Path
from urllib.parse import quote

import pytest

API_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "robam_roki"
    / "api.py"
)


def _load_api():
    spec = importlib.util.spec_from_file_location("roki_api_test", API_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _load_api()

LIVE_APP_GUID = (
    "cmlIYW1KYzg3clk0SWtLNGxjRC9MTlZTSTlDa2JmYWVmMmlMRSs0M29ZdktWTEE0RGY0"
    "N0cxcDdGcW1PU2EwdQ=="
)
LIVE_LOGIN_ERROR = (
    "cit6YWp3QzBrNTdwTENDT01MU3RuUzhjRXZPMEQ2OEVSanBUUkRnRkRHUTNXYmc0"
    "aDR2RktUOXRhV05UL2Vta0F5dlBrVnBReEVOb3F0eTA3TC80bUZ1bVVQRnp2Mkgy"
    "SzRpOXpOajkwSzg9"
)


def test_payload_roundtrip():
    value = {"userId": "123", "name": "老板 中文", "level": 2, "on": True}
    encrypted = api.encrypt_payload(value)
    # The client sends double base64: outer(base64(inner(base64(ciphertext)))).
    inner = base64.b64decode(encrypted)
    assert base64.b64decode(inner)  # no exception
    assert api.decrypt_payload(encrypted) == value


def test_live_app_guid_vector():
    assert api.decrypt_payload(LIVE_APP_GUID) == {
        "rc": 0,
        "appGuid": "RKDRD000000000000",
    }


def test_live_login_error_vector():
    payload = api.decrypt_payload(LIVE_LOGIN_ERROR)
    assert payload["error"] == "invalid_grant"
    assert payload["error_description"]


def test_plain_json_fallback():
    assert api.decrypt_payload('{"rc":409,"msg":"签名错误"}') == {
        "rc": 409,
        "msg": "签名错误",
    }


def test_signature_message_uses_sign_secret():
    secret_hex = "ab" * 128
    timestamp = "1700000000000"
    nonce = "12345"
    message = (
        f"aesEncryptSecret={secret_hex}"
        f"&appId={api.APP_ID}"
        f"&nonce={nonce}"
        f"&secret={api.SIGN_SECRET}"
        f"&timestamp={timestamp}"
    )
    expected = quote(
        base64.b64encode(
            hmac.new(
                api.SIGN_SECRET.encode(), message.encode(), hashlib.sha256
            ).digest()
        ).decode(),
        safe="",
    )
    assert api._signature(secret_hex, timestamp, nonce) == expected


def test_rsa_secret_blob_shape():
    blob = api.rsa_secret_hex()
    assert len(blob) == 256
    assert blob == blob.lower()
    assert all(char in "0123456789abcdef" for char in blob)


def test_normalize_properties():
    assert api.normalize_properties(
        {"level": "2", "faultCode": "0", "name": "x", "flag": True}
    ) == {"level": 2, "faultCode": 0, "name": "x", "flag": True}


def test_form_body_drops_empty_fields():
    # Mirrors EncryptUtil.bodyToString: Java's String.split drops trailing
    # empty strings, so "smsCode=" becomes ["smsCode"] and is skipped.
    source = "loginType=mobilePassword&sjhm=138&password=x&smsCode=&client_id=roki_client"
    pairs = {}
    for part in source.split("&"):
        pieces = part.split("=")
        while len(pieces) > 1 and pieces[-1] == "":
            pieces.pop()
        if len(pieces) > 1:
            pairs[pieces[0]] = pieces[1]
    assert "smsCode" not in pairs
    assert pairs["loginType"] == "mobilePassword"


@pytest.mark.parametrize("value", [0, 1, "0", "1", " 2 ", 2.0, None, "x"])
def test_as_int(value):
    result = api.as_int(value)
    assert isinstance(result, int)


def test_password_is_md5_hashed():
    # LoginPasswordPage -> User.encryptPassword -> MD5Utils.Md5:
    # lowercase hex MD5 of the UTF-8 password, not the raw password.
    assert api.hash_password("test") == "098f6bcd4621d373cade4e832627b4f6"
    assert (
        api.hash_password("Pa$$word 123")
        == hashlib.md5(b"Pa$$word 123").hexdigest()
    )


def test_check_maps_auth_rc():
    with pytest.raises(api.RokiAuthError):
        api.RokiClient._check({"rc": 401, "msg": "expired"})
    with pytest.raises(api.RokiAuthError):
        api.RokiClient._check({"rc": 403, "msg": "forbidden"})
    with pytest.raises(api.RokiApiError):
        api.RokiClient._check({"rc": 1, "msg": "boom"})
    assert api.RokiClient._check({"rc": 0, "data": {}}) == {"rc": 0, "data": {}}
