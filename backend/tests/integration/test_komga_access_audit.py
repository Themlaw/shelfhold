"""EF-EXP-047, ENF-SEC-016 (M4 L08b): the read-only audit of the accounts of a Komga.

The partitioning of a Komga is granted only when the audit proves it: no account that is a
child's (an age restriction) reaches a library above its rating, the library holds only the
folder of the shelf, the release is at least 1.22.0, and the reading itself succeeded. Any doubt
closes: a server that does not answer, a key that may not list accounts, an answer that cannot be
read, an unknown version. Synthetic server, no network.
"""

from __future__ import annotations

import json

import pytest
import sqlalchemy as sa

from shelfhold.events.waiting_state import WaitingState
from shelfhold.export.application.fan_out import bindings_of
from shelfhold.export.application.komga_audit_state import audit_reason, proved_for
from shelfhold.export.application.target_state import load_health
from shelfhold.export.domain.bindings import UnpartitionedDestinationError
from shelfhold.export.targets.komga import KomgaClient, KomgaKey
from shelfhold.export.targets.komga.audit import KomgaAuditor, KomgaAuditReport
from shelfhold.infrastructure.schema import export, platform
from tests.export_env import ExportEnv
from tests.fakes.komga import (
    API_KEY,
    BASE_URL,
    OTHER_KEY,
    FakeKomga,
    administrator,
    build_fetcher,
    reader,
)
from tests.family_env import add_adult_shelf, family_env
from tests.fan_out_env import add_target, bind, service, tree_of
from tests.kavita_env import RecordingEvents
from tests.komga_env import run

LIBRARY = "0KIDSLIB01"
ROOT = "/manga/Kavita"  # the tree of the shelf's own binding, the one Komga indexes (J-SIMP-4)


def library(ref: str = LIBRARY, root: str = ROOT) -> dict[str, object]:
    return {"id": ref, "name": "Synthetic Kids", "root": root, "unavailable": False}


def kit(
    env: ExportEnv, *, key: str = API_KEY, **options: object
) -> tuple[FakeKomga, KomgaClient, KomgaAuditor, RecordingEvents, str]:
    options.setdefault("libraries", [library()])
    fake = FakeKomga(**options)  # type: ignore[arg-type]
    client = KomgaClient(build_fetcher(fake), BASE_URL, KomgaKey(key), clock=env.clock)
    events = RecordingEvents()
    target = add_target(env, kind="komga", partitioning="none", name="Komga")
    return fake, client, KomgaAuditor(env.engine, env.clock, events), events, target


def admit(env: ExportEnv, auditor: KomgaAuditor, client: KomgaClient, target: str,
          ceiling: int | None = 13, root: str = ROOT) -> KomgaAuditReport:  # fmt: skip
    return run(auditor.admit(client, target, shelf_ceiling=ceiling, remote_root=root))


def codes(report: KomgaAuditReport) -> set[str]:
    return {f.code for f in report.findings}


def try_bind(env: ExportEnv, target: str) -> str | None:
    """The refusal of the family shelf to the Komga target, None when the indexer binding was
    accepted. A Komga binding never owns a tree (J-SIMP-4): the tree of the shelf's own binding
    is the one it indexes."""
    service(env).list_bindings(env.shelf_id)
    tree_id = tree_of(env, env.destination_id)
    assert tree_id is not None
    try:
        bind(env, "Kavita", target_id=target, attach_to_tree=tree_id, link=False)
    except UnpartitionedDestinationError as refused:
        return str(refused)
    return None


def bind_indexer(env: ExportEnv, target: str) -> str:
    """The indexer binding of the Komga target on the tree of the shelf's own binding."""
    service(env).list_bindings(env.shelf_id)
    tree_id = tree_of(env, env.destination_id)
    assert tree_id is not None
    return bind(env, "Kavita", target_id=target, attach_to_tree=tree_id, link=False)


# -- the attack of the child ------------------------------------------------------------------


@pytest.mark.req("EF-EXP-047")
def test_komga_child_account_audit_flags_library_above_its_rating() -> None:
    with family_env(13) as env:
        fake, client, auditor, _events, target = kit(env)
        fake.users = [administrator(), reader("kid", [LIBRARY], age=8)]  # reads 8+, shelf is 13+
        report = admit(env, auditor, client, target)
        assert not report.proved and not report.failed
        assert [(f.code, f.library) for f in report.findings] == [("library_above_rating", LIBRARY)]
        assert report.reason == "library_above_rating"
        refused = try_bind(env, target)  # the binding is refused, with its reason
        assert refused is not None and "library_above_rating" in refused
        with env.engine.connect() as connection:
            assert audit_reason(connection, target) == "library_above_rating"
            assert not proved_for(connection, target, ROOT, 13)


@pytest.mark.req("EF-EXP-047")
def test_a_child_old_enough_for_the_shelf_proves_the_partitioning() -> None:
    with family_env(13) as env:
        fake, client, auditor, _events, target = kit(env)
        fake.users = [administrator(), reader("teen", [LIBRARY], age=13)]
        report = admit(env, auditor, client, target)
        assert report.proved and report.findings == () and report.reason is None
        assert try_bind(env, target) is None
        (binding,) = [b for b in bindings_of_all(env) if b.target_id == target]
        assert binding.partitioning == "library"


def bindings_of_all(env: ExportEnv):  # type: ignore[no-untyped-def]
    with env.engine.connect() as connection:
        return bindings_of(connection, env.shelf_id)


@pytest.mark.req("EF-EXP-047")
@pytest.mark.parametrize(
    ("account", "flagged"),
    [
        (reader("kid", [LIBRARY], age=8), True),
        (reader("kid", [], age=8, shared_all=True), True),  # shared with every library
        (reader("kid", [LIBRARY], age=13, restriction="EXCLUDE"), True),  # hides 13+: reads 12
        (reader("kid", [LIBRARY], age=14, restriction="EXCLUDE"), False),
        (reader("kid", [LIBRARY], age=99), False),
        (reader("kid", ["0ELSEWHERE"], age=8), False),  # reaches another library only
        (reader("adult", [LIBRARY]), False),  # a member without restriction is not a child
        (reader("adult", [], shared_all=True), False),
    ],
)
def test_the_rating_of_an_account_is_compared_with_the_ceiling_of_the_library(
    account: dict[str, object], flagged: bool
) -> None:
    with family_env(13) as env:
        fake, client, auditor, _events, target = kit(env)
        fake.users = [administrator(), account]
        report = admit(env, auditor, client, target)
        assert report.proved is not flagged
        assert (codes(report) == {"library_above_rating"}) is flagged


@pytest.mark.req("EF-EXP-047")
def test_a_library_that_holds_more_than_the_folder_of_the_shelf_is_not_a_family_library() -> None:
    with family_env(13) as env:
        _fake, client, auditor, _events, target = kit(env, libraries=[library(root="/manga")])
        report = admit(env, auditor, client, target)
        assert not report.proved and "library_not_dedicated" in codes(report)


@pytest.mark.req("EF-EXP-047")
def test_a_shelf_no_library_answers_for_is_not_proved() -> None:
    with family_env(13) as env:
        _fake, client, auditor, _events, target = kit(env, libraries=[library(root="/elsewhere")])
        report = admit(env, auditor, client, target)
        assert not report.proved and codes(report) == {"library_unmapped"}


# -- the audit that cannot be done closes the access --------------------------------------------


@pytest.mark.req("EF-EXP-047", "ENF-SEC-016")
@pytest.mark.parametrize(
    "setup",
    ["asleep", "read_only_key", "unreadable_accounts", "no_version", "garbage_version", "old"],
)
def test_an_audit_that_cannot_be_done_never_deduces_the_partitioning(setup: str) -> None:
    with family_env(13) as env:
        key = OTHER_KEY if setup == "read_only_key" else API_KEY
        fake, client, auditor, _events, target = kit(env, key=key)
        fake.users = [administrator()]  # no child at all: the best case, if it could be read
        if setup == "asleep":
            fake.asleep = True
        elif setup == "unreadable_accounts":
            fake.users = [{"nonsense": True}]  # type: ignore[list-item]
        elif setup == "no_version":
            fake.version = ""  # type: ignore[assignment]
        elif setup == "garbage_version":
            fake.version = "next-gen"
        elif setup == "old":
            fake.version = "1.21.9"
        report = admit(env, auditor, client, target)
        assert not report.proved
        assert report.failed is (setup in {"asleep", "read_only_key", "unreadable_accounts"})
        assert report.reason in {
            "audit_failed", "version_unknown", "version_below_floor",
        }  # fmt: skip
        refused = try_bind(env, target)
        assert refused is not None and report.reason in refused
        with env.engine.connect() as connection:
            assert not proved_for(connection, target, ROOT, 13)


@pytest.mark.req("EF-EXP-047")
def test_a_target_never_audited_is_refused_a_family_shelf_with_its_reason() -> None:
    with family_env(13) as env:
        target = add_target(env, kind="komga", partitioning="library", name="Komga")
        refused = try_bind(env, target)
        assert refused is not None and "never_audited" in refused


@pytest.mark.req("EF-EXP-047", "ENF-SEC-016")
def test_a_proof_is_for_the_folder_and_the_ceiling_it_was_audited_for() -> None:
    with family_env(13) as env:
        _fake, client, auditor, _events, target = kit(
            env, libraries=[library(), library("0OTHERLIB2", "/manga/Other")]
        )
        assert admit(env, auditor, client, target, ceiling=13).proved
        with env.engine.connect() as connection:
            assert proved_for(connection, target, ROOT, 13)
            assert proved_for(connection, target, ROOT, 10)  # a lower ceiling is covered
            assert not proved_for(connection, target, ROOT, 16)  # a higher one is not
            assert not proved_for(connection, target, "/manga/Other", 13)  # another folder
            assert not proved_for(connection, target, ROOT + "/../Other", 13)
            assert not proved_for(connection, "01HZZZZZZZZZZZZZZZZZZZZZZZ", ROOT, 13)


# -- the daily audit: a gap opens after the binding ---------------------------------------------


@pytest.mark.req("EF-EXP-047")
def test_a_gap_that_opens_later_pauses_the_shelf_and_shows_in_health() -> None:
    with family_env(13) as env:
        fake, client, auditor, events, target = kit(env)
        fake.users = [administrator(), reader("teen", [LIBRARY], age=13)]
        assert admit(env, auditor, client, target).proved
        destination = bind_indexer(env, target)
        report = run(auditor.audit(client, target))
        assert report.proved and report.paused_shelves == []
        fake.users = [administrator(), reader("teen", [LIBRARY], age=6)]  # lowered in Komga
        report = run(auditor.audit(client, target))
        assert not report.proved and report.paused_shelves == [env.shelf_id]
        assert env.rows(export.shelf)[0].paused_reason == "child_exposure"
        with env.engine.connect() as connection:
            health = load_health(connection, target)
            assert not proved_for(connection, target, ROOT, 13)
        assert health.child_exposure and health.waiting_state is WaitingState.CHILD_EXPOSURE
        (detail,) = events.details("target.child_exposure")
        assert detail["shelves"] == [env.shelf_id] and detail["cause"] == "library_above_rating"
        assert [
            b.partitioning for b in bindings_of_all(env) if b.destination_id == destination
        ] == ["none"]  # the pass itself refuses the binding from now on
        fake.users = [administrator(), reader("teen", [LIBRARY], age=13)]  # repaired
        assert run(auditor.audit(client, target)).proved
        assert env.rows(export.shelf)[0].paused_reason is None  # only a complete audit lifts it


@pytest.mark.req("EF-EXP-047")
def test_an_audit_that_fails_after_a_proof_withdraws_it_and_pauses() -> None:
    with family_env(13) as env:
        fake, client, auditor, events, target = kit(env)
        assert admit(env, auditor, client, target).proved
        bind_indexer(env, target)
        fake.asleep = True
        report = run(auditor.audit(client, target))
        assert report.failed and not report.proved and report.paused_shelves == [env.shelf_id]
        with env.engine.connect() as connection:
            assert not proved_for(connection, target, ROOT, 13)
            assert audit_reason(connection, target) == "audit_failed"
        assert events.details("target.child_exposure")[0]["cause"] == "audit_failed"


@pytest.mark.req("EF-EXP-047")
def test_a_child_account_on_the_library_of_an_adult_shelf_pauses_it() -> None:
    """The library of a shelf 18+ is not a family library: any child reaching it is a gap."""
    with family_env(13) as env:
        _adult_shelf, _ = add_adult_shelf(env)
        fake, client, auditor, _events, target = kit(env)
        fake.users = [administrator(), reader("kid", [LIBRARY], age=16)]
        report = admit(env, auditor, client, target, ceiling=None)
        assert not report.proved and codes(report) == {"library_above_rating"}


# -- credentials and names are never shown ------------------------------------------------------


@pytest.mark.req("EF-EXP-047", "ENF-SEC-016")
def test_no_key_and_no_account_name_leaves_the_audit() -> None:
    with family_env(13) as env:
        fake, client, auditor, events, target = kit(env)
        fake.users = [administrator(), reader("secretname", [LIBRARY], age=6)]
        report = admit(env, auditor, client, target)
        run(auditor.audit(client, target))
        fake.asleep = True
        failed = admit(env, auditor, client, target)
        shown = [repr(report), repr(failed), str(events.emitted), repr(client)]
        with env.engine.connect() as connection:
            shown.append(
                json.dumps(
                    [dict(r._mapping) for r in connection.execute(sa.select(platform.app_setting))],
                    default=str,
                )
            )
            shown.append(json.dumps(load_health(connection, target).to_json()))
        for text in shown:
            assert API_KEY not in text and OTHER_KEY not in text and "secretname" not in text
        assert all(r.headers.get("x-api-key") == API_KEY for r in fake.requests)
        assert fake.violations == []  # only listed routes were called


@pytest.mark.req("ENF-SEC-016", "EF-EXP-047")
def test_the_status_page_lists_the_komga_floor_and_the_version_audited() -> None:
    from datetime import date

    from shelfhold.ops.status import known_bad_report

    with family_env(13) as env:
        fake, client, auditor, _events, target = kit(env)
        fake.version = "1.21.0"
        admit(env, auditor, client, target)
        with env.engine.connect() as connection:
            known = known_bad_report(connection, date(2026, 11, 16))
        assert known["komga"] == {
            "minimum_version": "1.22.0",
            "entries": [{"advisory": "komga-below-1.22.0", "category": "cve", "below": "1.22.0"}],
        }
        row = next(t for t in known["targets"] if t["target"] == target)  # type: ignore[attr-defined]
        assert row["version"] == "1.21" or row["version"] == "1.21.0"
        assert row["advisories"] == ["komga-below-1.22.0 (cve)"]
