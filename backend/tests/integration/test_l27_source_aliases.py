"""M3 L27 (EF-SRC-022, EF-BIB-054 (4)): `source migrate-ids`, the bulk migration of the identity of
a source's series, with a preview that writes nothing and a tombstone check that cannot be walked
around.

A real instance (database, keyring, roots). The tombstone is laid by the real `purge --now --work`
of the host CLI; the aliases are rows of `source_alias`, or what a plugin's `migrate_ref` says.
Every reference is synthetic (`source-a`, `plugin-a`)."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.application.alias_migration import resolve_alias_chain
from shelfhold.config import Settings
from shelfhold.identity.public import (
    RESERVED_ACTIONS,
    Credential,
    CredentialNotAllowed,
    Principal,
    ReauthRequired,
    authorize_reserved,
)
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, catalog, coherence, identity, platform
from shelfhold.library.public import SeriesBindingRef, TombstoneService, VariantRef
from shelfhold.ops import source_migration
from shelfhold.ops.errors import CodedRefusal, StalePreviewError
from shelfhold.ops.source_migration import SourceMigrationOps
from tests.auth_env import db_dump, run_cli
from tests.integration.test_schema_catalog import add_binding, add_edition, add_work
from tests.l50_env import Seeded, Series, seeded

pytestmark = pytest.mark.req("EF-SRC-022", "EF-BIB-054")

PLUGIN, SOURCE = "plugin-a", "source-a"
FROM, TO = "slug", "numeric"
ADMIN = Principal("1000", "uid:1000", "admin", Credential.LOCAL_CLI)
GONE = "gone-1"  # the series that was erased


def live(env: Seeded, remote_id: str, *, chapters: tuple[str, ...] = ("1", "2")) -> Series:
    """A Work, its Edition, a source item at `remote_id` and the chapters received for it."""
    with env.inst.engine.begin() as connection:
        work = add_work(connection, title=f"Title {remote_id}", sort_title=remote_id)
        edition = add_edition(connection, work, lang="fr", shelf_id=env.shelf_id)
        item_id = new_ulid()
        connection.execute(
            sa.insert(acquisition.source_item).values(
                id=item_id, plugin_id=PLUGIN, source_key=SOURCE, remote_id=remote_id
            )
        )
        binding = add_binding(connection, work, edition, item_id)
    series = Series(work, edition, binding)
    for number in chapters:
        env.add_chapter(series, number)
    return series


def alias(env: Seeded, old: str, new: str, *, old_key: str = SOURCE, new_key: str = SOURCE) -> None:
    with env.inst.engine.begin() as connection:
        connection.execute(
            sa.insert(acquisition.source_alias).values(
                plugin_id=PLUGIN, old_source_key=old_key, old_remote_id=old,
                new_source_key=new_key, new_remote_id=new, id_scheme=TO,
            )
        )  # fmt: skip


def item_ref(env: Seeded, series: Series) -> str:
    with env.inst.engine.connect() as connection:
        return connection.execute(
            sa.select(acquisition.source_item.c.remote_id)
            .select_from(
                acquisition.source_binding.join(
                    acquisition.source_item,
                    acquisition.source_binding.c.source_item_id == acquisition.source_item.c.id,
                )
            )
            .where(acquisition.source_binding.c.id == series.binding_id)
        ).scalar_one()


def iid(env: Seeded, series: Series) -> str:
    """The `source_item` row of a series: what a plan names (never a title, never a reference)."""
    with env.inst.engine.connect() as connection:
        return connection.execute(
            sa.select(acquisition.source_binding.c.source_item_id).where(
                acquisition.source_binding.c.id == series.binding_id
            )
        ).scalar_one()


def tree(root: Path, skip: str) -> list[tuple[str, int, int]]:
    return sorted(
        (str(p.relative_to(root)), p.stat().st_size, p.stat().st_mtime_ns)
        for p in root.rglob("*")
        if p.is_file() and not p.name.startswith(skip)
    )


def everything(env: Seeded, settings: Settings) -> tuple[Any, ...]:
    """The whole state: the database, the data directory and every root, byte for byte."""
    return (
        db_dump(settings).splitlines(),
        tree(settings.data_dir, settings.database_path.name),
        tree(env.inst.library, "\0"),
    )


def make_ops(env: Seeded, mapping: dict[str, str] | None = None) -> SourceMigrationOps:
    """What `migrate_ref` of a plugin says (a test's own: no plugin is loaded)."""
    said = mapping or {}

    def migrate_ref(plugin_id: str, source_key: str, remote_id: str) -> tuple[str, str] | None:
        return (source_key, said[remote_id]) if remote_id in said else None

    return SourceMigrationOps(env.inst.comp, ref_migrator=migrate_ref)


def plan(ops: SourceMigrationOps, confirm: str | None = None) -> dict[str, Any]:
    return ops.migrate_ids(SOURCE, from_scheme=FROM, to_scheme=TO, confirm=confirm, principal=ADMIN)


def ids(report: dict[str, Any], key: str) -> set[str]:
    return {row["id"] for row in report[key]}


def erase(env: Seeded, monkeypatch: pytest.MonkeyPatch, settings: Settings, series: Series) -> None:
    """The real `purge --now --work`: it lays the `series_binding` tombstone of the item."""
    assert run_cli(monkeypatch, settings, "purge", "--now", "--work", series.work_id,
                   "--confirm", series.work_id) == 0  # fmt: skip


class World:
    """One clean series (`old-1`, to `new-1`), one erased (`gone-1`) and live ones whose alias
    would land on it by a spelling, a chain or an inverse alias."""

    def __init__(self, env: Seeded, monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
        self.env, self.settings = env, settings
        self.erased = live(env, GONE)
        self.clean = live(env, "old-1")
        alias(env, "old-1", "new-1")
        erase(env, monkeypatch, settings, self.erased)
        self.hits = {
            name: live(env, name, chapters=("1",))
            for name in (
                "hit-exact",
                "hit-case",
                "hit-slash",
                "hit-wide",
                "hit-quoted",
                "hit-chain",
                "hit-middle",
            )
        }
        alias(env, "hit-exact", GONE)
        alias(env, "hit-case", "GONE-1")
        alias(env, "hit-slash", "gone-1/")
        alias(env, "hit-wide", "\uff47one-1")
        alias(env, "hit-quoted", "gone%2D1")
        alias(env, "hit-chain", "hop-1")  # hop-1 -> hop-2 -> GONE-1: the end of the chain
        alias(env, "hop-1", "hop-2")
        alias(env, "hop-2", "GONE-1")
        alias(env, "hit-middle", GONE)  # gone-1 -> fresh-1: the middle of the chain is erased
        alias(env, GONE, "fresh-1")
        self.before = everything(env, settings)

    def blocked_ids(self, report: dict[str, Any]) -> set[str]:
        return ids(report, "blocked")


# -- the preview, the application ---------------------------------------------------------------


def test_source_alias_resolution_and_bulk_migration_dry_run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with seeded(settings, tmp_path / "inst") as env:
        mine = live(env, "old-2")  # no alias row: only `migrate_ref` knows it
        other = live(env, "old-3")
        alias(env, "old-3", "new-3")
        alias(env, "old-1", "new-1")  # (no live item at old-1: only the chain below uses it)
        # resolution: a chain is followed to its end, a cycle and an unknown ref are said
        alias(env, "new-1", "newer-1")
        alias(env, "loop-a", "loop-b")
        alias(env, "loop-b", "loop-a")
        with env.inst.engine.connect() as connection:
            chain = resolve_alias_chain(connection, PLUGIN, SOURCE, "old-1")
            assert chain.terminal == (SOURCE, "newer-1")
            assert chain.members == ((SOURCE, "old-1"), (SOURCE, "new-1"), (SOURCE, "newer-1"))
            assert resolve_alias_chain(connection, PLUGIN, SOURCE, "loop-a").cyclic
            assert resolve_alias_chain(connection, PLUGIN, SOURCE, "never-seen").terminal is None
        ops = make_ops(env, {"old-2": "new-2"})
        before = everything(env, settings)
        events = env.rows(platform.event)

        report = plan(ops)  # the preview lists the correspondences...
        assert ids(report, "migrate") == {iid(env, mine), iid(env, other)}
        olds = {row["old"]: row["new"] for row in report["migrate"]}
        assert olds == {"old-2": "new-2", "old-3": "new-3"}
        assert report["applied"] is None and len(report["fingerprint"]) == 64
        # ...and writes nothing, nowhere: database, data directory, roots, events, jobs
        assert everything(env, settings) == before
        assert env.rows(platform.event) == events
        capsys.readouterr()
        assert run_cli(monkeypatch, settings, "source", "migrate-ids", SOURCE, "--from-scheme",
                       FROM, "--to-scheme", TO, "--preview") == 0  # fmt: skip
        assert "old-3" in capsys.readouterr().out  # the host CLI sees the aliases of the table
        assert everything(env, settings) == before
        assert item_ref(env, mine) == "old-2" and item_ref(env, other) == "old-3"


def test_applied_migration_keeps_every_unit_and_downloads_nothing(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    with seeded(settings, tmp_path / "inst") as env:
        world = World(env, monkeypatch, settings)
        ops = make_ops(env, {"old-2": "new-2"})
        mine = live(env, "old-2")
        units, variants = env.rows(catalog.unit), env.rows(catalog.unit_variant)
        files, queue = tree(env.inst.library, "\0"), env.rows(platform.job_view)
        fingerprint = plan(ops)["fingerprint"]

        done = plan(ops, fingerprint)
        assert done["applied"] == 2  # old-1 and old-2; everything a tombstone covers is held
        assert item_ref(env, world.clean) == "new-1" and item_ref(env, mine) == "new-2"
        # the Units keep their identity: same rows, same variants, same files, nothing queued
        assert env.rows(catalog.unit) == units and env.rows(catalog.unit_variant) == variants
        assert tree(env.inst.library, "\0") == files and env.rows(platform.job_view) == queue
        # what came from `migrate_ref` leaves its alias: the old reference still resolves
        with env.inst.engine.connect() as connection:
            assert resolve_alias_chain(connection, PLUGIN, SOURCE, "old-2").terminal == (
                SOURCE, "new-2")  # fmt: skip
        record = env.rows(acquisition.source_alias_migration)
        assert [(r.source_key, r.from_scheme, r.to_scheme, r.state) for r in record] == [
            (SOURCE, FROM, TO, "applied")
        ]
        # run again: nothing is left to do, and nothing is duplicated
        again = plan(ops)
        assert not again["migrate"] and again["applied"] is None
        assert len(env.rows(acquisition.source_item)) == len(
            {(r.plugin_id, r.source_key, r.remote_id) for r in env.rows(acquisition.source_item)}
        )


def test_audit_and_events_hold_no_readable_value(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    with seeded(settings, tmp_path / "inst") as env:
        World(env, monkeypatch, settings)
        ops = make_ops(env)
        known = {e.id for e in env.rows(platform.event)}
        plan(ops, plan(ops)["fingerprint"])
        audit = [r for r in env.rows(identity.audit) if r.action == "source.migrate_ids"]
        shape = r"mig:[0-9A-Z]{26};fp:[0-9a-f]{16};rows:\d+;held:\d+"
        assert len(audit) == 1 and re.fullmatch(shape, audit[0].target)
        written = json.dumps(
            [
                [str(c) for c in r]
                for r in (
                    *audit,
                    *[e for e in env.rows(platform.event) if e.id not in known],
                    *[(r.id, r.preview) for r in env.rows(acquisition.source_alias_migration)],
                )
            ]
        ).lower()
        for value in ("old-1", "new-1", "gone", "hit-", "hop-", "fresh-1", SOURCE, PLUGIN):
            assert value not in written, value


# -- the tombstones --------------------------------------------------------------------------


def test_identity_migration_consults_tombstones(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    with seeded(settings, tmp_path / "inst") as env:
        world = World(env, monkeypatch, settings)
        ops = make_ops(env)
        report = plan(ops)
        # by an alias, a case, a trailing slash, a wide form, an encoding, a chain: blocked
        held = {name: iid(env, s) for name, s in world.hits.items()}
        for name in ("hit-exact", "hit-case", "hit-slash", "hit-wide", "hit-quoted", "hit-chain",
                     "hit-middle"):  # fmt: skip
            assert held[name] in ids(report, "blocked"), name
            assert held[name] not in ids(report, "migrate"), name
        assert ids(report, "migrate") == {iid(env, world.clean)}
        assert world.before == everything(env, settings)  # the preview wrote nothing
        assert not [e for e in env.rows(platform.event) if e.type == "tombstone.migration_hit"]

        applied = plan(ops, report["fingerprint"])
        assert applied["applied"] == 1  # only the clean one
        for name, series in world.hits.items():  # no blocked row moved, none came back
            assert item_ref(env, series) == name
        hits = [e for e in env.rows(platform.event) if e.type == "tombstone.migration_hit"]
        assert hits and {json.dumps(e.payload) for e in hits} == {'{"scope": "series_binding"}'}
        with env.inst.engine.connect() as connection:  # no alias was written for a blocked row
            rows = connection.execute(sa.select(acquisition.source_alias)).all()
        assert not [r for r in rows if r.new_remote_id.lower() == GONE and r.id_scheme == FROM]


def test_migrate_ref_of_a_plugin_cannot_bring_an_erased_series_back(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    with seeded(settings, tmp_path / "inst") as env:
        erased = live(env, GONE)
        erase(env, monkeypatch, settings, erased)
        victim = live(env, "old-5")
        for said in (GONE, "Gone-1", "gone-1/", "gone%2D1"):  # the plugin names the erased series
            ops = make_ops(env, {"old-5": said})
            before = everything(env, settings)  # (an apply below leaves its record)
            report = plan(ops)
            assert iid(env, victim) in ids(report, "blocked"), said
            assert not report["migrate"]
            assert everything(env, settings) == before  # the preview wrote nothing
            applied = plan(ops, report["fingerprint"])
            assert applied["applied"] == 0
            assert item_ref(env, victim) == "old-5"
        with env.inst.engine.connect() as connection:
            # no alias at all: it would write the erased reference in clear (the folded forms are
            # held by the tombstone as it is)
            assert not connection.execute(sa.select(acquisition.source_alias)).all()
        service = SourceMigrationOps(env.inst.comp)._service()
        assert service.check(SeriesBindingRef(PLUGIN, SOURCE, "gone%2D1"), root_id=None).blocked


def test_alias_inverse_checked_for_membership(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    """The series `gone-1` was erased; a live row at `back-1` has an alias to `target-1`, and
    `gone-1` has one to `target-1` as well: both are the same series upstream. The reverse alias
    of the target names the erased one, so the migration is blocked."""
    with seeded(settings, tmp_path / "inst") as env:
        erased = live(env, GONE)
        erase(env, monkeypatch, settings, erased)
        back = live(env, "back-1")
        fine = live(env, "fine-1")
        alias(env, "back-1", "target-1")
        alias(env, GONE, "target-1")  # the inverse: what is erased also leads to the target
        alias(env, "fine-1", "target-2")
        ops = make_ops(env)
        report = plan(ops)
        assert ids(report, "blocked") == {iid(env, back)}
        assert ids(report, "migrate") == {iid(env, fine)}
        plan(ops, report["fingerprint"])
        assert item_ref(env, back) == "back-1" and item_ref(env, fine) == "target-2"


# -- what a migration must not do ----------------------------------------------------------------


def test_a_migration_never_duplicates_an_existing_series(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    with seeded(settings, tmp_path / "inst") as env:
        mover = live(env, "old-1")
        occupant = live(env, "new-1")
        first, second = live(env, "old-a"), live(env, "old-b")
        alias(env, "old-1", "new-1")  # the target is already a series of the library
        alias(env, "old-a", "same-1")
        alias(env, "old-b", "same-1")  # two series that would land on one reference
        ops = make_ops(env)
        report = plan(ops)
        assert ids(report, "unresolved") == {iid(env, mover), iid(env, first), iid(env, second)}
        assert not report["migrate"]
        assert plan(ops, report["fingerprint"])["applied"] == 0
        assert item_ref(env, occupant) == "new-1" and item_ref(env, mover) == "old-1"


def test_a_cycle_of_aliases_is_unresolved_never_applied(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    with seeded(settings, tmp_path / "inst") as env:
        series = live(env, "loop-a")
        alias(env, "loop-a", "loop-b")
        alias(env, "loop-b", "loop-a")
        report = plan(make_ops(env))
        assert ids(report, "unresolved") == {iid(env, series)} and not report["migrate"]


def test_migration_is_all_or_nothing_and_resumes_after_an_interruption(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    with seeded(settings, tmp_path / "inst") as env:
        a, b = live(env, "old-a"), live(env, "old-b")
        alias(env, "old-a", "new-a")
        alias(env, "old-b", "new-b")
        ops = make_ops(env)
        fingerprint = plan(ops)["fingerprint"]
        before = everything(env, settings)

        def cut(*_: Any) -> None:  # the process dies after the rows moved, before the commit
            raise RuntimeError("cut")

        monkeypatch.setattr(SourceMigrationOps, "_audit", staticmethod(lambda principal: cut))
        with pytest.raises(RuntimeError):
            plan(ops, fingerprint)
        monkeypatch.undo()
        assert everything(env, settings) == before  # nothing moved, no alias, no record
        assert item_ref(env, a) == "old-a" and item_ref(env, b) == "old-b"
        assert plan(make_ops(env), fingerprint)["applied"] == 2  # a rerun finishes the job
        assert (item_ref(env, a), item_ref(env, b)) == ("new-a", "new-b")


def test_a_stale_or_foreign_fingerprint_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    with seeded(settings, tmp_path / "inst") as env:
        series = live(env, "old-1")
        alias(env, "old-1", "new-1")
        ops = make_ops(env)
        fingerprint = plan(ops)["fingerprint"]
        before = everything(env, settings)
        with pytest.raises(StalePreviewError):
            plan(ops, "0" * 64)
        with pytest.raises(StalePreviewError):  # the plan of another scheme pair is not this one
            ops.migrate_ids(SOURCE, from_scheme=FROM, to_scheme="other", confirm=fingerprint,
                            principal=ADMIN)  # fmt: skip
        with pytest.raises(StalePreviewError):  # nor is the plan of another source
            ops.migrate_ids("source-b", from_scheme=FROM, to_scheme=TO, confirm=fingerprint,
                            principal=ADMIN)  # fmt: skip
        live(env, "old-2")  # a row that appears after the preview changes the plan
        alias(env, "old-2", "new-2")
        before = everything(env, settings)
        with pytest.raises(StalePreviewError):
            plan(ops, fingerprint)
        assert everything(env, settings) == before and item_ref(env, series) == "old-1"


def test_source_migrate_ids_is_reserved_one_door_per_interface(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    from shelfhold.interfaces.local_cli.ops import OPS_METHODS

    action = RESERVED_ACTIONS["source.migrate_ids"]
    assert OPS_METHODS["ops.source_migrate_ids"][1] == "source.migrate_ids"
    key = Principal("u", "admin", "admin", Credential.API_KEY, key_id="k")
    stale = Principal("u", "admin", "admin", Credential.SESSION, session_id="s")
    member = Principal("u", "member", "member", Credential.SESSION, session_id="s",
                       auth_time=datetime(2026, 11, 6, tzinfo=UTC))  # fmt: skip
    child = Principal("u", "child", "child", Credential.API_KEY, key_id="k")
    now = datetime(2026, 11, 6, tzinfo=UTC)
    assert isinstance(authorize_reserved(key, action, now=now), CredentialNotAllowed)
    assert isinstance(authorize_reserved(stale, action, now=now), ReauthRequired)
    with seeded(settings, tmp_path / "inst") as env:
        series = live(env, "old-1")
        alias(env, "old-1", "new-1")
        before = everything(env, settings)
        ops = make_ops(env)
        for who in (key, stale, member, child):
            for confirm in (None, "0" * 64):  # the preview opens the tombstones: reserved too
                with pytest.raises(CodedRefusal):
                    ops.migrate_ids(SOURCE, from_scheme=FROM, to_scheme=TO, confirm=confirm,
                                    principal=who)  # fmt: skip
        assert everything(env, settings) == before and item_ref(env, series) == "old-1"


def test_cli_apply_needs_the_fingerprint_and_a_stale_one_exits_4(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with seeded(settings, tmp_path / "inst") as env:
        series = live(env, "old-1")
        alias(env, "old-1", "new-1")
        before = everything(env, settings)
        base = ("source", "migrate-ids", SOURCE, "--from-scheme", FROM, "--to-scheme", TO)
        assert run_cli(monkeypatch, settings, *base, "--apply") != 0  # no fingerprint
        assert run_cli(monkeypatch, settings, *base, "--apply", "--confirm", "0" * 64) == 4
        with pytest.raises(SystemExit):  # neither --preview nor --apply
            run_cli(monkeypatch, settings, *base)
        assert everything(env, settings) == before
        capsys.readouterr()
        assert run_cli(monkeypatch, settings, *base, "--preview") == 0
        out = capsys.readouterr().out
        found = re.search(r"\b[0-9a-f]{64}\b", out)
        assert found, out
        assert run_cli(monkeypatch, settings, *base, "--apply", "--confirm", found.group(0)) == 0
        assert item_ref(env, series) == "new-1"


class _Record:
    def __init__(self, instance: object) -> None:
        self.instance = instance


class _Registry:
    def __init__(self, records: dict[str, _Record]) -> None:
        self._records = records

    def get(self, key: str) -> _Record | None:
        return self._records.get(key)


def test_loaded_plugin_migrate_ref_is_untrusted_and_screened(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    class Plugin:
        answers: ClassVar[dict[str, object]] = {}

        def migrate_ref(self, key: str, remote_id: str) -> object:
            answer = self.answers[remote_id]
            if isinstance(answer, Exception):
                raise answer
            return answer

    with seeded(settings, tmp_path / "inst") as env:
        erased = live(env, GONE)
        erase(env, monkeypatch, settings, erased)
        fine, boom, junk, empty, back = (live(env, n) for n in ("f-1", "b-1", "j-1", "e-1", "r-1"))
        Plugin.answers = {
            "f-1": (SOURCE, "f-2"), "b-1": RuntimeError("plugin failure"),
            "j-1": ["not", "a", "tuple"], "e-1": (SOURCE, ""), "r-1": (SOURCE, "GONE-1/"),
        }  # fmt: skip
        monkeypatch.setattr(
            env.inst.comp, "plugins", lambda: _Registry({PLUGIN: _Record(Plugin())})
        )
        report = SourceMigrationOps(env.inst.comp).migrate_ids(
            SOURCE, from_scheme=FROM, to_scheme=TO, confirm=None, principal=ADMIN
        )
        assert ids(report, "migrate") == {
            iid(env, fine)
        }  # a failing or malformed plugin says nothing
        assert ids(report, "blocked") == {iid(env, back)}  # an erased series is not brought back
        assert {iid(env, boom), iid(env, junk), iid(env, empty)}.isdisjoint(
            ids(report, "migrate") | ids(report, "blocked")
        )
        monkeypatch.setattr(
            env.inst.comp, "plugins", lambda: _Registry({})
        )  # no plugin: no mapping
        again = SourceMigrationOps(env.inst.comp).migrate_ids(
            SOURCE, from_scheme=FROM, to_scheme=TO, confirm=None, principal=ADMIN
        )
        assert not again["migrate"]
        monkeypatch.setattr(env.inst.comp, "plugins", lambda: None)  # the server is not running
        assert not SourceMigrationOps(env.inst.comp).migrate_ids(
            SOURCE, from_scheme=FROM, to_scheme=TO, confirm=None, principal=ADMIN
        )["migrate"]


def test_migrate_ids_refuses_what_it_cannot_read(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    from shelfhold.ops.errors import InvalidChoiceError

    with seeded(settings, tmp_path / "inst") as env:
        ops = make_ops(env)
        for source, a, b in ((SOURCE, FROM, FROM), (SOURCE, "", TO), ("", FROM, TO),
                             (SOURCE, "x" * 65, TO)):  # fmt: skip
            with pytest.raises(InvalidChoiceError):
                ops.migrate_ids(source, from_scheme=a, to_scheme=b, confirm=None, principal=ADMIN)
        base = ("source", "migrate-ids", SOURCE, "--from-scheme", FROM, "--to-scheme", TO)
        assert run_cli(monkeypatch, settings, *base, "--preview", "--confirm", "f" * 64) == 2
        assert run_cli(monkeypatch, settings, *base, "--preview") == 0
        assert "Aucune série" in capsys.readouterr().out


def _gate(env: Seeded) -> Any:
    return SourceMigrationOps(env.inst.comp)._service()


def test_a_target_that_differs_from_a_live_series_only_by_case_is_not_a_new_series(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    with seeded(settings, tmp_path / "inst") as env:
        occupant = live(env, "Series-One")
        mover, twin_a, twin_b = live(env, "old-1"), live(env, "old-a"), live(env, "old-b")
        alias(env, "old-1", "series-one")  # the same series as the occupant, in lower case
        alias(env, "old-a", "Twin-1")
        alias(env, "old-b", "TWIN-1/")  # two series that fold to one
        renamed = live(env, "Case-Only")
        alias(env, "Case-Only", "case-only")  # only the spelling of its own reference changes
        report = plan(make_ops(env))
        assert ids(report, "unresolved") == {iid(env, mover), iid(env, twin_a), iid(env, twin_b)}
        assert ids(report, "migrate") == {iid(env, renamed)}
        plan(make_ops(env), report["fingerprint"])
        assert item_ref(env, occupant) == "Series-One" and item_ref(env, mover) == "old-1"
        assert item_ref(env, renamed) == "case-only"


def test_a_target_with_nothing_left_after_the_fold_is_refused(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    with seeded(settings, tmp_path / "inst") as env:
        series = live(env, "old-1")
        report = plan(make_ops(env, {"old-1": "/?"}))
        assert ids(report, "unresolved") == {iid(env, series)} and not report["migrate"]
        assert report["unresolved"][0]["reason"] == "empty_ref"


def test_an_erased_unit_stays_erased_after_its_source_key_moves(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    """The `variant` tombstone is asked through the same central check: the Unit reference under
    another source key that an alias ties to the old one, or under another spelling, is erased."""
    with seeded(settings, tmp_path / "inst") as env:
        erased = live(env, GONE, chapters=("1",))
        with env.inst.engine.connect() as connection:
            remote_unit = connection.execute(
                sa.select(catalog.unit_variant.c.remote_unit_id)
            ).scalar_one()
        erase(env, monkeypatch, settings, erased)
        alias(env, GONE, "gone-new", new_key="source-b")  # the source key moves too
        gate = _gate(env)
        for key, unit in ((SOURCE, remote_unit), ("source-b", remote_unit),
                          ("SOURCE-B", remote_unit.upper())):  # fmt: skip
            assert gate.check(VariantRef(PLUGIN, key, unit), root_id=None).blocked, (key, unit)
        assert gate.check(SeriesBindingRef(PLUGIN, "source-b", "gone-new"), root_id=None).blocked
        assert not gate.check(VariantRef(PLUGIN, "source-b", "unit-other"), root_id=None).blocked
        assert not gate.check(SeriesBindingRef(PLUGIN, "source-b", "fresh"), root_id=None).blocked


def test_the_central_check_follows_aliases_of_every_scheme_in_both_directions(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    with seeded(settings, tmp_path / "inst") as env:
        erased = live(env, GONE)
        erase(env, monkeypatch, settings, erased)
        with env.inst.engine.begin() as connection:
            for old, new, scheme in (("mid-1", GONE, "legacy"), ("front-1", "mid-1", None)):
                connection.execute(
                    sa.insert(acquisition.source_alias).values(
                        plugin_id=PLUGIN, old_source_key=SOURCE, old_remote_id=old,
                        new_source_key=SOURCE, new_remote_id=new, id_scheme=scheme,
                    )
                )  # fmt: skip
            connection.execute(  # a cycle somewhere in the graph ends
                sa.insert(acquisition.source_alias).values(
                    plugin_id=PLUGIN, old_source_key=SOURCE, old_remote_id=GONE,
                    new_source_key=SOURCE, new_remote_id="front-1", id_scheme="cycle",
                )
            )  # fmt: skip
        gate = _gate(env)
        for ref in (GONE, "mid-1", "FRONT-1", "front-1/"):
            assert gate.check(SeriesBindingRef(PLUGIN, SOURCE, ref), root_id=None).blocked, ref
        assert not gate.check(SeriesBindingRef(PLUGIN, SOURCE, "other"), root_id=None).blocked
        assert gate.check(SeriesBindingRef(PLUGIN, SOURCE, "/?"), root_id=None).blocked  # refused
        other = SeriesBindingRef("plugin-b", SOURCE, "mid-1")  # aliases are per plugin
        assert not gate.check(other, root_id=None).blocked


def clear_text_hits(env: Seeded, settings: Settings, needles: tuple[str, ...]) -> list[str]:
    """Every table cell (all columns of all tables) and every file of the data directory and the
    roots that holds one of `needles` in clear, whatever its case."""
    found: list[str] = []
    connection = sqlite3.connect(settings.database_path)
    try:
        tables = [
            r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        for table in tables:
            columns = [r[1] for r in connection.execute(f'PRAGMA table_info("{table}")')]
            for row in connection.execute(f'SELECT * FROM "{table}"'):  # noqa: S608 (own tables)
                for column, cell in zip(columns, row, strict=True):
                    text = cell.decode("latin-1") if isinstance(cell, bytes) else str(cell)
                    found += [f"{table}.{column}" for n in needles if n in text.lower()]
    finally:
        connection.close()
    for root in (settings.data_dir, env.inst.library):
        for path in root.rglob("*"):
            if path.is_file():  # the database file and its -wal too: the raw bytes
                data = path.read_bytes().decode("latin-1").lower()
                found += [f"file:{path.name}" for n in needles if n in data]
    return found


def test_the_sweep_sees_a_reference_written_in_clear(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    """Positive witness: the sweep fails when a reference is written in clear in a table."""
    with seeded(settings, tmp_path / "inst") as env:
        live(env, "old-1")
        assert clear_text_hits(env, settings, ("witness-ref",)) == []
        with env.inst.engine.begin() as connection:
            connection.execute(
                sa.insert(acquisition.source_alias).values(
                    plugin_id=PLUGIN, old_source_key=SOURCE, old_remote_id="witness-ref",
                    new_source_key=SOURCE, new_remote_id="elsewhere", id_scheme="legacy",
                )
            )  # fmt: skip
        assert any(
            "source_alias" in hit for hit in clear_text_hits(env, settings, ("witness-ref",))
        )


def test_no_table_holds_an_erased_reference_in_clear_after_the_migration(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    """The plugin says where the erased series went: the new form is sealed as one more tombstone
    (same scope), never written as an alias, a row, an event or a title anywhere."""
    with seeded(settings, tmp_path / "inst") as env:
        erased = live(env, GONE)
        erase(env, monkeypatch, settings, erased)
        clean = live(env, "old-1")
        needles = (GONE, "gone-new", "title gone")
        ops = make_ops(env, {GONE: "gone-new", "old-1": "new-1"})
        report = plan(ops)
        assert plan(ops, report["fingerprint"])["applied"] == 1  # only the clean series moves
        assert item_ref(env, clean) == "new-1"
        assert clear_text_hits(env, settings, needles) == []
        gate = _gate(env)  # yet every new form is held, by the sealed tombstones
        for ref in ("gone-new", "Gone-New/", GONE):
            assert gate.check(SeriesBindingRef(PLUGIN, SOURCE, ref), root_id=None).blocked, ref
        assert not gate.check(SeriesBindingRef(PLUGIN, SOURCE, "gone-other"), root_id=None).blocked
        record = env.rows(acquisition.source_alias_migration)[0].preview
        assert record == {"moved": 1, "held": 0, "new_forms": 1, "sealed": True}  # never values
        # a rerun has nothing left to seal: no second tombstone for a form already held
        again = plan(ops)
        assert not again["migrate"]


def test_a_blocked_row_seals_the_new_form_the_plugin_gave_and_writes_no_alias(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    with seeded(settings, tmp_path / "inst") as env:
        erased = live(env, GONE)
        erase(env, monkeypatch, settings, erased)
        victim = live(env, "old-5")
        with env.inst.engine.begin() as connection:  # another scheme ties it to the erased series
            connection.execute(
                sa.insert(acquisition.source_alias).values(
                    plugin_id=PLUGIN, old_source_key=SOURCE, old_remote_id="old-5",
                    new_source_key=SOURCE, new_remote_id=GONE, id_scheme="legacy",
                )
            )  # fmt: skip
        aliases = env.rows(acquisition.source_alias)
        ops = make_ops(env, {"old-5": "elsewhere-1"})
        report = plan(ops)
        assert ids(report, "blocked") == {iid(env, victim)} and not report["migrate"]
        assert plan(ops, report["fingerprint"])["applied"] == 0
        assert item_ref(env, victim) == "old-5"
        assert env.rows(acquisition.source_alias) == aliases  # no alias written
        assert (
            _gate(env).check(SeriesBindingRef(PLUGIN, SOURCE, "elsewhere-1"), root_id=None).blocked
        )
        assert env.rows(acquisition.source_alias_migration)[0].preview["new_forms"] == 1


def test_a_new_form_stone_is_linked_to_the_tombstone_of_the_erased_series(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    """The levée liée (L26): the new form names the tombstone it comes from, so that lifting the
    origin lifts it; `tombstone list` shows the link, never a value."""
    with seeded(settings, tmp_path / "inst") as env:
        erased = live(env, GONE)
        erase(env, monkeypatch, settings, erased)
        ops = make_ops(env, {GONE: "gone-new"})
        preview = plan(ops)
        assert preview["new_forms"] == 1
        plan(ops, preview["fingerprint"])
        rows = {r.seq: r for r in _gate(env).listing()}
        [new] = [r for r in rows.values() if r.reason == "new_form"]
        [origin] = new.linked_to
        assert origin in rows and rows[origin].reason != "new_form"
        assert rows[origin].linked_to == ()


def _new_form_stones(env: Seeded) -> int:
    """The tombstones laid for a new form: they carry their own closed reason, never a value."""
    return sum(1 for r in env.rows(coherence.content_tombstone) if r.reason == "new_form")


def test_a_cut_before_the_commit_changes_nothing_and_the_rerun_is_the_same_plan(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    with seeded(settings, tmp_path / "inst") as env:
        erased = live(env, GONE)
        erase(env, monkeypatch, settings, erased)
        clean = live(env, "old-1")
        ops = make_ops(env, {GONE: "gone-new", "old-1": "new-1"})
        first = plan(ops)

        def cut(*_args: object, **_kwargs: object) -> int:
            raise RuntimeError("cut")  # the process dies before the rows are written

        with monkeypatch.context() as patched:
            patched.setattr(source_migration, "apply_alias_migration", cut)
            with pytest.raises(RuntimeError):
                plan(ops, first["fingerprint"])
        assert item_ref(env, clean) == "old-1" and _new_form_stones(env) == 0
        assert not env.rows(acquisition.source_alias_migration)  # no intent without the rows
        again = plan(ops)
        assert again["fingerprint"] == first["fingerprint"]
        assert plan(ops, again["fingerprint"])["applied"] == 1
        assert _new_form_stones(env) == 1


def test_a_cut_between_the_commit_and_the_seal_is_seen_and_finished_by_a_rerun(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    with seeded(settings, tmp_path / "inst") as env:
        erased = live(env, GONE)
        erase(env, monkeypatch, settings, erased)
        live(env, "old-1")
        ops = make_ops(env, {GONE: "gone-new", "old-1": "new-1"})
        fingerprint = plan(ops)["fingerprint"]

        def cut(self: SourceMigrationOps, _plan: object) -> None:
            raise RuntimeError("cut")

        with monkeypatch.context() as patched:
            patched.setattr(SourceMigrationOps, "_seal", cut)
            with pytest.raises(RuntimeError):
                plan(ops, fingerprint)
        intent = env.rows(acquisition.source_alias_migration)[0].preview
        assert intent["sealed"] is False and intent["new_forms"] == 1  # the durable intent
        assert _new_form_stones(env) == 0
        assert (
            not _gate(env).check(SeriesBindingRef(PLUGIN, SOURCE, "gone-new"), root_id=None).blocked
        )
        again = plan(ops)  # the preview says what is still to seal
        assert again["new_forms"] == 1 and not again["migrate"]
        assert plan(ops, again["fingerprint"])["new_forms"] == 1  # the rerun seals it
        assert _new_form_stones(env) == 1
        assert _gate(env).check(SeriesBindingRef(PLUGIN, SOURCE, "gone-new"), root_id=None).blocked
        assert all(r.preview["sealed"] for r in env.rows(acquisition.source_alias_migration)[:1])
        assert plan(ops)["new_forms"] == 0  # nothing is left to seal


def test_a_cut_in_the_middle_of_the_seal_finishes_without_a_second_stone(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    with seeded(settings, tmp_path / "inst") as env:
        first, second = live(env, GONE), live(env, "gone-2")
        erase(env, monkeypatch, settings, first)
        erase(env, monkeypatch, settings, second)
        ops = make_ops(env, {GONE: "gone-new", "gone-2": "gone-new-2"})
        fingerprint = plan(ops)["fingerprint"]
        real = TombstoneService.record
        calls: list[int] = []

        def flaky(self: TombstoneService, *args: Any, **kwargs: Any) -> Any:
            if kwargs.get("reason") == "new_form":
                calls.append(1)
                if len(calls) == 2:
                    raise RuntimeError("cut")  # dies after the first stone, before the second
            return real(self, *args, **kwargs)

        with monkeypatch.context() as patched:
            patched.setattr(TombstoneService, "record", flaky)
            with pytest.raises(RuntimeError):
                plan(ops, fingerprint)
        assert _new_form_stones(env) == 1
        again = plan(ops)
        assert again["new_forms"] == 1  # only the one left
        plan(ops, again["fingerprint"])
        assert _new_form_stones(env) == 2  # no stone twice
        for ref in ("gone-new", "gone-new-2"):
            assert _gate(env).check(SeriesBindingRef(PLUGIN, SOURCE, ref), root_id=None).blocked
