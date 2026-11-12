"""EF-INT-001: `?since=` tokens on lists, proved on their own (the codec). The `ETag` and the `304`
of a list are proved on the real job list (`test_api_conventions.py`); the lists of Works and of
shelves, and what the tokens carry, are proved through the application in `test_since_tokens.py`.
Since M3 L11 a token is signed: `s2.<sequence>.<signature>`."""

import pytest

from shelfhold.events.since_key import SinceKey
from shelfhold.interfaces.http_api.lists import decode_since, encode_since
from shelfhold.interfaces.http_api.problems import ProblemError

RETENTION_FLOOR = 40  # events up to this sequence number have been purged
KEY = bytes(range(32))


def _token(seq: int) -> str:
    return encode_since(seq, key=KEY, kind="works")


def _decode(token: str) -> int:
    return decode_since(token, key=KEY, kind="works").seq


@pytest.mark.req("EF-INT-001")
def test_since_token_is_opaque_and_round_trips() -> None:
    token = _token(100)
    assert token.isascii() and not token.isdigit()  # opaque, not a bare sequence number
    assert _decode(token) == 100


@pytest.mark.req("EF-INT-001")
def test_since_token_too_old_returns_resync_required() -> None:
    with pytest.raises(ProblemError) as old:
        decode_since(_token(RETENTION_FLOOR - 1), key=KEY, kind="works").require_not_older_than(
            RETENTION_FLOOR
        )
    assert str(old.value) == "resync_required"
    decode_since(_token(RETENTION_FLOOR), key=KEY, kind="works").require_not_older_than(
        RETENTION_FLOOR
    )


@pytest.mark.req("EF-INT-001")
@pytest.mark.parametrize(
    "token",
    ["", "100", "s1.", "s1.!!!", "s2.", "s2.MTAw", "s2.MTAw.", "s1.LTU", "x" * 300, "s2.é.x"],
)
def test_since_token_malformed_is_a_400_problem(token: str) -> None:
    with pytest.raises(ProblemError) as bad:
        _decode(token)
    assert str(bad.value) == "invalid_cursor"


@pytest.mark.req("EF-INT-001")
def test_since_token_out_of_range_is_refused_both_ways() -> None:
    with pytest.raises(ValueError, match="sequence_out_of_range"):
        _token(2**63)
    with pytest.raises(ValueError, match="sequence_out_of_range"):
        _token(-1)


@pytest.mark.req("EF-INT-001")
def test_the_signing_key_is_made_once_private_and_shared_by_every_reader(tmp_path) -> None:  # type: ignore[no-untyped-def]
    first, second = SinceKey(tmp_path), SinceKey(tmp_path)
    value = first.value()
    assert len(value) == 32 and second.value() == value  # one key for the instance
    assert first.value() is value  # read once
    assert (tmp_path / "since-token.key").stat().st_mode & 0o077 == 0  # private
    with pytest.raises(ProblemError):
        _decode(encode_since(7, key=value, kind="works"))
