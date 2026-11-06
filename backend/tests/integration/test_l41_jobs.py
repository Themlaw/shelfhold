"""L41: the periodic jobs and the library operations, run in process over a real instance (the
database, the keyring and the roots are real; Kavita and Suwayomi are the simulated servers)."""

from typing import ClassVar

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.schema import export, platform
from shelfhold.runtime_jobs import (
    access_audit,
    export_handler,
    follow_handler,
    reconcile_handler,
    tree_destinations,
)
from tests.l41_env import context, instance

pytestmark = pytest.mark.req("EF-SRC-009", "EF-EXP-034", "EF-INT-023")


def test_jobs_return_quietly_on_an_unconfigured_instance(settings: Settings, tmp_path) -> None:
    with instance(settings, tmp_path) as inst:
        assert tree_destinations(inst.comp) == []
        export_handler(inst.comp)(context("export.run"))
        reconcile_handler(inst.comp, in_window=False)(context("reconcile.cycle"))
        access_audit(inst.comp)()
        assert inst.kavita.requests == []


def test_the_follow_cycle_waits_for_a_connected_service(settings: Settings, tmp_path) -> None:
    class Pools:
        submitted: ClassVar[list[str]] = []

        def submit(self, kind: str, **_: object) -> None:
            self.submitted.append(kind)

    with instance(settings, tmp_path) as inst:
        inst.configure()
        follow_handler(inst.comp, Pools())(context("follow.cycle"))  # type: ignore[arg-type]
        assert Pools.submitted == []  # no Suwayomi configured: nothing runs, nothing is exported


def test_the_follow_cycle_runs_then_asks_for_an_export(settings: Settings, tmp_path) -> None:
    class Pools:
        def __init__(self) -> None:
            self.submitted: list[str] = []
            self.slots = None

        def submit(self, kind: str, **_: object) -> None:
            self.submitted.append(kind)

    with instance(settings, tmp_path) as inst:
        inst.configure(suwayomi=True)
        pools = Pools()
        follow_handler(inst.comp, pools)(context("follow.cycle"))  # type: ignore[arg-type]
        assert inst.suwayomi.requests  # the cycle really spoke to the service
        assert pools.submitted == ["export.run"]


def test_export_and_reconcile_run_over_a_configured_destination(
    settings: Settings, tmp_path
) -> None:
    with instance(settings, tmp_path) as inst:
        inst.configure()
        assert len(tree_destinations(inst.comp)) == 1
        reconcile_handler(inst.comp, in_window=True)(context("reconcile.hash"))
        export_handler(inst.comp)(context("export.run"))
        access_audit(inst.comp)()
        assert inst.kavita.requests_to("Users")  # the daily audit read the accounts
        with inst.engine.connect() as c:
            assert (
                c.execute(
                    sa.select(sa.func.count()).select_from(platform.physical_root)
                ).scalar_one()
                >= 1
            )
            assert (
                c.execute(
                    sa.select(sa.func.count()).select_from(export.shelf_destination)
                ).scalar_one()
                == 1
            )


# -- the library operations, in process ----------------------------------------------------------


def _ops(inst):  # type: ignore[no-untyped-def]
    from shelfhold.ops.library import LibraryOps

    return LibraryOps(inst.comp)


def test_the_only_destination_is_the_default_and_none_is_refused(
    settings: Settings, tmp_path
) -> None:
    from shelfhold.ops.errors import UnknownSubjectError

    with instance(settings, tmp_path) as inst:
        with pytest.raises(UnknownSubjectError):
            _ops(inst)._destination(None)
        inst.configure()
        destination = tree_destinations(inst.comp)[0].id
        assert _ops(inst)._destination(None) == destination
        assert _ops(inst)._destination("given") == "given"


def test_pairing_and_giving_back_need_a_connected_service(settings: Settings, tmp_path) -> None:
    from shelfhold.ops.errors import ExitUnavailableError

    with instance(settings, tmp_path) as inst:
        inst.configure()
        ops = _ops(inst)
        for call in (
            lambda: ops.pair_propose(None),
            lambda: ops.pair_confirm(None, "x", source_key="s", remote_id="1"),
            lambda: ops.pair_auto(None, fingerprint=None),
            lambda: ops.set_follow_from(destination_id=None, series="x", chapter="1"),
            lambda: ops.give_back_upstream(),
        ):
            with pytest.raises(ExitUnavailableError):
                call()


def test_pairing_proposes_from_what_suwayomi_follows(settings: Settings, tmp_path) -> None:
    from shelfhold.ops.errors import UnknownSubjectError

    with instance(settings, tmp_path) as inst:
        inst.configure(suwayomi=True)
        ops = _ops(inst)
        assert ops.pair_propose(None) == []  # nothing adopted: nothing to pair
        report = ops.pair_auto(None, fingerprint=None)
        assert report["ready"] == [] and "fingerprint" in report
        with pytest.raises(UnknownSubjectError):
            ops.pair_confirm(None, "x", source_key="nope", remote_id="0")
        with pytest.raises(UnknownSubjectError):
            ops.set_follow_from(destination_id=None, series="ghost", chapter="3")
        given = ops.give_back_upstream()
        assert given["deleted_files"] == [] or given["deleted_files"] == 0


def test_the_migration_report_and_the_repatriation_of_an_empty_destination(
    settings: Settings, tmp_path
) -> None:
    with instance(settings, tmp_path) as inst:
        inst.configure()
        ops = _ops(inst)
        report = ops.migration_report(None)
        assert report["detected"] is False and report["steps"]
        from shelfhold.ops.errors import CodedRefusal

        with pytest.raises(CodedRefusal) as blocked:  # no read-only source is mounted
            ops.repatriate_plan(None)
        assert blocked.value.code.startswith("repatriate.")
        assert ops.repatriate_check(None)["safe_to_unmount"] in {True, False}
        with pytest.raises(CodedRefusal):
            ops.repatriate_apply(None, fingerprint="sha256:0")


def test_probe_reports_a_writable_and_a_missing_folder(settings: Settings, tmp_path) -> None:
    with instance(settings, tmp_path) as inst:
        inst.configure()
        ok = _ops(inst).probe(str(inst.library / "Kavita"))
        assert ok["ok"] is True
        gone = _ops(inst).probe(str(inst.library / "absent"))
        assert gone["ok"] is False and gone["failed_step"]


def test_purge_of_an_unknown_unit_is_a_coded_refusal(settings: Settings, tmp_path) -> None:
    from datetime import UTC, datetime

    from shelfhold.identity.public import Credential, Principal
    from shelfhold.ops.errors import CodedRefusal

    admin = Principal(user_id="1", username="a", role="admin", credential=Credential.LOCAL_CLI)
    with instance(settings, tmp_path) as inst:
        inst.configure()
        with pytest.raises(CodedRefusal) as refused:
            _ops(inst).purge(
                kind="unit",
                anchor_id="01M6Q0XWXW10VPKHV7VNA88WAQ",
                typed="x",
                principal=admin,
                now=datetime.now(UTC),
            )
        assert refused.value.code in {"ops.unknown_subject", "unit.confirmation_required"}


def test_exceptions_of_the_services_become_catalogued_refusals() -> None:
    from shelfhold.composition_errors import KeyringUnavailableError, NoLibraryError
    from shelfhold.export.application.purge import (
        PurgeCollateralError,
        PurgeRefusedError,
        PurgeUnknownError,
    )
    from shelfhold.ops.library import refusal_of

    for error, code in (
        (PurgeUnknownError("x"), "ops.unknown_subject"),
        (PurgeRefusedError("x"), "forbidden"),
        (NoLibraryError("x"), "ops.exit_unavailable"),
        (KeyringUnavailableError("x"), "ops.exit_unavailable"),
    ):
        refusal = refusal_of(error)
        assert refusal is not None and refusal.code == code
    assert refusal_of(PurgeCollateralError.__new__(PurgeCollateralError)) is not None
    assert refusal_of(ValueError("a real failure")) is None
