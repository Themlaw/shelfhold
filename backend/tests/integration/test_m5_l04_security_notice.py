"""M5 L04: the security notice of an account reaches its holder alone (EF-USR-004, #347 MIN-B).

It is one more kind of item in the digest that already tells people what concerns them. What it
says is what happened to the account, never an identifier nor who did it, and it goes to the
person's own destination or nowhere: the shared channels are read by a household, a child and the
admin who may be the one who did it. Nothing else is new: same sweep, same channels.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.schema import identity
from tests.notify_env import ADMIN_ID, MEMBER_ID, Env, PrivateSink, world

pytestmark = pytest.mark.req("EF-USR-004")

LINKED = "identity_linked_by_admin"
UNLINKED = "identity_unlinked"
BY_ADMIN = "identity_unlinked_by_admin"


def tell(env: Env, what: str) -> None:
    """What the store writes with the change, due now."""
    with env.engine.begin() as connection:
        connection.execute(
            sa.insert(identity.notification_digest).values(
                user_id=MEMBER_ID,
                due_at=env.clock.now(),
                payload={
                    "urgent": True,
                    "attempts": 0,
                    "items": [{"kind": "security", "what": what}],
                },
            )
        )


def test_the_notice_goes_to_the_destination_of_the_holder_and_to_no_shared_channel(
    settings: Settings,
) -> None:
    private = PrivateSink(users={MEMBER_ID})
    with world(settings, private=private) as env:
        tell(env, BY_ADMIN)
        env.run()
        (message,) = private.to(MEMBER_ID)
        assert message.title == "Shelfhold - security notice"
        assert "administrator removed the identity-provider link" in message.body
        assert env.sink.sent == []  # the household reads the shared channels, not this
        assert env.pending() == []  # sent: the row is gone


def test_the_notice_without_a_destination_goes_nowhere_and_is_not_kept(settings: Settings) -> None:
    with world(settings, private=PrivateSink(users=set())) as env:
        tell(env, UNLINKED)
        env.run()
        assert env.sink.sent == []
        assert env.pending() == []  # nothing to retry: the settings screen keeps the trace


def test_the_notice_names_no_identifier_and_no_author(settings: Settings) -> None:
    private = PrivateSink(users={MEMBER_ID})
    with world(settings, private=private) as env:
        for what in (LINKED, UNLINKED, BY_ADMIN):
            tell(env, what)
            env.advance(seconds=1)
        env.run()
        text = private.text(MEMBER_ID)
        assert "identity-provider" in text
        assert "sub" not in text.lower().replace("subject", "")
        assert "admin" not in text.lower().replace("an administrator", "")  # no name, only a role


def test_an_unknown_kind_of_notice_says_nothing(settings: Settings) -> None:
    private = PrivateSink(users={MEMBER_ID})
    with world(settings, private=private) as env:
        tell(env, "something_else")
        env.run()
        assert private.sent == [] and env.sink.sent == []


def test_the_shared_message_of_a_mixed_digest_omits_the_security_line(settings: Settings) -> None:
    """If a notice ever shares a digest row with other news, the shared text keeps only the news."""
    with world(settings, private=PrivateSink(users={ADMIN_ID})) as env:
        payload = {
            "items": [
                {"kind": "outage", "type": "target.unreachable", "start_id": 1, "vis": "ops"},
                {"kind": "security", "what": UNLINKED},
            ]
        }
        with env.engine.connect() as connection:
            person = _person(connection, ADMIN_ID)
            shared = env.sender._build(connection, person, payload)
            titled = env.sender._build(connection, person, payload, titles=True)
    assert shared is not None and "identity-provider" not in shared.body
    assert titled is not None and "identity-provider" in titled.body


def _person(connection: sa.Connection, user_id: str):  # type: ignore[no-untyped-def]
    from shelfhold.integrations.notify.service import _person as person

    found = person(connection, user_id)
    assert found is not None
    return found
