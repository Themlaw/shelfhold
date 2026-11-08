"""Suwayomi `ui_login` (M1b L20): the SuwayomiJwt AuthFlow, the service account, the claim release.

Against the simulated server, which plays several accounts, shares the downloaded files by
reference and accepts `login` / `refreshToken` only in the forms spike S5 measured.
"""

import asyncio
import traceback
from collections.abc import Awaitable

import pytest

from shelfhold.infrastructure.logging import _scrub_text
from shelfhold.plugin_host.adapters.meta.suwayomi import SuwayomiService
from shelfhold.plugin_host.adapters.meta.suwayomi import queries as q
from shelfhold.plugin_host.authflow import SuwayomiJwtFetcher
from shelfhold.plugin_host.errors import AuthExpired, AuthModeUnsupported
from shelfhold.sdk.refs import SeriesRef, UnitRef
from tests.fakes.suwayomi import (
    BASE_URL,
    PASSWORD,
    USER,
    FakeSuwayomi,
    FakeSuwayomiFetcher,
)
from tests.fakes.suwayomi.server import FORBIDDEN_FIELDS

ALPHA = SeriesRef(source_key="suwayomi:7001", remote_id="/series-alpha", title="Series Alpha")
GAMMA = SeriesRef(source_key="suwayomi:7003", remote_id="/series-gamma", title="Series Gamma")
CH1 = "/series-alpha/ch-001"
MOM = "mom"
MOM_PASSWORD = "synthetic-other-account-password"  # noqa: S105 - throwaway value


def run[T](coro: Awaitable[T]) -> T:
    async def go() -> T:
        return await coro

    return asyncio.run(go())


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def ui_login_server(**kw: object) -> FakeSuwayomi:
    return FakeSuwayomi(auth_mode="ui_login", **kw)  # type: ignore[arg-type]


def jwt_fetcher(
    server: FakeSuwayomi, clock: Clock | None = None, *, password: str = PASSWORD
) -> SuwayomiJwtFetcher:
    return SuwayomiJwtFetcher(
        FakeSuwayomiFetcher(server),  # type: ignore[arg-type]
        {"username": USER, "password": password},
        origin=BASE_URL,
        now=clock or Clock(),
    )


def library_titles(service: SuwayomiService, f: SuwayomiJwtFetcher) -> list[str]:
    return sorted(r.title for r in run(service.followed_series(f)))  # type: ignore[arg-type]


# -- login, refresh, rotation -------------------------------------------------------------------
@pytest.mark.req("EF-SRC-031")
def test_suwayomi_jwt_login_refresh_rotation() -> None:
    server, clock = ui_login_server(), Clock()
    f, service = jwt_fetcher(server, clock), SuwayomiService(BASE_URL)
    assert library_titles(service, f) == ["Series Alpha", "Series Beta"]
    assert (server.logins, server.refreshes) == (1, 0)  # one login, then the token is reused
    library_titles(service, f)
    assert (server.logins, server.refreshes) == (1, 0)

    server.expire_access_tokens()  # the 5-minute JWT ran out: refreshed, not logged in again
    assert library_titles(service, f) == ["Series Alpha", "Series Beta"]
    assert (server.logins, server.refreshes) == (1, 1)

    clock.t += 250  # known to be near its end: renewed before it is refused
    library_titles(service, f)
    assert (server.logins, server.refreshes) == (1, 2)

    server.rotate_refresh_token = True  # a server that also answers a new refresh token
    server.expire_access_tokens()
    library_titles(service, f)
    server.expire_access_tokens()
    library_titles(service, f)  # the second refresh uses the rotated token, not the first
    assert (server.logins, server.refreshes) == (1, 4)

    server.expire_all_tokens()  # the refresh token is dead too: a new login with the account
    library_titles(service, f)
    assert (server.logins, server.refreshes) == (2, 4)
    assert server.violations == []


@pytest.mark.req("EF-SRC-031")
def test_concurrent_refresh_does_a_single_refresh() -> None:
    server = ui_login_server()
    f, service = jwt_fetcher(server), SuwayomiService(BASE_URL)
    library_titles(service, f)

    async def slow() -> None:
        await asyncio.sleep(0.01)

    server.refresh_delay = slow
    server.expire_access_tokens()

    async def five() -> list[list[SeriesRef]]:
        return await asyncio.gather(*(service.followed_series(f) for _ in range(5)))  # type: ignore[arg-type]

    assert all(len(r) == 2 for r in run(five()))
    assert server.refreshes == 1 and server.logins == 1
    assert f.generation == server.logins + server.refreshes  # each rotation replaced the pair once


@pytest.mark.req("EF-SRC-031")
def test_refused_credentials_pause_without_looping_or_echoing() -> None:
    server = ui_login_server()
    f, service = (
        jwt_fetcher(server, password="wrong-synthetic-password-1"),  # noqa: S106 - throwaway synthetic value
        SuwayomiService(BASE_URL),
    )
    with pytest.raises(AuthExpired) as refused:
        run(service.followed_series(f))  # type: ignore[arg-type]
    assert server.logins == 1  # no retry storm against the account
    shown = "".join(traceback.format_exception(refused.value)) + repr(f)
    assert "wrong-synthetic-password-1" not in shown


@pytest.mark.req("EF-SRC-031", "ENF-SEC-005")
def test_tokens_never_appear_in_repr_errors_or_scrubbed_text() -> None:
    server = ui_login_server()
    f, service = jwt_fetcher(server), SuwayomiService(BASE_URL)
    library_titles(service, f)
    access, refresh = server.issued_tokens()[-1]
    for shown in (repr(f), str(f), repr(f.__dict__), repr(vars(f))):
        assert access not in shown and refresh not in shown
    # every form of the token is registered for scrubbing (the Bearer header included)
    for secret in (access, refresh, f"Bearer {access}"):
        assert secret not in _scrub_text(f"upstream said {secret} and again {secret}")
    # a failure that carries the token in its text (a proxy echo) is scrubbed the same way
    server.expire_all_tokens()
    server.accounts[USER].password = "changed-on-the-server-2"  # noqa: S105 - throwaway synthetic value
    with pytest.raises(AuthExpired) as expired:
        run(service.followed_series(f))  # type: ignore[arg-type]
    assert access not in "".join(traceback.format_exception(expired.value))
    assert refresh not in "".join(traceback.format_exception(expired.value))


@pytest.mark.req("EF-SRC-031")
def test_token_goes_only_to_the_declared_origin() -> None:
    from shelfhold.infrastructure.fetcher.errors import InvalidFetchUrl

    server = ui_login_server()
    f = jwt_fetcher(server)
    with pytest.raises(InvalidFetchUrl):
        run(f.get("http://elsewhere.example:4567/api/graphql"))
    assert server.logins == 0  # nothing left, not even a login


# -- the service account ------------------------------------------------------------------------
@pytest.mark.req("EF-SRC-031")
def test_service_account_never_gets_install_extensions() -> None:
    # the adapter never speaks an extension operation, whatever it does
    server = ui_login_server(expose_roles=True)
    f, service = jwt_fetcher(server), SuwayomiService(BASE_URL)
    run(service.followed_series(f))  # type: ignore[arg-type]
    run(service.set_follow(f, GAMMA, True))  # type: ignore[arg-type]
    sent = set().union(*(r.fields for r in server.requests))
    assert not sent & FORBIDDEN_FIELDS and server.violations == []
    assert not any("xtension" in name for name in sent)
    assert "user" in sent  # the roles were read, since this server exposes them

    # a server that exposes the roles: an account that may install extensions is refused
    for roles in (["ADMIN"], ["USER", "INSTALL_EXTENSIONS"]):
        server = ui_login_server(expose_roles=True)
        server.accounts[USER].roles = roles
        f, service = jwt_fetcher(server), SuwayomiService(BASE_URL)
        with pytest.raises(AuthModeUnsupported) as refused:
            run(service.followed_series(f))  # type: ignore[arg-type]
        assert "extension" in str(refused.value)
        assert not any(r.fields & {"mangas", "enqueueChapterDownload"} for r in server.requests)

    # v2.4.2366 exposes no role (S5): the roles cannot be checked, the account is accepted (D1)
    server = ui_login_server(expose_roles=False)
    f, service = jwt_fetcher(server), SuwayomiService(BASE_URL)
    assert len(run(service.followed_series(f))) == 2  # type: ignore[arg-type]


@pytest.mark.req("EF-SRC-009", "EF-SRC-031")
def test_other_accounts_library_never_read() -> None:
    server = ui_login_server()
    server.add_account(MOM, MOM_PASSWORD, library={"/series-gamma", "/series-beta"})
    f, service = jwt_fetcher(server), SuwayomiService(BASE_URL)
    # only the service account's library is the truth: Gamma (mom's alone) is not followed
    assert library_titles(service, f) == ["Series Alpha", "Series Beta"]
    run(service.set_follow(f, GAMMA, True))  # type: ignore[arg-type]
    assert library_titles(service, f) == ["Series Alpha", "Series Beta", "Series Gamma"]
    run(service.set_follow(f, GAMMA, False))  # type: ignore[arg-type]
    assert server.account_library(MOM) == {"/series-gamma", "/series-beta"}  # untouched
    assert {account for account, _ in server.reads_by_account} == {USER}
    assert server.login_users == [USER]  # no other account was ever logged in
    assert server.violations == []


# -- purge in ui_login --------------------------------------------------------------------------
def ready_ticket(service: SuwayomiService, f: SuwayomiJwtFetcher, server: FakeSuwayomi):
    _, units = run(service.followed_series(f)), run(service.list_units(f, ALPHA))  # type: ignore[arg-type]
    ticket = run(service.request(f, UnitRef(series=ALPHA, unit=units[0])))  # type: ignore[arg-type]
    while run(service.poll(f, ticket)) != "ready":  # type: ignore[arg-type]
        pass
    return ticket


@pytest.mark.req("EF-SRC-031", "EF-SRC-009")
def test_ui_login_purge_removes_claim_and_measures_freed_space() -> None:
    size = len(FakeSuwayomi().cbz_bytes(CH1))
    # alone on the file: the claim goes, the file goes, the space freed is measured
    server = ui_login_server()
    f, service = jwt_fetcher(server), SuwayomiService(BASE_URL)
    ticket = ready_ticket(service, f, server)
    released = run(service.release(f, ticket))  # type: ignore[arg-type]
    assert released.released is True
    assert released.freed_bytes == size and released.still_present is False
    assert not server.is_downloaded(CH1)

    # another account keeps the same chapter: only our claim goes, nothing is freed (EF-SRC-031)
    server = ui_login_server()
    server.add_account(MOM, MOM_PASSWORD)
    f, service = jwt_fetcher(server), SuwayomiService(BASE_URL)
    ticket = ready_ticket(service, f, server)
    server.hold(MOM, CH1)
    released = run(service.release(f, ticket))  # type: ignore[arg-type]
    assert released.released is True  # Shelfhold's claim is withdrawn
    assert released.freed_bytes == 0 and released.still_present is True  # measured, not assumed
    assert server.is_downloaded(CH1) and server.claims[server.local_chapter_ids()[CH1]] == {MOM}
    assert server.files_removed == [] and server.violations == []


# -- the wire forms -----------------------------------------------------------------------------
@pytest.mark.req("EF-SRC-031", "EF-SRC-033")
def test_login_and_refresh_documents_are_the_forms_measured_by_s5() -> None:
    assert (
        q.login("u", "p")
        == 'mutation { login(input: {username: "u", password: "p"}) { accessToken refreshToken } }'
    )
    assert (
        q.refresh_token("r")
        == 'mutation { refreshToken(input: {refreshToken: "r"}) { accessToken } }'
    )
    # a quote or a backslash in a secret stays inside the literal
    server = ui_login_server()
    server.accounts[USER].password = 'pa"ss\\word-synthetic'  # noqa: S105 - throwaway synthetic value
    f = jwt_fetcher(server, password='pa"ss\\word-synthetic')  # noqa: S106 - throwaway synthetic value
    library_titles(SuwayomiService(BASE_URL), f)
    assert server.logins == 1 and server.violations == []
