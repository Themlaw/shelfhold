"""The secret of a device token (ADR-0053 §4, EF-USR-008, EF-LEC-009, ENF-SEC-013).

Generated, never chosen: 26 characters of a 32-symbol alphabet, 130 random bits, typed once on
the keyboard of an e-reader. The alphabet has no similar-looking characters (no `i`, `l`, `o`,
`u`) and is lower case, the state an e-reader keyboard starts in. The public identifier
(`x-auth-user`, the name of an HTTP Basic user) is 8 characters of the same alphabet and says
nothing of the secret.

What is stored: one SHA-256 of `md5(secret)` in lower-case hexadecimal, and nothing else. KOReader
cannot do otherwise than send `md5(typed secret)` as `x-auth-key` (`plugins/kosync.koplugin`,
`md5(password)`), so the server must know what that md5 is worth; it never keeps the md5 itself,
which would be a replayable credential, and the secret a Basic client sends is reduced to the same
md5 and then to the same digest. With 130 random bits there is nothing to guess, so a salt, a work
factor or a keyed hash would add a key to rotate and no protection.
"""

import base64
import binascii
import hashlib
import re
import secrets
from dataclasses import dataclass, field

ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"  # Crockford's, lower case: no i, l, o, u
SECRET_LENGTH = 26  # 26 x 5 bits = 130 bits
PUBLIC_ID_LENGTH = 8
_MD5_HEX = re.compile(r"[0-9a-fA-F]{32}")
_SEPARATORS = re.compile(r"[\s\-]")


@dataclass(frozen=True)
class DeviceCredential:
    public_id: str
    secret: str = field(repr=False)  # shown once, never anywhere else

    def __str__(self) -> str:
        return f"DeviceCredential(public_id={self.public_id!r})"


def _draw(length: int) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def new_device_credential() -> DeviceCredential:
    return DeviceCredential(public_id=_draw(PUBLIC_ID_LENGTH), secret=_draw(SECRET_LENGTH))


def normalise_secret(typed: str) -> str:
    """What a person typed (with the groups' separators, in capitals) reduced to the canonical
    secret. For HTTP Basic only: KOReader's `x-auth-key` is the md5 of exactly what was typed."""
    return _SEPARATORS.sub("", typed).lower()


def md5_key(secret: str) -> str:
    """The `x-auth-key` KOReader sends for this secret: the protocol's own, not a protection."""
    return hashlib.md5(secret.encode("utf-8"), usedforsecurity=False).hexdigest()


def _digest_of(key: str) -> str:
    return "sha256:" + hashlib.sha256(key.encode("ascii")).hexdigest()


def secret_digest(secret: str) -> str:
    """The stored value, from a secret a Basic client sent (reduced to its canonical form)."""
    return _digest_of(md5_key(normalise_secret(secret)))


def key_digest(key: object) -> str | None:
    """The stored value, from the `x-auth-key` of KOReader; None when it is not an md5 digest."""
    if not isinstance(key, str) or not _MD5_HEX.fullmatch(key):
        return None
    return _digest_of(key.lower())


# The token a Kobo carries in its sync address (M6 L17a, EF-INT-012): the whole credential as one
# path segment, `base64url("<public_id>:<secret>")`, shown once at the pairing. Nothing else is in
# the path, and nothing of it is ever logged: the segment after `/kobo/` is masked whole
# (`infrastructure.logging`).
_KOBO_SEGMENT = re.compile(r"[0-9a-hjkmnp-tv-z]+")  # the credential's alphabet, as it travels
_MAX_SEGMENT = 128  # a path segment is short; a longer one is not a token, whatever it decodes to


def kobo_path_token(public_id: str, secret: str) -> str:
    """The path segment of the sync address: both halves of the credential, as one word."""
    raw = f"{public_id}:{secret}".encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def kobo_path_credential(segment: object) -> tuple[str, str] | None:
    """The credential a path segment carries, or None when it is not one: not decodable, not the
    two halves of a device credential, or too long. Nothing is guessed, nothing is kept."""
    if not isinstance(segment, str) or not segment or len(segment) > _MAX_SEGMENT:
        return None
    try:
        decoded = base64.b64decode(
            segment + "=" * (-len(segment) % 4), altchars=b"-_", validate=True
        ).decode("ascii")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    public_id, separator, secret = decoded.partition(":")
    if not separator:
        return None
    if (
        len(public_id) != PUBLIC_ID_LENGTH
        or len(secret) != SECRET_LENGTH
        or not _KOBO_SEGMENT.fullmatch(public_id)
        or not _KOBO_SEGMENT.fullmatch(secret)
    ):
        return None
    return public_id, secret
