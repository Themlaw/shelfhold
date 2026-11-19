"""M5 L17a: the proofs of `/lite` (EF-LEC-010, ENF-SEC-010): a session that is bound to the
secret of a token (rotating it ends it), a form token bound to one browser, and none of them
valid in the place of the signed "already logged in here" cookie of the sign-in page (and the
other way round). Pure functions of a key file and a clock: no network, no application."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from shelfhold.identity.adapters.device_cookie import DeviceCookieSigner, LiteCookieSigner

pytestmark = pytest.mark.req("EF-LEC-010", "ENF-SEC-010")

NOW = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)
DIGEST = "sha256:" + "ab" * 32


def signer(tmp_path: Path) -> LiteCookieSigner:
    return LiteCookieSigner(tmp_path)


def test_session_roundtrip_binds_the_public_id_and_the_secret(tmp_path: Path) -> None:
    lite = signer(tmp_path)
    value = lite.issue("abcd1234", DIGEST, NOW)
    claims = lite.verify(value, NOW + timedelta(hours=1))
    assert claims is not None and claims.public_id == "abcd1234"
    assert lite.bound(claims, DIGEST)
    assert not lite.bound(claims, "sha256:" + "cd" * 32)  # a rotated secret: another digest
    assert DIGEST not in value and "abcd1234" not in value[value.index(".") :]


def test_session_is_refused_when_altered_expired_future_or_foreign(tmp_path: Path) -> None:
    lite = signer(tmp_path)
    value = lite.issue("abcd1234", DIGEST, NOW)
    payload, _, mac = value.partition(".")
    for bad in (None, "", "x", value + "x", payload, f"{payload}.{mac[::-1]}", f".{mac}", "a." * 3):
        assert lite.verify(bad, NOW) is None
    assert lite.verify(value, NOW + LiteCookieSigner.MAX_AGE + timedelta(seconds=1)) is None
    assert lite.verify(value, NOW - timedelta(minutes=5)) is None  # from the future
    other = tmp_path / "other"
    other.mkdir()
    assert LiteCookieSigner(other).verify(value, NOW) is None  # another instance's key


def test_the_two_cookies_of_the_instance_are_not_interchangeable(tmp_path: Path) -> None:
    login = DeviceCookieSigner(tmp_path)
    lite = signer(tmp_path)  # same key file, different purpose
    assert lite.verify(login.issue("mum", NOW), NOW) is None
    session = lite.issue("abcd1234", DIGEST, NOW)
    assert not login.verify(session, "mum", NOW)
    assert not login.verify(session, "abcd1234", NOW)


def test_flow_token_is_bound_to_one_nonce(tmp_path: Path) -> None:
    lite = signer(tmp_path)
    nonce, token = lite.new_flow()
    other_nonce, other_token = lite.new_flow()
    assert nonce != other_nonce and token != other_token
    assert lite.flow_ok(nonce, token)
    assert not lite.flow_ok(nonce, other_token) and not lite.flow_ok(other_nonce, token)
    assert not lite.flow_ok("", token) and not lite.flow_ok(nonce, "")
    assert not lite.flow_ok(nonce, token + "0") and not lite.flow_ok(nonce + "0", token)


def test_csrf_token_of_a_session_is_not_a_flow_token_nor_another_sessions(tmp_path: Path) -> None:
    lite = signer(tmp_path)
    first = lite.issue("abcd1234", DIGEST, NOW)
    second = lite.issue("abcd1234", DIGEST, NOW + timedelta(seconds=1))
    token = lite.csrf_of(first)
    assert lite.csrf_ok(first, token)
    assert (
        not lite.csrf_ok(second, token)
        and not lite.csrf_ok(first, "")
        and not lite.csrf_ok("", token)
    )
    nonce, flow = lite.new_flow()
    assert not lite.csrf_ok(nonce, flow) and not lite.flow_ok(first, token)
