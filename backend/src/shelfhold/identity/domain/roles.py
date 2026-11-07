"""The roles and the right to classify (EF-USR-005, EF-BIB-036), closed by default.

A role the specification does not know is never admitted: not by the registry of reserved
actions, not by a route that lists it. `integration` is a viewer of events (webhooks, outbox:
GAP-7-16), never a person who signs in, so it is not a known role.

"Classer" (the `rate` action) is a right of its own, carried by the `can_rate` flag of the user.
Before M3 only the admin holds it: `RATE_ROLES` is the one line M3 widens, with the member
accounts. The flag alone never gives it to another role.
"""

from typing import Final

KNOWN_ROLES: Final = frozenset({"admin", "member", "child"})
INTEGRATION_ROLE: Final = "integration"
RATE_ROLES: Final = frozenset({"admin"})
# Roles a table lists: the three that sign in, then the one that only receives projections.
TABLE_ROLES: Final = ("admin", "member", "child", INTEGRATION_ROLE)


def holds_rate_right(role: str, can_rate: bool) -> bool:
    return role in RATE_ROLES and can_rate
