"""Forms a credential takes inside text, and the test that finds it there (EF-SRC-035).

A key placed in a URL is rarely written as it was received: it is percent-encoded once or twice,
`+` stands for a space, the hexadecimal digits are in either case, the path keeps its slashes.
Both sides use the same two functions, so what is masked in a log line is exactly what the guards
refuse in a stored reference.

Nothing here logs, and nothing quotes the value it was given.
"""

import base64
import binascii
import math
import re
from collections import Counter
from collections.abc import Iterable, Iterator
from urllib.parse import quote, quote_plus, unquote, unquote_plus

__all__ = [
    "SecretIndex",
    "contains_secret",
    "credentialish",
    "decoded_views",
    "secret_forms",
    "sensitive_name",
    "tokenlike",
    "without_internal_ids",
]

_DECODE_ROUNDS = 4
_HEX = re.compile(r"%[0-9A-Fa-f]{2}")


def _lower_hex(text: str) -> str:
    return _HEX.sub(lambda m: m.group().lower(), text)


def secret_forms(value: str) -> frozenset[str]:
    """The value as it is, and as a URL would carry it: percent-encoded (with or without the
    slashes kept), form-encoded, encoded twice, each with upper- and lower-case hexadecimal."""
    once = quote(value, safe="")
    forms = {
        value,
        once,
        quote(value, safe="/"),
        quote_plus(value),
        quote(once, safe=""),
        quote(quote(once, safe=""), safe=""),
    }
    forms |= {_lower_hex(f) for f in forms}
    for form in (value, once):  # base64 of the value, padded or not, standard or URL-safe
        raw = form.encode("utf-8", errors="replace")
        for encoder in (base64.b64encode, base64.urlsafe_b64encode):
            text = encoder(raw).decode()
            forms |= {
                text,
                text.rstrip("="),
                quote(text, safe=""),
                quote(text.rstrip("="), safe=""),
            }
    return frozenset(forms)


_B64_RUN = re.compile(r"[A-Za-z0-9+_-]{12,}={0,2}")  # `/` splits the path: a segment per run


def _base64_views(text: str) -> Iterator[str]:
    """What the base64-looking runs of `text` decode to (twice at most: a base64 JSON that holds a
    base64 value). A run that is not base64, or not text, is skipped."""
    pending = [text]
    for _ in range(2):
        found: list[str] = []
        for chunk in pending:
            for match in _B64_RUN.finditer(chunk):
                run = match.group().rstrip("=")
                padded = run + "=" * (-len(run) % 4)
                try:
                    raw = base64.b64decode(padded, altchars=b"-_", validate=False)
                except (binascii.Error, ValueError):
                    continue
                decoded = raw.decode("utf-8", errors="ignore")
                if decoded:
                    found.append(decoded)
                    yield decoded
        pending = found


def decoded_views(text: str) -> tuple[str, ...]:
    """The text, then the text decoded once, twice, ... (`%2520` -> `%20` -> ` `), with `+` read
    both as itself and as a space. Stops when nothing changes."""
    views: list[str] = [text]
    current = text
    for _ in range(_DECODE_ROUNDS):
        if "%" not in current and "+" not in current:
            break
        for decoder in (unquote, unquote_plus):
            candidate = decoder(current)
            if candidate not in views:
                views.append(candidate)
        current = unquote(current)
    return tuple(views)


class SecretIndex:
    """A set of known values that finds any of them in a text in one pass.

    The registry holds a dozen forms of every secret, and the guards look at every parameter of
    every write: a loop over the values for each text costs too much once a long-running process
    has registered a few hundred secrets. The text is slid over once, each window of the width of
    the shortest value is looked up in a table keyed by the value's start, and only a hit is
    compared.
    """

    __slots__ = ("_table", "_width", "values")

    def __init__(self, values: Iterable[str] = ()) -> None:
        self.values: frozenset[str] = frozenset(v for v in values if v)
        self._width = min((len(v) for v in self.values), default=1)
        table: dict[str, list[str]] = {}
        for value in self.values:
            table.setdefault(value[: self._width], []).append(value)
        self._table = table

    def __bool__(self) -> bool:
        return bool(self.values)

    def __iter__(self) -> Iterator[str]:
        return iter(self.values)

    def find(self, text: str) -> bool:
        width, table = self._width, self._table
        for start in range(len(text) - width + 1):
            candidates = table.get(text[start : start + width])
            if candidates is not None and any(text.startswith(v, start) for v in candidates):
                return True
        return False


def contains_secret(text: str, known: "Iterable[str] | SecretIndex") -> bool:
    """True when `text` holds one of the known values, as is, in a URL form or after decoding."""
    index = known if isinstance(known, SecretIndex) else SecretIndex(known)
    if not index or not text:
        return False
    return any(
        index.find(inner) for view in decoded_views(text) for inner in (view, *_base64_views(view))
    )


# -- what a credential looks like when nobody registered it ------------------------------------

# Words that name a credential once a parameter name is cut into words (`accessToken`,
# `X-Amz-Security-Token`, `token[]`, `Key-Pair-Id`). Whole words only: `author` is not `auth`.
_SENSITIVE_WORDS = frozenset(
    {
        "key", "t", "token", "hmac", "policy", "signature", "sig", "secret", "password", "passwd",
        "pwd", "auth", "authorization", "session", "sid", "cookie", "credential", "credentials",
        "code", "jwt", "bearer", "sas", "otp", "sso", "apikey", "accesstoken", "refreshtoken",
        "idtoken", "authtoken", "sessionid", "jsessionid", "phpsessid", "pass", "passphrase",
        "komga",
    }
)  # fmt: skip
# Long enough not to be found inside a word that means something else: any name that *contains*
# one of these is sensitive (`sessionkey`, `mytoken`). `auth`, `key`, `sig`, `code` are not here:
# `author`, `keyword` and `sigma` are not credentials.
_LONG_WORDS = ("token", "secret", "password", "passwd", "passphrase", "cookie", "credential",
               "session", "signature", "apikey")  # fmt: skip
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_JWT = re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]*")
_BASE64URL = re.compile(r"[A-Za-z0-9_-]+")
_ALNUM = re.compile(r"[A-Za-z0-9]+")
# Shelfhold's own identifiers (ULID, Crockford base32) are not credentials: they are shown to the
# administrator and typed back. A *registered* secret is matched by value before shape is asked.
_INTERNAL_ID = re.compile(
    r"(?<![A-Za-z0-9])(?:[0-9A-HJKMNP-TV-Z]{26}"
    r"|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?![A-Za-z0-9])",
    re.IGNORECASE,
)  # a ULID or a UUID standing alone in a segment, or between `-`, `_`, `.`, `:` (`ch-<ulid>`)


def _words(name: str) -> list[str]:
    text = unquote(name)
    while text.endswith("[]"):
        text = text[:-2]
    return [w.lower() for w in re.split(r"[^A-Za-z0-9]+", _CAMEL.sub("-", text)) if w]


def sensitive_name(name: str) -> bool:
    """A parameter name that says it holds a credential, read word by word (camelCase, `-`, `_`,
    a trailing `[]`) and never as a substring: `author` is not `auth`."""
    words = _words(name)
    joined = "".join(words)
    return (
        any(w in _SENSITIVE_WORDS for w in words)
        or joined in _SENSITIVE_WORDS
        or any(w in joined for w in _LONG_WORDS)
    )


def _entropy(text: str) -> float:
    counts = Counter(text)
    return -sum(n / len(text) * math.log2(n / len(text)) for n in counts.values())


def without_internal_ids(text: str) -> str:
    """`text` with the ULIDs and UUIDs Shelfhold itself writes taken out (`ch-<ulid>` -> `ch-`)."""
    return _INTERNAL_ID.sub("", text)


def tokenlike(text: str) -> bool:
    """Shaped like an opaque credential, whatever the place it sits in: a JWT, a mixed
    case run of 12 or more (base64url), or a run of 20 or more letters and digits with both and a
    spread of characters (hexadecimal, base36). A word, a slug, a number and a short id are not."""
    if _INTERNAL_ID.search(text):
        text = _INTERNAL_ID.sub("", text)  # `ch-<ulid>`: judge what is left, never the identifier
        if not text.strip("-_.:"):
            return False
    if _JWT.fullmatch(text):
        return True
    has_digit = any(c.isdigit() for c in text)
    has_lower = any(c.islower() for c in text)
    has_upper = any(c.isupper() for c in text)
    if len(text) >= 12 and _BASE64URL.fullmatch(text) and has_digit and has_lower and has_upper:
        return True
    return (
        len(text) >= 20
        and _ALNUM.fullmatch(text) is not None
        and has_digit
        and (has_lower or has_upper)
        and _entropy(text) >= 3.2
    )


def credentialish(value: str) -> bool:
    """The value of a parameter whose *name* says credential: long enough and not a plain number
    or word (`session=2024`, `sid=42` and `code=fr` are not credentials)."""
    if tokenlike(value):
        return True
    has_digit = any(c.isdigit() for c in value)
    has_alpha = any(c.isalpha() for c in value)
    return len(value) >= 10 and has_digit and has_alpha
