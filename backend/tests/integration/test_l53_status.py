"""L53: what `shelfhold status` shows besides the waits. Every test runs the command itself over an
instance built by the production services: the keyring on disk, the follow cycle, a real purge, the
runtime boot that probes the roots."""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.composition import Composition
from shelfhold.config import Settings, load_settings
from shelfhold.export.domain.kavita_compat import (
    KNOWN_BAD_LISTED_ON,
    KNOWN_BAD_VERSIONS,
    MINIMUM_VERSION,
    known_bad_age,
)
from shelfhold.infrastructure.clock import SystemClock
from shelfhold.infrastructure.fsops import ConfinedRoot, Link, new_probe_name
from shelfhold.infrastructure.fsops.capabilities import probe_root
from shelfhold.infrastructure.migrate import migrate
from shelfhold.infrastructure.secrets.diagnostics import boot_check
from shelfhold.runtime_jobs import follow_handler
from tests.auth_env import run_cli
from tests.integration.test_l52_ops_events import boot_runtime, end, tree_of
from tests.integration.test_l54_adopt import SERIES, _adopt_mixed, _pair
from tests.l41_env import context, instance
from tests.l50_env import seeded
from tests.l53_env import age_discoveries, status_json, status_text
from tests.l54_env import Bench54, Pools, bench54

# -- ENF-SEC-005: the master key inside /data -------------------------------------------------


@pytest.mark.req("ENF-SEC-005")
def test_status_warns_master_key_inside_data(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    inside = load_settings(
        {"SHELFHOLD_DATA_DIR": str(tmp_path / "data"), "SHELFHOLD_STORE_DIR": str(tmp_path / "s")}
    ).values
    migrate(inside)
    boot_check(inside, announce=False)  # the keyring is generated in /data, as on a first start
    codes = [w["code"] for w in status_json(monkeypatch, inside, capsys)["warnings"]]
    assert "keyring.key_in_data" in codes
    text = status_text(monkeypatch, inside, capsys)
    assert "/data" in text and "SHELFHOLD_SECRET_KEY_FILE" in text
    # nothing of the key itself is ever printed
    material = (inside.data_dir / "keyring").read_bytes()
    assert material.decode("latin-1") not in text

    outside_file = tmp_path / "keys" / "keyring"
    outside_file.parent.mkdir(mode=0o700)
    shutil.copyfile(inside.data_dir / "keyring", outside_file)
    outside_file.chmod(0o400)
    outside = load_settings(
        {
            "SHELFHOLD_DATA_DIR": str(tmp_path / "data"),
            "SHELFHOLD_STORE_DIR": str(tmp_path / "s"),
            "SHELFHOLD_SECRET_KEY_FILE": str(outside_file),
        }
    ).values
    monkeypatch.setenv("SHELFHOLD_SECRET_KEY_FILE", str(outside_file))
    codes = [w["code"] for w in status_json(monkeypatch, outside, capsys)["warnings"]]
    assert "keyring.key_in_data" not in codes


# -- EF-SRC-026 (c): the series that went silent ----------------------------------------------


def _paired(b, capsys) -> str:  # type: ignore[no-untyped-def]
    series_id, edition = _adopt_mixed(b, capsys)
    b.upstream({n: f"Chapter {n}" for n in range(1, 21)})
    assert _pair(b, series_id, "--follow-from", "12") == 0
    b.follow()
    return str(edition)


def _restart(b: Bench54) -> Callable[[], None]:
    """The follow cycle of a server that has just started: a new composition, the same disk."""
    comp = Composition(b.settings, b.engine, clock=SystemClock(), network=b.env.inst.network)

    def cycle() -> None:
        follow_handler(comp, Pools())(context("follow.cycle"))  # type: ignore[arg-type]

    return cycle


@pytest.mark.req("EF-SRC-026")
def test_series_stale_m1a_default_k3(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    from tests.l52_env import events_of

    with bench54(monkeypatch, settings, tmp_path) as b:
        edition = _paired(b, capsys)
        assert status_json(monkeypatch, settings, capsys)["stale_series"] == []
        # a cadence of ten days, the last chapter seen 20 days ago: 3 x 10 = 30, not silent yet
        age_discoveries(b, edition, [40, 30, 20])
        assert status_json(monkeypatch, settings, capsys)["stale_series"] == []
        # the last one 31 days ago: past 3 x the cadence, silent
        age_discoveries(b, edition, [51, 41, 31])
        listed = status_json(monkeypatch, settings, capsys)["stale_series"]
        assert [s["edition_id"] for s in listed] == [edition]
        assert listed[0]["threshold_days"] == 30 and listed[0]["silent_days"] >= 31
        assert SERIES in status_text(monkeypatch, settings, capsys)
        # the cycle announces it once, aggregated: one event whatever the cycles that follow
        b.follow()
        b.follow()
        assert len(events_of(b.engine, "series.stale")) == 1
        # no measurable cadence (one instant): the floor of 14 days
        age_discoveries(b, edition, [13])
        assert status_json(monkeypatch, settings, capsys)["stale_series"] == []
        age_discoveries(b, edition, [15])
        assert len(status_json(monkeypatch, settings, capsys)["stale_series"]) == 1
        # "paused" by the admin: the series is not checked, hence never silent
        with b.engine.begin() as connection:
            connection.execute(sa.text("UPDATE series_follow SET check_override = 'paused'"))
        assert status_json(monkeypatch, settings, capsys)["stale_series"] == []


# -- EF-BIB-053 (2): the series a purge buried, still in Suwayomi -----------------------------


@pytest.mark.req("EF-BIB-053")
def test_status_lists_tombstoned_upstream_series(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    from tests.l52_env import events_of

    with bench54(monkeypatch, settings, tmp_path) as b:
        edition = _paired(b, capsys)
        with b.engine.begin() as connection:
            work = connection.execute(
                sa.text("SELECT work_id FROM edition WHERE id = :e"), {"e": edition}
            ).scalar_one()
        assert status_json(monkeypatch, settings, capsys)["tombstoned_upstream"] == []
        # the purge of the Work is the real command: the tombstone is sealed, the files go
        purge = ("purge", "--now", "--work", work, "--confirm", work)
        assert run_cli(monkeypatch, settings, *purge) == 0
        # the server restarts after the purge (the CLI ran on its own): a fresh composition
        # replays the tombstones, and its follow cycle meets the series again in Suwayomi
        cycle = _restart(b)
        cycle()
        listed = status_json(monkeypatch, settings, capsys)["tombstoned_upstream"]
        assert len(listed) == 1 and listed[0]["title"]
        text = status_text(monkeypatch, settings, capsys)
        assert listed[0]["title"] in text and "Suwayomi" in text
        assert events_of(b.engine, "series.tombstoned_upstream")
        cycle()  # listed as long as the series is there, once whatever the number of cycles
        assert len(status_json(monkeypatch, settings, capsys)["tombstoned_upstream"]) == 1


# -- EF-EXP-059: what each root can do --------------------------------------------------------


@pytest.mark.req("EF-EXP-059")
def test_status_shows_per_root_reflink_and_hardlink_capabilities(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with instance(settings, tmp_path) as inst:
        inst.configure()
        end(boot_runtime(inst))  # the probes run at the start of the server
        report = status_json(monkeypatch, settings, capsys)
        by_root = {c["root"]: c for c in report["capabilities"]}
        assert str(inst.library) in by_root
        caps = by_root[str(inst.library)]
        assert caps["reflink"] in (True, False)
        assert caps["hardlink_store"] in ("yes", "no")  # /store is mounted here
        assert caps["inodes_stable"] is True
        assert caps["copy_fallback"] == (not caps["reflink"] and caps["hardlink_store"] != "yes")
        text = status_text(monkeypatch, settings, capsys)
        line = next(x for x in text.splitlines() if x.startswith(f"Racine {inst.library}"))
        assert "reflink" in line and "lien dur vers /store" in line and "inodes stables" in line


@pytest.mark.req("EF-EXP-059")
def test_hardlink_to_store_is_not_applicable_without_store(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    """M1a is pass-through mode: there is no `/store`, and `status` says so instead of "no"."""
    with instance(settings, tmp_path) as inst:
        inst.configure()
        shutil.rmtree(settings.store_dir)
        end(boot_runtime(inst))
        report = status_json(monkeypatch, settings, capsys)
        (caps,) = [c for c in report["capabilities"] if c["root"] == str(inst.library)]
        assert caps["hardlink_store"] == "not_applicable" and caps["store_reason"]
        assert "non applicable" in status_text(monkeypatch, settings, capsys)


@pytest.mark.req("EF-EXP-059")
def test_capability_probe_leaves_nothing_on_the_root(settings: Settings, tmp_path: Path) -> None:
    with instance(settings, tmp_path) as inst:
        inst.configure()
        end(boot_runtime(inst))
        before = tree_of(inst.library)
        end(boot_runtime(inst))  # a second start probes again
        assert tree_of(inst.library) == before
        assert not [p for p in before if ".shelfhold-probe-" in p]
        assert not [p for p in tree_of(settings.store_dir) if ".shelfhold-probe-" in p]


SHM = Path("/dev/shm")  # noqa: S108 (a second, real file system for the EXDEV case)


@pytest.mark.req("EF-EXP-059")
@pytest.mark.skipif(not SHM.is_dir(), reason="needs a second file system")
def test_hardlink_to_a_store_on_another_file_system_is_refused(tmp_path: Path) -> None:
    """A real `EXDEV`: the root on the temporary folder, the store on `/dev/shm`."""
    other = SHM
    if other.stat().st_dev == tmp_path.stat().st_dev:
        pytest.skip("the temporary folder is on /dev/shm")
    root_dir = tmp_path / "root"
    root_dir.mkdir()
    store_dir = Path(os.path.join(other, f"shelfhold-l53-{new_probe_name()[-8:]}"))
    store_dir.mkdir()
    try:
        with ConfinedRoot.open(root_dir) as root, ConfinedRoot.open(store_dir) as store:
            found = probe_root(root, store=store, filesystem="test", now="t")
            assert found.hardlink_store is Link.NO and found.copy_fallback in (True, False)
            assert found.inodes_stable is True
            assert not list(root_dir.iterdir()) and not list(store_dir.iterdir())
            alone = probe_root(root, store=None, filesystem="test", now="t", store_reason="none")
            assert alone.hardlink_store is Link.NOT_APPLICABLE and alone.store_reason == "none"
            assert not list(root_dir.iterdir())
    finally:
        shutil.rmtree(store_dir, ignore_errors=True)


# -- ENF-COMP-001: the age of the known_bad_versions list -------------------------------------


@pytest.mark.req("ENF-COMP-001")
def test_status_shows_known_bad_versions_age(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with seeded(settings, tmp_path):
        report = status_json(monkeypatch, settings, capsys)
        known = report["known_bad"]
        assert known["listed_on"] == KNOWN_BAD_LISTED_ON.isoformat()
        assert known["age_days"] == known_bad_age(date.today()).days
        assert known["minimum_version"] == str(MINIMUM_VERSION) == "0.9.1"
        advisories = {k.advisory for k in KNOWN_BAD_VERSIONS}
        assert {e["advisory"] for e in known["entries"]} == advisories
        text = status_text(monkeypatch, settings, capsys)
        assert re.search(
            rf"liste des versions vulnérables.*{KNOWN_BAD_LISTED_ON.isoformat()}", text
        )
        assert known["outdated"] is (known["age_days"] > 120)


@pytest.mark.req("ENF-COMP-001")
def test_known_bad_list_age_is_never_negative_and_flags_an_old_list() -> None:
    assert known_bad_age(KNOWN_BAD_LISTED_ON - timedelta(days=3)) == timedelta(0)
    assert known_bad_age(KNOWN_BAD_LISTED_ON + timedelta(days=200)).days == 200


@pytest.mark.req("ENF-COMP-001")
def test_known_bad_target_version_is_listed_with_its_advisories(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    from shelfhold.export.application.target_state import load_health, save_health

    with seeded(settings, tmp_path) as env:
        with env.inst.engine.begin() as connection:
            target = connection.execute(sa.text("SELECT id FROM export_target")).scalar_one()
            health = load_health(connection, target)
            health.version = "0.8.9"
            health.warnings = (KNOWN_BAD_VERSIONS[0].advisory,)
            save_health(connection, target, health)
        known = status_json(monkeypatch, settings, capsys)["known_bad"]
        assert known["targets"][0]["advisories"] == [KNOWN_BAD_VERSIONS[0].advisory]
        assert "0.8.9" in status_text(monkeypatch, settings, capsys)
