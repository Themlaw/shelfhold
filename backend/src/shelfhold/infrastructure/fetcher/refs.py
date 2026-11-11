"""Remote references without a secret (EF-SRC-035).

A reference a source hands back (`remote_id`, `remote_unit_id`, a file URL) is an identifier. It
is opaque, it is stable when the credentials rotate and it never holds a secret: not a value the
Fetcher injects, not a `user:password@` part, and not a token a source placed in the query or in a
path segment. This module is the pure part: it says whether a text holds one, and rewrites a key
segment to the declared `{secret}` template. `plugin_host.refs_guard` applies it to what a source
returns; `infrastructure.write_guard` is the last line, at the database.

No message here quotes the reference or the value: the reason is a closed word.
"""

import re
from collections.abc import Iterable
from typing import Final

from shelfhold.infrastructure.secret_text import (
    SecretIndex,
    contains_secret,
    credentialish,
    decoded_views,
    secret_forms,
    sensitive_name,
    tokenlike,
)
from shelfhold.sdk.refs import SECRET_PLACEHOLDER

__all__ = [
    "SECRET_PLACEHOLDER",
    "SecretInRef",
    "check_ref",
    "has_userinfo",
    "projectable",
    "rewrite_ref",
]

# The authority of a reference as a URL parser reads it: an optional scheme, any run of slashes or
# backslashes (`https:/user:pw@h` and `https:\\\\user:pw@h` still have a userinfo), then up to the
# first `/`, `\\`, `?` or `#`. Spaces inside are kept: they are part of the userinfo.
_AUTHORITY: Final = re.compile(
    r"^\s*(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*:)?(?P<slashes>[/\\]*)(?P<auth>[^/\\?#]*)"
)
_JWT_SEGMENT: Final = re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]*")


class SecretInRef(ValueError):
    """A reference holds, or may hold, a secret. `reason` is one of `secret` (a value the Fetcher
    knows), `userinfo`, `query`, `fragment`, `path`; it never quotes the reference."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"the reference holds a secret ({reason})")
        self.reason = reason


def has_userinfo(text: str) -> bool:
    """A `user[:password]@` part, as written, encoded, or spelled the way a URL parser accepts."""
    for view in decoded_views(text):
        match = _AUTHORITY.match(view)
        if match is None or "@" not in match.group("auth"):
            continue
        slashes = len(match.group("slashes"))
        if match.group("scheme") or slashes >= 2 or (slashes == 0 and ":" in match.group("auth")):
            return True
    return False


def _sensitive_pairs(part: str) -> bool:
    for piece in re.split(r"[&;]", part):
        name, equals, value = piece.partition("=")
        if not equals or not value or value == SECRET_PLACEHOLDER:
            continue
        if _JWT_SEGMENT.fullmatch(value) or (sensitive_name(name) and credentialish(value)):
            return True
    return False


def _pattern_reason(ref: str) -> str | None:
    """A secret nobody registered still shows by what it is shaped like and where it sits: the
    value of a credential-named parameter, a token-shaped path segment or fragment. A word, a
    slug or a short number never counts."""
    for view in decoded_views(ref):
        body, _, fragment = view.partition("#")
        path, _, query = body.partition("?")
        if _sensitive_pairs(query):
            return "query"
        if fragment and (_sensitive_pairs(fragment) if "=" in fragment else tokenlike(fragment)):
            return "fragment"
        for segment in path.split("/"):
            head, _, matrix = segment.partition(";")
            if tokenlike(head) or _sensitive_pairs(matrix):
                return "path"
    return None


def _index(known: "Iterable[str] | SecretIndex") -> SecretIndex:
    return known if isinstance(known, SecretIndex) else SecretIndex(known)


def check_ref(ref: str, known: "Iterable[str] | SecretIndex", *, patterns: bool = True) -> str:
    """`ref` when it is free of secrets, else `SecretInRef`.

    `known` is the set of values the Fetcher injects (and any registered secret). `patterns=False`
    is for a URL that is used and not stored (a file to download, a signed image address): only a
    known value and a userinfo part are refused, a signature in a query is its business.
    """
    values = _index(known)
    if contains_secret(ref, values):
        raise SecretInRef("secret")
    if has_userinfo(ref):
        raise SecretInRef("userinfo")
    reason = _pattern_reason(ref) if patterns else None
    if reason is not None:
        raise SecretInRef(reason)
    return ref


def rewrite_ref(
    ref: str,
    known: "Iterable[str] | SecretIndex",
    template: str = SECRET_PLACEHOLDER,
    *,
    patterns: bool = True,
) -> str:
    """The reference with every known value (in any form a URL carries it) replaced by `template`.

    The same key then gives the same reference whatever its value: a rotation changes no
    identifier. The result is checked again: a form this function did not find, a userinfo part or
    a token left over is still refused, never written.
    """
    values = _index(known)
    text = ref
    if contains_secret(ref, values):  # the common case holds nothing: no form is computed
        forms = sorted(
            {f for value in values.values for f in secret_forms(value) if f}, key=len, reverse=True
        )
        for form in forms:
            text = text.replace(form, template)
    return check_ref(text, values, patterns=patterns)


def projectable(ref: str | None, known: "Iterable[str] | SecretIndex") -> str | None:
    """A stored reference as it may be copied into a file the host writes (the provenance mirror,
    `unit.json`): rewritten to the placeholder when it holds a known value, `None` when it still
    cannot be shown. A row written before the guards existed never leaks through a projection."""
    if ref is None:
        return None
    try:
        return rewrite_ref(ref, known)
    except SecretInRef:
        return None
