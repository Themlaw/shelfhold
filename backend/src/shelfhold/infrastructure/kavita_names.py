"""How Kavita compares account names: the one definition, shared by the contexts.

Kavita upper-cases a name, character by character (the invariant culture), and compares the
result: "Lea" and "lea" are one account, and "ß" stays "ß" where `casefold` makes it "ss" and
would find a second account in a first. The audit of the accounts (export), the uniqueness of the
declared names (identity) and the check of a pasted key (export, at link time) all use this one.
"""

from __future__ import annotations

__all__ = ["kavita_name_key", "same_kavita_username"]


def kavita_name_key(name: str) -> str:
    return "".join(c.upper() if len(c.upper()) == 1 else c for c in name)


def same_kavita_username(left: str, right: str) -> bool:
    return kavita_name_key(left) == kavita_name_key(right)
