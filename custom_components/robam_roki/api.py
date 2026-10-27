"""Async client for the ROBAM / ROKI (老板电器) cloud API.

The official Android app talks to ``api.myroki.com`` through an OkHttp stack
with three interceptors:

* a header interceptor adding ``authorization`` / ``app-id`` / ``appId`` / UA,
* an encryption interceptor (AES-128-ECB + HMAC-SHA256 signature),
* a response decrypt interceptor.

This module reproduces that protocol.  It intentionally has no Home Assistant
imports so it can also be used from a plain Python shell or the CLI in
``tools/roki_cli.py``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import random
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from typing import Any

from aiohttp import ClientError, ClientResponseError, ClientSession, ClientTimeout
from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from yarl import URL

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://api.myroki.com"

APP_ID = "roki_app"
AES_SECRET = b"xoNZXqGLoS2nhtfq"
SIGN_SECRET = "ee8694419924a22f04ac0e01368683521daa659f"

# Hard-coded RSA public key from com.legent.safe.Constants.  It is only used to
# wrap the AES key that travels in the ``secret`` header.
RSA_PUBLIC_KEY_B64 = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCQwjPnvbv5nPPUrCdo167kcYd+7UqW"
    "YuedCNhLQradkfVJ0Ce8qkqTEmD9aWH1+t6gYQVmaPWIjZCW0U1DqacYKJ6JZzUsFWzY"
    "aSIr+OXoJ5OvNNxWqAGMz/GkJkeXWcQ+dLLnMmLzNIhbakdXoZzzvdde5IOj5YWk2/n"
    "K97pHGwIDAQAB"
)

APP_VERSION_CODE = "6004000"
APP_VERSION_NAME = "6.4.0"
USER_AGENT = f"ROKI/{APP_VERSION_NAME}(Android)/brandModel/HomeAssistant(HomeAssistant)"

CLIENT_ID = "roki_client"
CLIENT_SECRET = "test"
APP_TYPE = "RKDRD"
GRANT_TYPE = "roki"

LOGIN_PASSWORD = "mobilePassword"


class RokiError(Exception):
    """Base error."""


class RokiAuthError(RokiError):
    """Authentication failed or the token expired."""


class RokiApiError(RokiError):
    """The cloud answered with an error."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        rc: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.rc = rc


# ---------------------------------------------------------------------------
# Crypto helpers
# ---------------------------------------------------------------------------

_secret_hex: str | None = None


def _aes_encrypt(data: bytes) -> bytes:
    padder = sym_padding.PKCS7(128).padder()
    padded = padder.update(data) + padder.finalize()
    encryptor = Cipher(algorithms.AES(AES_SECRET), modes.ECB()).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def _aes_decrypt(data: bytes) -> bytes:
    decryptor = Cipher(algorithms.AES(AES_SECRET), modes.ECB()).decryptor()
    padded = decryptor.update(data) + decryptor.finalize()
    unpadder = sym_padding.PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def rsa_secret_hex() -> str:
    """Return the hex encoded RSA/AES key blob used in the ``secret`` header.

    The app computes ``RSA/ECB/PKCS1Padding(public_key, aes_secret)`` at start
    up.  PKCS#1 v1.5 padding is randomised, so any valid ciphertext works.
    """
    global _secret_hex
    if _secret_hex is None:
        public_key = serialization.load_der_public_key(
            base64.b64decode(RSA_PUBLIC_KEY_B64)
        )
        _secret_hex = public_key.encrypt(AES_SECRET, asym_padding.PKCS1v15()).hex()
    return _secret_hex


def encrypt_payload(value: Any) -> str:
    """Encrypt a JSON value the way ``HttpEncryptUtil.encryptContent`` does.

    The Android helper base64 encodes twice: once around the AES ciphertext and
    once around that base64 string.
    """
    raw = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()
    inner = base64.b64encode(_aes_encrypt(raw))
    return base64.b64encode(inner).decode()


def decrypt_payload(text: str) -> Any:
    """Decrypt a response body, falling back to plain JSON."""
    text = text.strip()
    if not text:
        return {}
    try:
        inner = base64.b64decode(text)
        raw = _aes_decrypt(base64.b64decode(inner))
        return json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001 - the cloud may answer in clear text
        return json.loads(text)


def hash_password(password: str) -> str:
    """Hash an account password the way the app does before login.

    ``LoginPasswordPage`` calls ``User.encryptPassword`` -> ``MD5Utils.Md5``,
    which is plain lowercase hex MD5 of the UTF-8 password. The cloud rejects
    the raw password with ``密码不正确``.
    """
    return hashlib.md5(password.encode("utf-8")).hexdigest()


def _signature(secret_hex: str, timestamp: str, nonce: str) -> str:
    message = (
        f"aesEncryptSecret={secret_hex}"
        f"&appId={APP_ID}"
        f"&nonce={nonce}"
        f"&secret={SIGN_SECRET}"
        f"&timestamp={timestamp}"
    )
    digest = hmac.new(
        SIGN_SECRET.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).digest()
    return _quote(base64.b64encode(digest).decode("ascii"))


def _quote(value: str) -> str:
    # Equivalent of java.net.URLEncoder.encode(value, "UTF-8") for base64.
    from urllib.parse import quote

    return quote(value, safe="")


def _new_nonce() -> str:
    # The app uses `new Random(9000000).nextInt()` which always returns
    # -109881635.  A real nonce is used here; the server does not require the
    # app's constant.
    return str(random.randint(1, 2**31 - 1))


def as_int(value: Any, default: int = 0) -> int | None:
    """Best effort conversion of the cloud's stringly typed properties."""
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def normalize_properties(data: Mapping[str, Any] | None) -> dict[str, Any]:
    """Convert numeric strings from the IoT shadow into numbers."""
    if not data:
        return {}
    result: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, str):
            text = value.strip()
            if text and (text.lstrip("-").isdigit()):
                result[key] = int(text)
                continue
            result[key] = value
        else:
            result[key] = value
    return result


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class RokiClient:
    """Minimal async client for the ROKI cloud."""

    def __init__(
        self,
        session: ClientSession,
        phone: str | None = None,
        password: str | None = None,
        *,
        base_url: str = BASE_URL,
    ) -> None:
        self._session = session
        self.phone = phone
        self.password = password
        self.base_url = base_url.rstrip("/")

        self.access_token: str | None = None
        self.token_type: str | None = None
        self.refresh_token: str | None = None
        self.expires_at: float = 0.0
        self.user_id: str | None = None
        self.device_token: str = uuid.uuid4().hex[:16]
        self.app_guid: str | None = None

        self._relogin_lock = asyncio.Lock()

    # -- token handling ---------------------------------------------------

    @property
    def authorization(self) -> str | None:
        if not self.access_token:
            return None
        token_type = self.token_type or "bearer"
        return f"{token_type} {self.access_token}"

    @property
    def token_valid(self) -> bool:
        # Renew five minutes before the server side expiry.
        return bool(self.access_token) and time.time() < self.expires_at - 300

    def restore(
        self,
        *,
        access_token: str | None = None,
        token_type: str | None = None,
        refresh_token: str | None = None,
        expires_at: float = 0.0,
        user_id: str | None = None,
        device_token: str | None = None,
        app_guid: str | None = None,
    ) -> None:
        """Load a session previously persisted in a config entry."""
        self.access_token = access_token
        self.token_type = token_type
        self.refresh_token = refresh_token
        self.expires_at = expires_at or 0.0
        self.user_id = user_id
        if device_token:
            self.device_token = device_token
        self.app_guid = app_guid

    async def async_ensure_token(self) -> None:
        if self.token_valid:
            return
        async with self._relogin_lock:
            if self.token_valid:
                return
            if not self.phone or not self.password:
                raise RokiAuthError("No credentials available for re-login")
            await self.async_login()

    async def async_force_relogin(self) -> None:
        """Log in again even if the local token still looks valid."""
        async with self._relogin_lock:
            await self.async_login()

    # -- low level transport ---------------------------------------------

    def _base_headers(self, *, authenticated: bool) -> dict[str, str]:
        headers = {
            "app-id": APP_ID,
            "appId": APP_ID,
            "app-version": APP_VERSION_CODE,
            "User-Agent": USER_AGENT,
        }
        if authenticated and (authorization := self.authorization):
            headers["authorization"] = authorization
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: Any | None = None,
        query: Mapping[str, Any] | None = None,
        form: Mapping[str, Any] | None = None,
        authenticated: bool = True,
        _retry: bool = True,
    ) -> Any:
        method = method.upper()
        if authenticated:
            await self.async_ensure_token()

        secret_hex = rsa_secret_hex()
        timestamp = str(int(time.time() * 1000))
        nonce = _new_nonce()

        headers = self._base_headers(authenticated=authenticated)
        headers["timestamp"] = timestamp
        headers["nonce"] = nonce
        headers["secret"] = secret_hex
        headers["signature"] = _signature(secret_hex, timestamp, nonce)

        url = f"{self.base_url}{path}"
        request_kwargs: dict[str, Any] = {"headers": headers}

        if method in ("GET", "DELETE"):
            if query:
                payload = encrypt_payload(dict(query))
                # The app appends the base64 blob verbatim, without percent
                # encoding.  `encoded=True` keeps aiohttp from touching it, so
                # the bytes on the wire match the Android client.
                url = URL(f"{url}?data={payload}", encoded=True)
        elif form is not None:
            # The app drops empty form fields before encrypting and writes the
            # base64 blob verbatim into a form body.
            fields = {
                key: value
                for key, value in form.items()
                if value not in ("", None)
            }
            payload = encrypt_payload(fields)
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            request_kwargs["data"] = f"data={payload}".encode("ascii")
        else:
            payload = encrypt_payload(body if body is not None else {})
            headers["Content-Type"] = "application/json; charset=utf-8"
            request_kwargs["data"] = payload.encode("ascii")

        try:
            async with self._session.request(
                method, url, timeout=ClientTimeout(total=30), **request_kwargs
            ) as response:
                text = await response.text()
                if response.status in (401, 403):
                    raise RokiAuthError(
                        f"HTTP {response.status}: {self._error_message(text)}"
                    )
                if response.status >= 400:
                    raise RokiApiError(
                        f"HTTP {response.status}: {self._error_message(text)}",
                        status=response.status,
                    )
                payload = decrypt_payload(text)
                # Some IoT routes answer HTTP 200 with a business rc instead of
                # an HTTP status; treat an auth rc like an expired session.
                if isinstance(payload, Mapping) and as_int(
                    payload.get("rc"), 0
                ) in (401, 403):
                    raise RokiAuthError(
                        str(
                            payload.get("msg")
                            or payload.get("message")
                            or "Session expired"
                        )
                    )
                return payload
        except RokiAuthError:
            if authenticated and _retry and self.phone and self.password:
                async with self._relogin_lock:
                    await self.async_login()
                return await self._request(
                    method,
                    path,
                    body=body,
                    query=query,
                    form=form,
                    authenticated=authenticated,
                    _retry=False,
                )
            raise
        except (TimeoutError, ClientError, ClientResponseError) as err:
            raise RokiApiError(f"Connection error: {err}") from err
        except json.JSONDecodeError as err:
            raise RokiApiError(f"Invalid response: {err}") from err

    @staticmethod
    def _error_message(text: str) -> str:
        try:
            payload = decrypt_payload(text)
        except Exception:  # noqa: BLE001
            return text[:300]
        if isinstance(payload, Mapping):
            for key in ("message", "msg", "error_description", "error"):
                if payload.get(key):
                    return str(payload[key])
        return text[:300]

    @staticmethod
    def _check(payload: Any) -> Mapping[str, Any]:
        if not isinstance(payload, Mapping):
            raise RokiApiError(f"Unexpected response: {payload!r}")
        rc = payload.get("rc")
        if rc is not None:
            rc_int = as_int(rc, -1)
            if rc_int in (401, 403):
                raise RokiAuthError(
                    str(payload.get("msg") or payload.get("message") or "Session expired")
                )
            if rc_int != 0:
                raise RokiApiError(
                    str(payload.get("msg") or payload.get("message") or f"rc={rc}"),
                    rc=rc_int,
                )
        if payload.get("success") is False:
            raise RokiApiError(
                str(payload.get("msg") or payload.get("message") or "Request failed")
            )
        return payload

    # -- authentication ---------------------------------------------------

    async def async_login(self) -> dict[str, Any]:
        if not self.phone or not self.password:
            raise RokiAuthError("Phone number and password are required")

        try:
            payload = await self._request(
                "POST",
                "/rest/auth/api/oauth/token",
                form={
                    "loginType": LOGIN_PASSWORD,
                    "sjhm": self.phone,
                    "smsCode": "",
                    "password": hash_password(self.password),
                    "access_token": "",
                    "refreshToken": "",
                    "openId": "",
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                    "appType": APP_TYPE,
                    "grant_type": GRANT_TYPE,
                },
                authenticated=False,
            )
        except RokiApiError as err:
            # The token endpoint answers 400 invalid_grant for a bad account
            # or password; surface that as an authentication failure.
            if err.status in (400, 401):
                raise RokiAuthError(str(err)) from err
            raise

        data = self._check(payload)

        access_token = data.get("access_token")
        if not access_token:
            raise RokiAuthError(
                str(data.get("error_description") or data.get("msg") or "Login failed")
            )

        self.access_token = str(access_token)
        self.token_type = str(data.get("token_type") or "bearer")
        self.refresh_token = data.get("refresh_token")
        expires_in = as_int(data.get("expires_in"), 0)
        # `expires_in` is usually seconds; guard against millisecond values.
        if expires_in > 10_000_000:
            expires_in //= 1000
        self.expires_at = time.time() + (expires_in or 3600)

        if not self.user_id:
            try:
                await self.async_get_user()
            except RokiError:
                _LOGGER.debug("Could not fetch user profile after login", exc_info=True)
        await self.async_bind_app_guid()

        return dict(data)

    async def async_get_user(self) -> dict[str, Any]:
        """Fetch the logged in user (``getOauth`` + ``getUser2``)."""
        payload = await self._request("GET", "/rest/ums/api/user/get/oauth")
        data = self._check(payload)
        user = data.get("user") if isinstance(data.get("user"), Mapping) else data
        user_id = user.get("id") if isinstance(user, Mapping) else None
        if not user_id:
            raise RokiApiError("User profile did not contain an id")

        self.user_id = str(user_id)
        try:
            detail = await self._request(
                "POST", "/rest/ums/api/user/get", body={"userId": self.user_id}
            )
            detail_data = self._check(detail)
            if isinstance(detail_data.get("user"), Mapping):
                return dict(detail_data["user"])
        except RokiError:
            _LOGGER.debug("getUser2 failed, using getOauth profile", exc_info=True)
        return dict(user)

    # -- app guid (used as SSE connectId) ---------------------------------

    async def async_get_app_guid(self) -> str | None:
        if self.app_guid:
            return self.app_guid
        try:
            payload = await self._request(
                "POST",
                "/rest/dms/api/app/guid/get",
                body={
                    "appType": APP_TYPE,
                    # The app sends the Android id as `token` and the phone
                    # number as `phoneToken` (see Requests.GetAppIdRequest).
                    "token": self.device_token,
                    "phoneToken": self.phone or "",
                    "versionName": APP_VERSION_NAME,
                },
                authenticated=False,
            )
            if isinstance(payload, Mapping) and payload.get("appGuid"):
                self.app_guid = str(payload["appGuid"])
        except RokiError:
            _LOGGER.debug("Could not obtain app guid", exc_info=True)
        if not self.app_guid:
            # 17 characters, RKDRD + 12: the same shape the cloud returns and
            # the MQTT username/HMAC expect.
            self.app_guid = "RKDRD" + uuid.uuid4().hex[:12]
        return self.app_guid

    async def async_bind_app_guid(self) -> None:
        """Associate this app instance with the account.

        The Android client posts ``{appGuid, userId}`` to
        ``/rest/api/app/user/bind`` after login
        (``CommonService.bindAppGuidAndUser``).  The cloud uses it to route
        pushes and to authorise the MQTT client id.
        """
        guid = await self.async_get_app_guid()
        if not guid or not self.user_id:
            return
        try:
            await self._request(
                "POST",
                "/rest/api/app/user/bind",
                body={"appGuid": guid, "userId": self.user_id},
            )
        except RokiError:
            _LOGGER.debug("bindAppGuidAndUser failed", exc_info=True)

    # -- devices ----------------------------------------------------------

    async def async_get_devices(self) -> list[dict[str, Any]]:
        await self.async_ensure_token()
        if not self.user_id:
            await self.async_get_user()
        payload = await self._request(
            "POST", "/rest/api/device/v4/get", body={"userId": self.user_id}
        )
        data = self._check(payload)
        devices = data.get("data") or []
        if not isinstance(devices, list):
            return []
        result: list[dict[str, Any]] = []
        for device in devices:
            if not isinstance(device, Mapping) or not device.get("guid"):
                continue
            result.append(dict(device))
        return result

    # -- IoT properties / commands ---------------------------------------

    async def async_get_properties(self, device_id: str) -> dict[str, Any]:
        await self.async_ensure_token()
        payload = await self._request(
            "GET",
            "/rest/iot/api/device/property/latest",
            query={"deviceId": device_id, "uid": self.user_id or ""},
        )
        data = self._check(payload)
        properties = data.get("data")
        if isinstance(properties, Mapping):
            return normalize_properties(properties)
        return {}

    async def async_invoke(
        self, device_id: str, function: str | int, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        await self.async_ensure_token()
        payload = await self._request(
            "POST",
            "/rest/iot/api/device/function/invoke",
            body={
                "messageId": int(time.time() * 1000),
                "deviceId": device_id,
                "uid": self.user_id or "",
                "function": str(function),
                "params": dict(params),
            },
        )
        return dict(self._check(payload))

    async def async_set_properties(
        self, device_id: str, properties: Mapping[str, Any]
    ) -> dict[str, Any]:
        await self.async_ensure_token()
        payload = await self._request(
            "POST",
            "/rest/iot/api/device/property",
            body={
                "deviceId": device_id,
                "uid": self.user_id or "",
                "properties": dict(properties),
            },
        )
        return dict(self._check(payload))

    # -- hood helpers -----------------------------------------------------

    async def async_get_online_state(
        self, device_ids: list[str]
    ) -> dict[str, int | None]:
        """Return the cloud's online code per device.

        ``POST /rest/iot/api/device/info/state`` takes a list of device ids and
        answers ``{data: [{deviceId, status}]}``.  Devices missing from the
        answer map to ``None``.  The semantics of ``status`` are unconfirmed:
        the same field name is used for a device's binding state, so treat a
        non-zero value as "reachable" and nothing stronger.
        """
        if not device_ids:
            return {}
        await self.async_ensure_token()
        payload = await self._request(
            "POST", "/rest/iot/api/device/info/state", body=list(device_ids)
        )
        data = self._check(payload)
        entries = data.get("data")
        if not isinstance(entries, list):
            return {}
        result: dict[str, int | None] = {guid: None for guid in device_ids}
        for entry in entries:
            if not isinstance(entry, Mapping) or not entry.get("deviceId"):
                continue
            result[str(entry["deviceId"])] = as_int(entry.get("status"), None)
        return result

    async def async_set_power(self, device_id: str, on: bool) -> dict[str, Any]:
        return await self.async_set_properties(
            device_id, {"working_status": 1 if on else 0}
        )

    async def async_set_level(self, device_id: str, level: int) -> dict[str, Any]:
        return await self.async_invoke(device_id, "134", {"level": int(level)})

    async def async_set_light(self, device_id: str, on: bool) -> dict[str, Any]:
        """Toggle the hood light.

        The write API uses protocol style (snake_case) property names while the
        read API returns camelCase ones, so both spellings are attempted.
        """
        value = 1 if on else 0
        last_error: RokiError | None = None
        for key in ("light_status", "lightStatus"):
            try:
                return await self.async_set_properties(device_id, {key: value})
            except RokiApiError as err:
                last_error = err
                _LOGGER.debug("Writing %s failed: %s", key, err)
        if last_error:
            raise last_error
        raise RokiApiError("Light control failed")

    async def async_set_smart_switch(
        self, device_id: str, function: str, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        return await self.async_invoke(device_id, function, params)

    # -- server sent events ----------------------------------------------

    async def async_sse_events(
        self,
        device_ids: list[str],
        *,
        connect_id: str,
        uid: str,
        sock_read: int = 120,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield property push payloads from the SSE endpoint."""
        if not self.authorization:
            raise RokiAuthError("Not authenticated")

        from urllib.parse import quote

        ids = ",".join(device_ids)
        url = (
            f"{self.base_url}/rest/iot/api/device/property/connect"
            f"?deviceIds={quote(ids, safe=',')}"
            f"&connectId={quote(connect_id)}"
            f"&uid={quote(uid)}"
        )
        headers = {
            "authorization": self.authorization,
            "app-id": APP_ID,
            "appId": APP_ID,
            "app-version": APP_VERSION_CODE,
            "User-Agent": USER_AGENT,
            "Accept": "text/event-stream",
            "Cache-Control": "no-cache",
        }
        timeout = ClientTimeout(total=None, sock_connect=15, sock_read=sock_read)

        try:
            async with self._session.get(
                url, headers=headers, timeout=timeout
            ) as response:
                if response.status in (401, 403):
                    raise RokiAuthError(f"SSE HTTP {response.status}")
                response.raise_for_status()

                buffer: list[str] = []
                while True:
                    raw = await response.content.readline()
                    if not raw:
                        break
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if not line:
                        if buffer:
                            payload = "\n".join(buffer)
                            buffer.clear()
                            event = self._parse_sse_payload(payload)
                            if event is not None:
                                yield event
                        continue
                    if line.startswith(":"):
                        continue
                    if line.startswith("data:"):
                        buffer.append(line[5:].lstrip())
                    # `event:` / `id:` / `retry:` are not used by the app.
        except RokiAuthError:
            raise
        except (TimeoutError, ClientError) as err:
            raise RokiApiError(f"SSE connection error: {err}") from err

    @staticmethod
    def _parse_sse_payload(payload: str) -> dict[str, Any] | None:
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            _LOGGER.debug("Ignoring non JSON SSE payload: %s", payload[:200])
            return None
        if not isinstance(parsed, Mapping):
            return None
        return dict(parsed)

    async def async_close(self) -> None:
        """Nothing to close: the aiohttp session is owned by Home Assistant."""
