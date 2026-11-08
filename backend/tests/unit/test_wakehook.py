"""EF-INT-022: the WakeHook port and its Sablier adapter, against a simulated Sablier."""

import asyncio
from datetime import timedelta

import pytest
import structlog

from shelfhold.integrations.sablier import SablierHook, SablierTarget
from shelfhold.sdk.connected import WakeError, WakeHook, WakeTimeout
from tests.fakes.sablier import BASE_URL, FakeSablier

TARGETS = {
    "suwayomi": SablierTarget(names=("suwayomi",)),
    "kavita": SablierTarget(group="reading"),
}


def hook_for(fake: FakeSablier, **options: object) -> SablierHook:
    return SablierHook(
        fake.fetcher(),
        BASE_URL,
        TARGETS,
        session_duration=timedelta(minutes=5),
        timeout=timedelta(seconds=30),
        **options,
    )  # type: ignore[arg-type]


@pytest.mark.req("EF-INT-022")
def test_sablier_hook_is_a_wakehook_and_wakes_by_name_or_group() -> None:
    fake = FakeSablier()
    hook: WakeHook = hook_for(fake)
    asyncio.run(hook.wake("suwayomi"))
    asyncio.run(hook.wake("kavita"))
    first, second = fake.calls
    assert first.path == "/api/strategies/blocking" and first.names == ["suwayomi"]
    assert first.session_duration == "300s" and first.timeout == "30s"  # the wake delay is its own
    assert second.group == "reading" and second.names == []


@pytest.mark.req("EF-INT-022")
def test_sablier_hook_renew_asks_for_the_same_session() -> None:
    fake = FakeSablier()
    hook = hook_for(fake)
    asyncio.run(hook.wake("suwayomi"))
    asyncio.run(hook.renew("suwayomi"))
    assert [c.names for c in fake.calls] == [["suwayomi"], ["suwayomi"]]


@pytest.mark.req("EF-INT-022")
def test_sablier_hook_not_ready_in_time_is_a_timeout() -> None:
    fake = FakeSablier(slow=True)
    with pytest.raises(WakeTimeout):
        asyncio.run(hook_for(fake).wake("suwayomi"))


@pytest.mark.req("EF-INT-022")
def test_sablier_hook_unknown_target_and_unreachable_sablier() -> None:
    with pytest.raises(WakeError):
        asyncio.run(hook_for(FakeSablier()).wake("lncrawl"))
    with pytest.raises(WakeError) as raised:
        asyncio.run(hook_for(FakeSablier(unreachable=True)).wake("suwayomi"))
    assert not isinstance(raised.value, WakeTimeout)


@pytest.mark.req("EF-INT-022", "ENF-SEC-001")
def test_sablier_call_goes_through_the_fetcher_policy_no_private_redirect() -> None:
    fake = FakeSablier(redirect_to="http://10.0.0.5:10000/api/strategies/blocking")
    with pytest.raises(WakeError):
        asyncio.run(hook_for(fake).wake("suwayomi"))
    assert len(fake.calls) == 1  # the redirection to an undeclared private address was refused


@pytest.mark.req("EF-INT-022", "ENF-SEC-016")
def test_sablier_token_never_reaches_logs_or_errors() -> None:
    secret = "wake-" + "x" * 24  # built at run time: no literal token in the source
    fake = FakeSablier(unreachable=True)
    hook = hook_for(fake, headers={"Authorization": f"Bearer {secret}"})
    with structlog.testing.capture_logs() as logs, pytest.raises(WakeError) as raised:
        asyncio.run(hook.wake("suwayomi"))
    assert secret not in repr(logs) and secret not in str(raised.value) and secret not in repr(hook)
    ok = FakeSablier()
    asyncio.run(hook_for(ok, headers={"Authorization": f"Bearer {secret}"}).wake("suwayomi"))
    assert ok.calls[0].headers["authorization"] == f"Bearer {secret}"  # sent, never shown
