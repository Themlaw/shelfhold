"""One way for fixtures to create a user row: a child always gets an age ceiling (M3 L01)."""

from typing import Any

STRICTEST_CEILING = 0  # a child's NULL is never "all ages": the strictest ceiling is the default


def with_ceiling(role: str, values: dict[str, Any]) -> dict[str, Any]:
    """`values` of a `user` row, with the strictest ceiling added for a child that has none."""
    if role == "child" and values.get("max_age_rating") is None:
        return {**values, "max_age_rating": STRICTEST_CEILING}
    return values
