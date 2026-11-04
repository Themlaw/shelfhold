"""Range of Suwayomi-Server versions this adapter is verified against (spike S5, EF-SRC-008).

Minimum: v2.4.2366, the version S5 measured. Maximum: below v3, the next major, which may change
the contract. A version outside the range is reported (`source.version_unsupported`) and the
service stays usable: the admin decides.
"""

import re

MIN_VERSION: tuple[int, int, int] = (2, 4, 2366)
MAX_EXCLUSIVE: tuple[int, int, int] = (3, 0, 0)
_VERSION = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")


def parse_version(raw: str) -> tuple[int, int, int] | None:
    found = _VERSION.match(raw.strip())
    if found is None:
        return None
    major, minor, patch = (int(g) for g in found.groups())
    return major, minor, patch


def is_supported(raw: str) -> bool:
    parsed = parse_version(raw)
    return parsed is not None and MIN_VERSION <= parsed < MAX_EXCLUSIVE
