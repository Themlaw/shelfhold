"""ENF-SEC-005: versioned secret envelope {v, kid, alg, ct}, AAD bound to (table, column, id)."""

import base64
import dataclasses
from datetime import UTC, datetime

import pytest

from shelfhold.infrastructure.secrets.envelope import (
    ALG,
    FORMAT_VERSION,
    NONCE_BYTES,
    Envelope,
    build_aad,
)
from shelfhold.infrastructure.secrets.errors import (
    DecryptionError,
    EnvelopeFormatError,
    UnknownKidError,
    UnsupportedAlgorithmError,
    UnsupportedVersionError,
)
from shelfhold.infrastructure.secrets.keyring import Keyring
from shelfhold.infrastructure.secrets.vault import SecretVault

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
PLAINTEXT = b'{"password": "' + b"fake-pw" + b'"}'
WHERE = {"table": "source_config", "column": "secret_ct", "row_id": "plugin-a"}


@pytest.fixture
def vault() -> SecretVault:
    return SecretVault(Keyring.generate(NOW))


@pytest.fixture
def sealed(vault: SecretVault) -> Envelope:
    return vault.seal(PLAINTEXT, **WHERE)


@pytest.mark.req("ENF-SEC-005")
def test_secret_envelope_has_version_kid_and_aad(vault: SecretVault, sealed: Envelope) -> None:
    assert sealed.v == FORMAT_VERSION == 1
    assert sealed.kid == vault.keyring.active.kid
    assert sealed.alg == ALG == "xchacha20poly1305"
    assert len(sealed.nonce) == NONCE_BYTES == 24
    assert b"fake-pw" not in sealed.ct
    assert vault.open(sealed, **WHERE) == PLAINTEXT
    # the wire shape is exactly {v, kid, alg, ct}
    wire = sealed.to_wire()
    assert set(wire) == {"v", "kid", "alg", "ct"}
    assert Envelope.from_wire(wire) == sealed
    # the AAD names the format version and the three coordinates, unambiguously
    assert build_aad(1, "a", "bc", "d") != build_aad(1, "ab", "c", "d")
    assert build_aad(1, "a", "b", "c") != build_aad(2, "a", "b", "c")


@pytest.mark.req("ENF-SEC-005")
def test_nonce_is_random_per_seal(vault: SecretVault) -> None:
    nonces = {vault.seal(b"same", **WHERE).nonce for _ in range(200)}
    assert len(nonces) == 200


@pytest.mark.req("ENF-SEC-005")
def test_columns_round_trip(sealed: Envelope) -> None:
    columns = sealed.to_columns()
    assert set(columns) == {"secret_kid", "secret_v", "secret_nonce", "secret_ct"}
    assert Envelope.from_columns(columns) == sealed


@pytest.mark.req("ENF-SEC-005")
@pytest.mark.parametrize(
    "moved",
    [
        {"table": "source_account"},
        {"column": "other_ct"},
        {"row_id": "plugin-b"},
    ],
)
def test_secret_copied_to_another_row_does_not_decrypt(
    vault: SecretVault, sealed: Envelope, moved: dict[str, str]
) -> None:
    with pytest.raises(DecryptionError):
        vault.open(sealed, **{**WHERE, **moved})


@pytest.mark.req("ENF-SEC-005")
def test_unknown_kid_is_refused(vault: SecretVault, sealed: Envelope) -> None:
    other = SecretVault(Keyring.generate(NOW))
    with pytest.raises(UnknownKidError) as info:
        other.open(sealed, **WHERE)
    assert info.value.kid == sealed.kid


@pytest.mark.req("ENF-SEC-005")
@pytest.mark.parametrize("cut", [1, 15, 16])
def test_truncated_ciphertext_is_refused(vault: SecretVault, sealed: Envelope, cut: int) -> None:
    truncated = dataclasses.replace(sealed, ct=sealed.ct[:-cut])
    with pytest.raises((DecryptionError, EnvelopeFormatError)):
        vault.open(truncated, **WHERE)


@pytest.mark.req("ENF-SEC-005")
@pytest.mark.parametrize("position", ["first", "last", "tag"])
def test_altered_ciphertext_is_refused(vault: SecretVault, sealed: Envelope, position: str) -> None:
    index = {"first": 0, "last": len(sealed.ct) - 1, "tag": len(sealed.ct) - 8}[position]
    altered = bytearray(sealed.ct)
    altered[index] ^= 0x01
    with pytest.raises(DecryptionError):
        vault.open(dataclasses.replace(sealed, ct=bytes(altered)), **WHERE)


@pytest.mark.req("ENF-SEC-005")
def test_altered_nonce_is_refused(vault: SecretVault, sealed: Envelope) -> None:
    flipped = bytes([sealed.nonce[0] ^ 1]) + sealed.nonce[1:]
    with pytest.raises(DecryptionError):
        vault.open(dataclasses.replace(sealed, nonce=flipped), **WHERE)
    with pytest.raises(EnvelopeFormatError):
        vault.open(dataclasses.replace(sealed, nonce=sealed.nonce[:12]), **WHERE)


@pytest.mark.req("ENF-SEC-005")
@pytest.mark.parametrize("version", [0, 2, 99, -1])
def test_wrong_format_version_is_refused(
    vault: SecretVault, sealed: Envelope, version: int
) -> None:
    with pytest.raises(UnsupportedVersionError):
        vault.open(dataclasses.replace(sealed, v=version), **WHERE)


@pytest.mark.req("ENF-SEC-005")
def test_wrong_algorithm_is_refused(vault: SecretVault, sealed: Envelope) -> None:
    with pytest.raises(UnsupportedAlgorithmError):
        vault.open(dataclasses.replace(sealed, alg="aes-gcm"), **WHERE)


@pytest.mark.req("ENF-SEC-005")
def test_wrong_key_under_a_known_kid_is_refused(sealed: Envelope) -> None:
    impostor = Keyring.generate(NOW)
    forged = dataclasses.replace(impostor.active, kid=sealed.kid)  # same name, other key material
    keyring = dataclasses.replace(impostor, keys=(forged,))
    with pytest.raises(DecryptionError):
        SecretVault(keyring).open(sealed, **WHERE)


@pytest.mark.req("ENF-SEC-005")
@pytest.mark.parametrize(
    "wire",
    [
        {},
        {"v": 1, "kid": "k", "alg": ALG},
        {"v": "1", "kid": "k", "alg": ALG, "ct": "AAAA"},
        {"v": 1, "kid": "k", "alg": ALG, "ct": "not base64!!"},
        {"v": 1, "kid": "k", "alg": ALG, "ct": base64.urlsafe_b64encode(b"short").decode()},
    ],
)
def test_malformed_wire_envelope_is_refused(wire: dict[str, object]) -> None:
    with pytest.raises(EnvelopeFormatError):
        Envelope.from_wire(wire)


@pytest.mark.req("ENF-SEC-005")
def test_plaintext_and_key_never_appear_in_reprs(vault: SecretVault, sealed: Envelope) -> None:
    key = vault.keyring.active
    text = repr(key) + repr(vault.keyring) + repr(vault) + repr(sealed)
    assert base64.b64encode(key.material).decode() not in text
    assert key.material.hex() not in text


@pytest.mark.req("ENF-SEC-005")
def test_review_wire_form_is_strict_canonical_base64url(sealed: Envelope) -> None:
    wire = sealed.to_wire()
    standard = base64.b64encode(base64.urlsafe_b64decode(wire["ct"])).decode()
    candidates = [
        {**wire, "ct": wire["ct"] + "\n"},
        {**wire, "ct": " " + wire["ct"]},
    ]
    if wire["ct"].endswith("="):
        candidates.append({**wire, "ct": wire["ct"].rstrip("=")})  # unpadded
    if standard != wire["ct"]:
        candidates.append({**wire, "ct": standard})  # the + / alphabet
    for candidate in candidates:
        with pytest.raises(EnvelopeFormatError):
            Envelope.from_wire(candidate)
    assert Envelope.from_wire(wire) == sealed


@pytest.mark.req("ENF-SEC-005")
def test_review_wire_form_refuses_non_canonical_trailing_bits(sealed: Envelope) -> None:
    wire = sealed.to_wire()
    packed = wire["ct"]
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    pad = len(packed) - len(packed.rstrip("="))
    if pad:
        index = len(packed) - pad - 1
        tweaked = packed[:index] + alphabet[alphabet.index(packed[index]) ^ 1] + packed[index + 1 :]
        with pytest.raises(EnvelopeFormatError):
            Envelope.from_wire({**wire, "ct": tweaked})


@pytest.mark.req("ENF-SEC-005")
def test_review_wire_form_refuses_unknown_fields(sealed: Envelope) -> None:
    with pytest.raises(EnvelopeFormatError):
        Envelope.from_wire({**sealed.to_wire(), "evil": 1})
