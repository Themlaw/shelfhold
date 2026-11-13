"""The Kavita account a child's profile names (EF-USR-017): a name, never a credential.

The mapping is declarative. Shelfhold does not sign in to Kavita as the child and holds no token
for it; the name is what the read-only audit looks for among the accounts of Kavita. Kavita does
not tell "Lea" from "lea", so neither does the rule of uniqueness.
"""

from typing import Final

from shelfhold.identity.domain.roles import ProfileError

MAX_KAVITA_USERNAME: Final = 128


def check_kavita_username(value: object) -> str | None:
    """The name as it is stored (trimmed), `None` to clear the mapping, or a `ProfileError`.

    A control character is refused (a name is one line, it ends up in a log and a table), and so
    is anything that is not text: the route takes a string or nothing, never a structure."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProfileError("invalid_kavita_username")
    name = value.strip()
    if not name or len(name) > MAX_KAVITA_USERNAME:
        raise ProfileError("invalid_kavita_username")
    if any(not char.isprintable() for char in name):
        raise ProfileError("invalid_kavita_username")
    return name


def same_kavita_username(left: str, right: str) -> bool:
    return left.casefold() == right.casefold()
