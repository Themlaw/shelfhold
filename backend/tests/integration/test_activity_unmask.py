"""EF-USR-016: unmasking a private title is explicit, reserved and audited in the same transaction."""

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from shelfhold.events.activity import ActivityEvent, UnmaskRefusedError, Viewer, unmask_title

from shelfhold.config import Settings
from shelfhold.identity.public import Credential, Principal
from shelfhold.infrastructure.migrate import migrate
from shelfhold.infrastructure.schema import identity

pytestmark = pytest.mark.req("EF-USR-016", "ENF-SEC-010")

NOW = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)
OWNER = "01HZZZZZZZZZZZZZZZZZZZZZZB"
SESSION = "01HZZZZZZZZZZZZZZZZZZZZZZS"
ADMIN = "01HZZZZZZZZZZZZZZZZZZZZZZA"
TITLE = "Un titre"


@pytest.fixture
def engine(settings: Settings) -> sa.Engine:
    migrate(settings)
    return sa.create_engine(f"sqlite:///{settings.database_path}")


def private_adult_request() -> ActivityEvent:
    return ActivityEvent(
        id=42, type="job.dead_lettered", visibility="private", payload={"job_kind": "download"},
        owner_id=OWNER, owner_role="member", title=TITLE,
    )  # fmt: skip


def admin_session(*, age: timedelta = timedelta(hours=3)) -> Principal:
    return Principal(
        user_id=ADMIN, username="admin", role="admin", credential=Credential.SESSION,
        session_id=SESSION, auth_time=NOW - age, can_rate=True,
    )  # fmt: skip


def audit_rows(engine: sa.Engine) -> list[sa.Row[tuple[object, ...]]]:
    with engine.connect() as connection:
        return list(connection.execute(sa.select(identity.audit)))


@pytest.mark.req("EF-USR-016")
def test_unmask_is_audited(engine: sa.Engine) -> None:
    with engine.begin() as connection:
        item = unmask_title(connection, admin_session(), private_adult_request(), now=NOW)
    assert (item.title, item.title_masked) == (TITLE, False)
    [row] = audit_rows(engine)
    assert row.action == "request.unmask"
    assert row.target == "event:42"
    assert row.actor_id == ADMIN
    assert row.session_id == SESSION and row.auth_time is not None  # EF-USR-022 (3)


@pytest.mark.req("EF-USR-016")
def test_unmask_and_its_audit_commit_or_roll_back_together(engine: sa.Engine) -> None:
    class Boom(Exception):
        pass

    with pytest.raises(Boom), engine.begin() as connection:
        unmask_title(connection, admin_session(), private_adult_request(), now=NOW)
        raise Boom  # the caller fails after the unmasking: no title was disclosed, no entry kept
    assert audit_rows(engine) == []


@pytest.mark.req("EF-USR-016", "EF-USR-024")
def test_unmask_refused_to_keys_members_and_hardened_stale_sessions_writes_nothing(
    engine: sa.Engine,
) -> None:
    key = Principal(user_id=ADMIN, username="admin", role="admin", credential=Credential.API_KEY,
                    key_id="k", scopes=("admin",), can_rate=True)  # fmt: skip
    member = Principal(user_id=OWNER, username="m", role="member", credential=Credential.SESSION,
                       session_id=SESSION, auth_time=NOW, can_rate=False)  # fmt: skip
    for who in (key, member):
        with pytest.raises(UnmaskRefusedError), engine.begin() as connection:
            unmask_title(connection, who, private_adult_request(), now=NOW)
    with pytest.raises(UnmaskRefusedError), engine.begin() as connection:
        unmask_title(connection, admin_session(), private_adult_request(), now=NOW, hardened=True)
    assert audit_rows(engine) == []


@pytest.mark.req("EF-USR-016")
def test_nothing_to_unmask_writes_no_audit_and_shows_no_more(engine: sa.Engine) -> None:
    """Only a title that is masked for this viewer can be unmasked; asking for another one is a
    refusal, never a free read of a title the viewer could not see."""
    other = ActivityEvent(
        id=43, type="job.dead_lettered", visibility="private", payload={}, owner_id=OWNER,
        owner_role="member", title=None,
    )  # fmt: skip
    with pytest.raises(UnmaskRefusedError), engine.begin() as connection:
        unmask_title(connection, admin_session(), other, now=NOW)
    assert audit_rows(engine) == []
    assert Viewer(ADMIN, "admin")  # the viewer of an item comes from the principal, not the caller


@pytest.mark.req("EF-USR-016")
def test_a_title_hidden_from_the_viewer_cannot_be_unmasked_by_a_member(engine: sa.Engine) -> None:
    other = Principal(user_id=OWNER, username="x", role="child", credential=Credential.SESSION,
                      session_id=SESSION, auth_time=NOW)  # fmt: skip
    with pytest.raises(UnmaskRefusedError), engine.begin() as connection:
        unmask_title(connection, other, private_adult_request(), now=NOW)
    assert audit_rows(engine) == []
