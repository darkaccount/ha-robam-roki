"""Privacy gate for the regression fixtures.

The pre-push gate scans added lines once; this test keeps the fixtures clean at
commit time, so a real phone number, GUID or token can never land there even if
the push gate is skipped.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
FIXTURE_FILES = sorted(path for path in FIXTURE_DIR.rglob("*") if path.is_file())
FAKE_PHONE = "138" + "00000000"

COLON_MAC = re.compile(
    r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:])"
)
DASH_MAC = re.compile(
    r"(?<![0-9A-Fa-f-])(?:[0-9A-Fa-f]{2}-){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f-])"
)
JWT = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
GUID32 = re.compile(r"(?<![0-9A-Za-z])[0-9A-Fa-f]{32}(?![0-9A-Za-z])")
GITHUB_TOKEN = re.compile(r"ghp_[A-Za-z0-9]{20,}")
OPENAI_TOKEN = re.compile(r"sk-[A-Za-z0-9]{20,}")
PRIVATE_KEY = "-----BEGIN " + "PRIVATE KEY-----"


def _violations(text: str) -> list[str]:
    found: list[str] = []
    for match in PHONE.findall(text):
        if match != FAKE_PHONE:
            found.append("phone")
    if GUID32.search(text):
        found.append("guid32")
    if COLON_MAC.search(text) or DASH_MAC.search(text):
        found.append("mac")
    if JWT.search(text):
        found.append("jwt")
    if PRIVATE_KEY in text:
        found.append("private-key")
    if GITHUB_TOKEN.search(text) or OPENAI_TOKEN.search(text):
        found.append("token")
    return sorted(set(found))


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda path: path.name)
def test_fixture_files_are_free_of_secrets(path: Path) -> None:
    assert _violations(path.read_text(encoding="utf-8")) == []


def test_the_scanner_catches_a_planted_phone_number() -> None:
    planted = "138" + "12345678"
    assert _violations('{"phone": "' + planted + '"}') == ["phone"]


def test_the_scanner_catches_other_secret_shapes() -> None:
    guid = "0123456789abcdef" * 2
    jwt = "eyJ" + "e30e30e30e30" + "." + "payload" + "." + "sig"
    assert "guid32" in _violations(guid)
    assert "jwt" in _violations(jwt)
    assert "private-key" in _violations(PRIVATE_KEY)
    assert "token" in _violations("gh" + "p_" + "abcdefghijklmnopqrstuvwxyz")


def test_the_repository_fake_number_is_allowed() -> None:
    assert _violations('{"phone": "' + FAKE_PHONE + '"}') == []
