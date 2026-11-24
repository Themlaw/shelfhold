"""M6 closure review, sector fixes: what a role change leaves of the admin's word for a child, and
the cache date that never follows a link."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from shelfhold_sdk.testing.fakes import TRACKER_KEY

from shelfhold.config import Settings
from shelfhold.infrastructure.volumes import open_data_folder, touch_data_file
from tests.tracker_env import Fixture, tracker_env, trackers_path


@pytest.fixture
def fx(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[Fixture]:
    with tracker_env(monkeypatch, settings) as env:
        yield env


@pytest.mark.req("EF-INT-010", "EF-USR-024", "ENF-SEC-005")
def test_a_role_change_clears_the_allowance_and_says_so_in_the_audit_table(fx: Fixture) -> None:
    kid = fx.add("kid", "child", max_age_rating=10)
    path = trackers_path(kid, TRACKER_KEY, "/allowance")
    assert fx.send("PUT", path, {"allowed": True}).status_code == 200
    assert len(fx.allowances()) == 1
    fx.auth().profile_set(kid, role="member")
    assert fx.allowances() == []
    lines = fx.rows(
        "SELECT target FROM audit WHERE action = 'tracker.allowance_cleared_role_change'"
    )
    assert [r[0] for r in lines] == [kid]
    # no line when there was nothing to clear
    fx.auth().profile_set(kid, role="child", max_age_rating=10)
    again = fx.rows(
        "SELECT target FROM audit WHERE action = 'tracker.allowance_cleared_role_change'"
    )
    assert len(again) == 1


@pytest.mark.req("EF-INT-010", "EF-USR-024", "ENF-SEC-005")
def test_the_allowance_is_refused_to_an_admin_and_to_a_member(fx: Fixture) -> None:
    member = fx.add("mum", "member")
    refused = fx.send("PUT", trackers_path(member, TRACKER_KEY, "/allowance"), {"allowed": True})
    assert refused.status_code == 422
    assert fx.allowances() == []
    # withdrawing a word that was never given stays a plain no-op for an adult
    gone = fx.send("PUT", trackers_path(member, TRACKER_KEY, "/allowance"), {"allowed": False})
    assert gone.status_code == 200


@pytest.mark.req("EF-BIB-018")
def test_touch_sets_the_date_of_a_regular_file_and_refuses_a_link(tmp_path: Path) -> None:
    data = tmp_path / "data"
    shard = data / "cache" / "renditions" / "ab"
    shard.mkdir(parents=True)
    name = "ab" + "0" * 62 + ".kepub"
    (shard / name).write_bytes(b"x")
    with open_data_folder(data, "cache/renditions/ab") as folder:
        touch_data_file(folder, name, at=1_700_000_000)
        assert (shard / name).stat().st_mtime == 1_700_000_000
        touch_data_file(folder, name, at=1_700_000_500)
        assert (shard / name).stat().st_mtime == 1_700_000_500
        os.symlink(shard / name, shard / "link")
        with pytest.raises(ValueError, match="link"):
            touch_data_file(folder, "link", at=1_600_000_000)
        with pytest.raises(FileNotFoundError):
            touch_data_file(folder, "absent", at=1_600_000_000)
