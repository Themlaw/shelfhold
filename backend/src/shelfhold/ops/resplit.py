"""`coverage resplit propose|apply` (EF-BIB-044, M2 L26): a chapter the source cut again.

Chapter 10 became 10.1 and 10.2: the new Units are covered by the old one (`carrier_type = unit`),
a proposal to the admin, never a removal. `propose` writes nothing and prints a code; `apply` acts
only for that code. The code is `nonce.snapshot.token`: the token is signed with the persistent
key `resplit.key` of `/data` over the Edition, the chapters, the coverage seen and the nonce, so
a code is made by this instance only, for these chapters only, and no longer holds once the
coverage moved. It is also spent once: the audit entry of an application names the token, and the
same code is not applied again after the coverage was taken back by hand.

A reserved action (`coverage.resplit`): the host CLI or a fresh admin session, never a key, for
the two commands (a token is a decision in waiting). The audit entry is written in the same
transaction as the rows, and names no title.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa

from shelfhold.catalog.public import (
    ConfirmationRequiredError,
    ResplitError,
    ResplitProposal,
    ResplitSigner,
    apply_resplit,
    propose_resplit,
)
from shelfhold.identity.public import RESERVED_ACTIONS, Allowed, Principal, authorize_reserved
from shelfhold.infrastructure.ids import is_ulid, new_ulid
from shelfhold.infrastructure.schema import catalog, export, identity
from shelfhold.ops.errors import (
    CodedRefusal,
    InvalidChoiceError,
    StalePreviewError,
    UnknownSubjectError,
)

if TYPE_CHECKING:
    from shelfhold.composition import Composition

__all__ = ["ACTION", "ResplitOps"]

ACTION: Final = "coverage.resplit"
_CODE: Final = re.compile(r"([0-9a-f]{16})\.([0-9a-f]{64})\.([0-9a-f]{32})")
MAX_CHILDREN: Final = 200


def _ids(parent: str, children: Sequence[str]) -> tuple[str, ...]:
    kids = tuple(dict.fromkeys(children))
    if len(kids) > MAX_CHILDREN or not kids:
        raise InvalidChoiceError(f"a chapter is cut into 1 to {MAX_CHILDREN} chapters")
    if not is_ulid(parent) or not all(is_ulid(c) for c in kids):
        raise InvalidChoiceError("a chapter is named by its identifier")
    return kids


class ResplitOps:
    def __init__(self, comp: Composition) -> None:
        self._comp = comp

    def _authorize(self, principal: Principal) -> None:
        verdict = authorize_reserved(
            principal, RESERVED_ACTIONS[ACTION], now=self._comp.clock.now(),
            fresh_auth_s=self._comp.settings.fresh_auth_s,
        )  # fmt: skip
        if not isinstance(verdict, Allowed):  # the socket decided first; this one is the last gate
            raise CodedRefusal("forbidden", type(verdict).__name__)

    def _signer(self) -> ResplitSigner:
        return ResplitSigner(self._comp.settings.data_dir)

    def propose(
        self, parent: str, children: Sequence[str], *, principal: Principal
    ) -> dict[str, Any]:
        """The proposal for `parent` cut into `children`, and the code that confirms it."""
        self._authorize(principal)
        kids = _ids(parent, children)
        with self._comp.engine.connect() as connection:
            try:
                found = propose_resplit(connection, parent, kids, self._signer())
            except ResplitError as error:
                raise InvalidChoiceError(str(error)) from None
        return {
            "edition_id": found.edition_id, "parent": found.parent_unit_id,
            "children": list(found.child_unit_ids),
            "code": f"{found.nonce}.{found.snapshot}.{found.token}",
        }  # fmt: skip

    def apply(
        self, parent: str, children: Sequence[str], *, confirm: str, principal: Principal
    ) -> dict[str, Any]:
        """Write the coverage of a proposal the admin confirmed with its code."""
        self._authorize(principal)
        kids = _ids(parent, children)
        parts = _CODE.fullmatch(confirm)
        if parts is None:
            raise InvalidChoiceError("not a confirmation code")
        nonce, snapshot, token = parts.groups()
        signer = self._signer()
        target = f"{parent};{len(kids)};tok:{token}"
        try:
            with self._comp.engine.connect() as connection:
                connection.execution_options(shelfhold_begin="BEGIN IMMEDIATE")
                with connection.begin():
                    edition_id = connection.execute(
                        sa.select(catalog.unit.c.edition_id).where(catalog.unit.c.id == parent)
                    ).scalar()
                    if edition_id is None:
                        raise UnknownSubjectError("no such chapter")
                    proposal = ResplitProposal(
                        str(edition_id), parent, kids, snapshot, nonce, token
                    )
                    held = set(connection.execute(
                        sa.select(export.unit_coverage.c.unit_id).where(
                            export.unit_coverage.c.carrier_type == "unit",
                            export.unit_coverage.c.carrier_id == parent,
                        )
                    ).scalars())  # fmt: skip
                    spent = connection.execute(
                        sa.select(identity.audit.c.id).where(
                            identity.audit.c.action == ACTION, identity.audit.c.target == target
                        ).limit(1)
                    ).first() is not None  # fmt: skip
                    if spent:
                        if held >= set(kids):  # applied already, still there: nothing changes
                            return self._answer(proposal, 0)
                        raise StalePreviewError("this confirmation was used already")
                    added = apply_resplit(connection, proposal, signer, confirmation=token)
                    if added:
                        connection.execute(sa.insert(identity.audit).values(
                            id=new_ulid(), actor_id=principal.actor_id, action=ACTION,
                            target=target, session_id=principal.session_id,
                            auth_time=principal.auth_time,
                        ))  # fmt: skip
        except (ConfirmationRequiredError, ResplitError):
            raise StalePreviewError(
                "the proposal is not the one confirmed, or no longer holds"
            ) from None
        return self._answer(proposal, added)

    @staticmethod
    def _answer(proposal: ResplitProposal, added: int) -> dict[str, Any]:
        return {"edition_id": proposal.edition_id, "parent": proposal.parent_unit_id,
                "children": list(proposal.child_unit_ids), "added": added}  # fmt: skip
