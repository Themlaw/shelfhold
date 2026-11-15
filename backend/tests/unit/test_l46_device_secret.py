"""M4 L46: the secret of a device token (ADR-0053 §4, EF-USR-008, EF-LEC-009, ENF-SEC-013).

Generated, never chosen: 128 bits or more, an alphabet without similar-looking characters, typed
once on an e-reader. The database keeps one SHA-256 of `md5(secret)` and nothing else: the md5
that KOReader sends as `x-auth-key` and the secret a Basic client sends both lead to that single
digest.
"""

import hashlib
import math
import re

import pytest

from shelfhold.identity.domain.device_secret import (
    ALPHABET,
    SECRET_LENGTH,
    key_digest,
    md5_key,
    new_device_credential,
    normalise_secret,
    secret_digest,
)

pytestmark = pytest.mark.req("EF-USR-008", "EF-LEC-009", "ENF-SEC-013")


def test_device_secret_is_generated_with_at_least_128_bits() -> None:
    credential = new_device_credential()
    assert len(credential.secret) == SECRET_LENGTH
    assert set(credential.secret) <= set(ALPHABET)
    assert len(set(ALPHABET)) == 32
    assert SECRET_LENGTH * math.log2(len(ALPHABET)) >= 128
    assert len({new_device_credential().secret for _ in range(200)}) == 200


def test_device_secret_alphabet_has_no_look_alike_characters() -> None:
    assert not set("iloOILU") & set(ALPHABET)  # 0/o, 1/l/i, u/v are told apart on a small screen
    assert ALPHABET.lower() == ALPHABET  # an e-reader keyboard starts in lower case


def test_public_identifier_is_short_unique_and_not_derived_from_the_secret() -> None:
    first, second = new_device_credential(), new_device_credential()
    assert re.fullmatch(f"[{ALPHABET}]{{8}}", first.public_id)
    assert first.public_id != second.public_id
    assert first.public_id not in first.secret


def test_secret_is_displayed_as_typed_and_basic_tolerates_separators() -> None:
    """kosync gets `md5(typed text)`, which the server cannot normalise: the secret is shown in
    its one canonical form, without separators. Only HTTP Basic reduces what was typed."""
    credential = new_device_credential()
    assert normalise_secret(credential.secret) == credential.secret
    spaced = " ".join(credential.secret[i : i + 4] for i in range(0, SECRET_LENGTH, 4))
    assert normalise_secret(spaced.upper().replace(" ", "-")) == credential.secret


def test_stored_digest_is_sha256_of_md5_and_never_the_md5_itself() -> None:
    credential = new_device_credential()
    weak = hashlib.md5(credential.secret.encode(), usedforsecurity=False).hexdigest()
    assert md5_key(credential.secret) == weak
    digest = secret_digest(credential.secret)
    assert digest == "sha256:" + hashlib.sha256(weak.encode()).hexdigest()
    assert weak not in digest and credential.secret not in digest
    # what KOReader sends and what a Basic client types reach the same stored value
    assert key_digest(weak) == digest
    assert key_digest(weak.upper()) == digest


@pytest.mark.parametrize("bad", ["", "abc", "z" * 32, "0" * 31, "0" * 33, " " + "0" * 31, None, 5])
def test_a_key_that_is_not_an_md5_digest_has_no_digest(bad: object) -> None:
    assert key_digest(bad) is None  # type: ignore[arg-type]


def test_the_secret_never_shows_in_the_repr_of_the_credential() -> None:
    credential = new_device_credential()
    for text in (repr(credential), str(credential)):
        assert credential.secret not in text
        assert md5_key(credential.secret) not in text
