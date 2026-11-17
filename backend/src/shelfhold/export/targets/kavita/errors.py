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


class TargetAsleep(TargetError):
    """`wake.deferred`: Kavita sleeps behind Sablier and could not be woken in time. The batch is
    put off: it is neither `target.unreachable` nor a failure, and nothing was written."""

    code = "wake.deferred"


class TargetForbidden(TargetError):
    """`target.forbidden`: 401/403, the dedicated account was demoted or its key revoked."""

    def __init__(self, route: str) -> None:
        super().__init__(f"{route} was refused: the destination is paused")
        self.route = route


class TargetKeyRevoked(TargetForbidden):
    """401: the key was revoked. Same pause and same exit as `TargetForbidden`, but the code is
    that of the event it announces, `target.unreachable` (matrix row `kavita_key_revoked`)."""

    code = "target.unreachable"


class RouteMissing(TargetError):
    """`target.route_missing`: a listed route answers 404, its features are cut."""

    def __init__(self, route: str) -> None:
        super().__init__(f"{route} does not exist on this Kavita")
        self.route = route


class TargetDegraded(TargetError):
    """The version is out of range (`degraded_readonly`): preview only, nothing is written."""


class ConfirmationRequired(TargetError):
    """An adopted library is never modified without the admin's confirmation."""


class ProtocolError(TargetError):
    """Kavita answered something the contract does not describe. `status` is the HTTP status
    when there was one (a 4xx on one path is a refusal of that path, a 5xx is an outage)."""

    def __init__(
        self, message: str = "", *, status: int | None = None, retry_after: float | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after  # seconds a 429 or 503 asked to wait, when it said


class ListingIncomplete(ProtocolError):
    """A listing hit its cap: what was read is not all there is, so nothing may be concluded."""

    incomplete = True


class HubDown(Exception):
    """The hub of Kavita cannot be used right now (no token, no connection, a connection that
    ended or fell silent). Not a `TargetError`: it says nothing of the destination, only that the
    transport of the events is cut; the polling goes on. The text never carries a URL or a token.
    """
