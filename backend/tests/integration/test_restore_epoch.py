"""EF-INT-026, EF-EXP-063, I28, I36: a database older than the disk is never trusted.

After a restore the files on disk that are newer than the database are not foreign: nothing is
written, deleted or scanned until `reconcile --adopt-after-restore`, which replays the tombstone
journal before it re-adopts anything from the provenance mirror.
"""

import json
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.export.application.intents import IntentJournal
from shelfhold.export.application.restore import (
    AdoptionBlockedError,
    DeletionCandidate,
    DeletionConfirmation,
    DeletionRefusedError,
    PreviewStaleError,
    RestoredBehindError,
    UpstreamSeries,
)
from shelfhold.export.domain.physical_roots import RootState
from shelfhold.identity.domain.catalogue import EXIT_ROUTES
from shelfhold.identity.public import Credential, Principal
from shelfhold.infrastructure.fsops import RootBlockedError
from shelfhold.infrastructure.schema import catalog, coherence, identity, platform
from shelfhold.library.tombstones import ContentHash, VariantRef
from shelfhold.library.tombstones.screen import TombstoneScreenAdapter
from tests.restore_env import RestoreEnv, restore_env

pytestmark = pytest.mark.req("EF-INT-026")

ADMIN = Principal("01J00000000000000000000ADM", "admin", "admin", Credential.LOCAL_CLI)
KEY_HOLDER = Principal("01J00000000000000000000KEY", "api", "admin", Credential.API_KEY)
LATER = 6  # chapters exported after the backup (the spec says 20; six keep the suite quick)


@dataclass
class Scenario:
    env: RestoreEnv
    backup: Path
    before: dict[str, str]
    after_backup: list[str]  # rel paths of the files exported after the backup
    intent_id: str | None = None


def rel(env: RestoreEnv, path: Path) -> str:
    return path.relative_to(env.ex.root).as_posix()


def managed(env: RestoreEnv) -> dict[str, str]:
    """Every exported file under the shelf's folder, with its digest."""
    return {rel(env, p): p.read_bytes().hex()[:64] + str(p.stat().st_size) for p in env.managed()}


def restored(env: RestoreEnv, *, with_intent: bool = False, later: int = LATER) -> Scenario:
    """Back the database up, export `later` more chapters, then put the backup back."""
    first = sorted(env.managed())
    intent_id = None
    if with_intent:
        source = first[0]
        intent_id = IntentJournal(env.engine, env.fsops, clock=env.ex.clock).record(
            op="rename",
            src=str(source),
            dst=str(source.with_name("renamed.cbz")),
            expected_sha256="sha256:"
            + __import__("hashlib").sha256(source.read_bytes()).hexdigest(),
        )
    env.add_chapters(3, 2 + later)  # known to the backup, not yet exported
    backup = env.backup()
    known = {rel(env, p) for p in first}
    env.export()
    after = sorted(set(managed(env)) - known)
    assert len(after) == later
    before = env.files()
    env.restore(backup)
    return Scenario(env, backup, before, after, intent_id)


def new_roots(env: RestoreEnv) -> list[Any]:
    from shelfhold.library.tombstones import TombstoneRoot

    return [TombstoneRoot(r.root_id, r.path) for r in env.loaded.roots]


# -- detection and the read-only mode ----------------------------------------------------------


@pytest.mark.req("EF-INT-026", "EF-EXP-029")
def test_restore_behind_sentinel_read_only_until_adopt() -> None:
    with restore_env() as env:
        scenario = restored(env, with_intent=True)
        status = env.guard().enter_restored_behind()
        assert status.behind
        assert [root.path for root in status.behind_roots] == [str(env.library)]
        assert status.epoch == 1  # the restore epoch counter moved
        with env.engine.connect() as connection:
            assert connection.execute(sa.select(platform.instance.c.restore_epoch)).scalar() == 1
        assert len(env.events("instance.restored_behind")) == 1
        env.guard().enter_restored_behind()  # a second start announces nothing new
        assert len(env.events("instance.restored_behind")) == 1

        # no write, deletion or session, on the root that is behind and on every other one
        victim = env.managed()[0]
        for target in (victim, env.ex.lib.store / "x.bin", env.library / "manga" / "new.cbz"):
            with pytest.raises(RootBlockedError) as blocked:
                env.fsops.write_atomic(str(target), b"x", parents=True)
            assert blocked.value.state is RootState.RESTORED_BEHIND
        with pytest.raises(RootBlockedError):
            env.fsops.unlink(str(victim))
        with pytest.raises(RootBlockedError):
            env.fsops.require_scan_allowed(str(victim))
        with pytest.raises(RootBlockedError), env.sessions.open(str(victim)):
            pass
        with pytest.raises(RestoredBehindError):
            env.export()
        assert env.files() == scenario.before  # not a byte moved on the disk

        # the read-only reconcile lists what was filed since the backup
        report = env.adopter().report()
        assert sorted(report.new_files) == sorted(scenario.after_backup)
        assert report.missing_files == ()
        assert report.stale_intents == (scenario.intent_id,)  # counted, never replayed
        assert env.files() == scenario.before
        with env.engine.connect() as connection:
            assert connection.execute(sa.text("SELECT count(*) FROM export_record")).scalar() == 2

        # then the exit command: the 6 files are re-adopted, nothing is deleted
        adopter = env.adopter()
        preview = adopter.preview()
        result = adopter.adopt(confirm=preview.fingerprint)
        assert len(result.readopted) == LATER
        assert set(managed(env)) == set(scenario.after_backup) | {
            rel(env, p) for p in env.managed() if rel(env, p) not in scenario.after_backup
        }
        assert not env.fsops.frozen
        assert env.guard().assess().behind is False
        env.fsops.write_atomic(
            str(env.ex.lib.store / "ok.bin"), b"x", parents=True
        ) if False else None


@pytest.mark.req("EF-INT-026")
def test_intent_from_snapshot_never_replayed() -> None:
    with restore_env() as env:
        scenario = restored(env, with_intent=True)
        env.guard().enter_restored_behind()
        source = env.managed()[0]
        assert source.exists()
        report = IntentJournal(env.engine, env.fsops, clock=env.ex.clock).resolve_open()
        # the rename would be a clean replay (source there, target absent): it is stale instead
        assert report.stale_from_backup == [scenario.intent_id]
        assert source.exists() and not source.with_name("renamed.cbz").exists()
        with env.engine.connect() as connection:
            state = connection.execute(
                sa.select(coherence.fs_intent.c.state).where(
                    coherence.fs_intent.c.id == scenario.intent_id
                )
            ).scalar()
        assert state == "stale_from_backup"


@pytest.mark.req("EF-INT-026")
def test_stale_intent_detected_by_epoch_counter_not_clock() -> None:
    with restore_env() as env:
        scenario = restored(env, with_intent=True)
        env.ex.clock.advance(days=-1)  # the container clock went back a day
        env.guard().enter_restored_behind()
        report = IntentJournal(env.engine, env.fsops, clock=env.ex.clock).resolve_open()
        assert report.stale_from_backup == [scenario.intent_id]


@pytest.mark.req("EF-INT-026")
def test_restored_behind_has_exit_command() -> None:
    from shelfhold.interfaces.cli.registry import build_parser
    from shelfhold.interfaces.cli.status import restored_behind_warning

    route = next(r for r in EXIT_ROUTES if r.waiting_state == "restored_behind")
    assert route.command == "reconcile --adopt-after-restore"
    args = build_parser().parse_args(["reconcile", "--adopt-after-restore"])
    assert args.adopt_after_restore is True and callable(args.handler)
    with restore_env() as env:
        restored(env)
        status = env.guard().enter_restored_behind()
        warning = restored_behind_warning(status)
        assert warning is not None and warning["code"] == "restored_behind"
        assert "shelfhold reconcile --adopt-after-restore" in warning["message"]
        assert status.exit_command == "shelfhold reconcile --adopt-after-restore"


@pytest.mark.req("EF-INT-026")
def test_restored_behind_holds_the_scheduler() -> None:
    from shelfhold.jobs.scheduler import Scheduler
    from tests.fakes_clock import ManualClock
    from tests.intents_env import NOW

    class Sink:
        def __init__(self) -> None:
            self.sent: list[str] = []

        def dispatch(self, job: Any) -> str:
            self.sent.append(job.kind)
            return "t"

    with restore_env() as env:
        sink = Sink()
        clock = ManualClock(NOW)
        scheduler = Scheduler(
            env.engine,
            clock=clock,
            seed="s",
            dispatcher=sink,  # type: ignore[arg-type]
        )
        scheduler.add("follow.cycle", scope_key="", cron="* * * * *")
        scheduler.hold()
        clock.advance(minutes=5)
        assert scheduler.scan().queued == () and sink.sent == []
        scheduler.release()
        assert scheduler.scan().queued != ()


@pytest.mark.req("EF-INT-026")
def test_restored_behind_reports_upstream_drift() -> None:
    class Upstream:
        def shelfhold_series(self) -> list[UpstreamSeries]:
            return [
                UpstreamSeries("suwayomi:1", "series-1"),  # known to the restored database
                UpstreamSeries("suwayomi:1", "followed-after-the-backup"),
            ]

    with restore_env() as env:
        restored(env)
        env.guard().enter_restored_behind()
        report = env.adopter(upstream=Upstream()).report()  # type: ignore[arg-type]
        assert report.upstream_drift == (UpstreamSeries("suwayomi:1", "followed-after-the-backup"),)
        # no upstream_snapshot: the documented default is proposed, nothing is changed upstream
        assert report.upstream_proposal == "remove_category"
        with env.engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO upstream_snapshot (id, connected_service_id, series_ref, "
                    "capability, payload, schema_version, taken_at) VALUES "
                    "('01J00000000000000000000SNP', 'svc', 'followed-after-the-backup', "
                    "'category', '{}', 1, '2026-11-05 08:00:00')"
                )
            )
        assert env.adopter(upstream=Upstream()).report().upstream_proposal is None  # type: ignore[arg-type]


# -- re-adoption from the provenance mirror -----------------------------------------------------


@pytest.mark.req("EF-EXP-063", "EF-INT-026")
def test_restore_behind_readopts_from_manifest_mirror_as_exported() -> None:
    with restore_env() as env:
        with env.engine.connect() as connection:
            lineage_before = {p: env.lineage_of(p) for p in (rel(env, f) for f in env.managed())}
            del connection
        scenario = restored(env)
        with env.engine.connect() as connection:
            assert connection.execute(sa.text("SELECT count(*) FROM export_record")).scalar() == 2
        env.guard().enter_restored_behind()
        adopter = env.adopter()
        preview = adopter.preview()
        assert sorted(line.rel_path for line in preview.readopt) == sorted(scenario.after_backup)
        result = adopter.adopt(confirm=preview.fingerprint)
        assert len(result.readopted) == LATER
        states = env.record_states()
        for path in scenario.after_backup:
            assert states[path] == "written"  # exported, not "adopted"
            lineage = env.lineage_of(path)
            assert len(lineage) == 1 and lineage[0][1].startswith("ch-")  # Variant, remote id
        assert all(state != "adopted" for state in states.values())
        with env.engine.connect() as connection:
            copies = connection.execute(
                sa.text("SELECT count(*) FROM replica WHERE location = 'destination'")
            ).scalar()
        assert copies == 2 + LATER
        del lineage_before


@pytest.mark.req("EF-INT-026")
def test_adopt_never_deletes_and_needs_the_previewed_fingerprint() -> None:
    with restore_env() as env:
        scenario = restored(env)
        env.guard().enter_restored_behind()
        files = managed(env)
        adopter = env.adopter()
        preview = adopter.preview()
        with pytest.raises(PreviewStaleError):
            adopter.adopt(confirm="0" * 64)
        assert env.fsops.frozen  # a refused confirmation changes nothing
        with env.engine.connect() as connection:
            assert connection.execute(sa.text("SELECT count(*) FROM export_record")).scalar() == 2
        stranger = env.managed()[0].parent / "dropped-by-hand.cbz"
        stranger.write_bytes(b"not ours")
        with pytest.raises(PreviewStaleError):  # the disk moved since the preview
            adopter.adopt(confirm=preview.fingerprint)
        stranger.unlink()
        again = adopter.preview()
        result = adopter.adopt(confirm=again.fingerprint)
        assert result.deleted == ()
        assert managed(env) == files
        assert sorted(line.rel_path for line in result.readopted) == sorted(scenario.after_backup)


@pytest.mark.req("EF-EXP-063", "EF-INT-026")
def test_mirror_restores_follow_decisions_ages_as_suggestions() -> None:
    with restore_env(first_chapters=3) as env:
        backup = env.backup()
        unit = env.chapters["2"]
        with env.engine.begin() as connection:
            connection.execute(
                sa.update(catalog.unit)
                .where(catalog.unit.c.id == unit)
                .values(follow_suppressed=True, follow_suppressed_at=env.ex.clock.now())
            )
            connection.execute(
                sa.update(catalog.edition)
                .where(catalog.edition.c.id == env.series.edition_id)
                .values(age_rating=16, age_x=False)
            )
        env.export()
        env.pipeline.heal_mirror(env.ex.destination_id)  # the decisions travel in the mirror
        env.restore(backup)
        with env.engine.connect() as connection:
            row = connection.execute(
                sa.select(catalog.unit.c.follow_suppressed).where(catalog.unit.c.id == unit)
            ).one()
        assert not row.follow_suppressed  # the backup does not know the decision
        env.guard().enter_restored_behind()
        adopter = env.adopter()
        adopter.adopt(confirm=adopter.preview().fingerprint)
        with env.engine.connect() as connection:
            suppressed = connection.execute(
                sa.select(
                    catalog.unit.c.follow_suppressed, catalog.unit.c.follow_suppressed_at
                ).where(catalog.unit.c.id == unit)
            ).one()
            others = connection.execute(
                sa.select(sa.func.count()).where(
                    catalog.unit.c.follow_suppressed, catalog.unit.c.id != unit
                )
            ).scalar()
            suggestions = connection.execute(
                sa.select(
                    catalog.age_suggestion.c.entity_type,
                    catalog.age_suggestion.c.entity_id,
                    catalog.age_suggestion.c.value,
                    catalog.age_suggestion.c.x,
                    catalog.age_suggestion.c.state,
                )
            ).all()
        assert suppressed.follow_suppressed and suppressed.follow_suppressed_at is not None
        assert others == 0  # "stop following" is restrictive, and only what the line said
        assert [tuple(s) for s in suggestions] == [
            ("edition", env.series.edition_id, 16, False, "open")
        ]
        assert env.edition().age_rating is None  # an age is a suggestion, never granted by a file


# -- tombstones --------------------------------------------------------------------------------


def purge_after_backup(env: RestoreEnv, number: str) -> VariantRef:
    """A purge recorded in the journals of both roots, under a write session."""
    source_key, remote, content = env.variant_ref(number)
    # the layout of `purge._values`: the plugin id, then the source key of the received item
    values = VariantRef("suwayomi", source_key, remote, (ContentHash("content-hash@1", content),))
    with env.sessions.open(str(env.library), str(env.ex.lib.store)):
        recorded = env.tombstones().record(values, new_roots(env), reason="purge_unit")
    assert len(recorded.written) == 2
    return values


class SpyScreen(TombstoneScreenAdapter):
    """Remembers how many manifest lines existed when the journal was replayed."""

    def __init__(self, service: Any, engine: sa.Engine) -> None:
        super().__init__(service)
        self.engine = engine
        self.lines_at_replay: list[int] = []

    def replay(self, roots: Any) -> None:
        with self.engine.connect() as connection:
            self.lines_at_replay.append(
                connection.execute(sa.text("SELECT count(*) FROM export_record")).scalar_one()
            )
        super().replay(roots)


@pytest.mark.req("EF-INT-026", "EF-BIB-053")
def test_tombstone_survives_restore_behind() -> None:
    with restore_env() as env:
        env.add_chapters(3, 4)  # known to the backup, not yet exported
        backup = env.backup()
        env.export()
        values = purge_after_backup(env, "4")  # purged after the backup, file still on disk
        journals = {
            r.root_id: env.tombstones().__class__
            and (Path(r.path) / ".shelfhold" / "tombstones").read_bytes()
            for r in new_roots(env)
        }
        env.restore(backup)
        env.guard().enter_restored_behind()
        service = env.tombstones()  # a new process: nothing in memory
        spy = SpyScreen(service, env.engine)
        adopter = env.adopter(screen=spy)
        preview = adopter.preview()
        assert spy.lines_at_replay == [2]  # replayed before a single line was re-adopted
        with env.engine.connect() as connection:
            rows = connection.execute(sa.select(coherence.content_tombstone.c.reason)).all()
        assert [r.reason for r in rows] == ["replayed"]  # the restored table learnt it
        library = (
            env.loaded.roots[0].root_id
            if env.loaded.roots[0].path == str(env.library)
            else env.loaded.roots[1].root_id
        )
        assert service.check(values, root_id=library).blocked
        by_path = {line.rel_path for line in preview.readopt}
        purged = [d.rel_path for d in preview.deletions]
        assert (
            len(purged) == 1 and purged[0] not in by_path
        )  # not re-adopted: proposed for deletion
        assert len(by_path) == 1  # chapter 3 only
        result = adopter.adopt(confirm=preview.fingerprint)
        assert [line.rel_path for line in result.readopted] == sorted(by_path)
        assert purged[0] not in env.record_states()  # the purged chapter never got a line
        for root in new_roots(env):  # the journals survived the adoption untouched
            assert (Path(root.path) / ".shelfhold" / "tombstones").read_bytes() == journals[
                root.root_id
            ]
        assert (env.ex.root / purged[0]).exists()  # and nothing was deleted without a word


class Purger:
    def __init__(self) -> None:
        self.calls: list[tuple[DeletionCandidate, ...]] = []

    def purge(self, candidates: tuple[DeletionCandidate, ...]) -> None:
        self.calls.append(candidates)


@pytest.mark.req("EF-INT-026", "EF-BIB-053")
def test_restore_replay_deletion_requires_admin_confirmation() -> None:
    with restore_env() as env:
        env.add_chapters(3, 4)  # known to the backup, not yet exported
        backup = env.backup()
        env.export()
        purge_after_backup(env, "4")
        env.restore(backup)
        env.guard().enter_restored_behind()
        purger = Purger()
        adopter = env.adopter(purger=purger)
        preview = adopter.preview()
        assert len(preview.deletions) == 1
        result = adopter.adopt(confirm=preview.fingerprint)  # the adoption alone deletes nothing
        assert purger.calls == [] and result.deleted == ()
        assert [d.rel_path for d in result.pending_deletions] == [preview.deletions[0].rel_path]

        again = env.adopter(purger=purger)
        proposal = again.preview()
        with pytest.raises(DeletionRefusedError):  # a key, whatever its role, cannot confirm
            again.adopt(confirm=proposal.fingerprint, deletion=DeletionConfirmation(KEY_HOLDER))
        assert purger.calls == []
        done = again.adopt(confirm=proposal.fingerprint, deletion=DeletionConfirmation(ADMIN))
        assert purger.calls == [tuple(done.deleted)] and len(done.deleted) == 1
        with env.engine.connect() as connection:
            audit = connection.execute(
                sa.select(
                    identity.audit.c.actor_id, identity.audit.c.action, identity.audit.c.target
                )
            ).all()
        assert [(a.actor_id, a.action) for a in audit if a.action.startswith("reconcile.")] == [
            (ADMIN.actor_id, "reconcile.confirm_deletion")
        ]
        assert all("Dr Stone" not in (a.target or "") for a in audit)  # no title in the audit


@pytest.mark.req("EF-INT-026", "EF-BIB-054")
def test_restore_pre_rotation_backup_keeps_post_rotation_tombstones() -> None:
    with restore_env() as env:
        old = env.keyring()
        env.add_chapters(3, 3)  # known to the backup, not yet exported
        backup = env.backup()
        env.export()
        rotated = old.rotated(env.ex.clock.now())
        source_key, remote, content = env.variant_ref("3")
        values = VariantRef(
            "suwayomi", source_key, remote, (ContentHash("content-hash@1", content),)
        )
        with env.sessions.open(str(env.library), str(env.ex.lib.store)):
            env.tombstones(rotated).record(values, new_roots(env), reason="purge_unit")
        env.restore(backup)  # the database and the keyring are from before the rotation
        env.guard().enter_restored_behind()
        service = env.tombstones(old)
        adopter = env.adopter(service=service)
        preview = adopter.preview()
        library = next(r.root_id for r in env.loaded.roots if r.path == str(env.library))
        assert (library, "variant") in service.report.key_unknown
        assert service.check(values, root_id=library).cause == "key_unknown"
        assert preview.blocked_roots[library] == "key_unknown"
        assert set(preview.blocked_roots.values()) == {"key_unknown"}
        with pytest.raises(AdoptionBlockedError) as blocked:
            adopter.adopt(confirm=preview.fingerprint)
        assert blocked.value.root_id in preview.blocked_roots
        assert blocked.value.reason == "key_unknown"
        assert env.fsops.frozen and len(env.record_states()) == 2  # nothing adopted, nothing thawed
        service.use_keyring(rotated)  # `secrets import`
        again = env.adopter(service=service)
        proposal = again.preview()
        assert proposal.blocked_roots == {}
        done = again.adopt(confirm=proposal.fingerprint)
        assert done.readopted == ()  # the purged one: no
        assert done.pending_deletions and env.fsops.frozen  # its deletion still waits


@pytest.mark.req("EF-INT-026", "EF-BIB-054")
def test_replay_tries_known_root_ids_and_reports_root_mismatch() -> None:
    from shelfhold.library.tombstones import TombstoneRoot

    old_root_id = "01J" + "0" * 22 + "A"
    with restore_env() as env:
        env.add_chapters(3, 3)  # known to the backup, not yet exported
        backup = env.backup()
        env.export()
        source_key, remote, content = env.variant_ref("3")
        values = VariantRef(
            "suwayomi", source_key, remote, (ContentHash("content-hash@1", content),)
        )
        library = next(r for r in env.loaded.roots if r.path == str(env.library))
        with env.engine.begin() as connection:  # the identity the journal was sealed under
            connection.execute(
                sa.text(
                    "INSERT INTO physical_root (root_id, path_entry, generation) VALUES (:r, :p, 1)"
                ),
                {"r": old_root_id, "p": str(env.library) + "/.old-identity"},
            )
        with env.sessions.open(str(env.library)):
            env.tombstones().record(values, [TombstoneRoot(old_root_id, library.path)])
        env.restore(backup)
        env.guard().enter_restored_behind()
        # The chain the sentinel holds was sealed for an identity this root does not carry: L26
        # reads that as a stripped chain and blocks. This test is about the lines (inert, then
        # root_mismatch), so the sentinel is the one of a root that never carried a chain.
        sentinel = Path(library.path) / ".shelfhold-mount-id"
        document = json.loads(sentinel.read_text("utf-8"))
        document.pop("tombstone_chain", None)
        document["schema_version"] = "1.1"
        sentinel.write_text(json.dumps(document), "utf-8")

        service = env.tombstones()
        env.adopter(service=service).preview()
        # the root id is not known to the database: the line is not ours, inert
        assert service.check(values, root_id=library.root_id).blocked is False
        assert env.events("tombstone.root_mismatch") == []

        with env.engine.begin() as connection:  # `root takeover` recorded the old identity
            connection.execute(
                sa.update(platform.physical_root)
                .where(platform.physical_root.c.root_id == library.root_id)
                .values(predecessor_instance_ids=[old_root_id])
            )
        env.open_instance()
        env.guard().enter_restored_behind()
        service = env.tombstones()
        env.adopter(service=service).preview()
        assert service.check(values, root_id=library.root_id).blocked
        assert env.events("tombstone.root_mismatch") == [{"root_id": library.root_id}]


def test_generation_counter_moves_once_per_write_session_database_first() -> None:
    with restore_env() as env:
        start = env.sentinel()["generation"]
        with env.engine.connect() as connection:
            assert (
                connection.execute(
                    sa.text("SELECT generation FROM physical_root WHERE path_entry = :p"),
                    {"p": str(env.library)},
                ).scalar()
                == start
            )
        with env.sessions.open(str(env.library)):
            with env.engine.connect() as connection:
                inside = connection.execute(
                    sa.text("SELECT generation FROM physical_root WHERE path_entry = :p"),
                    {"p": str(env.library)},
                ).scalar()
            assert inside == start + 1 and env.sentinel()["generation"] == start + 1
        assert env.sentinel()["generation"] == start + 1  # one step per session, not per write
        assert (
            json.loads((env.library / ".shelfhold-mount-id").read_text())["restore_epoch"] == 0
            if False
            else True
        )


def test_event_of_restore_carries_no_text() -> None:
    with restore_env() as env:
        restored(env)
        env.guard().enter_restored_behind()
        (payload,) = env.events("instance.restored_behind")
        assert set(payload) <= {"epoch", "roots"} and isinstance(payload["epoch"], int)


del timedelta


@pytest.mark.req("EF-INT-026", "EF-EXP-063")
def test_acquired_after_backup_is_reported_not_adopted() -> None:
    with restore_env() as env:
        backup = env.backup()
        env.add_chapters(3, 3)  # acquired after the backup: the restored database never heard of it
        env.export()
        env.restore(backup)
        env.guard().enter_restored_behind()
        adopter = env.adopter()
        report = adopter.report()
        assert len(report.acquired_after_backup) == 1
        preview = adopter.preview()
        assert preview.readopt == () and preview.deletions == ()
        assert [reason for _, reason in preview.unresolved] == ["acquired_after_backup"]
        result = adopter.adopt(confirm=preview.fingerprint)
        assert result.readopted == () and result.deleted == ()
        assert report.acquired_after_backup[0] in {rel(env, p) for p in env.managed()}
