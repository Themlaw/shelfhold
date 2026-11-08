"""EF-INT-022, EF-EXP-055, ENF-OPS-011 (`kavita_asleep_sablier`): a Kavita behind Sablier.

`scan-folder` and the access audit wake it first. A Kavita that cannot be woken defers the batch:
no `target.unreachable`, no write, and the parental barrier of the audit is never lifted by it.
"""

import pytest

from shelfhold.export.targets.kavita import AdminKey, KavitaClient
from shelfhold.export.targets.kavita.audit import AccessAuditor
from shelfhold.export.targets.kavita.errors import TargetAsleep, TargetError
from shelfhold.infrastructure.schema import export
from shelfhold.infrastructure.wake import WakeDeferred
from tests.export_env import ExportEnv, export_env
from tests.fakes.kavita import API_KEY, BASE_URL, build_fetcher, member
from tests.kavita_env import run
from tests.kavita_export_env import LIBRARY_ID, Wired, wire
from tests.wake_env import RecordingHook, make_wakes

TARGET = "kavita:t1"


def _woken(w: Wired, hook: RecordingHook) -> KavitaClient:
    """The client of the wired target, with its wake delegated to the coordinator."""
    wakes = make_wakes(w.env.engine, w.env.clock, hook, frozenset({TARGET}))

    async def wake() -> None:
        await wakes.ensure(TARGET)

    return KavitaClient(
        build_fetcher(w.fake), BASE_URL, AdminKey(API_KEY), events=w.events, clock=w.env.clock,
        wake=wake,
    )  # fmt: skip


def _paused(env: ExportEnv) -> str | None:
    reason: str | None = env.rows(export.shelf)[0].paused_reason
    return reason


@pytest.mark.req("EF-INT-022", "EF-EXP-055")
def test_kavita_asleep_woken_before_scan_folder() -> None:
    with export_env() as env:
        w = wire(env)
        hook = RecordingHook(starts={TARGET: w.fake.wake_up})
        w.fake.asleep = True
        client = _woken(w, hook)
        run(client.scan_folder("/manga/Series A"))
        assert hook.log == [f"wake:{TARGET}"] and w.fake.scans == ["/manga/Series A"]
        assert w.events.codes() == []  # no target.unreachable while it was starting


@pytest.mark.req("EF-INT-022", "ENF-OPS-011")
def test_kavita_asleep_wake_timeout_defers_without_unreachable_or_write() -> None:
    with export_env() as env:
        w = wire(env)
        hook = RecordingHook(timeout=True)
        w.fake.asleep = True
        client = _woken(w, hook)
        with pytest.raises(TargetAsleep) as raised:
            run(client.scan_folder("/manga/Series A"))
        assert isinstance(raised.value, TargetError) and raised.value.code == "wake.deferred"
        assert w.events.codes() == []  # not target.unreachable
        assert w.fake.requests == [] and w.fake.writes == []  # nothing reached Kavita


@pytest.mark.req("EF-INT-022", "EF-EXP-055")
def test_kavita_asleep_audit_woken_then_reads_the_accounts() -> None:
    with export_env() as env:
        w = wire(env)
        hook = RecordingHook(starts={TARGET: w.fake.wake_up})
        w.fake.asleep = True
        client = _woken(w, hook)
        auditor = AccessAuditor(env.engine, env.clock, w.events)
        report = run(auditor.audit(client, env.target_id))
        assert not report.failed and hook.count("wake") == 1
        assert w.fake.requests_to("/api/Users")


@pytest.mark.req("EF-EXP-055", "ENF-OPS-011")
def test_kavita_asleep_never_bypasses_the_parental_barrier() -> None:
    with export_env() as env:
        w = wire(env)
        hook = RecordingHook(timeout=True)
        w.fake.asleep = True
        w.fake.users.append(member("kid", libraries=(LIBRARY_ID,), age_rating=8))
        client = _woken(w, hook)
        auditor = AccessAuditor(env.engine, env.clock, w.events)
        report = run(auditor.audit(client, env.target_id))
        assert report.failed and report.paused_shelves == [env.shelf_id]  # fail closed
        assert _paused(env) == "child_exposure"
        assert w.fake.writes == [] and "target.unreachable" not in w.events.codes()


@pytest.mark.req("EF-INT-022")
def test_wake_deferred_is_not_a_kavita_unreachable() -> None:
    assert issubclass(TargetAsleep, TargetError) and issubclass(WakeDeferred, Exception)
