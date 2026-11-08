"""EF-SRC-025 / D8: the default of `source.check_mode`, and the wake settings of the host."""

import pytest

from shelfhold.config import Settings
from shelfhold.headless.keys import KEYS, effective_check_mode, warning_of


@pytest.mark.req("EF-SRC-025")
def test_check_mode_default_is_always_on_and_piloted_only_with_a_wakehook() -> None:
    assert KEYS["source.check_mode"].default == "always_on"
    assert effective_check_mode(None, wake_configured=False) == "always_on"
    assert effective_check_mode(None, wake_configured=True) == "piloted"  # D8
    assert effective_check_mode("always_on", wake_configured=True) == "always_on"  # explicit wins
    assert effective_check_mode("piloted", wake_configured=False) == "piloted"  # (a) needs no hook


@pytest.mark.req("EF-SRC-025")
def test_piloted_is_no_longer_reported_as_inactive() -> None:
    assert warning_of("source.check_mode", "piloted") is None


@pytest.mark.req("EF-INT-022")
def test_wake_is_configured_only_with_an_address_and_a_tool_behind_it() -> None:
    assert not Settings().wake_configured
    assert not Settings(wake_url="http://sablier:10000").wake_configured  # nothing to wake
    configured = Settings(wake_url="http://sablier:10000", wake_suwayomi=("suwayomi",))
    assert configured.wake_configured
    assert configured.wake_targets() == {"suwayomi": (("suwayomi",), None)}
    group = Settings(wake_url="http://sablier:10000", wake_kavita=("group:reading",))
    assert group.wake_targets() == {"kavita": ((), "reading")}
    assert Settings(wake_token="wake-" + "x" * 24).wake_token is not None


@pytest.mark.req("EF-INT-022")
def test_wake_delay_is_its_own_setting() -> None:
    settings = Settings()
    assert (settings.wake_timeout_s, settings.wake_session_s) == (60, 300)
    assert Settings(wake_timeout_s=120).wake_timeout_s == 120
    with pytest.raises(ValueError, match="wake_timeout_s"):
        Settings(wake_timeout_s=1)
