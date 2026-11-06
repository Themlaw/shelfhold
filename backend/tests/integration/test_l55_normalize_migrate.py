"""L55 (EF-BIB-054 (3), (4), ENF-OPS-021, EF-INT-014): `shelfhold normalize migrate`.

A real instance. The tombstone is laid by the real `purge --now` of the host CLI; the new version
of the normalizer is `number@2`, declared in the code; the command is the real one, over the real
channel. The chapter that was purged is `1500`;
`number@2` reads "1,500" as 1500, so the live chapter "1,500" would land on the purged place.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa

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
from tests.auth_env import run_cli
from tests.l50_env import Seeded, Series, seeded

pytestmark = pytest.mark.req("EF-BIB-054", "ENF-OPS-021", "EF-INT-014")
STALE_PREVIEW = 4


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
        series = env.new_series()
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


def test_normalize_migrate_preview_intent_blocks_tombstone_hit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with seeded(settings, tmp_path / "inst") as env:
        world = World(env, monkeypatch, settings)
        capsys.readouterr()

        # the preview writes nothing, names what would happen and the one thing it blocks
        assert run_cli(monkeypatch, settings, "normalize", "migrate", "number") == 0
        out = capsys.readouterr().out
        assert "number@2" in out
        assert f"blocked : {world.hit}" in out  # by a tombstone, named by identifier, no title
        assert f"merge : {world.merged}" in out  # taken by `occupant`: a decision for a person
        assert (
            world.merged in out.split("Même place")[1]
            and world.occupant in out.split("Même place")[1]
        )
        assert f"migrate : {world.clean}" in out and f"migrate : {world.same}" in out
        assert stored(env) == world.before
        fingerprint = fingerprint_of(out)
        hits = [e for e in env.rows(platform.event) if e.type == "tombstone.migration_hit"]
        assert [e.payload for e in hits] == [{"scope": "place"}]  # told once, with no title

        # a fingerprint that is not the previewed one changes nothing
        assert run_cli(monkeypatch, settings, "normalize", "migrate", "number",
                       "--confirm", "0" * 64) == STALE_PREVIEW  # fmt: skip
        assert stored(env) == world.before

        # the confirmed migration moves what is safe and only that
        assert run_cli(monkeypatch, settings, "normalize", "migrate", "number",
                       "--confirm", fingerprint) == 0  # fmt: skip
        after = stored(env)
        assert after[world.clean] == ("4.25", "number@2")
        assert after[world.same] == ("7", "number@2")
        assert after[world.occupant] == (
            "2000",
            "number@2",
        )  # its value is unchanged: only the version moves
        for held in (world.hit, world.merged):  # blocked by a tombstone, a merge to decide
            assert after[held] == world.before[held]
        assert "3 ligne(s) migrée(s)" in capsys.readouterr().out
        audit = [r for r in env.rows(identity.audit) if r.action == "normalize.migrate"]
        assert [(a.target) for a in audit] == ["number@2"]
        assert str(audit[0].actor_id).startswith("local_cli:")

        # run again: what was held is still held, nothing else is left to do
        assert run_cli(monkeypatch, settings, "normalize", "migrate", "number") == 0
        again = capsys.readouterr().out
        assert f"blocked : {world.hit}" in again and f"merge : {world.merged}" in again
        assert f"migrate : {world.clean}" not in again


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
