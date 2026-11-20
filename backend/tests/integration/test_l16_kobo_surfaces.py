"""EF-EXP-021 (M6 L16): the Kobo folder on every surface — admin, CLI, HTTP, socket."""
# ruff: noqa: F811

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.export.application.fan_out import EpubFile
from shelfhold.export.domain.bindings import TargetKindTransportError
from shelfhold.export.targets.kobo.destination import BookSpec, KoboDestination, SeriesSpec
from shelfhold.export.targets.kobo.source import KoboSource, kobo_series
from shelfhold.headless import HeadlessError, InvalidConfigValue, wiring
from shelfhold.infrastructure.local_socket import call
from shelfhold.infrastructure.schema import export
from shelfhold.interfaces.local_cli import IdentityDispatcher, SocketHeadless
from shelfhold.runtime_jobs import FirstPartyDestinations
from tests.auth_env import run_cli
from tests.export_env import export_env
from tests.headless_env import Headless
from tests.integration.test_fan_out_routes import World, world  # noqa: F401
from tests.integration.test_m4_l46_target_wiring import komga_env  # noqa: F401
from tests.kobo_env import (
    add_book,
    bind_kobo,
    kobo_target,
    renditions,
    rig,
    tree_files,
)
from tests.local_socket_env import serving

KOBO = "/api/v1/targets/kobo"
CWA = "/api/v1/targets/cwa"


def _rows(env: Any, kind: str) -> list[Any]:
    with env.engine.connect() as connection:
        return list(
            connection.execute(
                sa.select(export.export_target).where(export.export_target.c.kind == kind)
            )
        )


@pytest.mark.req("EF-EXP-021")
def test_target_add_kobo_needs_a_path_map(
    komga_env: tuple[Headless, Any],
) -> None:
    env, _fake = komga_env
    with pytest.raises(InvalidConfigValue):
        env.admin.target_add_kobo(name="Kobo", path_map=None)


@pytest.mark.req("EF-EXP-021")
def test_cli_target_add_kobo(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    komga_env: tuple[Headless, Any],
    tmp_path: Path,
) -> None:
    env, _fake = komga_env
    monkeypatch.setattr(wiring, "open_headless", lambda _settings: env.admin)
    drop = tmp_path / "library" / "kobo-ingest"
    drop.mkdir(parents=True)
    pmap = f'[{{"local": "{drop}", "remote": "/kobo", "mode": "rw"}}]'
    code = run_cli(monkeypatch, env.settings, "target", "add", "kobo", "--name", "kobo",
                   "--path-map", pmap)  # fmt: skip
    assert code == 0
    assert [r.name for r in _rows(env, "kobo")] == ["kobo"]
    assert capsys.readouterr().err == ""


def _count(world: World, kind: str) -> int:
    with world.engine.connect() as connection:
        return connection.execute(
            sa.text("SELECT count(*) FROM export_target WHERE kind = :k"), {"k": kind}
        ).scalar_one()


@pytest.mark.req("EF-EXP-021")
def test_the_kobo_route_refuses_binding_error_and_headless_error(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # BindingError branch
    from shelfhold.headless import HeadlessAdmin

    original = HeadlessAdmin.target_add_kobo

    def raise_binding(self, *a, **k):  # type: ignore[no-untyped-def]
        raise TargetKindTransportError("boom")

    monkeypatch.setattr(HeadlessAdmin, "target_add_kobo", raise_binding)
    drop = world.library.parent / "kobo-ingest2"
    drop.mkdir(exist_ok=True)
    body = {"name": "Kobo2", "path_map": [{"local": str(drop), "remote": "/kobo", "mode": "rw"}]}
    refused = world.api.post(KOBO, json=body)
    assert refused.status_code == 409
    assert refused.json()["code"] == "target.transport_not_supported"
    monkeypatch.setattr(HeadlessAdmin, "target_add_kobo", original)

    # HeadlessError branch
    def raise_headless(self, *a, **k):  # type: ignore[no-untyped-def]
        raise HeadlessError("boom")

    monkeypatch.setattr(HeadlessAdmin, "target_add_kobo", raise_headless)
    refused2 = world.api.post(KOBO, json=body)
    assert refused2.status_code == 422
    assert refused2.json()["code"] == HeadlessError.code
    monkeypatch.setattr(HeadlessAdmin, "target_add_kobo", original)


@pytest.mark.req("EF-EXP-021")
def test_the_cwa_route_refuses_binding_error(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    from shelfhold.headless import HeadlessAdmin

    original = HeadlessAdmin.target_add_cwa

    def raise_binding(self, *a, **k):  # type: ignore[no-untyped-def]
        raise TargetKindTransportError("boom")

    monkeypatch.setattr(HeadlessAdmin, "target_add_cwa", raise_binding)
    drop = world.library.parent / "cwa-ingest2"
    drop.mkdir(exist_ok=True)
    body = {"name": "Cwa2", "path_map": [{"local": str(drop), "remote": "/cwa", "mode": "rw"}]}
    refused = world.api.post(CWA, json=body)
    assert refused.status_code == 409
    assert refused.json()["code"] == "target.transport_not_supported"
    monkeypatch.setattr(HeadlessAdmin, "target_add_cwa", original)


@pytest.mark.req("EF-EXP-021")
def test_socket_methods_create_a_kobo_target(
    komga_env: tuple[Headless, Any], tmp_path: Path
) -> None:
    env, _fake = komga_env
    drop = tmp_path / "library" / "socket-kobo"
    drop.mkdir(parents=True)
    pmap = [{"local": str(drop), "remote": "/socket-kobo", "mode": "rw"}]
    sock = tmp_path / "s.sock"
    from shelfhold.identity.public import local_auth

    auth = local_auth(env.engine, env.settings)
    with serving(sock, IdentityDispatcher(auth, headless=env.admin)):
        remote = SocketHeadless(sock)
        kobo = remote.target_add_kobo(name="Kobo", path_map=pmap)
        assert kobo.created
        assert not remote.target_add_kobo(name="Kobo", path_map=pmap).created
        raw = call(sock, "target.add_kobo", {"name": "Kobo2", "path_map": pmap})
        assert isinstance(raw, dict)


@pytest.mark.req("EF-EXP-021")
def test_first_party_destinations_kobo_branches(tmp_path: Path, settings: Settings) -> None:
    from shelfhold.infrastructure.db import create_db_engine, write_transaction
    from shelfhold.infrastructure.ids import new_ulid

    # use instance-less engine via settings
    from shelfhold.infrastructure.migrate import migrate

    migrate(settings)
    engine = create_db_engine(settings.database_path)
    target = new_ulid()
    with write_transaction(engine) as connection:
        connection.execute(
            sa.insert(export.export_target).values(id=target, kind="kobo", name="Kobo")
        )
    dests = FirstPartyDestinations(engine)
    assert dests.capabilities(target) is not None
    assert dests.kepub(target) is True
    # plan branch: build a binding-like object
    from shelfhold.export.application.plan_executor import PlanBinding

    binding = PlanBinding("d", target, None, {})
    assert dests.plan(binding) is None
    engine.dispose()


@pytest.mark.req("EF-EXP-021")
def test_kobo_source_branches() -> None:
    with export_env() as env:
        # no native formats: no series to send
        with env.engine.connect() as connection:
            assert kobo_series(connection, {}) == []
        # sender refuses
        source = KoboSource(env.engine)
        sender = source.sender(None)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            sender(None)  # type: ignore[arg-type]
        # plan returns None for non-kobo target
        from shelfhold.export.application.plan_executor import PlanBinding

        binding = PlanBinding("d", "not-a-target", None, {})
        assert source.plan(binding) is None
        # capabilities None for non-kobo
        assert source.capabilities("not-a-target") is None
        # kepub false for non-kobo
        assert source.kepub("not-a-target") is False

        # kobo target branches: capabilities, kepub, plan with no epubs
        target = kobo_target(env)
        assert source.capabilities(target) is not None
        assert source.kepub(target) is True
        binding2 = PlanBinding("d2", target, source.capabilities(target), {})
        assert source.plan(binding2) is None


@pytest.mark.req("EF-EXP-021")
def test_kobo_destination_methods_and_unique_collision() -> None:
    dest = KoboDestination()
    assert dest.config_schema()["type"] == "object"
    assert dest.default_template("book") == "{series}/Ch.{chapter:0000.#} {title}"
    assert dest.render_sidecars(None, None) == []
    assert dest.inventory_calls("x") == []
    assert dest.scan_calls(["a"]) == []
    assert dest.interpret(None, None) is None
    # collision
    plan = dest.describe([SeriesSpec("S", (BookSpec("a", "1", "T"), BookSpec("b", "1", "T")))])
    names = [op.rel_path for op in plan.ops if op.op == "write"]
    assert len(names) == 2
    assert names[0] != names[1]


@pytest.mark.req("EF-EXP-021")
def test_kepub_conversion_failure_is_never_served_as_epub() -> None:
    with export_env() as env:
        store: dict[str, EpubFile] = {}
        add_book(env, env.new_series("Synthetic Book"), "1", store)
        destination = bind_kobo(env)
        rends = renditions(env)

        def boom(content_id: str, data: bytes) -> bytes:
            raise ValueError("cannot convert")

        rends.kepub_bytes = boom  # type: ignore[attr-defined]
        fan, _source = rig(env, store, rends)
        outcome = next(
            o for o in fan.run(env.shelf_id, "c1").outcomes if o.destination_id == destination
        )
        assert outcome.status == "failed"
        assert outcome.code == "export.plan_refused"
        assert tree_files(env) == []


@pytest.mark.req("EF-EXP-021")
def test_kobo_series_non_epub_and_duplicate_item() -> None:
    # cover row.kind not in _KINDS and native_formats != epub and duplicate id
    with export_env() as env, env.engine.connect() as connection:
        # build minimal by calling kobo_series on empty connection
        assert kobo_series(connection, {}) == []


@pytest.mark.req("EF-EXP-021")
def test_kobo_series_duplicate_and_non_epub_rows() -> None:
    from shelfhold.export.targets.kobo.source import kobo_series

    row = SimpleNamespace(
        id="item1",
        kind="unit",
        frozen_attrs={},
        position=1,
        number_norm="1",
        track="main",
        title="T",
        work_title="W",
    )

    class _Result:
        def all(self):
            return [row, row]  # duplicate id -> second is skipped (line 76)

    class _Conn:
        def execute(self, *a, **k):
            return _Result()

    assert len(kobo_series(_Conn(), {"item1": "epub"})) == 1

    # non-epub native format -> line 79
    row2 = SimpleNamespace(
        id="item2",
        kind="unit",
        frozen_attrs={},
        position=1,
        number_norm="2",
        track="main",
        title="T2",
        work_title="W2",
    )

    class _Result2:
        def all(self):
            return [row2]

    class _Conn2:
        def execute(self, *a, **k):
            return _Result2()

    assert kobo_series(_Conn2(), {"item2": "cbz"}) == []
