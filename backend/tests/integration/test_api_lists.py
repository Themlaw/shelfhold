"""EF-INT-001 (volet coupable M1a): `?since=` tokens on lists. The `ETag` and the `304` of a list
are proved on the real job list (`test_api_conventions.py`); no list is backed by the event
sequence before M3, so the token is proved on its own."""

import pytest

from shelfhold.interfaces.http_api.lists import decode_since, encode_since
from shelfhold.interfaces.http_api.problems import ProblemError

RETENTION_FLOOR = 40  # events up to this sequence number have been purged


@pytest.mark.req("EF-INT-001")
def test_since_token_is_opaque_and_round_trips() -> None:
    token = encode_since(100)
    assert token.isascii() and not token.isdigit()  # opaque, not a bare sequence number
    assert decode_since(token).seq == 100


@pytest.mark.req("EF-INT-001")
def test_since_token_too_old_returns_resync_required() -> None:
    with pytest.raises(ProblemError) as old:
        decode_since(encode_since(RETENTION_FLOOR - 1)).require_not_older_than(RETENTION_FLOOR)
    assert str(old.value) == "resync_required"
    decode_since(encode_since(RETENTION_FLOOR)).require_not_older_than(RETENTION_FLOOR)


@pytest.mark.req("EF-INT-001")
@pytest.mark.parametrize(
    "token", ["", "100", "s1.", "s1.!!!", "s2.MTAw", "s1.LTU", "x" * 300, encode_since(2**63)]
)
def test_since_token_malformed_is_a_400_problem(token: str) -> None:
    with pytest.raises(ProblemError) as bad:
        decode_since(token)
    assert str(bad.value) == "invalid_cursor"
