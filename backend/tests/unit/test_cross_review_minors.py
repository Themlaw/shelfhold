"""Minor findings of the cross-cutting security review (2026-11-11): small, one test each."""

from __future__ import annotations

import asyncio
import secrets
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.identity.domain.credentials import Credential, Principal
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.fetcher.client import _parse_url
from shelfhold.infrastructure.fetcher.errors import InvalidFetchUrl
from shelfhold.interfaces.http_api import plans
from shelfhold.interfaces.http_api.problems import ProblemError
from shelfhold.plugin_host.adapters.meta.suwayomi import SuwayomiService
from shelfhold.sdk.fetcher import CookieJarView, Response
from shelfhold.sdk.source import SourceChanged
from tests.fakes.suwayomi import BASE_URL

pytestmark = pytest.mark.req("ENF-SEC-005")


def test_integrity_error_carries_no_parameter_value(tmp_path: Path) -> None:
    secret = "csrf-value-that-must-not-leak-9f3a"  # noqa: S105 (synthetic)
    engine = create_db_engine(tmp_path / "t.db")
    table = sa.Table("s", sa.MetaData(), sa.Column("id", sa.String, primary_key=True),
                     sa.Column("v", sa.String))  # fmt: skip
    table.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(table.insert().values(id="a", v=secret))
    with pytest.raises(sa.exc.IntegrityError) as caught, engine.begin() as connection:
        connection.execute(table.insert().values(id="a", v=secret))
    engine.dispose()
    assert secret not in str(caught.value)


@pytest.mark.parametrize("role", ["member", "child"])
def test_plans_refuse_a_key_with_the_admin_scope_of_a_non_admin(
    role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    principal = Principal("u", "u", role, Credential.API_KEY, key_id="k", scopes=("admin",))
    monkeypatch.setattr(plans, "require_principal", lambda _request: principal)
    with pytest.raises(ProblemError):
        plans.require_admin(object())  # type: ignore[arg-type]
    admin = Principal("a", "a", "admin", Credential.API_KEY, key_id="k", scopes=("admin",))
    monkeypatch.setattr(plans, "require_principal", lambda _request: admin)
    assert plans.require_admin(object()) is admin  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "url",
    [
        "http://svc.example:0/x",
        "https://svc.example:0/",
        "http://svc.example:-1/",
        "http://svc.example:65536/",
        "http://svc.example:99999999999999999999/",
    ],
)
def test_fetcher_refuses_a_port_out_of_range(url: str) -> None:
    with pytest.raises(InvalidFetchUrl):
        _parse_url(url)


class _Deep:
    """A Fetcher whose GraphQL answer is nested far past the interpreter's stack."""

    async def post(self, url: str, **kwargs: Any) -> Response:
        return Response(status=200, body=b"[" * 200_000 + b"]" * 200_000)

    async def get(self, url: str, **kwargs: Any) -> Response:  # pragma: no cover - unused
        raise AssertionError

    def cookies(self) -> CookieJarView:  # pragma: no cover - unused
        return CookieJarView()


def test_suwayomi_deeply_nested_answer_is_an_invalid_answer() -> None:
    service = SuwayomiService(BASE_URL)
    with pytest.raises(SourceChanged):
        asyncio.run(service.search(_Deep(), "", {}, None))  # type: ignore[arg-type]


_DEEP = b"[" * 200_000 + b"]" * 200_000


def test_kavita_deeply_nested_answer_is_a_protocol_error() -> None:
    from shelfhold.export.targets.kavita import routes
    from shelfhold.export.targets.kavita.client import ProtocolError, _decode, _is_json

    assert _is_json(_DEEP) is False
    route = next(iter(vars(routes).values()), None)  # any route: only its label is read
    del route
    with pytest.raises(ProtocolError):
        _decode(_DEEP, type("R", (), {"label": "route"})())  # type: ignore[arg-type]


def test_suwayomi_token_endpoint_deeply_nested_answer_is_source_changed() -> None:
    from shelfhold.plugin_host.adapters.meta.suwayomi.jwt import suwayomi_jwt_fetcher

    fetcher = suwayomi_jwt_fetcher(
        _Deep(),  # type: ignore[arg-type]
        {"username": "u", "password": secrets.token_hex(8)},
        origin=BASE_URL,
    )
    with pytest.raises(SourceChanged):
        asyncio.run(fetcher._token_call("query", "login"))
    assert fetcher._refused(Response(status=200, body=b"nauthorized" + _DEEP)) is False
