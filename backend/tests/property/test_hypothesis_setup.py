"""The property suite runs: Hypothesis is configured and python-ulid round-trips."""

import pytest
from hypothesis import given
from hypothesis import strategies as st
from ulid import ULID


@pytest.mark.req("ENF-QUAL-006")
@given(st.binary(min_size=16, max_size=16))
def test_ulid_text_round_trip_keeps_identity(raw: bytes) -> None:
    ident = ULID.from_bytes(raw)
    assert ULID.from_str(str(ident)) == ident
    assert bytes(ident) == raw
