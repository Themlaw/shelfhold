"""The tombstone cipher: XChaCha20-Poly1305 under a key derived from the keyring (EF-BIB-054).

`K_tomb = HKDF-SHA256(key of the kid, info "shelfhold/tombstone/v1")`, one per kid, distinct from
the key that seals secrets (same material, other purpose). A random 192-bit nonce per line: the
same values purged twice give two unrelated ciphertexts, so neither a dictionary of titles nor
the knowledge of a `content_hash` lets anyone without the keyring recognise a line.

Associated data = domain || format || scope || root || seq, each length-prefixed. `root` is the
`root_id` of the root whose journal holds the line (never the `instance_id`), or `db` for the
table. A line copied to another root, given another scope or seq, or changed by one bit does not
open (I41, I46). HKDF is RFC 5869 over the standard library's HMAC; the AEAD is libsodium's.
"""

import base64
import hashlib
import hmac
import struct
from typing import Final

import nacl.bindings
import nacl.exceptions
import nacl.utils

from shelfhold.infrastructure.secrets.envelope import ALG, KEY_BYTES, NONCE_BYTES, TAG_BYTES
from shelfhold.infrastructure.secrets.errors import DecryptionError, EnvelopeFormatError

__all__ = [
    "ALG",
    "DB_ROOT",
    "FORMAT",
    "HKDF_INFO",
    "NONCE_BYTES",
    "TAG_BYTES",
    "build_aad",
    "chain_mac",
    "chain_step",
    "hkdf_sha256",
    "kid_tag",
    "open_values",
    "seal_values",
    "tombstone_key",
    "verify_chain_mac",
    "verify_kid_tag",
]

FORMAT: Final = "2.0"  # journal format 2.0; the table row carries `v = 2`
HKDF_INFO: Final = b"shelfhold/tombstone/v1"
KID_TAG_INFO: Final = b"shelfhold/tombstone-kid/v1"  # sentinel registry entries (review PR #52)
DB_ROOT: Final = "db"  # the `root` coordinate of a row of `content_tombstone`
CHAIN_INFO: Final = b"shelfhold/tombstone-chain/v1"  # the chained label of a journal (L26)
_AAD_DOMAIN: Final = b"shelfhold/tombstone"
_HASH_BYTES: Final = hashlib.sha256().digest_size


def hkdf_sha256(ikm: bytes, *, info: bytes, salt: bytes = b"", length: int = KEY_BYTES) -> bytes:
    """RFC 5869, extract then expand."""
    if length > 255 * _HASH_BYTES:
        raise ValueError("HKDF output too long")
    prk = hmac.new(salt or b"\x00" * _HASH_BYTES, ikm, hashlib.sha256).digest()
    out, block, counter = b"", b"", 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]


def tombstone_key(material: bytes) -> bytes:
    """The tombstone key of one kid of the keyring."""
    if len(material) != KEY_BYTES:
        raise ValueError("keyring key has the wrong length")
    return hkdf_sha256(material, info=HKDF_INFO)


def build_aad(fmt: str, scope: str, root: str, seq: int) -> bytes:
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise ValueError("seq is a positive integer")
    parts = [_AAD_DOMAIN]
    for coordinate in (fmt, scope, root):
        raw = coordinate.encode("utf-8")
        parts.append(struct.pack(">I", len(raw)) + raw)
    parts.append(struct.pack(">Q", seq))
    return b"\x00".join(parts)


def seal_values(
    material: bytes, plaintext: bytes, *, scope: str, root: str, seq: int
) -> tuple[bytes, bytes]:
    """`(nonce, ct)` of the values of one tombstone, bound to its scope, root and seq."""
    nonce = nacl.utils.random(NONCE_BYTES)
    aad = build_aad(FORMAT, scope, root, seq)
    ct = nacl.bindings.crypto_aead_xchacha20poly1305_ietf_encrypt(
        plaintext, aad, nonce, tombstone_key(material)
    )
    return nonce, ct


def open_values(
    material: bytes, nonce: bytes, ct: bytes, *, scope: str, root: str, seq: int
) -> bytes:
    if len(nonce) != NONCE_BYTES or len(ct) < TAG_BYTES:
        raise EnvelopeFormatError("tombstone nonce or ciphertext has the wrong length")
    aad = build_aad(FORMAT, scope, root, seq)
    try:
        return nacl.bindings.crypto_aead_xchacha20poly1305_ietf_decrypt(
            ct, aad, nonce, tombstone_key(material)
        )
    except nacl.exceptions.CryptoError:
        raise DecryptionError from None


def _kid_tag_message(root_id: str, kid: str) -> bytes:
    parts = [_AAD_DOMAIN + b"/kid"]
    for coordinate in (root_id, kid):
        raw = coordinate.encode("utf-8")
        parts.append(struct.pack(">I", len(raw)) + raw)
    return b"\x00".join(parts)


def kid_tag(signer_material: bytes, root_id: str, kid: str) -> str:
    """Tag of one entry `kid` of the registry in the sentinel of `root_id`, under one key of the
    keyring (the signer). A third party that writes the sentinel cannot make one: an entry whose
    tag does not verify under a key we hold is not a registration (EF-BIB-053, R58)."""
    if len(signer_material) != KEY_BYTES:
        raise ValueError("keyring key has the wrong length")
    key = hkdf_sha256(signer_material, info=KID_TAG_INFO)
    digest = hmac.new(key, _kid_tag_message(root_id, kid), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii")


def verify_kid_tag(signer_material: bytes, root_id: str, kid: str, tag: object) -> bool:
    if not isinstance(tag, str):
        return False
    return hmac.compare_digest(kid_tag(signer_material, root_id, kid), tag)


# -- the chained label of a journal (EF-BIB-054 (6), M1b L26) -----------------------------------


def chain_step(previous: str, seq: int, tag: bytes) -> str:
    """The label after one more line: `H(previous || seq || AEAD tag of the line)`. The tag is the
    last 16 bytes of the ciphertext, so the label commits to every line before it, in `seq` order
    (the order of the file is not part of it: reordering or repeating lines changes nothing)."""
    if len(tag) != TAG_BYTES:
        raise ValueError("an AEAD tag has 16 bytes")
    digest = hashlib.sha256(
        _AAD_DOMAIN + b"/chain\x00" + previous.encode("ascii") + struct.pack(">Q", seq) + tag
    ).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii")


def _chain_message(root_id: str, seq: int, count: int, label: str) -> bytes:
    parts = [_AAD_DOMAIN + b"/chain-state"]
    for coordinate in (root_id, label):
        raw = coordinate.encode("utf-8")
        parts.append(struct.pack(">I", len(raw)) + raw)
    parts.append(struct.pack(">QQ", seq, count))
    return b"\x00".join(parts)


def chain_mac(signer_material: bytes, root_id: str, seq: int, count: int, label: str) -> str:
    """MAC of the state `(seq, count, label)` kept in the sentinel of `root_id`, under one key of
    the keyring: a third party that rewrites the sentinel cannot make one that verifies."""
    if len(signer_material) != KEY_BYTES:
        raise ValueError("keyring key has the wrong length")
    key = hkdf_sha256(signer_material, info=CHAIN_INFO)
    digest = hmac.new(key, _chain_message(root_id, seq, count, label), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii")


def verify_chain_mac(
    signer_material: bytes, root_id: str, seq: int, count: int, label: str, mac: object
) -> bool:
    if not isinstance(mac, str):
        return False
    return hmac.compare_digest(chain_mac(signer_material, root_id, seq, count, label), mac)
