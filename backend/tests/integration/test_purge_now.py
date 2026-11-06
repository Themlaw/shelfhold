"""EF-BIB-052, EF-BIB-053: `purge --now` (L34). The order is the safety: the tombstone first and
durable, then the intent, then the deletion, then the closing. Real files, real database, real
cipher; the exported library comes from the real pipeline."""

import ast
import inspect
from pathlib import Path

import pytest
import sqlalchemy as sa

import shelfhold.export.application.purge as purge_module
from shelfhold.export.application.purge import (
    OUT_OF_REACH,
    PurgeCollateralError,
    PurgeRefusedError,
    PurgeReport,
)
from shelfhold.export.domain.physical_roots import RECEIVED_DIR
from shelfhold.infrastructure.fsops import Fsops, TombstonePendingError
from shelfhold.infrastructure.fsops.errors import RootBlockedError
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import (
    acquisition,
    catalog,
    coherence,
    identity,
    platform,
    storage,
)
from shelfhold.library.public import ConfirmationRequiredError, record_replica
from shelfhold.library.tombstones import PlaceRef
from tests.export_env import ExportEnv, export_env
from tests.purge_env import (
    ADMIN,
    KEY,
    READER,
    STALE,
    TITLE,
    PurgeEnv,
    make_purge_env,
    open_ticket,
    spread,
)

pytestmark = pytest.mark.req("EF-BIB-052")


def files_everywhere(env: ExportEnv) -> list[Path]:
    """Every regular file a Unit's copy could be, in the two roots, bin included."""
    found: list[Path] = []
    for root in (env.lib.library, env.lib.store):
        for path in root.rglob("*"):
            if not path.is_file() or path.name == ".shelfhold-mount-id":
                continue
            rel = path.relative_to(root).parts
            if rel[:2] in {(".shelfhold", "tombstones"), (".shelfhold", "lease")}:
                continue
            found.append(path)
    return sorted(found)


def run(purge: PurgeEnv, unit: str, **kw: object) -> PurgeReport:
    kw.setdefault("principal", ADMIN)
    kw.setdefault("typed", unit)
    return purge.purge.purge_unit(unit, now=purge.clock.now(), **kw)  # type: ignore[arg-type]


@pytest.mark.req("EF-BIB-052", "EF-BIB-053")
def test_purge_now_removes_all_replicas_and_thumbnails() -> None:
    with export_env() as env:
        purge = make_purge_env(env.lib)
        series = env.new_series(TITLE)
        unit, placed = spread(env, series)
        other = env.add_chapter(series, "2")  # a neighbour: it must survive
        assert len(files_everywhere(env)) >= 5
        work_json = env.lib.data / "meta" / "works" / series.work_id[:2] / f"{series.work_id}.json"
        assert unit in work_json.read_text()
        assert (env.lib.data / "meta" / "units" / unit[:2] / f"{unit}.json").exists()
        mirrors = sorted((env.lib.library / ".shelfhold" / "manifest").rglob("*.jsonl"))
        assert mirrors and unit in "".join(m.read_text() for m in mirrors)  # a line for the Unit

        report = run(purge, unit)

        assert report.complete and report.unreachable_roots == () and report.refused == ()
        remaining = [p for p in files_everywhere(env) if "manifest" not in p.parts]
        assert remaining == [env.lib.manga / RECEIVED_DIR / f"{other}.cbz"]  # only the neighbour
        assert not placed["binned"].exists()  # type: ignore[union-attr]
        for folder in placed["caches"]:  # type: ignore[union-attr]
            assert not folder.exists()  # thumbnails and content cache
        assert not (env.lib.data / "meta" / "units" / unit[:2] / f"{unit}.json").exists()
        assert unit not in work_json.read_text() and other in work_json.read_text()  # rewritten
        for path in (env.lib.library / ".shelfhold" / "manifest").rglob("*.jsonl"):
            text = path.read_text()
            assert unit not in text  # no line of the purged Unit
        for table in (catalog.unit, catalog.unit_variant, storage.content):
            assert (
                purge.count(
                    table,
                    table.c.unit_id == unit if table is not catalog.unit else table.c.id == unit,
                )
                == 0
            )
        assert purge.count(storage.replica, storage.replica.c.unit_id == unit) == 0
        assert purge.count(storage.trash_item, storage.trash_item.c.ref == unit) == 0
        assert purge.count(catalog.unit, catalog.unit.c.id == other) == 1  # the neighbour is intact
        assert [e for e in report.out_of_reach] == list(OUT_OF_REACH)
        assert "host_backups" in report.out_of_reach


@pytest.mark.req("EF-BIB-052")
def test_purge_writes_tombstone_before_any_deletion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tombstone, fsync (the line is in the journal), intent `applying`, then the first unlink."""
    with export_env() as env:
        purge = make_purge_env(env.lib)
        unit, _ = spread(env, env.new_series(TITLE))
        seen: list[dict[str, object]] = []
        real = Fsops.purge_unlink

        def spy(self: Fsops, local: str) -> None:
            with env.engine.connect() as connection:
                seen.append(
                    {
                        "rows": connection.execute(
                            sa.select(sa.func.count()).select_from(coherence.content_tombstone)
                        ).scalar_one(),
                        "pending": connection.execute(
                            sa.select(sa.func.count()).select_from(coherence.tombstone_pending_root)
                        ).scalar_one(),
                        "intent": connection.execute(
                            sa.select(coherence.fs_intent.c.state).where(
                                coherence.fs_intent.c.src_path == local,
                                coherence.fs_intent.c.op == "unlink",
                            )
                        ).scalar(),
                        "journals": [
                            len(purge.journal(r)) for r in (env.lib.library, env.lib.store)
                        ],
                    }
                )
            real(self, local)

        monkeypatch.setattr(Fsops, "purge_unlink", spy)
        run(purge, unit)
        assert seen, "nothing was deleted"
        for step in seen:
            assert step["rows"] == 2  # variant and place, in the database
            assert step["pending"] == 0  # and already written to every root's journal
            assert all(size > 0 for size in step["journals"])  # type: ignore[attr-defined]
            assert step["intent"] == "applying"
        with env.engine.connect() as connection:
            left = connection.execute(
                sa.select(coherence.fs_intent.c.state).where(coherence.fs_intent.c.op == "unlink")
            ).all()
        assert left == []  # closed (done), then scrubbed: their paths spell the title


@pytest.mark.req("EF-BIB-052")
def test_purge_crash_midway_keeps_tombstone_and_rerun_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Crash(BaseException):
        pass

    with export_env() as env:
        purge = make_purge_env(env.lib)
        unit, _ = spread(env, env.new_series(TITLE))
        real = Fsops.purge_unlink
        calls: list[str] = []

        def crash_second(self: Fsops, local: str) -> None:
            calls.append(local)
            if len(calls) == 2:
                raise Crash
            real(self, local)

        monkeypatch.setattr(Fsops, "purge_unlink", crash_second)
        with pytest.raises(Crash):
            run(purge, unit)
        monkeypatch.undo()
        assert purge.count(coherence.content_tombstone) == 2  # the tombstone outlives the crash
        assert purge.count(catalog.unit, catalog.unit.c.id == unit) == 1  # rows kept: nothing lost
        again = make_purge_env(env.lib)  # a restart: the membership index is rebuilt from disk
        report = run(again, unit)
        assert report.complete
        assert again.count(coherence.content_tombstone) == 2  # not written twice
        assert again.count(catalog.unit, catalog.unit.c.id == unit) == 0
        with env.engine.connect() as connection:
            open_ones = connection.execute(
                sa.select(coherence.fs_intent.c.id).where(
                    coherence.fs_intent.c.state.in_(("pending", "applying"))
                )
            ).all()
        assert open_ones == []


@pytest.mark.req("EF-BIB-052", "EF-USR-024")
@pytest.mark.parametrize("who", ["api_key", "stale_session", "non_admin"])
def test_purge_requires_a_fresh_admin_reserved_action(who: str) -> None:
    principal = {"api_key": KEY, "stale_session": STALE, "non_admin": READER}[who]
    with export_env() as env:
        purge = make_purge_env(env.lib)
        unit, _ = spread(env, env.new_series(TITLE))
        before = files_everywhere(env)
        with pytest.raises(PurgeRefusedError):
            run(purge, unit, principal=principal)
        assert files_everywhere(env) == before  # nothing deleted
        assert purge.count(coherence.content_tombstone) == 0  # nothing recorded either
        assert purge.count(catalog.unit, catalog.unit.c.id == unit) == 1


@pytest.mark.req("EF-BIB-052", "EF-BIB-031")
@pytest.mark.parametrize("typed", [None, "", "yes", "purge"])
def test_purge_needs_the_typed_confirmation_always(typed: str | None) -> None:
    with export_env() as env:
        purge = make_purge_env(env.lib)
        unit, _ = spread(env, env.new_series(TITLE))
        before = files_everywhere(env)
        with pytest.raises(ConfirmationRequiredError):
            run(purge, unit, typed=typed)
        assert files_everywhere(env) == before
        assert purge.count(coherence.content_tombstone) == 0


@pytest.mark.req("EF-BIB-052", "EF-BIB-053")
def test_purge_with_absent_rw_root_writes_journal_on_remount() -> None:
    with export_env() as env:
        purge = make_purge_env(env.lib)
        unit, _ = spread(env, env.new_series(TITLE))
        away = env.lib.tmp / "store-away"
        env.lib.store.rename(away)  # the second rw root is not mounted
        report = run(purge, unit)

        assert not report.complete
        assert report.unreachable_roots == (env.lib.store_root,)
        assert (
            purge.count(
                coherence.tombstone_pending_root,
                coherence.tombstone_pending_root.c.root_id == env.lib.store_root,
            )
            == 2
        )
        assert purge.journal(env.lib.library)  # the mounted root has its lines already
        # never acquirable again, even while the root is away and its journal has no line
        place = PlaceRef.of(
            _key(env, unit), track="main", number_segment="", number_raw="1", part=""
        )
        assert purge.tombstones.check(place, root_id=env.lib.store_root).blocked
        assert purge.count(catalog.unit, catalog.unit.c.id == unit) == 1  # the copy there is kept

        away.rename(env.lib.store)  # the root comes back
        env.lib.fsops.enforce_tombstone_flush(
            lambda path: purge.tombstones.has_pending(_root_id(env, path))
        )
        with pytest.raises((TombstonePendingError, RootBlockedError)):
            env.lib.fsops.write_atomic(
                str(env.lib.store / "intruder.cbz"), b"x"
            )  # before the flush
        flushed = purge.tombstones.replay(purge.roots)
        assert flushed.flushed == 2
        assert purge.journal(env.lib.store)  # the lines are there, before any other write
        assert not (env.lib.store / "intruder.cbz").exists()
        env.lib.fsops.write_atomic(str(env.lib.store / "after.cbz"), b"x")  # now it may write
        done = run(purge, unit)  # the rerun erases the copy that was out of reach
        assert done.complete and purge.count(catalog.unit, catalog.unit.c.id == unit) == 0
        assert purge.count(coherence.content_tombstone) == 2  # no second tombstone


def _key(env: ExportEnv, unit: str) -> dict[str, str]:
    with env.engine.connect() as connection:
        row = connection.execute(
            sa.select(catalog.edition)
            .select_from(
                catalog.edition.join(
                    catalog.unit, catalog.unit.c.edition_id == catalog.edition.c.id
                )
            )
            .where(catalog.unit.c.id == unit)
        ).one()
    return {
        k: str(getattr(row, k))
        for k in (
            "work_id",
            "lang",
            "translation_type",
            "media",
            "publisher",
            "text_version",
            "performance",
            "packaging",
        )
    }


def _root_id(env: ExportEnv, path: str) -> str:
    return env.lib.store_root if path.startswith(str(env.lib.store)) else env.lib.library_root


@pytest.mark.req("EF-BIB-052", "EF-SRC-009")
def test_purge_m1a_releases_claim_without_writing_suwayomi() -> None:
    with export_env() as env:
        purge = make_purge_env(env.lib)
        unit, _ = spread(env, env.new_series(TITLE))
        assert purge.count(storage.replica, storage.replica.c.location == "upstream") == 1
        report = run(purge, unit)
        # the claim is withdrawn on our side, and the admin is told to delete upstream
        assert purge.count(storage.replica, storage.replica.c.location == "upstream") == 0
        assert report.upstream_copies == 1
        assert report.alerts == ("unit.delete_in_suwayomi",)
        # the purge holds no client of the service: it cannot write upstream (structural)
        source = Path(inspect.getsourcefile(purge_module) or "").read_text(encoding="utf-8")
        imported = [
            n.module or "" for n in ast.walk(ast.parse(source)) if isinstance(n, ast.ImportFrom)
        ]
        assert not [m for m in imported if "plugin_host" in m or ".sdk" in m or "fetcher" in m]


@pytest.mark.req("EF-BIB-052")
def test_purge_m1a_closes_download_tickets_shelfhold_side() -> None:
    with export_env() as env:
        purge = make_purge_env(env.lib)
        unit, _ = spread(env, env.new_series(TITLE))
        open_one = open_ticket(env, unit, "requested")
        report = run(purge, unit)
        assert report.closed_tickets == 1
        with env.engine.connect() as connection:
            row = connection.execute(
                sa.select(acquisition.download_request).where(
                    acquisition.download_request.c.id == open_one
                )
            ).one()
        assert row.state == "closed_by_purge" and row.variant_id is None


@pytest.mark.req("EF-BIB-052")
def test_purge_never_deletes_in_an_adoption_root_and_says_so() -> None:
    with export_env() as env:
        purge = make_purge_env(env.lib)
        unit, _ = spread(env, env.new_series(TITLE))
        ro_id = new_ulid()
        adopted = env.lib.tmp / "upstream-ro"
        adopted.mkdir()
        (adopted / "ch.cbz").write_bytes(b"read-only original")
        with env.engine.begin() as connection:
            connection.execute(
                sa.insert(platform.physical_root).values(
                    root_id=ro_id, path_entry=str(adopted), mode="adopt_ro"
                )
            )
            record_replica(
                connection, unit_id=unit, location="adopted_ro", content_hash=None,
                independent=False, mutable_by_third_party=True, physical_root_id=ro_id,
                rel_path="ch.cbz",
            )  # fmt: skip
        report = run(purge, unit)
        assert (adopted / "ch.cbz").read_bytes() == b"read-only original"
        assert [(r.reason, r.location) for r in report.refused] == [
            ("read_only_root", "adopted_ro")
        ]
        assert not report.complete  # it cannot be erased: the report says so


@pytest.mark.req("EF-BIB-052")
def test_purge_refuses_a_file_that_also_carries_another_unit() -> None:
    """A volume file that covers two Units cannot be erased for one of them without erasing the
    other: refused, listing the count, unless the caller names the collateral."""
    with export_env() as env:
        purge = make_purge_env(env.lib)
        series = env.new_series(TITLE)
        unit, _ = spread(env, series)
        neighbour = env.add_chapter(series, "2")
        with env.engine.begin() as connection:
            item = connection.execute(
                sa.text(
                    "SELECT carrier_id FROM unit_coverage "
                    "WHERE unit_id = :u AND carrier_type = 'export_item'"
                ),
                {"u": unit},
            ).scalar_one()
            connection.execute(
                sa.text(
                    "INSERT INTO unit_coverage (carrier_type, carrier_id, unit_id, position, "
                    "extent, origin) VALUES ('export_item', :i, :n, 2, 'full', 'manual')"
                ),
                {"i": item, "n": neighbour},
            )
        before = files_everywhere(env)
        with pytest.raises(PurgeCollateralError) as raised:
            run(purge, unit)
        assert raised.value.other_units == 1
        assert files_everywhere(env) == before and purge.count(coherence.content_tombstone) == 0


@pytest.mark.req("EF-BIB-052", "EF-BIB-053")
def test_purge_waits_for_the_resource_locks_of_the_exporter() -> None:
    from shelfhold.export.application.locks import LockBusyError, ResourceLocks

    with export_env() as env:
        purge = make_purge_env(env.lib)
        series = env.new_series(TITLE)
        unit, _ = spread(env, series)
        locks = ResourceLocks(env.engine, clock=env.clock)
        held = locks.hold(new_ulid(), work=series.work_id, series="x/y")
        with held, pytest.raises(LockBusyError):
            run(purge, unit)
        assert purge.count(coherence.content_tombstone) == 0  # nothing started


@pytest.mark.req("EF-BIB-053")
def test_tombstone_and_audit_hold_no_title() -> None:
    with export_env() as env:
        purge = make_purge_env(env.lib)
        unit, _ = spread(env, env.new_series(TITLE))
        run(purge, unit)
        text = purge.dump() + (
            purge.journal(env.lib.library) + purge.journal(env.lib.store)
        ).decode("latin-1")
        assert TITLE not in text and TITLE.lower().replace(" ", "-") not in text
        with env.engine.connect() as connection:
            audit = connection.execute(sa.select(identity.audit)).all()
            reasons = set(
                connection.execute(sa.select(coherence.content_tombstone.c.reason)).scalars()
            )
        (entry,) = [a for a in audit if a.action == "purge.now"]
        assert entry.target == f"unit:{unit}" and entry.actor_id.startswith("local_cli:")
        assert reasons == {"purge_unit"}  # an enumeration, never free text
        for event in env.rows(platform.event):
            if event.type == "ops.purge_done":
                assert set(event.payload) <= {"unit_id", "erased", "complete", "upstream_copies"}


@pytest.mark.req("EF-BIB-052")
def test_purge_deletes_only_inside_the_roots_and_never_follows_links() -> None:
    """A replica row that points at a link or at a path through a link deletes nothing outside."""
    with export_env() as env:
        purge = make_purge_env(env.lib)
        unit, _ = spread(env, env.new_series(TITLE))
        outside = env.lib.tmp / "outside"
        outside.mkdir()
        keep = outside / "precious.cbz"
        keep.write_bytes(b"not ours")
        (env.lib.manga / "link").symlink_to(outside)
        with env.engine.begin() as connection:
            record_replica(
                connection, unit_id=unit, location="destination", content_hash=None,
                independent=False, mutable_by_third_party=True,
                physical_root_id=env.lib.library_root, rel_path="manga/link/precious.cbz",
            )  # fmt: skip
        report = run(purge, unit)
        assert keep.read_bytes() == b"not ours"  # never followed
        assert any(r.reason == "path_escape" for r in report.refused)
        assert not report.complete
