"""L12 (EF-BIB-003, EF-BIB-037): the attaches on the host CLI, the real command over a real
instance.

`edition attachments` shows them, `edition detach` undoes one ("Séparer"), `edition
attach-confirm` prints the removal plan of a suggested attach and applies it only for the token
of that plan.
"""

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.catalog.public import (
    AutoAttachService,
    ExternalId,
    IncomingEdition,
    RatingService,
    add_external_id,
)
from shelfhold.config import Settings
from shelfhold.identity.public import Credential, Principal
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.sdk.age import AgeRating
from tests.integration.test_schema_catalog import add_edition, add_work
from tests.l54_env import Bench54, bench54
from tests.null_tombstones import NO_CATALOG_HITS

pytestmark = pytest.mark.req("EF-BIB-003", "EF-BIB-037")

ANILIST = ExternalId("work", "anilist", "30013")
NOW = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)


def arrive(b: Bench54, *, age: int | None = None) -> tuple[str, str, str]:
    """(holder Work, arriving Edition, attach id)."""
    holder = b.env.new_series("Dr Stone").work_id
    with b.engine.begin() as connection:
        add_external_id(connection, holder, ANILIST, origin="provider")
        work = add_work(connection, title="Dr Stone (uncut)", sort_title="dr stone (uncut)")
        edition = add_edition(connection, work, lang="en")
        if age is not None:  # the holder "Tous publics", the Edition rated above it
            admin = Principal(
                user_id=new_ulid(),
                username="a",
                role="admin",
                credential=Credential.SESSION,
                session_id=new_ulid(),
                auth_time=NOW,
                can_rate=True,
            )
            RatingService().rate(connection, holder, AgeRating(0), principal=admin, now=NOW)
            RatingService().rate_edition(
                connection, edition, AgeRating(age), principal=admin, now=NOW
            )  # fmt: skip
        arrival = AutoAttachService(tombstones=NO_CATALOG_HITS).arrive(
            connection, IncomingEdition(edition, ids=(ANILIST,)), now=NOW
        )
    assert arrival.attach_id is not None
    return holder, edition, arrival.attach_id


def work_of(b: Bench54, edition: str) -> str:
    return str(b.rows("SELECT work_id FROM edition WHERE id = :e", e=edition)[0]["work_id"])


def test_cli_attachments_and_detach(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], settings: Settings,
    tmp_path: Path,
) -> None:  # fmt: skip
    with bench54(monkeypatch, settings, tmp_path) as b:
        holder, edition, attach_id = arrive(b)
        capsys.readouterr()
        assert b.cli("edition", "attachments") == 0
        assert attach_id in capsys.readouterr().out
        assert work_of(b, edition) == holder
        assert b.cli("edition", "detach", attach_id) == 0
        assert work_of(b, edition) != holder
        assert [r["action"] for r in b.rows("SELECT action FROM audit")][-1] == "edition.detach"
        capsys.readouterr()
        assert b.cli("edition", "detach", attach_id) != 0  # once
        assert "Refusé" in capsys.readouterr().err


def test_cli_attach_confirm_prints_the_plan_then_applies_it_for_its_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], settings: Settings,
    tmp_path: Path,
) -> None:  # fmt: skip
    with bench54(monkeypatch, settings, tmp_path) as b:
        holder, edition, attach_id = arrive(b, age=18)
        capsys.readouterr()
        assert b.cli("edition", "attach-confirm", attach_id) == 2  # the plan, nothing applied
        out = capsys.readouterr().out
        token = re.search(r"--confirm (\S+)", out)
        assert token is not None
        assert work_of(b, edition) != holder
        assert b.cli("edition", "attach-confirm", attach_id, "--confirm", "0-00000000") != 0
        assert work_of(b, edition) != holder
        assert b.cli("edition", "attach-confirm", attach_id, "--confirm", token.group(1)) == 0
        assert work_of(b, edition) == holder
        with b.engine.connect() as connection:
            assert (
                connection.execute(
                    sa.text("SELECT state FROM edition_attach WHERE id = :a"), {"a": attach_id}
                ).scalar_one()
                == "attached"
            )
