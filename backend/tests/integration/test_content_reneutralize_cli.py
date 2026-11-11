"""M2 L03: `shelfhold content reneutralize` over a real instance (EF-BIB-051, ENF-OPS-021).

A real database, real roots (/library and /store), the production composition and the real CLI
entry point. The ingest of the EPUB is the production store; the "earlier release" is a derivative
recorded under `xhtml-neutralizer@v0`, so the shipped release is the security update to apply.
"""

import os
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

import shelfhold.runtime_jobs as rj
from shelfhold.config import Settings
from shelfhold.events.waiting_state import WaitingState  # noqa: F401  (the catalogue is loaded)
from shelfhold.export.application.locks import work_lock_for
from shelfhold.export.application.pipeline import SourceMissingError
from shelfhold.identity.public import Credential, Principal
from shelfhold.infrastructure.content_trust import DERIVED_BY
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import storage
from shelfhold.interfaces.cli import adopt as adopt_cli
from shelfhold.library.public import UntrustedEpubStore
from shelfhold.ops import OpsServices
from shelfhold.ops.content import ContentOps
from shelfhold.ops.errors import ExitUnavailableError, StalePreviewError
from tests.auth_env import run_cli
from tests.integration.test_schema_catalog import add_unit, add_variant
from tests.integration.test_untrusted_epub_pipeline import TRAPPED
from tests.l50_env import Seeded, seeded

pytestmark = pytest.mark.req("EF-BIB-051", "ENF-OPS-021")

OLD = "xhtml-neutralizer@v0"
ADMIN = Principal("1000", "uid:1000", "admin", Credential.LOCAL_CLI)


def add_epub(env: Seeded, data: bytes = TRAPPED) -> tuple[str, Path]:
    """An EPUB received through the production store, whose derivative is then `v0`'s."""
    series = env.new_series()
    with env.inst.engine.begin() as connection:
        unit = add_unit(connection, series.edition_id, "1")
        variant = add_variant(connection, unit, series.binding_id, f"ch-{new_ulid()}")
    staged = env.tmp / "staging" / f"{unit}.epub"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(data)
    with env.inst.comp.write_context() as w:
        dest = Path(w.received_path(variant)).with_suffix(".epub")
        store_dir = str(env.inst.settings.store_dir)
        UntrustedEpubStore(
            env.inst.engine, w.fsops, w.loaded.locate, env.inst.settings.data_dir,
            store_dir=store_dir if any(e.path == store_dir for e in w.loaded.expected) else None,
            tombstones=w.tombstones,
            hold_work=work_lock_for(env.inst.engine, clock=env.inst.comp.clock),
        ).ingest(unit_id=unit, variant_id=variant, staged=staged, dest=str(dest))  # fmt: skip
    with env.inst.engine.begin() as connection:
        connection.execute(
            sa.update(storage.content).where(storage.content.c.unit_id == unit)
            .values(derived_by=OLD)
        )  # fmt: skip
    return unit, dest


def content(env: Seeded, unit: str) -> Any:
    with env.inst.engine.connect() as connection:
        return connection.execute(
            sa.select(storage.content).where(storage.content.c.unit_id == unit)
        ).one()


def fingerprint_of(out: str) -> str:
    return next(
        line.split(": ")[1].split(" ")[0].strip()
        for line in out.splitlines()
        if "Empreinte" in line
    )


def test_preview_then_confirmation_regenerates_and_a_stale_plan_changes_nothing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with seeded(settings, tmp_path / "inst") as env:
        unit, library = add_epub(env)
        before = library.read_bytes()
        capsys.readouterr()

        assert run_cli(monkeypatch, settings, "content", "reneutralize") == 0  # the preview
        out = capsys.readouterr().out
        assert unit in out and OLD in out and "original" in out
        assert content(env, unit).derived_by == OLD and library.read_bytes() == before
        fingerprint = fingerprint_of(out)

        stale = run_cli(monkeypatch, settings, "content", "reneutralize", "--confirm", "0" * 64)
        assert stale == 4 and content(env, unit).derived_by == OLD  # not the plan that was shown
        assert "plan" in capsys.readouterr().err

        assert run_cli(monkeypatch, settings, "content", "reneutralize", "--confirm",
                       fingerprint) == 0  # fmt: skip
        row = content(env, unit)
        assert row.derived_by == DERIVED_BY
        assert [e["derived_by"] for e in row.derived_chain] == [OLD]
        assert "régénéré" in capsys.readouterr().out
        assert any(Path(env.inst.settings.store_dir).rglob("*.epub"))  # the original is still kept

        assert run_cli(monkeypatch, settings, "content", "reneutralize") == 0
        assert "déjà produits" in capsys.readouterr().out  # nothing left to do


def test_a_derivative_that_cannot_be_regenerated_is_reported_blocked(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with seeded(settings, tmp_path / "inst") as env:
        unit, library = add_epub(env)
        for kept in Path(settings.store_dir).rglob("*.epub"):
            os.remove(kept)  # the original is lost (the test plays the disk failing)
        library.write_bytes(b"someone else's file")
        capsys.readouterr()
        assert run_cli(monkeypatch, settings, "content", "reneutralize") == 0
        out = capsys.readouterr().out
        assert "blocked" in out and "library_file_was_replaced" in out
        fingerprint = fingerprint_of(out)
        assert run_cli(monkeypatch, settings, "content", "reneutralize", "--confirm",
                       fingerprint) == 0  # fmt: skip
        assert "1 bloqué" in capsys.readouterr().out
        assert library.read_bytes() == b"someone else's file"  # nothing replaced, nothing guessed
        assert content(env, unit).derived_by == OLD


def test_a_failing_regeneration_exits_nonzero_and_keeps_the_old_file(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    import shelfhold.ops.content as ops_content

    def crash(source: Any, out: Any, **kw: Any) -> Any:
        raise RuntimeError("the release crashed")

    real = ops_content.Reneutralizer
    monkeypatch.setattr(
        ops_content, "Reneutralizer",
        lambda store, engine: real(store, engine=engine, neutralize=crash),
    )  # fmt: skip
    with seeded(settings, tmp_path / "inst") as env:
        unit, library = add_epub(env)
        before = library.read_bytes()
        capsys.readouterr()
        run_cli(monkeypatch, settings, "content", "reneutralize")
        fingerprint = fingerprint_of(capsys.readouterr().out)
        assert run_cli(monkeypatch, settings, "content", "reneutralize", "--confirm",
                       fingerprint) == 1  # fmt: skip
        assert "1 en échec" in capsys.readouterr().out
        assert library.read_bytes() == before and content(env, unit).derived_by == OLD


def test_the_ops_service_previews_confirms_and_refuses_a_stale_plan(
    settings: Settings, tmp_path: Path
) -> None:
    with seeded(settings, tmp_path / "inst") as env:
        unit, _library = add_epub(env)
        ops = ContentOps(env.inst.comp)
        shown = ops.reneutralize(confirm=None)
        assert shown["applied"] is None and [i["unit"] for i in shown["items"]] == [unit]
        with pytest.raises(StalePreviewError):
            ops.reneutralize(confirm="1" * 64)
        done = ops.reneutralize(confirm=shown["fingerprint"])
        assert done["applied"] == {"regenerated": 1, "blocked": 0, "failed": 0}
        assert content(env, unit).derived_by == DERIVED_BY


def test_the_content_exit_is_unavailable_without_the_service(settings: Settings) -> None:
    from shelfhold.infrastructure.clock import SystemClock
    from shelfhold.infrastructure.db import create_db_engine
    from shelfhold.infrastructure.migrate import migrate

    migrate(settings)
    engine = create_db_engine(settings.database_path)
    try:
        from shelfhold.events.bus import EventBus

        ops = OpsServices(engine, clock=SystemClock(), bus=EventBus(SystemClock()))
        with pytest.raises(ExitUnavailableError):
            ops.content  # noqa: B018
    finally:
        engine.dispose()


def test_the_local_socket_method_reaches_the_service(settings: Settings, tmp_path: Path) -> None:
    from shelfhold.interfaces.local_cli.ops import _content_reneutralize  # the socket's method

    with seeded(settings, tmp_path / "inst") as env:
        unit, _ = add_epub(env)

        class Ops:
            content = ContentOps(env.inst.comp)

        shown: Any = _content_reneutralize(Ops(), ADMIN, {})  # type: ignore[arg-type]
        assert [i["unit"] for i in shown["items"]] == [unit]
        done: Any = _content_reneutralize(Ops(), ADMIN, {"confirm": shown["fingerprint"]})  # type: ignore[arg-type]
        assert done["applied"]["regenerated"] == 1


def test_export_refuses_an_untrusted_epub_whose_derivative_is_gone(
    settings: Settings, tmp_path: Path
) -> None:
    """The export pipeline asks for the derivative: no derivative, no source."""
    with seeded(settings, tmp_path / "inst") as env:
        unit, library = add_epub(env)
        pipeline, _reconciler, _fsops = rj.build_pipeline(env.inst.comp, env.scans)
        # M2 L09: an EPUB is exported, by its verified derivative and by nothing else
        assert pipeline._source(unit).local_path == str(library)
        library.write_bytes(TRAPPED)  # the original lands in /library: it is not servable
        with pytest.raises(SourceMissingError, match="library_not_derivative"):
            pipeline._source(unit)
        with env.inst.engine.begin() as connection:
            connection.execute(
                sa.update(storage.content).values(
                    derived_hash=None, derived_by=None, derived_hash_by=None
                )
            )
        with pytest.raises(SourceMissingError, match="no_derivative"):
            pipeline._source(unit)


def test_the_adoption_report_lists_active_content_for_the_admin(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from tests.adopt_env import adoption
    from tests.export_env import export_env
    from tests.kavita_env import run

    with export_env() as env:
        a = adoption(env)
        a.add_series("Shelf", {"Book.epub": TRAPPED})
        report = run(a.adopter.preview(a.wired.client, env.destination_id))
    adopt_cli._show(report)
    assert "Book.epub" in capsys.readouterr().out
