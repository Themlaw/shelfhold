"""The secret envelope `{v, kid, alg, ct}` (ENF-SEC-005).

XChaCha20-Poly1305 from libsodium through PyNaCl, nothing home-made: a random 192-bit nonce per
seal and associated data binding the ciphertext to its format version and to the exact
`(table, column, row id)` it is stored in, so a secret copied to another row does not open.

The wire form carries `ct` as URL-safe base64 of `nonce || ciphertext`. In the database the nonce
has its own column (`secret_nonce`), see `to_columns`.
"""

import struct
from dataclasses import dataclass
from typing import Any

import nacl.bindings
import nacl.exceptions
import nacl.utils

from shelfhold.infrastructure.secrets.errors import (
    DecryptionError,
    EnvelopeFormatError,
    UnsupportedAlgorithmError,
    UnsupportedVersionError,
)

ALG = "xchacha20poly1305"  # same identifier as `secret_key.alg` and the tombstone columns
FORMAT_VERSION = 1
SUPPORTED_VERSIONS = frozenset({FORMAT_VERSION})
KEY_BYTES = nacl.bindings.crypto_aead_xchacha20poly1305_ietf_KEYBYTES  # 32
NONCE_BYTES = nacl.bindings.crypto_aead_xchacha20poly1305_ietf_NPUBBYTES  # 24
TAG_BYTES = nacl.bindings.crypto_aead_xchacha20poly1305_ietf_ABYTES  # 16
_AAD_DOMAIN = b"shelfhold/secret-envelope"


def build_aad(version: int, table: str, column: str, row_id: str) -> bytes:
    """Associated data: domain, version, then each coordinate prefixed by its length.

    Length prefixes keep `("a", "bc")` and `("ab", "c")` apart.
    """
    parts = [_AAD_DOMAIN, struct.pack(">I", version & 0xFFFFFFFF)]
    for coordinate in (table, column, row_id):
        raw = coordinate.encode("utf-8")
        parts.append(struct.pack(">I", len(raw)) + raw)
    return b"\x00".join(parts)


@dataclass(frozen=True)
class Envelope:
    v: int
    kid: str
    alg: str
    nonce: bytes
    ct: bytes

    def to_columns(self, prefix: str = "secret_") -> dict[str, Any]:
        return {
            f"{prefix}kid": self.kid,
            f"{prefix}v": self.v,
            f"{prefix}nonce": self.nonce,
            f"{prefix}ct": self.ct,
        }

    @classmethod
    def from_columns(cls, columns: Any, prefix: str = "secret_", *, alg: str = ALG) -> "Envelope":
        """Read the four sealed columns of a row (the algorithm is the key's, `secret_key.alg`)."""
        return cls(
            columns[f"{prefix}v"],
            columns[f"{prefix}kid"],
            alg,
            bytes(columns[f"{prefix}nonce"]),
            bytes(columns[f"{prefix}ct"]),
        )


def seal(
    key: bytes, kid: str, plaintext: bytes, *, table: str, column: str, row_id: str
) -> Envelope:
    nonce = nacl.utils.random(NONCE_BYTES)
    aad = build_aad(FORMAT_VERSION, table, column, row_id)
    ct = nacl.bindings.crypto_aead_xchacha20poly1305_ietf_encrypt(plaintext, aad, nonce, key)
    return Envelope(FORMAT_VERSION, kid, ALG, nonce, ct)


def open_with(key: bytes, envelope: Envelope, *, table: str, column: str, row_id: str) -> bytes:
    """Open an envelope whose version and algorithm were already checked by the caller."""
    if len(envelope.nonce) != NONCE_BYTES:
        raise EnvelopeFormatError("envelope nonce has the wrong length")
    if len(envelope.ct) < TAG_BYTES:
        raise EnvelopeFormatError("envelope ct is shorter than the authentication tag")
    aad = build_aad(envelope.v, table, column, row_id)
    try:
        return nacl.bindings.crypto_aead_xchacha20poly1305_ietf_decrypt(
            envelope.ct, aad, envelope.nonce, key
        )
    except nacl.exceptions.CryptoError:
        raise DecryptionError from None


def check_supported(envelope: Envelope) -> None:
    if envelope.v not in SUPPORTED_VERSIONS:
        raise UnsupportedVersionError(envelope.v)
    if envelope.alg != ALG:
        raise UnsupportedAlgorithmError(envelope.alg)
