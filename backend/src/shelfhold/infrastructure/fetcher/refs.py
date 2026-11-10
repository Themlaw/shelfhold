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
from urllib.parse import unquote

from shelfhold.infrastructure.secret_text import contains_secret, decoded_views, secret_forms
from shelfhold.sdk.refs import SECRET_PLACEHOLDER

__all__ = [
    "SECRET_PLACEHOLDER",
    "SecretInRef",
    "check_ref",
    "has_userinfo",
    "projectable",
    "rewrite_ref",
]

# `//name:pass@host` or `//name@host`; or, with no scheme, `name:pass@host`. An `@` after the first
# slash of the path is just a character.
_USERINFO: Final = re.compile(r"//[^/?#\s]*@|^[^/?#\s@]*:[^/?#\s@]*@")
# A parameter whose name says what it holds. `page_token` counts: refused is the safe side.
_SENSITIVE_NAME: Final = re.compile(
    r"(?:^|[-_.])(?:api[-_]?key|apikey|access[-_]?token|refresh[-_]?token|id[-_]?token|token|secret"
    r"|passw(?:or)?d|passwd|pwd|auth|authorization|signature|sig|sas|session(?:[-_]?id)?"
    r"|jsessionid|sid|cookie|credentials?)$",
    re.IGNORECASE,
)
_PATH_AFTER_NAME: Final = re.compile(
    r"/(?:api[-_]?key|apikey|key|token|access[-_]?token|secret|auth|bearer|session)/(?P<v>[^/?#]+)"
    r"|/api/opds/(?P<o>[^/?#]+)",
    re.IGNORECASE,
)
_JWT_SEGMENT: Final = re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]*")


class SecretInRef(ValueError):
    """A reference holds, or may hold, a secret. `reason` is one of `secret` (a value the Fetcher
    knows), `userinfo`, `query`, `fragment`, `path`; it never quotes the reference."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"the reference holds a secret ({reason})")
        self.reason = reason


def has_userinfo(text: str) -> bool:
    """A `user[:password]@` part, as written or encoded."""
    return any(_USERINFO.search(view) for view in decoded_views(text))


def _sensitive_pairs(part: str) -> bool:
    for piece in re.split(r"[&;]", part):
        name, equals, value = piece.partition("=")
        if (
            equals
            and value
            and value != SECRET_PLACEHOLDER
            and _SENSITIVE_NAME.search(unquote(name))
        ):
            return True
    return False


def _pattern_reason(ref: str) -> str | None:
    """A secret nobody registered still shows by where it sits."""
    for view in decoded_views(ref):
        body, _, fragment = view.partition("#")
        path, _, query = body.partition("?")
        if _sensitive_pairs(query):
            return "query"
        if "=" in fragment and _sensitive_pairs(fragment):
            return "fragment"
        if _sensitive_pairs(path.replace("/", ";")) and ";" in path:
            return "path"  # a matrix parameter: `/series/1;jsessionid=...`
        for match in _PATH_AFTER_NAME.finditer(path):
            if (match.group("v") or match.group("o")) != SECRET_PLACEHOLDER:
                return "path"
        if any(_JWT_SEGMENT.fullmatch(segment) for segment in path.split("/")):
            return "path"
    return None


def check_ref(ref: str, known: Iterable[str], *, patterns: bool = True) -> str:
    """`ref` when it is free of secrets, else `SecretInRef`.

    `known` is the set of values the Fetcher injects (and any registered secret). `patterns=False`
    is for a URL that is used and not stored (a file to download, a signed image address): only a
    known value and a userinfo part are refused, a signature in a query is its business.
    """
    values = tuple(known)
    if contains_secret(ref, values):
        raise SecretInRef("secret")
    if has_userinfo(ref):
        raise SecretInRef("userinfo")
    reason = _pattern_reason(ref) if patterns else None
    if reason is not None:
        raise SecretInRef(reason)
    return ref


def rewrite_ref(
    ref: str, known: Iterable[str], template: str = SECRET_PLACEHOLDER, *, patterns: bool = True
) -> str:
    """The reference with every known value (in any form a URL carries it) replaced by `template`.

    The same key then gives the same reference whatever its value: a rotation changes no
    identifier. The result is checked again: a form this function did not find, a userinfo part or
    a token left over is still refused, never written.
    """
    values = tuple(known)
    forms = sorted({f for value in values for f in secret_forms(value) if f}, key=len, reverse=True)
    text = ref
    for form in forms:
        text = text.replace(form, template)
    return check_ref(text, values, patterns=patterns)


def projectable(ref: str | None, known: Iterable[str]) -> str | None:
    """A stored reference as it may be copied into a file the host writes (the provenance mirror,
    `unit.json`): rewritten to the placeholder when it holds a known value, `None` when it still
    cannot be shown. A row written before the guards existed never leaks through a projection."""
    if ref is None:
        return None
    try:
        return rewrite_ref(ref, known)
    except SecretInRef:
        return None
