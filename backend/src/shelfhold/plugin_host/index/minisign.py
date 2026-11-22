"""The minisign verification of the plugin index (EF-SRC-017, D14, ADR-0056).

Only the verification lives here: no secret key ever crosses into the product, and the tests
make their own keys (D-P3). The format is the prehashed one minisign writes by default since
0.4 (`ED`: an Ed25519 signature of the BLAKE2b-512 digest of the file), verified with `pynacl`;
the older non-prehashed `Ed` format is refused, whoever signed it. The public key a repository
is pinned by is the base64 blob of a minisign key file (`Ed` + key id + raw key), and the key id
it carries is the fingerprint the admin checks and the signature must repeat.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

__all__ = ["PinnedKey", "PluginIndexError", "parse_public_key", "verify_detached"]

_PUBLIC_BLOB = 42  # "Ed" + key id (8) + raw Ed25519 key (32)
_SIGNATURE_BLOB = 74  # "ED" + key id (8) + signature (64)
_LEGACY_ALGO = b"Ed"
_PREHASHED_ALGO = b"ED"


class PluginIndexError(Exception):
    """A refusal of the index client; `code` is stable (shown to the admin, never a secret)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class PinnedKey:
    """The signer an index repository is pinned by: the base64 blob and its key id."""

    b64: str
    key_id: str  # 16 uppercase hex characters
    raw: bytes  # the decoded blob, "Ed" + key id + raw key


def parse_public_key(text: str, *, code: str = "fingerprint_invalid") -> PinnedKey:
    """The minisign public key the admin gave: its key id is the fingerprint shown everywhere."""
    stripped = text.strip()
    try:
        blob = base64.b64decode(stripped, validate=True)
    except (binascii.Error, ValueError):
        raise PluginIndexError(code, "the public key is not the base64 minisign blob") from None
    if len(blob) != _PUBLIC_BLOB or blob[:2] != _LEGACY_ALGO:
        raise PluginIndexError(code, "the public key is not a minisign Ed key")
    return PinnedKey(stripped, blob[2:10].hex().upper(), blob)


def _decode(line: str, what: str) -> bytes:
    try:
        return base64.b64decode(line, validate=True)
    except (binascii.Error, ValueError):
        raise PluginIndexError("signature_invalid", f"the {what} is not base64") from None


def verify_detached(document: bytes, signature_file: bytes, key: PinnedKey) -> str:
    """Verify a detached minisign signature; the trusted comment is returned, signed and checked.

    The signature covers the BLAKE2b-512 digest of the document (the prehashed format); the
    global signature covers the trusted comment and the signature itself, which is what binds
    the comment to the signed bytes. Refusals are typed: the old non-prehashed format, a key id
    that is not the pinned one, a bad signature of the file or of the comment.
    """
    lines = signature_file.decode("utf-8", "replace").splitlines()
    if len(lines) != 4:
        raise PluginIndexError("signature_invalid", "the signature file has not its four lines")
    blob = _decode(lines[1], "signature")
    if len(blob) != _SIGNATURE_BLOB:
        raise PluginIndexError("signature_invalid", "the signature blob has not its 74 bytes")
    if blob[:2] == _LEGACY_ALGO:
        raise PluginIndexError(
            "legacy_signature", "the old non-prehashed minisign format is refused"
        )
    if blob[:2] != _PREHASHED_ALGO:
        raise PluginIndexError("signature_invalid", "the signature algorithm is not Ed25519")
    if blob[2:10].hex().upper() != key.key_id:
        raise PluginIndexError("key_mismatch", "the signature is not by the pinned key")
    digest = hashlib.blake2b(document, digest_size=64).digest()
    try:
        VerifyKey(key.raw[10:]).verify(digest, blob[10:])
    except BadSignatureError:
        raise PluginIndexError(
            "bad_signature", "the signature does not match the document"
        ) from None
    comment = lines[2]
    if not comment.startswith("trusted comment: "):
        raise PluginIndexError("signature_invalid", "the trusted comment line is malformed")
    text = comment.removeprefix("trusted comment: ")
    global_signature = _decode(lines[3], "global signature")
    try:  # minisign signs the comment's text, without the line's prefix, then the signature
        VerifyKey(key.raw[10:]).verify(text.encode("utf-8") + blob[10:], global_signature)
    except BadSignatureError:
        raise PluginIndexError(
            "bad_global_signature", "the trusted comment is not the one that was signed"
        ) from None
    return text
