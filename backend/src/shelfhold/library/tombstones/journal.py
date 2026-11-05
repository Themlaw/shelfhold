"""The journal `<root>/.shelfhold/tombstones`, format 2.0 (EF-BIB-053 (3), 02 section 5.9).

JSON Lines, append only. The first line is a versioned header (`schema_version` "major.minor",
ENF-OPS-013); every other line is `{v, seq, scope, kid, alg, nonce, ct}` with `nonce` and `ct` in
canonical base64url. The file is untrusted input: its size and every line are bounded, a line is
refused unless it has exactly these fields with exactly these types, and one bad line never stops
the reading of the next one. Parsing decides nothing: what a line means is the replay's job.
"""

import base64
import binascii
import json
from dataclasses import dataclass, field
from typing import Any, Final

from shelfhold.library.tombstones.values import SCOPES

FORMAT: Final = "2.0"
SUPPORTED_MAJOR: Final = 2
HEADER_KIND: Final = "shelfhold.tombstones"
ALG: Final = "xchacha20poly1305"
MAX_JOURNAL_BYTES: Final = 64 * 1024 * 1024
MAX_LINE_BYTES: Final = 8 * 1024
MAX_KID_CHARS: Final = 64
_LINE_FIELDS: Final = frozenset({"v", "seq", "scope", "kid", "alg", "nonce", "ct"})
NONCE_BYTES: Final = 24


class JournalFormatError(ValueError):
    """The journal as a whole cannot be read (too large, no header, newer major format)."""


@dataclass(frozen=True)
class JournalLine:
    seq: int
    scope: str
    kid: str
    nonce: bytes
    ct: bytes
    v: str = FORMAT
    alg: str = ALG

    def dumps(self) -> bytes:
        body = {
            "v": self.v,
            "seq": self.seq,
            "scope": self.scope,
            "kid": self.kid,
            "alg": self.alg,
            "nonce": _b64(self.nonce),
            "ct": _b64(self.ct),
        }
        return json.dumps(body, separators=(",", ":")).encode("ascii") + b"\n"


@dataclass(frozen=True)
class MalformedLine:
    number: int  # 1-based line number in the file, for the report (never the content)
    reason: str


@dataclass(frozen=True)
class Journal:
    header: dict[str, Any]
    lines: list[JournalLine] = field(default_factory=list)
    malformed: list[MalformedLine] = field(default_factory=list)


def header_bytes() -> bytes:
    body = {"kind": HEADER_KIND, "schema_version": FORMAT}
    return json.dumps(body, separators=(",", ":")).encode("ascii") + b"\n"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _unb64(text: object) -> bytes:
    if not isinstance(text, str):
        raise ValueError("not a string")
    raw = base64.urlsafe_b64decode(text.encode("ascii"))
    if _b64(raw) != text:
        raise ValueError("not canonical base64url")
    return raw


def parse_line(raw: bytes) -> JournalLine:
    if len(raw) > MAX_LINE_BYTES:
        raise ValueError("line too long")
    data = json.loads(raw.decode("ascii"))
    if not isinstance(data, dict) or set(data) != _LINE_FIELDS:
        raise ValueError("fields are not exactly v, seq, scope, kid, alg, nonce, ct")
    seq, scope, kid = data["seq"], data["scope"], data["kid"]
    if data["v"] != FORMAT or data["alg"] != ALG:
        raise ValueError("unknown version or algorithm")
    if isinstance(seq, bool) or not isinstance(seq, int) or not 1 <= seq < 2**63:
        raise ValueError("seq is a positive integer")
    if scope not in SCOPES:
        raise ValueError("unknown scope")
    if not isinstance(kid, str) or not 0 < len(kid) <= MAX_KID_CHARS or not kid.isascii():
        raise ValueError("kid is a short ASCII string")
    nonce, ct = _unb64(data["nonce"]), _unb64(data["ct"])
    if len(nonce) != NONCE_BYTES or len(ct) < 16:
        raise ValueError("nonce or ciphertext of the wrong length")
    return JournalLine(seq, scope, kid, nonce, ct)


def _major(version: object) -> int:
    if not isinstance(version, str):
        raise JournalFormatError("schema_version is not a string")
    major, sep, minor = version.partition(".")
    if not sep or not major.isdecimal() or not minor.isdecimal():
        raise JournalFormatError("schema_version is not major.minor")
    return int(major)


def parse_journal(raw: bytes) -> Journal:
    """The header and every line that parses; the others are listed, never trusted."""
    if len(raw) > MAX_JOURNAL_BYTES:
        raise JournalFormatError("journal too large")
    rows = raw.split(b"\n")
    if rows and rows[-1] == b"":
        rows.pop()
    if not rows:
        raise JournalFormatError("empty journal")
    try:
        header = json.loads(rows[0].decode("ascii")) if len(rows[0]) <= MAX_LINE_BYTES else None
    except (UnicodeDecodeError, ValueError):
        header = None
    if not isinstance(header, dict) or header.get("kind") != HEADER_KIND:
        raise JournalFormatError("no journal header")
    if _major(header.get("schema_version")) != SUPPORTED_MAJOR:
        raise JournalFormatError("journal of another major format")
    journal = Journal(header)
    for number, row in enumerate(rows[1:], start=2):
        try:
            journal.lines.append(parse_line(row))
        except (UnicodeDecodeError, ValueError, binascii.Error) as exc:
            reason = "line_too_long" if len(row) > MAX_LINE_BYTES else type(exc).__name__
            journal.malformed.append(MalformedLine(number, reason))
    return journal
