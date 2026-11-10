"""Forms a credential takes inside text, and the test that finds it there (EF-SRC-035).

A key placed in a URL is rarely written as it was received: it is percent-encoded once or twice,
`+` stands for a space, the hexadecimal digits are in either case, the path keeps its slashes.
Both sides use the same two functions, so what is masked in a log line is exactly what the guards
refuse in a stored reference.

Nothing here logs, and nothing quotes the value it was given.
"""

import re
from collections.abc import Iterable
from urllib.parse import quote, quote_plus, unquote, unquote_plus

__all__ = ["contains_secret", "decoded_views", "secret_forms"]

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
    return frozenset(forms | {_lower_hex(f) for f in forms})


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


def contains_secret(text: str, known: Iterable[str]) -> bool:
    """True when `text` holds one of the known values, as is, in a URL form or after decoding."""
    values = [k for k in known if k]
    if not values or not text:
        return False
    views = decoded_views(text)
    return any(value in view for value in values for view in views)
