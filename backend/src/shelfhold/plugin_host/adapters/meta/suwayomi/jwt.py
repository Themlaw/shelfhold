"""Suwayomi's `ui_login` AuthFlow: the JWT pair (EF-SRC-031), built on the host's `JwtFetcher`.

The host core knows no service: this module gives it Suwayomi's two documents (the forms spike S5
measured, `queries.login` and `queries.refresh_token`) and the lifetime of the access token (5 min).
"""

from collections.abc import Callable, Mapping

from shelfhold.plugin_host.adapters.meta.suwayomi import queries as q
from shelfhold.plugin_host.authflow import JwtDialect, JwtFetcher
from shelfhold.sdk.fetcher import Fetcher

SUWAYOMI_JWT = JwtDialect(
    path=q.GRAPHQL_PATH,
    login=q.login,
    refresh=q.refresh_token,
    access_lifetime=300.0,
    renew_margin=60.0,
)


def suwayomi_jwt_fetcher(
    inner: Fetcher,
    secret: Mapping[str, str],
    *,
    origin: str,
    now: Callable[[], float] | None = None,
) -> JwtFetcher:
    """The Fetcher a `ui_login` Suwayomi is spoken through; `secret` is `{username, password}`."""
    if now is None:
        return JwtFetcher(inner, SUWAYOMI_JWT, secret, origin=origin)
    return JwtFetcher(inner, SUWAYOMI_JWT, secret, origin=origin, now=now)
