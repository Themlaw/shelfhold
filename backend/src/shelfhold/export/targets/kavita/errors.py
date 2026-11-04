"""Failures of the Kavita destination. None of them ever carries the key or a response body."""

from __future__ import annotations


class TargetError(Exception):
    """Base of the destination's failures."""


class RouteNotAllowed(TargetError):
    """A call outside `docs/kavita-routes.md`: a bug or a hostile plugin, never sent."""

    def __init__(self, method: str, path: str) -> None:
        super().__init__(f"{method} {path[:80]!r} is not an allow-listed Kavita route")


class TargetUnreachable(TargetError):
    """`target.unreachable`: Kavita does not answer (network, DNS, SSRF guard, timeout)."""


class TargetForbidden(TargetError):
    """`target.forbidden`: 401/403, the dedicated account was demoted or its key revoked."""

    def __init__(self, route: str) -> None:
        super().__init__(f"{route} was refused: the destination is paused")
        self.route = route


class RouteMissing(TargetError):
    """`target.route_missing`: a listed route answers 404, its features are cut."""

    def __init__(self, route: str) -> None:
        super().__init__(f"{route} does not exist on this Kavita")
        self.route = route


class TargetDegraded(TargetError):
    """The version is out of range (`degraded_readonly`): preview only, nothing is written."""


class PlanNotVerified(TargetError):
    """No plan may be applied without the name check (`plugin/parse` or `parse-bulk` missing)."""


class ConfirmationRequired(TargetError):
    """An adopted library is never modified without the admin's confirmation."""


class ProtocolError(TargetError):
    """Kavita answered something the contract does not describe."""
