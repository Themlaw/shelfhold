"""M5 L05a (EF-LEC-005): the ticket of `/content` as a pure value, and the types it serves."""

from datetime import UTC, datetime, timedelta

import pytest

from shelfhold.reading.domain.content_ticket import (
    TICKET_TTL_S,
    TicketClaims,
    open_ticket,
    seal,
    session_fingerprint,
)
from shelfhold.reading.domain.content_types import media_type_of, safe_entry

KEY = bytes(range(32))
NOW = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)


def claims(**kw: object) -> TicketClaims:
    base: dict[str, object] = {
        "user_id": "U" * 26,
        "carrier_type": "content",
        "carrier_id": "C" * 26,
        "scope": "full",
        "entries": None,
        "session_fp": session_fingerprint(KEY, "S" * 26),
        "expires_at": int((NOW + timedelta(seconds=TICKET_TTL_S)).timestamp()),
    }
    return TicketClaims(**{**base, **kw})  # type: ignore[arg-type]


@pytest.mark.req("EF-LEC-005")
def test_ticket_round_trip_and_short_life() -> None:
    token = seal(KEY, claims())
    assert open_ticket(KEY, token, now=NOW) == claims()
    assert TICKET_TTL_S <= 600  # D11: ten minutes at most
    assert open_ticket(KEY, token, now=NOW + timedelta(seconds=TICKET_TTL_S + 1)) is None


@pytest.mark.req("EF-LEC-005")
def test_ticket_tampered_or_of_another_key_opens_nothing() -> None:
    token = seal(KEY, claims())
    assert open_ticket(bytes(32), token, now=NOW) is None
    body, _, tag = token.rpartition(".")
    forged = seal(KEY, claims(user_id="V" * 26)).rpartition(".")[0] + "." + tag
    assert open_ticket(KEY, forged, now=NOW) is None
    for bad in ("", ".", "a.b", token[:-2], token + "x", "A" * 400):
        assert open_ticket(KEY, bad, now=NOW) is None
    assert body  # the ticket is one opaque word, never a path


@pytest.mark.req("EF-LEC-005")
def test_ticket_carries_scope_list_digest_and_session_fingerprint_not_the_session() -> None:
    token = seal(KEY, claims(scope="excerpt", entries="d" * 64))
    opened = open_ticket(KEY, token, now=NOW)
    assert opened is not None and opened.scope == "excerpt" and opened.entries == "d" * 64
    assert "S" * 26 not in token and session_fingerprint(KEY, "S" * 26) != "S" * 26


@pytest.mark.req("EF-LEC-005", "ENF-SEC-004")
@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("a.png", "image/png"),
        ("a.JPG", "image/jpeg"),
        ("a.webp", "image/webp"),
        ("a.css", "text/css"),
        ("a.woff2", "font/woff2"),
        ("a.xhtml", "application/xhtml+xml"),
        ("a.svg", "image/svg+xml"),
        ("a.js", "application/octet-stream"),
        ("a.mjs", "application/octet-stream"),
        ("a.wasm", "application/octet-stream"),
        ("a.json", "application/octet-stream"),
        ("a", "application/octet-stream"),
        ("a.png.js", "application/octet-stream"),
        ("a.js.png", "image/png"),
    ],
)
def test_media_type_comes_from_the_extension_only(name: str, kind: str) -> None:
    assert media_type_of(name) == kind


@pytest.mark.req("EF-LEC-005", "ENF-SEC-004")
@pytest.mark.parametrize(
    ("name", "ok"),
    [
        ("OEBPS/ch1.xhtml", True),
        ("p1.png", True),
        ("../a", False),
        ("a/../b", False),
        ("/a", False),
        ("a//b", False),
        ("a\\b", False),
        ("a\x00b", False),
        ("", False),
        ("a/", False),
        ("./a", False),
        ("a/.", False),
    ],
)
def test_safe_entry_is_a_plain_relative_name(name: str, ok: bool) -> None:
    assert safe_entry(name) is ok
