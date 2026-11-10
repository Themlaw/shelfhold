"""age v1 with a scrypt recipient, written and read here (C2SP age.md), for small contents.

Why not `pyrage` for this: age sizes the scrypt work factor of a file it writes from a timing of
the machine (about one second), and refuses at decrypt time a file it judges too costly by a new
timing. A backup sealed on an idle host could then not be opened on a loaded or slower one, which
is exactly the situation of a restore after a disaster. Here the work factor is **fixed** when
writing (`LOG_N`) and **bounded** when reading (`MAX_LOG_N`): no timing is involved. `pyrage` stays
for the x25519 stream of the backup body, which has no work factor.

Only a passphrase-sealed blob goes through this module (the keyring export, the ephemeral key of a
backup): kilobytes, held in memory. The format is the standard one: `age -d` and `pyrage` open what
`seal` writes, and `unseal` opens what `age` and `pyrage` write (`tests/unit/test_age_scrypt.py`).

The file (age.md, "Binary format" and "scrypt recipient"):

    age-encryption.org/v1
    -> scrypt <base64 salt, 16 bytes> <log_n>
    <base64 wrapped file key, 32 bytes, one line>
    --- <base64 header MAC, 32 bytes>
    <nonce, 16 bytes> then the STREAM payload

- the file key is 16 random bytes;
- the wrap key is scrypt(passphrase, salt = "age-encryption.org/v1/scrypt" + salt, N = 2**log_n,
  r = 8, p = 1, 32 bytes); the file key is wrapped by ChaCha20-Poly1305 under it, nonce of 12 zero
  bytes, no associated data;
- the header MAC is HMAC-SHA256 under HKDF-SHA256(file key, no salt, info "header") of the header
  up to and including the three dashes (not the space that follows);
- the payload key is HKDF-SHA256(file key, salt = nonce, info "payload"); the plaintext is cut in
  chunks of 64 KiB, each sealed by ChaCha20-Poly1305 with a 12-byte nonce made of an 11-byte
  big-endian counter and a last-chunk flag (1 on the last chunk, 0 otherwise). The last chunk is
  empty only when the whole plaintext is.
"""

import base64
import binascii
import hashlib
import hmac
import os
from typing import Final

import nacl.bindings
import nacl.exceptions

from shelfhold.infrastructure.secrets.errors import ExportError, WorkFactorError

# The cost written. The memory of scrypt is 128 * r * N bytes: 256 MiB at 18, which is age's own
# default (about one second on a recent core) and fits the 500 MB budget of a backup together with
# the ~110 MB floor of the process (measured: `docs/decisions/M1b.md`, L23). 17 would be 128 MiB,
# 16 would be 64 MiB; below that the passphrase protects less against an offline guess.
LOG_N: Final = 18
# The cost read. age writes 18 (a fast host may write 19 or 20); 2**23 would cost 8 GiB: a forged
# header must not be able to ask for it. 20 is 1 GiB.
MAX_LOG_N: Final = 20
MAX_HEADER_BYTES: Final = 1024  # the real one is ~190 bytes
R: Final = 8
P: Final = 1
CHUNK: Final = 64 * 1024
TAG: Final = 16
FILE_KEY_BYTES: Final = 16
SALT_BYTES: Final = 16
NONCE_BYTES: Final = 16

_VERSION: Final = b"age-encryption.org/v1"
_LABEL: Final = b"age-encryption.org/v1/scrypt"
_DAMAGED: Final = "wrong passphrase or damaged file"
_ZERO_NONCE: Final = bytes(12)


def _b64(raw: bytes) -> bytes:
    return base64.b64encode(raw).rstrip(b"=")


def _unb64(text: bytes, size: int) -> bytes:
    """Unpadded standard base64 of exactly `size` bytes, in its canonical form."""
    try:
        raw = base64.b64decode(text + b"=" * (-len(text) % 4), validate=True)
    except binascii.Error:
        raise ExportError(_DAMAGED) from None
    if len(raw) != size or _b64(raw) != text:
        raise ExportError(_DAMAGED)
    return raw


def _hkdf(key: bytes, salt: bytes, info: bytes) -> bytes:
    """HKDF-SHA256 (RFC 5869) of one block: 32 bytes are what age asks for."""
    pseudo_random = hmac.new(salt or bytes(32), key, hashlib.sha256).digest()
    return hmac.new(pseudo_random, info + b"\x01", hashlib.sha256).digest()


def _wrap_key(passphrase: str, salt: bytes, log_n: int) -> bytes:
    cost = 1 << log_n
    try:
        return hashlib.scrypt(
            passphrase.encode("utf-8"),
            salt=_LABEL + salt,
            n=cost,
            r=R,
            p=P,
            maxmem=128 * R * (cost + P + 2) + (1 << 20),
            dklen=32,
        )
    except (ValueError, MemoryError):
        raise ExportError("not enough memory to derive the key from the passphrase") from None


def _chunk_nonce(index: int, last: bool) -> bytes:
    return index.to_bytes(11, "big") + (b"\x01" if last else b"\x00")


def seal(clear: bytes, passphrase: str) -> bytes:
    """A binary age v1 file that holds `clear`, opened by the passphrase, with the fixed cost."""
    file_key = os.urandom(FILE_KEY_BYTES)
    salt = os.urandom(SALT_BYTES)
    wrapped = nacl.bindings.crypto_aead_chacha20poly1305_ietf_encrypt(
        file_key, None, _ZERO_NONCE, _wrap_key(passphrase, salt, LOG_N)
    )
    header = b"\n".join(
        [_VERSION, b"-> scrypt " + _b64(salt) + b" " + str(LOG_N).encode(), _b64(wrapped)]
    )
    header += b"\n---"
    mac = hmac.new(_hkdf(file_key, b"", b"header"), header, hashlib.sha256).digest()
    nonce = os.urandom(NONCE_BYTES)
    payload_key = _hkdf(file_key, nonce, b"payload")
    pieces = [clear[i : i + CHUNK] for i in range(0, len(clear), CHUNK)] or [b""]
    body = b"".join(
        nacl.bindings.crypto_aead_chacha20poly1305_ietf_encrypt(
            piece, None, _chunk_nonce(i, i == len(pieces) - 1), payload_key
        )
        for i, piece in enumerate(pieces)
    )
    return header + b" " + _b64(mac) + b"\n" + nonce + body


def _read_header(binary: bytes) -> tuple[bytes, int, bytes, bytes, bytes, int]:
    """`(header up to the dashes, log_n, salt, wrapped key, mac, start of the nonce)`, after the
    shape and the cost have been checked: nothing is derived for a header that fails here."""
    end = binary.find(b"\n---", 0, MAX_HEADER_BYTES)
    lines = binary[:end].split(b"\n") if end > 0 else []
    if len(lines) != 3 or lines[0] != _VERSION:
        raise ExportError(_DAMAGED)
    stanza = lines[1].split(b" ")
    if len(stanza) != 4 or stanza[:2] != [b"->", b"scrypt"]:
        raise ExportError(_DAMAGED)  # scrypt must be the only recipient
    text = stanza[3]
    if not (text.isdigit() and text.isascii() and 1 <= len(text) <= 3 and text[:1] != b"0"):
        raise ExportError(_DAMAGED)
    log_n = int(text)
    if log_n > MAX_LOG_N:
        raise WorkFactorError(
            f"the file asks for scrypt work 2**{log_n}, more than the 2**{MAX_LOG_N} this "
            "program accepts; it was not opened"
        )
    salt = _unb64(stanza[2], SALT_BYTES)
    wrapped = _unb64(lines[2], FILE_KEY_BYTES + TAG)
    mac_line, newline, _rest = binary[end + 4 :].partition(b"\n")
    if not newline or not mac_line.startswith(b" "):
        raise ExportError(_DAMAGED)
    mac = _unb64(mac_line[1:], 32)
    return binary[: end + 4], log_n, salt, wrapped, mac, end + 4 + len(mac_line) + 1


def unseal(binary: bytes, passphrase: str) -> bytes:
    """The clear of a file made by `seal`, by `age` or by `pyrage`. Whatever is wrong (not age,
    wrong passphrase, damaged) is one `ExportError`; a cost above `MAX_LOG_N` is the
    `WorkFactorError` that says so, raised before any derivation."""
    header, log_n, salt, wrapped, mac, start = _read_header(binary)
    try:
        file_key = nacl.bindings.crypto_aead_chacha20poly1305_ietf_decrypt(
            wrapped, None, _ZERO_NONCE, _wrap_key(passphrase, salt, log_n)
        )
        expected = hmac.new(_hkdf(file_key, b"", b"header"), header, hashlib.sha256).digest()
        if not hmac.compare_digest(mac, expected):
            raise ExportError(_DAMAGED)
        nonce, body = binary[start : start + NONCE_BYTES], binary[start + NONCE_BYTES :]
        if len(nonce) != NONCE_BYTES or len(body) < TAG:
            raise ExportError(_DAMAGED)
        payload_key = _hkdf(file_key, nonce, b"payload")
        size = CHUNK + TAG
        count = -(-len(body) // size)
        out = []
        for index in range(count):
            last = index == count - 1
            piece = body[index * size : (index + 1) * size]
            if last and index > 0 and len(piece) == TAG:
                raise ExportError(_DAMAGED)  # an empty last chunk after others
            out.append(
                nacl.bindings.crypto_aead_chacha20poly1305_ietf_decrypt(
                    piece, None, _chunk_nonce(index, last), payload_key
                )
            )
        return b"".join(out)
    except nacl.exceptions.CryptoError:
        raise ExportError(_DAMAGED) from None
