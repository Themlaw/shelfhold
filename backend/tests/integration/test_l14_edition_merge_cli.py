"""L14 (EF-BIB-049, EF-BIB-054 (4)): `shelfhold edition merge` on a real instance. The plan is
shown and changes nothing; the merge acts for its fingerprint only; a Unit that would take the
place of a purged one is refused and reported, with nothing deleted; the action is reserved (never
a key, never a stale session); the audit entry names no title. The tombstone is laid by the real
`purge --now` of the host CLI, over the real channel."""

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
from shelfhold.infrastructure.schema import catalog, identity, platform
from shelfhold.ops.edition_merge import EditionMergeOps
from shelfhold.ops.errors import CodedRefusal, StalePreviewError
from tests.auth_env import run_cli
from tests.integration.test_schema_catalog import add_edition, add_unit
from tests.l50_env import Seeded, seeded

pytestmark = pytest.mark.req("EF-BIB-049", "EF-BIB-054")

ADMIN = Principal("1000", "uid:1000", "admin", Credential.LOCAL_CLI)


class World:
    def __init__(self, env: Seeded, monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
        self.env = env
        self.series = env.new_series("Un roman")
        self.keep = self.series.edition_id
        purged = env.add_chapter(self.series, "5")
        with env.inst.engine.begin() as connection:
            self.donor = add_edition(
                connection, self.series.work_id, lang="fr", publisher="Beta", rated_at=None
            )
            self.hit = add_unit(connection, self.donor, "5")  # the survivor purged this place
            self.free = add_unit(connection, self.donor, "6")
        assert run_cli(monkeypatch, settings, "purge", "--now", "--unit", purged,
                       "--confirm", purged) == 0  # fmt: skip

    def units(self) -> dict[str, str]:
        return {r.id: r.edition_id for r in self.env.rows(catalog.unit)}


def test_merge_plan_then_confirmation_through_the_cli(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with seeded(settings, tmp_path / "inst") as env:
        world = World(env, monkeypatch, settings)
        before = world.units()
        capsys.readouterr()
        # chapter 5 of the donor would take the purged place: refused, reported, nothing changes
        assert run_cli(monkeypatch, settings, "edition", "merge", world.keep, world.donor) == 1
        out = capsys.readouterr()
        assert "effacée" in out.out and "Ce plan ne peut pas" in out.err
        assert world.units() == before
        assert {r.id for r in env.rows(catalog.edition)} == {world.keep, world.donor}
        hits = [e for e in env.rows(platform.event) if e.type == "tombstone.migration_hit"]
        assert [e.payload for e in hits] == [{"scope": "place"}]
        assert (
            run_cli(
                monkeypatch, settings, "edition", "merge", world.keep, world.donor,
                "--apply", "0" * 16,
            )
            != 0
        )  # fmt: skip
        assert world.units() == before

        # the survivor lets the colliding Unit go (purged for the donor's chapter too): free again
        with env.inst.engine.begin() as connection:
            connection.execute(sa.delete(catalog.unit).where(catalog.unit.c.id == world.hit))
        capsys.readouterr()
        assert run_cli(monkeypatch, settings, "edition", "merge", world.keep, world.donor) == 0
        shown = capsys.readouterr().out
        fingerprint = next(w for w in shown.split() if len(w) == 16 and w.isalnum())
        assert world.units() == {k: v for k, v in before.items() if k != world.hit}
        late = []
        with env.inst.engine.begin() as connection:
            late.append(add_unit(connection, world.donor, "7"))  # the plan moved: stale
        assert (
            run_cli(
                monkeypatch, settings, "edition", "merge", world.keep, world.donor,
                "--apply", fingerprint,
            )
            == 4
        )  # fmt: skip
        capsys.readouterr()
        run_cli(monkeypatch, settings, "edition", "merge", world.keep, world.donor)
        fingerprint = next(
            w for w in capsys.readouterr().out.split() if len(w) == 16 and w.isalnum()
        )
        assert (
            run_cli(
                monkeypatch, settings, "edition", "merge", world.keep, world.donor,
                "--apply", fingerprint,
            )
            == 0
        )  # fmt: skip
        merged = world.units()
        assert {merged[world.free], merged[late[0]]} == {world.keep}
        audit = [r for r in env.rows(identity.audit) if r.action == "edition.merge"]
        assert len(audit) == 1 and "Un roman" not in str(audit[0].target)
        assert str(audit[0].actor_id).startswith("local_cli:")


def test_merge_is_a_reserved_action_never_for_a_key_or_a_stale_session(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    from shelfhold.interfaces.local_cli.ops import OPS_METHODS

    action = RESERVED_ACTIONS["edition.merge"]
    for method in ("ops.edition_merge", "ops.edition_merge_proposals"):
        assert OPS_METHODS[method][1] == "edition.merge"
    key = Principal("u", "admin", "admin", Credential.API_KEY, key_id="k")
    stale = Principal("u", "admin", "admin", Credential.SESSION, session_id="s")
    now = datetime(2026, 11, 6, tzinfo=UTC)
    assert isinstance(authorize_reserved(key, action, now=now), CredentialNotAllowed)
    assert isinstance(authorize_reserved(stale, action, now=now), ReauthRequired)
    with seeded(settings, tmp_path / "inst") as env:
        world = World(env, monkeypatch, settings)
        before = world.units()
        ops = EditionMergeOps(env.inst.comp)
        for who in (key, stale):
            with pytest.raises(CodedRefusal):
                ops.merge(world.keep, world.donor, confirm="0" * 16, principal=who)
            with pytest.raises(CodedRefusal):
                ops.proposals(world.keep, world.donor, principal=who)
        assert world.units() == before
        with pytest.raises(StalePreviewError):  # an admin, but not for this plan
            ops.merge(world.keep, world.donor, confirm="0" * 16, principal=ADMIN)
