"""`contributor set-died-for-france <id> --yes|--no|--unknown` (EF-SRC-024, M2 L15b).

"Mort pour la France" adds 30 years to the French term, and an unknown value makes the rights
warning show for a war death. Saying `yes` or `no` is the admin's word on a legal fact, so it is a
reserved action (`contributor.set_died_for_france`): the host CLI or a fresh admin session, never
a key. The value and its audit entry are written in one transaction; the entry names the
contributor's identifier and the answer, never a name. A refusal leaves nothing behind.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa

from shelfhold.identity.public import RESERVED_ACTIONS, Allowed, Principal, authorize_reserved
from shelfhold.infrastructure.ids import is_ulid, new_ulid
from shelfhold.infrastructure.schema import catalog, identity
from shelfhold.ops.errors import CodedRefusal, InvalidChoiceError, UnknownSubjectError

if TYPE_CHECKING:
    from shelfhold.composition import Composition

__all__ = ["ACTION", "ContributorOps"]

ACTION: Final = "contributor.set_died_for_france"
_WORDS: Final = {True: "yes", False: "no", None: "unknown"}


class ContributorOps:
    def __init__(self, comp: Composition) -> None:
        self._comp = comp

    def set_died_for_france(
        self, contributor_id: str, value: bool | None, *, principal: Principal
    ) -> dict[str, Any]:
        verdict = authorize_reserved(
            principal, RESERVED_ACTIONS[ACTION], now=self._comp.clock.now(),
            fresh_auth_s=self._comp.settings.fresh_auth_s,
        )  # fmt: skip
        if not isinstance(verdict, Allowed):  # the socket decided first; this one is the last gate
            raise CodedRefusal("forbidden", type(verdict).__name__)
        if not is_ulid(contributor_id):
            raise InvalidChoiceError("a contributor is named by its identifier")
        table = catalog.contributor
        with self._comp.engine.begin() as connection:
            found = connection.execute(
                sa.update(table).where(table.c.id == contributor_id).values(died_for_france=value)
            ).rowcount
            if not found:
                raise UnknownSubjectError("no such contributor")
            connection.execute(sa.insert(identity.audit).values(
                id=new_ulid(), actor_id=principal.actor_id, action=ACTION,
                target=f"{contributor_id};died_for_france:{_WORDS[value]}",
                session_id=principal.session_id, auth_time=principal.auth_time,
            ))  # fmt: skip
        return {"contributor_id": contributor_id, "answer": _WORDS[value]}
