"""Shape of one catalog entry (EF-UX-003). Each context declares its own in `<ctx>/errors.py`.

The `code` is the stable identifier: it is the RFC 9457 `type` (as a URI), the identifier of the
matching event of the failure matrix (EF-INT-006) and the key of the French texts in
`locales/fr.toml`. Renaming a code breaks API clients: add a new one instead.
"""

from dataclasses import dataclass
from typing import Literal

PROBLEM_TYPE_PREFIX = "/api/v1/problems/"

Audience = Literal["admin", "user"]


@dataclass(frozen=True)
class ErrorCode:
    code: str
    status: int  # HTTP status when the error is returned by the API
    audience: Audience  # who is expected to act (EF-UX-003)
    event: bool = False  # also an operating event of the same name (events/operating.py)

    @property
    def type_uri(self) -> str:
        return PROBLEM_TYPE_PREFIX + self.code.replace("_", "-")

    @property
    def title_key(self) -> str:
        return f"errors.{self.code}.title"

    @property
    def action_key(self) -> str:
        return f"errors.{self.code}.action"

    @property
    def cause_key(self) -> str:
        """Optional: a code without it gets the generic cause of its audience."""
        return f"errors.{self.code}.cause"
