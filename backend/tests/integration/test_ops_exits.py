"""EF-INT-023 (L36): every waiting state has an exit command, played end to end.

Each M1a state is put in its state the way the product does (a pause in the database, a health
document, a job gone dead), seen by `status` with its reason and its command, then left by the
command the specification names, through the services of the lot that owns the state. The
enumeration is partitioned (M1a / later): a new `WaitingState` fails here until it is classed.
"""
# ruff: noqa: S105, S106, E501 (synthetic secrets; scenarios read better long)

import argparse
import inspect
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.events.bus import EventBus
from shelfhold.events.waiting_state import WaitingState
from shelfhold.export.application.preflight import PermissionPreflight
from shelfhold.export.application.target_state import load_health
from shelfhold.export.domain.kavita_key_expiry import KeyNoticeState
from shelfhold.identity.public import EXIT_ROUTES
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.restore_notice import RestoreNoticeData, write_notice
from shelfhold.infrastructure.schema import catalog, export
from shelfhold.infrastructure.secrets.diagnostics import store_for
from shelfhold.infrastructure.secrets.keyring import Keyring
from shelfhold.interfaces.cli.registry import build_parser
from shelfhold.library.public import StoreBudget
from shelfhold.library.tombstones import LOST_KEYRING_PHRASE
from shelfhold.ops import (
    CLI_PENDING,
    ENDPOINTS,
    M1A_STATES,
    M1B_SERVED_STATES,
    UNSERVED,
    OpsServices,
    bare,
    endpoint_of,
)
from tests.auth_env import run_cli
from tests.export_env import ExportEnv, export_env
from tests.fakes.kavita import member
from tests.headless_env import KAVITA_KEY, KAVITA_URL, SUWAYOMI_URL, Headless, headless
from tests.integration.test_cli_rate import held_work, with_family_shelf
from tests.integration.test_restore_epoch import restored
from tests.integration.test_restore_review import edit_sentinel
from tests.integration.test_restore_review2 import production
from tests.kavita_export_env import wire
from tests.l50_env import seeded
from tests.l53_env import status_json, waiting_states
from tests.l54_env import SERIES, bench54, comic
from tests.library_env import lib_env
from tests.ops_env import (
    NOW,
    FakeKeyring,
    add_job,
    edition_lang,
    job_status,
    make_ops,
    pause_follow,
    put_health,
)
from tests.restore_env import RestoreEnv, restore_env
from tests.safeguard_env import World

pytestmark = pytest.mark.req("EF-INT-023")

# The M1b states, written out: their exit arrives with the milestone where the state appears. A
# state that is in neither M1A_STATES nor this set (nor the M3 one) fails the partition test, so a
# new state cannot slip into "later" without a decision.
M1B_STATES = frozenset(
    {
        WaitingState.RATING_PENDING,
        WaitingState.FOLLOW_SUSPENDED,
        WaitingState.SAFEGUARD_BACKLOG,
        WaitingState.FOREIGN_ROOT,
        WaitingState.ROOT_LOST,
        WaitingState.ROOT_BEHIND,
        WaitingState.STORE_LOST,
        WaitingState.TOMBSTONE_JOURNAL_TRUNCATED,
        WaitingState.SOURCE_INSTANCE_CHANGED,
        WaitingState.WITHDRAWAL_PENDING,
        WaitingState.RESTORED_CREDENTIALS,
    }
)
# The M3 states whose exit is served: `maintenance off` (L13, EF-INT-018).
M3_SERVED_STATES = frozenset({WaitingState.MAINTENANCE})
M3_STATES = frozenset({WaitingState.LIBRARY_TYPE_CHANGED})  # EF-EXP-049, `target reconnect`
PLAYED_STATES = (
    M1A_STATES | M1B_SERVED_STATES | M3_STATES | M3_SERVED_STATES
)  # the states with an end-to-end scenario below
LATER_STATES = M1B_STATES - M1B_SERVED_STATES


def _waiting(ops: OpsServices, state: str) -> list[object]:
    return [item for item in ops.status().waiting if item.state == state]


def _listed_then_gone(ops: OpsServices, state: str, leave: Callable[[], object]) -> None:
    """The state is listed with a reason and a command, `leave` is run, the state is gone."""
    before = _waiting(ops, state)
    assert before, f"{state} is not listed by status"
    assert all(item.reason and item.command.startswith("shelfhold ") for item in before)  # type: ignore[attr-defined]
    leave()
    assert _waiting(ops, state) == [], f"{state} is still listed after its exit command"


def _exported(env: ExportEnv, count: int = 1) -> tuple[str, list[str]]:
    series = env.new_series()
    for number in range(2, 2 + count):
        env.add_chapter(series, str(number))
    env.pipeline.run(env.destination_id, job_id=new_ulid())
    return series.edition_id, [r.export_item_id for r in env.rows(export.export_record)]


def _cli_has(command: str) -> bool:
    """Does `shelfhold <command>` exist in the parser? A `--flag` is an option of the group."""
    words = bare(command).split()
    parser = build_parser()
    for word in words:
        if word.startswith("--"):
            return any(word in action.option_strings for action in parser._actions)
        sub = next((a for a in parser._actions if isinstance(a, argparse._SubParsersAction)), None)
        if sub is None or word not in sub.choices:
            return False
        parser = sub.choices[word]
    return True


def _route_documented(command: str) -> bool:
    from fastapi.testclient import TestClient  # noqa: F401  (the app is built in `_openapi`)

    method, path = endpoint_of(command)
    return (method.lower(), path) in _openapi_paths()


_PATHS: set[tuple[str, str]] = set()


def _openapi_paths() -> set[tuple[str, str]]:
    if not _PATHS:
        from shelfhold.main import create_app

        spec = create_app().openapi()
        _PATHS.update((m, p) for p, ops in spec["paths"].items() for m in ops)
    return _PATHS


# -- the scenarios: one per M1a state ---------------------------------------------------------------


def missing() -> None:
    with export_env() as env:
        _edition, items = _exported(env)
        with env.engine.begin() as connection:
            connection.execute(sa.update(export.export_record).values(state="missing"))
        ops = make_ops(env)
        _listed_then_gone(
            ops, "missing", lambda: ops.missing_resolve(
                env.destination_id, items[0], "reexport", actor_id=None
            ),
        )  # fmt: skip
        assert env.rows(export.export_record)[0].state == "planned"  # the next export puts it back


def paused_foreign_writer() -> None:
    with export_env() as env:
        _exported(env)
        with env.engine.begin() as connection:
            connection.execute(
                sa.update(export.export_series).values(paused_reason="foreign_writer")
            )
        ops = make_ops(env)
        series_id = env.rows(export.export_series)[0].id
        _listed_then_gone(ops, "paused_foreign_writer", lambda: ops.series_resume(series_id))


def dead_letter() -> None:
    with export_env() as env:
        job = add_job(env.engine, kind="library.safeguard", status="dead_letter", code="job.failed")
        ops = make_ops(env)
        _listed_then_gone(ops, "dead_letter", lambda: ops.jobs_retry(job))
        assert job_status(env.engine, job) == "queued"


def instance_changed() -> None:
    with export_env() as env:
        w = wire(env)
        w.fake.reinstall("another-install")
        ops = make_ops(env, w)
        put_health(env, instance_changed=True, install_id="old")
        _listed_then_gone(ops, "instance_changed", lambda: ops.target_reconnect(env.target_id))


def library_type_changed() -> None:
    with export_env() as env:
        w = wire(env)
        ops = make_ops(env, w)
        put_health(env, library_type_changed=True)
        with env.engine.begin() as connection:
            connection.execute(sa.update(export.shelf).values(paused_reason="library_type_changed"))
        _listed_then_gone(ops, "library_type_changed", lambda: ops.target_reconnect(env.target_id))
        assert env.rows(export.shelf)[0].paused_reason is None


def target_forbidden() -> None:
    with export_env() as env:
        w = wire(env)
        ops = make_ops(env, w)
        put_health(env, paused=True, paused_route="POST /api/library/scan-folder")
        _listed_then_gone(ops, "target_forbidden", lambda: ops.target_reconnect(env.target_id))
        assert not load_health(env.engine.connect(), env.target_id).paused


def route_missing() -> None:
    with export_env() as env:
        w = wire(env)
        ops = make_ops(env, w)
        put_health(env, missing_routes=("POST /api/library/scan-folder",))
        _listed_then_gone(ops, "route_missing", lambda: ops.target_recheck(env.target_id))


def not_indexed() -> None:
    with export_env() as env:
        _edition, items = _exported(env)
        with env.engine.begin() as connection:
            connection.execute(
                sa.update(export.export_record).values(error_code="target.not_indexed")
            )
        ops = make_ops(env)
        _listed_then_gone(ops, "not_indexed", lambda: ops.jobs_retry(items[0]))
        assert env.rows(export.export_record)[0].error_code is None  # polled again


def child_exposure() -> None:
    with export_env() as env:
        w = wire(env)
        ops = make_ops(env, w)
        put_health(env, child_exposure=True)
        with env.engine.begin() as connection:
            connection.execute(sa.update(export.shelf).values(paused_reason="child_exposure"))
        assert ops.target_recheck_is_reserved(env.target_id)  # the route asks for a fresh session
        _listed_then_gone(ops, "child_exposure", lambda: ops.target_recheck(env.target_id))
        assert env.rows(export.shelf)[0].paused_reason is None


def permission_denied() -> None:
    with export_env() as env:
        _exported(env)
        series_id = env.rows(export.export_series)[0].id
        PermissionPreflight(
            env.engine, env.lib.fsops, make_ops(env)._bus, clock=env.clock
        ).mark_denied(env.destination_id, series_id)
        ops = make_ops(env)
        _listed_then_gone(ops, "permission_denied", lambda: ops.target_recheck(env.target_id))


def language_unknown() -> None:
    with export_env() as env:
        series = env.new_series(lang="x-unknown")
        ops = make_ops(env)
        _listed_then_gone(
            ops, "language_unknown", lambda: ops.edition_set_lang(series.edition_id, "ja")
        )
        assert edition_lang(env.engine, series.edition_id) == "ja"


def tombstone_lines_owed() -> None:
    with export_env() as env:
        keyring = FakeKeyring(kids=[], owed=["01JROOT0000000000000000000"])
        ops = make_ops(env, keyring=keyring)
        _listed_then_gone(ops, "tombstone_lines_owed", ops.tombstone_flush)
        assert keyring.owed == []


# The four states below are produced and left by the production code only (L53): the adoption and
# the pairing write `adoption_refused` / `coverage_unknown`, the restore guard and the sentinels
# make `restored_behind`, a real purge and a keyring that lost its key make `tombstone_key_unknown`.
# `shelfhold status` is what reads them, the command of the catalogue is what leaves them.


@dataclass
class L53Context:
    monkeypatch: pytest.MonkeyPatch
    capsys: pytest.CaptureFixture[str]
    settings: Settings

    def cli(self, *argv: str) -> int:
        return run_cli(self.monkeypatch, self.settings, *argv)

    def listed(self, state: str) -> list[dict[str, Any]]:
        report = status_json(self.monkeypatch, self.settings, self.capsys)
        return waiting_states(report).get(state, [])

    def listed_then_gone(self, state: str, leave: Callable[[], object]) -> None:
        """`status` lists the state with a reason and its command; `leave` plays the command;
        `status` no longer lists it."""
        before = self.listed(state)
        assert before, f"{state} is not listed by `shelfhold status`"
        assert all(i["reason"] and i["command"].startswith("shelfhold ") for i in before)
        leave()
        assert self.listed(state) == [], f"{state} is still listed after its exit command"


def adoption_refused(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        ctx = L53Context(monkeypatch, capsys, settings)
        # the `<team>_<chapter>` names of Suwayomi's own download folder (EF-EXP-044)
        b.kavita_series("Fed Series", ["Team_Chapter 1.cbz"], info=comic("Fed Series", number="1"))
        assert b.adopt(capsys) == 0

        def leave() -> None:
            capsys.readouterr()
            base = ("adopt", "--confirm-migration", "--destination", b.env.destination_id)
            assert b.cli(*base) == 2  # the report and its fingerprint first
            fingerprint = re.split(r"rapport\s:", capsys.readouterr().out)[1].split()[0]
            assert b.cli(*base, "--confirm", fingerprint) == 0

        ctx.listed_then_gone("adoption_refused", leave)


def coverage_unknown(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        ctx = L53Context(monkeypatch, capsys, settings)
        b.kavita_series(SERIES, [f"{SERIES} - Vol.1.cbz"])  # a volume: which chapters it holds
        assert b.adopt(capsys) == 0
        series_id = b.series_id(SERIES)
        (item,) = ctx.listed("coverage_unknown")
        assert item["subject"] == series_id
        assert item["command"] == f"shelfhold adopt set-follow-from {series_id} <chapter>"
        b.upstream({n: f"Chapter {n}" for n in range(1, 6)})
        ctx.listed_then_gone(
            "coverage_unknown",
            lambda: (
                b.cli("adopt", "set-follow-from", series_id, "3") == 0 or pytest.fail("refused")
            ),
        )


def series_name_collision(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    """Two Works of one title in one library: the export blocks, `status` lists both Editions,
    `edition set-series-name` names one of them. Nothing here writes the state: the export job does."""
    with bench54(monkeypatch, settings, tmp_path) as b:
        ctx = L53Context(monkeypatch, capsys, settings)
        english = b.env.new_series("Monster", lang="en")
        french = b.env.new_series("Monster", lang="fr")
        for series in (english, french):
            b.env.add_chapter(series, "1")
        b.export()
        assert {i["subject"] for i in ctx.listed("series_name_collision")} == {
            english.edition_id, french.edition_id,
        }  # fmt: skip

        def leave() -> None:
            assert b.cli("edition", "set-series-name", english.edition_id, "Monster (en)") == 0
            assert b.cli("edition", "set-series-name", french.edition_id, "Monster (fr)") == 0

        ctx.listed_then_gone("series_name_collision", leave)


def mount_sentinel_missing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    """The volume of the library comes back without its sentinel: the export blocks and writes
    `mount.sentinel_missing`; `status` lists it; `target recheck` leaves it once the sentinel is
    the expected one again."""
    with bench54(monkeypatch, settings, tmp_path) as b:
        ctx = L53Context(monkeypatch, capsys, settings)
        with b.engine.connect() as connection:
            roots = (
                connection.execute(sa.text("SELECT path_entry FROM physical_root")).scalars().all()
            )
            target_id = connection.execute(sa.text("SELECT id FROM export_target")).scalar_one()
        folder = str(b.env.inst.library / "Kavita")
        (root,) = [Path(r) for r in roots if folder.startswith(r.rstrip("/") + "/")]
        sentinel = root / ".shelfhold-mount-id"
        kept = sentinel.read_bytes()
        series = b.env.new_series("Alpha")
        b.env.add_chapter(series, "1")
        sentinel.unlink()
        b.export()
        (item,) = ctx.listed("mount_sentinel_missing")
        assert item["command"] == f"shelfhold target recheck {target_id}"

        def leave() -> None:
            sentinel.write_bytes(kept)
            assert b.cli("target", "recheck", target_id) == 0

        ctx.listed_then_gone("mount_sentinel_missing", leave)


def restored_behind(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    with restore_env() as env:
        restored(env)  # six chapters exported after the backup, then the backup put back
        assert env.guard().enter_restored_behind().behind
        with production(env) as own:
            ctx = L53Context(monkeypatch, capsys, own)

            def leave() -> None:
                capsys.readouterr()
                assert ctx.cli("reconcile", "--adopt-after-restore") == 2  # the preview
                out = capsys.readouterr().out
                fingerprint = next(
                    line.split(": ")[1].strip() for line in out.splitlines() if "Empreinte" in line
                )
                assert ctx.cli("reconcile", "--adopt-after-restore", "--confirm", fingerprint) == 0

            ctx.listed_then_gone("restored_behind", leave)


def _root_scenario(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    state: str,
    damage: Callable[[RestoreEnv, Settings], None],
    command: Callable[[RestoreEnv], tuple[str, ...]],
) -> None:
    """M1b L25: one of the four root states, found by `status` over the real roots, left by its
    reserved command from the host CLI; nothing is deleted on the way."""
    with restore_env() as env:
        with env.engine.connect() as connection:  # the copy of the file must hold everything
            connection.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
        with production(env) as own:
            damage(env, own)
            ctx = L53Context(monkeypatch, capsys, own)
            before = {p: p.read_bytes() for p in env.ex.lib.library.rglob("*") if p.is_file()}
            ctx.listed_then_gone(state, lambda: ctx.cli(*command(env)) == 0 or pytest.fail(state))
            for path, data in before.items():
                if path.name not in (".shelfhold-mount-id", "lease"):
                    assert path.read_bytes() == data, f"{path} changed"


def foreign_root(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def damage(env: RestoreEnv, _own: Settings) -> None:
        edit_sentinel(env.ex.lib.library, instance_id="01J0000000000000000FOREIGN")

    _root_scenario(
        monkeypatch, capsys, "foreign_root", damage,
        lambda env: ("root", "takeover", str(env.ex.lib.library)),
    )  # fmt: skip


def root_lost(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def damage(env: RestoreEnv, _own: Settings) -> None:
        (env.ex.lib.library / ".shelfhold-mount-id").unlink()

    _root_scenario(
        monkeypatch, capsys, "root_lost", damage,
        lambda env: ("root", "reinit", str(env.ex.lib.library)),
    )  # fmt: skip


def root_behind(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def damage(env: RestoreEnv, own: Settings) -> None:
        with sqlite3.connect(own.database_path) as conn:
            conn.execute("UPDATE physical_root SET generation = generation + 5")

    _root_scenario(
        monkeypatch, capsys, "root_behind", damage,
        lambda env: ("root", "restored", str(env.ex.lib.library)),
    )  # fmt: skip


def store_lost(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def damage(env: RestoreEnv, _own: Settings) -> None:
        (env.ex.lib.store / ".shelfhold-mount-id").unlink()

    _root_scenario(monkeypatch, capsys, "store_lost", damage, lambda _env: ("store", "reinit"))


def restored_credentials(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    """After a restore: the notice (a row of the database) is listed with its command until the reserved
    `restore acknowledge` runs; a second run finds nothing to acknowledge."""
    with seeded(settings, tmp_path):
        ctx = L53Context(monkeypatch, capsys, settings)
        with create_db_engine(settings.database_path).begin() as connection:
            write_notice(
                connection,
                RestoreNoticeData("2026-11-11T00:00:00", None, 1, 2),
                keep_existing=False,
            )
        (item,) = ctx.listed("restored_credentials")
        assert item["command"] == "shelfhold restore acknowledge"
        assert item["reserved"] is True
        ctx.listed_then_gone("restored_credentials", lambda: ctx.cli("restore", "acknowledge") == 0)
        assert ctx.cli("restore", "acknowledge") != 0  # nothing waits any more


def tombstone_key_unknown(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with seeded(settings, tmp_path) as env:
        ctx = L53Context(monkeypatch, capsys, settings)
        series = env.new_series("Purged")
        unit = env.add_chapter(series, "1")
        # a real purge writes a real tombstone, sealed under the key of the keyring
        assert ctx.cli("purge", "--now", "--unit", unit, "--confirm", unit) == 0
        assert ctx.listed("tombstone_key_unknown") == []  # the keyring has its key
        store_for(settings).write(Keyring.generate())  # /data restored, the keyring was not
        ctx.listed_then_gone(
            "tombstone_key_unknown",
            lambda: ctx.cli("tombstone", "accept-lost-keyring", "--confirm", LOST_KEYRING_PHRASE),
        )


def tombstone_journal_truncated(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    """M1b L26 (EF-BIB-054 (6), D12): two real purges, the last line of the journal cut off. The
    root waits, `status` names the exit, `tombstone set-aside-journal` writes the journal again
    from the table (the cut file is kept), and the state is gone."""
    with seeded(settings, tmp_path) as env:
        ctx = L53Context(monkeypatch, capsys, settings)
        series = env.new_series("Purged")
        for number in ("1", "2"):
            unit = env.add_chapter(series, number)
            assert ctx.cli("purge", "--now", "--unit", unit, "--confirm", unit) == 0
        (journal,) = [  # the journal of the library root (the store has its own)
            p for p in tmp_path.rglob("tombstones")
            if p.parent.name == ".shelfhold" and p.parts[len(tmp_path.parts)] == "library"
        ]  # fmt: skip
        complete = journal.read_bytes()
        assert ctx.listed("tombstone_journal_truncated") == []
        rows = complete.splitlines(keepends=True)
        assert len(rows) >= 3  # the header and a few lines (a purge seals several scopes)
        journal.write_bytes(b"".join(rows[:-1]))  # the attacker's cut
        (item,) = ctx.listed("tombstone_journal_truncated")
        assert item["command"].startswith("shelfhold tombstone set-aside-journal")

        def leave() -> None:
            assert ctx.cli("tombstone", "set-aside-journal") == 0

        ctx.listed_then_gone("tombstone_journal_truncated", leave)
        assert len(journal.read_bytes().splitlines()) == len(rows)  # whole again
        kept = list(journal.parent.glob("tombstones.aside-*"))
        assert len(kept) == 1 and kept[0].read_bytes() == b"".join(rows[:-1])


def key_expiring(settings: Settings) -> None:
    with headless(settings) as h:
        added = h.admin.target_add_kavita(
            name="kavita", url=KAVITA_URL, username="shelfhold", password="p" * 12,
            accept_admin_risk=True,
        )  # fmt: skip
        with h.engine.begin() as connection:
            health = load_health(connection, added.id)
            health.key_notices = KeyNoticeState(NOW + timedelta(days=10), frozenset({"J-14"}))
            health.refresh_waiting()
            from shelfhold.export.application.target_state import save_health

            save_health(connection, added.id, health)
        ops = OpsServices(h.engine, clock=h.clock, bus=_bus(h), headless=h.admin)
        _listed_then_gone(
            ops, "key_expiring", lambda: ops.target_rotate_key(added.id, password="p" * 12)
        )
        assert len(h.kavita.calls) == 2  # a new key was asked of Kavita
        # and the old one was revoked at Kavita: found by its sealed value, not left to expire
        assert [r["key"] for r in h.kavita.revoked] == [KAVITA_KEY]


def auth_expired(settings: Settings) -> None:
    with headless(settings) as h:
        h.admin.source_add_suwayomi(url=SUWAYOMI_URL, secret="admin:old-secret")
        pause_follow(h.engine, failure_class="auth", state="auth_expired")
        ops = OpsServices(h.engine, clock=h.clock, bus=_bus(h), headless=h.admin)
        _listed_then_gone(ops, "auth_expired", lambda: ops.source_set_secret("admin:new-secret"))
        assert h.suwayomi.calls[-1]["password"] == "new-secret"


def source_instance_changed(settings: Settings) -> None:
    """M1b L18: Suwayomi was reinstalled; `source reconnect` takes the pause off and the next
    cycle takes the fingerprint of the new installation as its reference."""
    with headless(settings) as h:
        h.admin.source_add_suwayomi(url=SUWAYOMI_URL, secret="admin:s3cret-synthetic")
        with h.engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE source_config SET instance_fingerprint = 'old' "
                        "WHERE plugin_id = 'suwayomi'")
            )  # fmt: skip
        pause_follow(h.engine, failure_class="instance", state="source_instance_changed")
        ops = OpsServices(h.engine, clock=h.clock, bus=_bus(h), headless=h.admin)
        (item,) = _waiting(ops, "source_instance_changed")
        assert item.command == "shelfhold source reconnect"  # type: ignore[attr-defined]
        _listed_then_gone(ops, "source_instance_changed", ops.source_reconnect)
        with h.engine.connect() as connection:
            stored = connection.execute(
                sa.text("SELECT instance_fingerprint FROM source_config")
            ).scalar_one()
        assert stored is None  # the reference is read again by the next cycle


def safeguard_backlog() -> None:
    """M1b L16: Units owed a copy in /store that a cycle could not take wait for the next window.
    `status` lists them with `jobs list`; the nightly job (the real cycle, with the budget of one
    Unit) takes them in order and the state leaves when the last one is kept."""
    one_at_a_time = StoreBudget(free_bytes=10**12, reserve_bytes=0, max_units=1)
    with lib_env() as env:
        world = World(env)
        for _ in range(3):
            world.add(owed=True)
        world.run_cycle(one_at_a_time)
        assert len(world.store_files()) == 1
        ops = OpsServices(env.engine, clock=world.clock, bus=EventBus(world.clock))
        (item,) = _waiting(ops, "safeguard_backlog")
        assert item.command == "shelfhold jobs list"  # type: ignore[attr-defined]
        assert "2 unit(s)" in item.reason  # type: ignore[attr-defined]
        _listed_then_gone(
            ops, "safeguard_backlog",
            lambda: [world.run_cycle(one_at_a_time) for _ in range(2)],
        )  # fmt: skip
        assert len(world.store_files()) == 3


def auth_mode_unsupported(settings: Settings) -> None:
    with headless(settings) as h:
        h.admin.source_add_suwayomi(url=SUWAYOMI_URL, secret="admin:s3cret-synthetic")
        pause_follow(h.engine, failure_class="auth_mode", state="auth_mode_unsupported")
        h.suwayomi.mode = "basic_auth"  # the server was set back to a supported mode
        ops = OpsServices(h.engine, clock=h.clock, bus=_bus(h), headless=h.admin)
        _listed_then_gone(ops, "auth_mode_unsupported", ops.source_recheck)


# The two states of the M1b parental control (L11): both leave through `rate`, the only door that
# sets an age. `follow_suspended` is produced by the real adoption of a library the children can
# read; `rating_pending` is a Work nobody rated waiting out of a shelf below 18+ (held, as the
# follow cycle leaves it for a sub-source that says "safe").


def follow_suspended(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        ctx = L53Context(monkeypatch, capsys, settings)
        b.env.inst.kavita.users.append(
            member("enfant", libraries=(int(b.library_ref),), age_rating=6)
        )
        b.kavita_series("Lupin", ["Lupin - Ch.1.cbz"], info=comic("Lupin", number="1"))
        b.kavita_series("Monster", ["Monster - Ch.1.cbz"], info=comic("Monster", number="1"))
        assert b.adopt(capsys) == 0  # a library the children can read is adopted (EF-EXP-048)
        items = ctx.listed("follow_suspended")
        assert len(items) == 2 and all(i["command"].startswith("shelfhold rate ") for i in items)
        first, second = (i["subject"] for i in items)
        assert ctx.cli("rate", "10+", "--work", first) == 0
        assert [i["subject"] for i in ctx.listed("follow_suspended")] == [second]  # per Work
        ctx.listed_then_gone(
            "follow_suspended",
            lambda: (
                ctx.cli("rate", "10+", "--pending", "--confirm", _token(ctx, capsys)) == 0
                or pytest.fail("refused")
            ),
        )


def _token(ctx: L53Context, capsys: pytest.CaptureFixture[str]) -> str:
    capsys.readouterr()
    assert ctx.cli("rate", "10+", "--pending") == 2  # the list, then what to type
    return capsys.readouterr().out.split("--confirm ")[1].split()[0]


def rating_pending(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        ctx = L53Context(monkeypatch, capsys, settings)
        with_family_shelf(b)
        work = held_work(b, "Lupin")
        (item,) = ctx.listed("rating_pending")
        assert item["subject"] == work
        assert item["command"].startswith(f"shelfhold rate <age> --work {work}")
        ctx.listed_then_gone(
            "rating_pending",
            lambda: ctx.cli("rate", "10+", "--work", work) == 0 or pytest.fail("refused"),
        )


def maintenance(settings: Settings) -> None:
    """M3 L13 (EF-INT-018): the mode is listed by `status` with `maintenance off` for as long as it
    lasts, and that command (a reserved one: the host here) ends it."""
    from shelfhold.identity.public import Credential, Principal
    from shelfhold.infrastructure.schema import platform
    from shelfhold.ops.maintenance import MaintenanceOps

    with headless(settings) as h:
        with h.engine.begin() as connection:
            if connection.execute(sa.select(platform.instance.c.instance_id)).first() is None:
                connection.execute(
                    sa.insert(platform.instance).values(instance_id=new_ulid(), jurisdiction="FR")
                )
        ops = OpsServices(
            h.engine, clock=h.clock, bus=_bus(h), headless=h.admin,
            maintenance=MaintenanceOps(h.engine, h.clock),
        )  # fmt: skip
        host = Principal("1000", "uid:1000", "admin", Credential.LOCAL_CLI)
        ops.maintenance.enter(principal=host)
        (item,) = _waiting(ops, "maintenance")
        assert item.command == "shelfhold maintenance off"  # type: ignore[attr-defined]
        _listed_then_gone(ops, "maintenance", lambda: ops.maintenance.leave(principal=host))


def _bus(h: Headless):  # type: ignore[no-untyped-def]
    from shelfhold.events.bus import EventBus

    return EventBus(h.clock)


SCENARIOS: dict[str, Callable[..., None]] = {
    s.__name__: s
    for s in (
        missing, paused_foreign_writer, dead_letter, instance_changed, target_forbidden,
        key_expiring, not_indexed, auth_expired, adoption_refused, permission_denied,
        child_exposure, auth_mode_unsupported, coverage_unknown, language_unknown,
        restored_behind, restored_credentials, tombstone_key_unknown, tombstone_lines_owed, route_missing,
        series_name_collision, mount_sentinel_missing, rating_pending, follow_suspended,
        source_instance_changed, foreign_root, root_lost, root_behind, store_lost,
        safeguard_backlog, library_type_changed,
        tombstone_journal_truncated, maintenance,
    )
}  # fmt: skip


# -- the parametrised test ----------------------------------------------------------------------------


@pytest.mark.parametrize("state", sorted(WaitingState, key=str), ids=str)
def test_every_waiting_state_has_exit_command(
    state: WaitingState,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    routes = [r for r in EXIT_ROUTES if r.waiting_state == state.value]
    if state in LATER_STATES:
        # the exit arrives with the milestone where the state appears (EF-INT-023): until then it
        # is declared in the catalogue and has no scenario
        assert routes
        assert state not in PLAYED_STATES
        return
    assert state in PLAYED_STATES and routes, f"{state} has no exit command"
    assert state.value in SCENARIOS, f"{state} has no end-to-end scenario"
    served = [r for r in routes if r.command not in UNSERVED]  # declared, not written yet
    assert served, f"{state} has no served exit"
    for route in served:
        assert route.command in ENDPOINTS, f"{route.command}: no API route declared"
        _method, path = endpoint_of(route.command)
        assert path.startswith("/api/v1/"), route.command
        pending = CLI_PENDING.get(bare(route.command))
        if pending is None:
            assert _cli_has(route.command), f"`shelfhold {route.command}` is not a command"
        else:  # the CLI module belongs to a lot still in flight: the day it lands, remove the entry
            assert not _cli_has(route.command), f"{route.command} exists: drop it from CLI_PENDING"
    scenario = SCENARIOS[state.value]
    given = {
        "settings": settings, "monkeypatch": monkeypatch, "capsys": capsys, "tmp_path": tmp_path,
    }  # fmt: skip
    scenario(**{n: given[n] for n in inspect.signature(scenario).parameters})


@pytest.mark.parametrize("command", sorted(ENDPOINTS))
def test_every_exit_route_is_in_the_openapi(command: str) -> None:
    assert _route_documented(command), command


def test_exit_tables_agree() -> None:
    declared = {
        r.command for r in EXIT_ROUTES if r.waiting_state in {s.value for s in PLAYED_STATES}
    }
    assert declared == set(ENDPOINTS) | set(UNSERVED)
    assert not set(ENDPOINTS) & set(UNSERVED)


def test_the_enumeration_is_partitioned_into_m1a_and_later() -> None:
    unplaced = set(WaitingState) - M1A_STATES - M1B_STATES - M3_STATES - M3_SERVED_STATES
    assert not unplaced, f"{sorted(map(str, unplaced))}: neither M1a, M1b nor M3 (decide)"
    assert {WaitingState.LIBRARY_TYPE_CHANGED} == M3_STATES
    assert not (M1A_STATES & M1B_STATES) and not (M1A_STATES & M3_STATES)
    assert not (M1B_STATES & M3_STATES)
    assert {
        r.waiting_state for r in EXIT_ROUTES if r.waiting_state in {s.value for s in M1A_STATES}
    } == {s.value for s in M1A_STATES}


def test_language_unknown_has_exit_command(tmp_path: Path) -> None:
    """ENF-I18N-005: `edition set-lang` leaves `language_unknown`, and refuses a tag that would
    collide with another Edition of the same Work or a placeholder."""
    assert _cli_has("edition set-lang")
    with export_env() as env:
        series = env.new_series(lang="x-unknown")
        ops = make_ops(env)
        with pytest.raises(Exception, match="language"):
            ops.edition_set_lang(series.edition_id, "und")  # a wildcard is not a language
        assert edition_lang(env.engine, series.edition_id) == "x-unknown"
        # another Edition of the same Work already has that language: no silent merge
        with env.engine.begin() as connection:
            work = connection.execute(
                sa.select(catalog.edition.c.work_id).where(
                    catalog.edition.c.id == series.edition_id
                )
            ).scalar_one()
        twin = new_ulid()
        with env.engine.begin() as connection:
            row = dict(
                connection.execute(
                    sa.select(catalog.edition).where(catalog.edition.c.id == series.edition_id)
                )
                .one()
                ._mapping
            )
            connection.execute(sa.insert(catalog.edition).values({**row, "id": twin, "lang": "ja"}))
        with pytest.raises(Exception, match="language"):
            ops.edition_set_lang(series.edition_id, "ja")
        assert work
        assert ops.edition_set_lang(series.edition_id, "fr-CA") == "fr-CA"


def test_waiting_state_scenarios_cover_the_m1a_states() -> None:
    assert set(SCENARIOS) == {s.value for s in PLAYED_STATES}
