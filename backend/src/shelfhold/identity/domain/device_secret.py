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
