"""M2 L19 (EF-SRC-015, EF-SRC-004): the doors of the one-shot acquisition, on a real instance: the
routes behind the session, the CSRF token and the role; the production job of the `request` class;
the commands. The source is a synthetic plugin at books.example.org over a scripted transport."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx2
import pytest
import sqlalchemy as sa

import shelfhold.main as main_module
from shelfhold.config import Settings
from shelfhold.plugin_host import attestation
from shelfhold.plugin_host.registry import PluginCandidate, PluginOrigin, PluginRegistry
from shelfhold.sdk.testing.corpus.trapped_epubs.build import epub_bytes, xhtml
from tests.acquire_env import HOST, KEY, PAGE, NativeBooks, Wire
from tests.auth_env import run_cli
from tests.fakes_fetcher import Recorder
from tests.l51_env import Live, live
from tests.support.sources import PUBLIC

TRAPPED = epub_bytes(xhtml("<p>chapitre</p><script>alert(1)</script>"))
FILE = "/files/book-42.epub"


@contextmanager
def with_source(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    tmp_path: Path,
    plugin: NativeBooks,
    files: dict[str, bytes],
    *,
    trusted: bool = False,
) -> Iterator[tuple[Live, Wire]]:
    holder: list[Wire] = []
    recorder = Recorder(lambda request: holder[0].respond(request))
    wire = Wire(recorder, files=dict(files))
    holder.append(wire)
    registry = PluginRegistry()
    registry.load_all(
        [PluginCandidate(key=KEY, origin=PluginOrigin.FIRST_PARTY, load=lambda: plugin)]
    )
    monkeypatch.setattr(main_module, "start_plugins", lambda *_a, **_k: (registry, []))
    keys = attestation.FIRST_PARTY_PLUGIN_KEYS | ({KEY} if trusted else set())
    with (
        patch.object(attestation, "FIRST_PARTY_PLUGIN_KEYS", keys),
        live(monkeypatch, settings, tmp_path, chapters=()) as api,
    ):
        network = api.env.inst.network
        original = network.transport
        known = network.resolver

        async def route(request: httpx2.Request) -> httpx2.Response:
            if request.headers.get("host", "").startswith(HOST):
                return recorder.transport.handle_request(request)  # type: ignore[no-any-return]
            return await original.handle_async_request(request)  # type: ignore[union-attr]

        class Resolver:
            async def resolve(self, host: str, port: int) -> list[str]:
                return [PUBLIC] if host == HOST else await known.resolve(host, port)

        network.transport = httpx2.MockTransport(route)
        network.resolver = Resolver()  # type: ignore[assignment]
        yield api, wire


def rows(api: Live, sql: str, **params: object) -> list[dict[str, Any]]:
    with api.env.inst.engine.connect() as connection:
        return [dict(r) for r in connection.execute(sa.text(sql), params).mappings()]


@pytest.mark.req("EF-SRC-015", "EF-SRC-004", "EF-BIB-051", "EF-INT-017")
def test_native_acquisition_uses_requests_priority_class(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    with with_source(monkeypatch, settings, tmp_path, NativeBooks(), {FILE: TRAPPED}) as (api, _w):
        csrf = {"X-CSRF-Token": api.api.csrf()}
        # no CSRF token on a session: refused, nothing resolved
        assert api.client.post("/api/v1/acquire", json={"url": PAGE}).status_code == 403
        assert _w.recorder.requests == []

        found = api.client.get(f"/api/v1/sources/{KEY}/search", params={"q": "livre"})
        assert found.status_code == 200, found.text
        assert found.json()["items"][0]["remote_id"] == "book/42"
        filters = api.client.get(f"/api/v1/sources/{KEY}/filters").json()
        assert [f["key"] for f in filters["filters"]] == ["language"]

        started = api.client.post("/api/v1/acquire", json={"url": PAGE}, headers=csrf)
        assert started.status_code == 202, started.text
        job_id = started.json()["job_id"]
        assert rows(api, "SELECT priority_class FROM job_view WHERE id = :j", j=job_id) == [
            {"priority_class": "request"}
        ]  # EF-INT-017: a person's request, not a catch-up
        done = api.wait_job(job_id)
        assert done["status"] == "succeeded", done
        (content,) = rows(api, "SELECT format, derived_by FROM content")
        assert content["format"] == "epub" and content["derived_by"]  # untrusted: derived
        assert {"kind": "novel"} in rows(api, "SELECT kind FROM work")
        assert rows(api, "SELECT count(*) AS n FROM event WHERE type = 'unit.stored'")[0]["n"] == 1
        # the same book again: it is already there, nothing is queued
        again = api.client.post("/api/v1/acquire", json={"url": PAGE}, headers=csrf)
        assert again.status_code == 200 and again.json() == {"state": "stored"}


@pytest.mark.req("EF-SRC-004", "EF-SRC-015")
def test_acquire_route_refusals_are_problems_of_the_catalog(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    with with_source(monkeypatch, settings, tmp_path, NativeBooks(), {FILE: TRAPPED}) as (api, w):
        csrf = {"X-CSRF-Token": api.api.csrf()}

        def post(body: dict[str, Any]) -> Any:
            return api.client.post("/api/v1/acquire", json=body, headers=csrf)

        none = post({"url": "https://elsewhere.example/book/1"})
        assert none.status_code == 422 and none.json()["type"].endswith("source.no-match")
        both = post({"url": PAGE, "source_key": KEY, "remote_id": "book/42"})
        assert both.status_code == 422
        neither = post({})
        assert neither.status_code == 422
        unknown = post({"source_key": "nope", "remote_id": "x"})
        assert unknown.status_code == 404 and unknown.json()["type"].endswith("plugin.unknown")
        filtered = api.client.get(
            f"/api/v1/sources/{KEY}/search", params={"q": "x", "filters": '{"nope": 1}'}
        )
        assert filtered.status_code == 422 and filtered.json()["type"].endswith(
            "source.filter-invalid"
        )
        malformed = api.client.get(
            f"/api/v1/sources/{KEY}/search", params={"q": "x", "filters": "[1]"}
        )
        assert malformed.status_code == 422
        assert rows(api, "SELECT count(*) AS n FROM work WHERE kind = 'novel'")[0]["n"] == 0
        # no session, no acquisition
        anonymous = api.api.other_client("203.0.113.9")
        assert anonymous.post("/api/v1/acquire", json={"url": PAGE}).status_code in (401, 403)
        assert anonymous.get(f"/api/v1/sources/{KEY}/search").status_code in (401, 403)
        assert not any("books.example.org/book" in p for p in w.paths)


@pytest.mark.req("EF-SRC-015", "EF-SRC-004")
def test_source_commands_search_filters_and_acquire(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    """The commands go to the running server (its plugins, its queue): the same services."""
    with with_source(monkeypatch, settings, tmp_path, NativeBooks(), {FILE: TRAPPED}) as (api, _w):
        capsys.readouterr()
        assert run_cli(monkeypatch, settings, "source", "search", KEY, "livre") == 0
        assert f"{KEY}  book/42  Un livre de test" in capsys.readouterr().out
        assert run_cli(monkeypatch, settings, "source", "filters", KEY) == 0
        assert "language" in capsys.readouterr().out
        assert (
            run_cli(monkeypatch, settings, "source", "search", KEY, "x", "--filter", "nope=1") == 1
        )
        assert "source.filter_invalid" in capsys.readouterr().err
        assert run_cli(monkeypatch, settings, "acquire", PAGE) == 0
        assert "Demande enregistrée" in capsys.readouterr().out
        assert run_cli(monkeypatch, settings, "acquire", "https://elsewhere.example/book/1") == 1
        assert "source.no_match" in capsys.readouterr().err
        assert api is not None
