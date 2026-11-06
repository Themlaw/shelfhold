"""L55 (EF-BIB-054 (3), (4), ENF-OPS-021, EF-INT-014): `shelfhold normalize migrate`.

A real instance. The tombstone is laid by the real `purge --now` of the host CLI; the new version
of the normalizer is `number@2`, declared in the code; the command is the real one, over the real
channel. The chapter that was purged is `1500`;
`number@2` reads "1,500" as 1500, so the live chapter "1,500" would land on the purged place.
"""

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.catalog.public import NormalizerRegistry, default_registry, parse_number
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
from shelfhold.infrastructure.schema import catalog, identity, platform
from shelfhold.ops.errors import StalePreviewError
from shelfhold.ops.normalize import NormalizeOps
from tests.auth_env import run_cli
from tests.l50_env import Seeded, Series, seeded

pytestmark = pytest.mark.req("EF-BIB-054", "ENF-OPS-021", "EF-INT-014")


def live_unit(env: Seeded, series: Series, raw: str, norm: str) -> str:
    unit_id = new_ulid()
    with env.inst.engine.begin() as connection:
        connection.execute(
            sa.insert(catalog.unit).values(
                id=unit_id, edition_id=series.edition_id, number_raw=raw, number_norm=norm,
                number_norm_by="number@1", number_sort=float(norm), track="main", kind="chapter",
            )
        )  # fmt: skip
    return unit_id


def stored(env: Seeded) -> dict[str, tuple[str, str | None]]:
    return {r.id: (r.number_norm, r.number_norm_by) for r in env.rows(catalog.unit)}


def fingerprint_of(out: str) -> str:
    return next(line.split(": ")[1].strip() for line in out.splitlines() if "Empreinte" in line)


class World:
    """The purged chapter (1500) and five live ones, every one stored under `number@1`."""

    def __init__(self, env: Seeded, monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
        series = self.series = env.new_series()
        purged = env.add_chapter(series, "1500")
        self.hit = live_unit(env, series, "1,500", "1.5")  # under @2: 1500, the purged place
        self.merged = live_unit(env, series, "2,000", "2")  # under @2: 2000, taken by `occupant`
        self.occupant = live_unit(env, series, "2000", "2000")
        self.clean = live_unit(
            env, series, "4,25", "4.25"
        )  # under @2: still 4.25, only the version moves
        self.same = live_unit(env, series, "7", "7")  # under @2: 7, only the version moves
        assert run_cli(monkeypatch, settings, "purge", "--now", "--unit", purged,
                       "--confirm", purged) == 0  # fmt: skip
        self.before = stored(env)
        assert purged not in self.before


def thousands_aware(raw: str) -> str:
    """Test-only `number@2`: a comma that groups exactly three digits is a thousands separator."""
    return parse_number(re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", raw)).norm


def registry_with_v2() -> NormalizerRegistry:
    """A release that would ship `number@2`: a test's own, handed in by parameter."""
    registry = default_registry()
    registry.register("number", 2, thousands_aware)
    return registry


ADMIN = Principal("1000", "uid:1000", "admin", Credential.LOCAL_CLI)


def ids(report: dict[str, Any], key: str) -> set[str]:
    return {r["id"] for r in report[key]}


def test_production_registry_has_nothing_to_migrate_on_a_fresh_database(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    """Without a newer normalizer in the code, the command proposes nothing, whatever the rows."""
    with seeded(settings, tmp_path / "inst") as env:
        world = World(env, monkeypatch, settings)
        capsys.readouterr()
        assert run_cli(monkeypatch, settings, "normalize", "migrate", "number") == 0
        out = capsys.readouterr().out
        assert "number@1" in out and "migrate :" not in out and "merge :" not in out
        assert stored(env) == world.before
        report = NormalizeOps(env.inst.comp).migrate("number", confirm=None, principal=ADMIN)
        assert not any(report[k] for k in ("migrate", "merge", "blocked", "unresolved"))
        assert str(default_registry().current("number")) == "number@1"


def test_normalize_migrate_preview_intent_blocks_tombstone_hit(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    with seeded(settings, tmp_path / "inst") as env:
        world = World(env, monkeypatch, settings)
        ops = NormalizeOps(env.inst.comp, registry_with_v2)

        # the preview writes nothing, names what would happen and the one thing it blocks
        report = ops.migrate("number", confirm=None, principal=ADMIN)
        assert report["target"] == "number@2"
        assert ids(report, "blocked") == {world.hit}  # by a tombstone, named by id, no title
        assert ids(report, "merge") == {world.merged}  # taken by `occupant`
        assert ids(report, "migrate") == {world.clean, world.same, world.occupant}
        assert report["collisions"] == [sorted([world.merged, world.occupant])]
        assert stored(env) == world.before
        fingerprint = report["fingerprint"]
        hits = [e for e in env.rows(platform.event) if e.type == "tombstone.migration_hit"]
        assert [e.payload for e in hits] == [{"scope": "place"}]  # told once, with no title

        # a fingerprint that is not the previewed one changes nothing
        with pytest.raises(StalePreviewError):
            ops.migrate("number", confirm="0" * 64, principal=ADMIN)
        assert stored(env) == world.before

        # a row that appears after the preview changes the plan: the old fingerprint moves nothing
        late = live_unit(env, world.series, "9,000", "9")
        with pytest.raises(StalePreviewError):
            ops.migrate("number", confirm=fingerprint, principal=ADMIN)
        assert stored(env)[late] == ("9", "number@1")
        fingerprint = ops.migrate("number", confirm=None, principal=ADMIN)["fingerprint"]

        # the confirmed migration moves what is safe and only that
        done = ops.migrate("number", confirm=fingerprint, principal=ADMIN)
        assert done["applied"] == 4
        after = stored(env)
        assert after[world.clean] == ("4.25", "number@2")
        assert after[world.same] == ("7", "number@2")
        assert after[world.occupant] == ("2000", "number@2")  # only the version moves
        for held in (world.hit, world.merged):  # blocked by a tombstone, a merge to decide
            assert after[held] == world.before[held]
        audit = [r for r in env.rows(identity.audit) if r.action == "normalize.migrate"]
        assert [a.target for a in audit] == [f"number@2;fp:{fingerprint[:16]};rows:4"]
        assert str(audit[0].actor_id).startswith("local_cli:")

        # run again: what was held is still held, nothing else is left to do
        again = ops.migrate("number", confirm=None, principal=ADMIN)
        assert ids(again, "blocked") == {world.hit} and ids(again, "merge") == {world.merged}
        assert world.clean not in ids(again, "migrate")


def test_migrating_a_normalizer_that_does_not_exist_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with seeded(settings, tmp_path / "inst") as env:
        world = World(env, monkeypatch, settings)
        capsys.readouterr()
        assert run_cli(monkeypatch, settings, "normalize", "migrate", "no_such_thing") != 0
        assert stored(env) == world.before


def test_normalize_migrate_is_a_reserved_action_never_for_a_key_or_a_stale_session(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    from shelfhold.interfaces.local_cli.ops import OPS_METHODS
    from shelfhold.ops.errors import CodedRefusal
    from shelfhold.ops.normalize import NormalizeOps

    action = RESERVED_ACTIONS["normalize.migrate"]
    assert OPS_METHODS["ops.normalize_migrate"][1] == "normalize.migrate"
    key = Principal("u", "admin", "admin", Credential.API_KEY, key_id="k")
    stale = Principal("u", "admin", "admin", Credential.SESSION, session_id="s")
    now = datetime(2026, 11, 6, tzinfo=UTC)
    assert isinstance(authorize_reserved(key, action, now=now), CredentialNotAllowed)
    assert isinstance(authorize_reserved(stale, action, now=now), ReauthRequired)

    with seeded(settings, tmp_path / "inst") as env:
        world = World(env, monkeypatch, settings)
        ops = NormalizeOps(env.inst.comp)
        for who in (key, stale):
            with pytest.raises(CodedRefusal):
                ops.migrate("number", confirm="0" * 64, principal=who)
        assert stored(env) == world.before
