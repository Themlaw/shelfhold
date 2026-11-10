"""Test support for the secret scrubber: the registry of known values is process-wide."""

from shelfhold.infrastructure import logging as shelfhold_logging


def forget_secret_values() -> None:
    shelfhold_logging._known_values.clear()


def fresh_secret(seed: str, *, specials: bool = False) -> str:
    """A synthetic credential built at run time from a seed: no literal in the sources, so the
    secret scanner of the repository has nothing to find, and the value is the same on every run.

    `specials` puts characters that URL encoding changes (`+`, `/`, `=`, `&`, space, `@`) inside.
    """
    import base64
    import hashlib

    raw = base64.urlsafe_b64encode(hashlib.sha256(f"synthetic|{seed}".encode()).digest()).decode()
    token = raw.rstrip("=")[:30]
    if not specials:
        return token
    return f"{token[:6]}+{token[6:12]}/{token[12:18]}={token[18:24]}&{token[24:]} @{token[:3]}"
