"""M2 wiring (L26, EF-BIB-044): `shelfhold coverage resplit propose|apply --confirm` on a real
instance. A chapter the source cut again (10 into 10.1 and 10.2) is a proposal that the admin
confirms with the code it printed; the code is signed with the persistent `resplit.key`, bound to
the chapters, to the coverage seen and to a nonce, and spent once. Nothing is removed, replayed,
forged or carried over to another Edition."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.catalog.public import ResplitSigner
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
from shelfhold.infrastructure.schema import catalog, export, identity
from shelfhold.ops.errors import CodedRefusal
from shelfhold.ops.resplit import ResplitOps
from tests.auth_env import run_cli
from tests.integration.test_schema_catalog import add_unit
from tests.l50_env import Seeded, seeded

pytestmark = pytest.mark.req("EF-BIB-044")

ADMIN = Principal("1000", "uid:1000", "admin", Credential.LOCAL_CLI)


@dataclass
class Novel:
    edition: str
    parent: str
    children: list[str]
    item: str


def novel(env: Seeded, title: str, kids: int = 2) -> Novel:
    """An Edition whose chapter 10 is carried by a file, and the sub-numbers it was cut into."""
    series = env.new_series(title, lang="en")
    item = new_ulid()
    with env.inst.engine.begin() as connection:
        parent = add_unit(connection, series.edition_id, "10")
        children = [add_unit(connection, series.edition_id, f"10.{k}") for k in range(1, kids + 1)]
        connection.execute(sa.insert(export.export_item).values(
            id=item, edition_id=series.edition_id, kind="bundle", frozen_attrs={}))  # fmt: skip
        connection.execute(sa.insert(export.unit_coverage).values(
            carrier_type="export_item", carrier_id=item, unit_id=parent, position=0,
            extent="full", origin="provider"))  # fmt: skip
    return Novel(series.edition_id, parent, children, item)


def unit_rows(env: Seeded) -> list[tuple[str, str]]:
    cov = export.unit_coverage
    return sorted(
        (str(r.carrier_id), str(r.unit_id)) for r in env.rows(cov) if r.carrier_type == "unit"
    )


def proposal_code(out: str) -> str:
    return next(w for w in out.split() if w.count(".") == 2 and len(w) > 100)


def propose(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, capsys: pytest.CaptureFixture[str],
    n: Novel,
) -> str:  # fmt: skip
    capsys.readouterr()
    args = ("coverage", "resplit", "propose", n.parent, *n.children)
    assert run_cli(monkeypatch, settings, *args) == 0
    return proposal_code(capsys.readouterr().out)


def apply(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, n: Novel, code: str | None,
    *, parent: str | None = None, children: list[str] | None = None,
) -> int:  # fmt: skip
    args = ["coverage", "resplit", "apply", parent or n.parent, *(children or n.children)]
    if code is not None:
        args += ["--confirm", code]
    return run_cli(monkeypatch, settings, *args)


def test_resplit_proposes_then_applies_only_with_the_confirmation_and_removes_nothing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with seeded(settings, tmp_path / "inst") as env:
        n = novel(env, "Un roman")
        before = len(env.rows(export.unit_coverage)), len(env.rows(catalog.unit))
        code = propose(monkeypatch, settings, capsys, n)
        assert unit_rows(env) == []  # a proposal writes nothing
        assert (settings.data_dir / "resplit.key").stat().st_mode & 0o077 == 0  # owner-only
        assert apply(monkeypatch, settings, n, None) == 2  # no confirmation: says what to type
        assert unit_rows(env) == []
        assert apply(monkeypatch, settings, n, code) == 0
        assert unit_rows(env) == sorted((n.parent, c) for c in n.children)
        assert len(env.rows(export.unit_coverage)) == before[0] + 2
        assert len(env.rows(catalog.unit)) == before[1]  # no Unit removed
        held = [r for r in env.rows(export.unit_coverage) if r.carrier_type == "export_item"]
        assert [(r.carrier_id, r.unit_id) for r in held] == [(n.item, n.parent)]  # still carried
        assert apply(monkeypatch, settings, n, code) == 0  # replayed: nothing is added twice
        assert len(unit_rows(env)) == 2
        audit = [r for r in env.rows(identity.audit) if r.action == "coverage.resplit"]
        assert len(audit) == 1 and str(audit[0].actor_id).startswith("local_cli:")
        assert "Un roman" not in str(audit[0].target)


def test_resplit_code_forged_truncated_or_signed_for_another_edition_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with seeded(settings, tmp_path / "inst") as env:
        mine, theirs = novel(env, "Un roman"), novel(env, "Un autre roman")
        code = propose(monkeypatch, settings, capsys, mine)
        nonce, snapshot, token = code.split(".")
        flipped = token[:-1] + ("0" if token[-1] != "0" else "1")
        for bad in (
            f"{nonce}.{snapshot}.{flipped}",  # a token nobody signed
            f"{nonce}.{'0' * 64}.{token}",  # another coverage than the one signed
            f"{'0' * 16}.{snapshot}.{token}",  # another nonce
            f"{nonce}.{snapshot}", f"{code}.x", "", ".", "..", "x" * 3000, f"{nonce}.{snapshot}.",
        ):  # fmt: skip
            assert apply(monkeypatch, settings, mine, bad) != 0
        # the code signed for my Edition does not move to the Units of another one
        assert apply(monkeypatch, settings, theirs, code) != 0
        assert apply(monkeypatch, settings, mine, code, parent=theirs.parent) != 0
        assert apply(monkeypatch, settings, mine, code, children=theirs.children) != 0
        assert apply(monkeypatch, settings, mine, code, children=mine.children[:1]) != 0
        assert apply(monkeypatch, settings, mine, code, children=mine.children[::-1]) != 0
        assert unit_rows(env) == []
        assert apply(monkeypatch, settings, mine, code) == 0  # the genuine one still works


def test_resplit_code_of_another_instance_key_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    """Whoever does not hold `resplit.key` cannot compute a token, and a new key voids the old."""
    with seeded(settings, tmp_path / "inst") as env:
        n = novel(env, "Un roman")
        code = propose(monkeypatch, settings, capsys, n)
        (settings.data_dir / "resplit.key").unlink()  # the instance made another one
        assert apply(monkeypatch, settings, n, code) != 0
        assert unit_rows(env) == []
        (tmp_path / "elsewhere").mkdir()
        forged = ResplitSigner(tmp_path / "elsewhere").sign("x")  # any signature of a stranger
        assert apply(monkeypatch, settings, n, f"{'1' * 16}.{'2' * 64}.{forged}") != 0


def test_resplit_is_refused_once_the_coverage_it_saw_has_moved(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with seeded(settings, tmp_path / "inst") as env:
        n = novel(env, "Un roman")
        code = propose(monkeypatch, settings, capsys, n)
        other = new_ulid()
        with env.inst.engine.begin() as connection:  # a file now carries 10.1
            connection.execute(sa.insert(export.export_item).values(
                id=other, edition_id=n.edition, kind="bundle", frozen_attrs={}))  # fmt: skip
            connection.execute(sa.insert(export.unit_coverage).values(
                carrier_type="export_item", carrier_id=other, unit_id=n.children[0], position=0,
                extent="full", origin="provider"))  # fmt: skip
        assert apply(monkeypatch, settings, n, code) != 0
        assert unit_rows(env) == []


def test_a_confirmation_spent_once_is_not_replayed_after_the_coverage_was_taken_back(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with seeded(settings, tmp_path / "inst") as env:
        n = novel(env, "Un roman")
        code = propose(monkeypatch, settings, capsys, n)
        assert apply(monkeypatch, settings, n, code) == 0
        with env.inst.engine.begin() as connection:  # the admin took the coverage back by hand
            connection.execute(sa.delete(export.unit_coverage).where(
                export.unit_coverage.c.carrier_type == "unit"))  # fmt: skip
        assert apply(monkeypatch, settings, n, code) != 0  # the same code is spent: no replay
        assert unit_rows(env) == []
        fresh = propose(monkeypatch, settings, capsys, n)  # a new decision needs a new code
        assert fresh != code and apply(monkeypatch, settings, n, fresh) == 0
        assert len(unit_rows(env)) == 2


@pytest.mark.parametrize("shape", ["none", "self", "duplicate", "foreign", "unrelated", "many"])
def test_resplit_propose_refuses_what_is_not_a_re_cut(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path, shape: str
) -> None:
    with seeded(settings, tmp_path / "inst") as env:
        n, other = novel(env, "Un roman"), novel(env, "Un autre")
        with env.inst.engine.begin() as connection:
            stray = add_unit(connection, n.edition, "99")
        children = {
            "none": [], "self": [n.parent], "duplicate": [n.children[0]] * 3,
            "foreign": [other.children[0]], "unrelated": [stray],
            "many": [new_ulid() for _ in range(300)],
        }[shape]  # fmt: skip
        code = run_cli(monkeypatch, settings, "coverage", "resplit", "propose", n.parent, *children)
        if shape == "duplicate":
            assert code == 0  # the same child three times is one child
        else:
            assert code != 0
        assert unit_rows(env) == []


def test_resplit_is_a_reserved_action_never_for_a_key_or_a_stale_session(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    from shelfhold.interfaces.local_cli.ops import OPS_METHODS

    action = RESERVED_ACTIONS["coverage.resplit"]
    for method in ("ops.coverage_resplit_propose", "ops.coverage_resplit_apply"):
        assert OPS_METHODS[method][1] == "coverage.resplit"
    key = Principal("u", "admin", "admin", Credential.API_KEY, key_id="k")
    stale = Principal("u", "admin", "admin", Credential.SESSION, session_id="s")
    now = datetime(2026, 11, 6, tzinfo=UTC)
    assert isinstance(authorize_reserved(key, action, now=now), CredentialNotAllowed)
    assert isinstance(authorize_reserved(stale, action, now=now), ReauthRequired)
    with seeded(settings, tmp_path / "inst") as env:
        n = novel(env, "Un roman")
        ops = ResplitOps(env.inst.comp)
        for who in (key, stale):
            with pytest.raises(CodedRefusal):
                ops.propose(n.parent, n.children, principal=who)
            with pytest.raises(CodedRefusal):
                ops.apply(n.parent, n.children, confirm="a.b.c", principal=who)
        assert unit_rows(env) == []
        assert env.rows(identity.audit) == [] or not [
            r for r in env.rows(identity.audit) if r.action == "coverage.resplit"
        ]
