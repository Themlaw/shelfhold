"""The scope of the profile that reads, read from the database at each request (EF-USR-006).

The ceiling and the role are never kept in a session or a key: a ceiling lowered, a role changed,
a profile removed, take effect on the next request. A key or a token reads as its own profile (the
reader), never as whoever issued it. A profile that is not there, or a role the rules do not know,
sees nothing.
"""

from collections.abc import Iterable
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from shelfhold.identity.domain.policy import effective_source_policy
from shelfhold.identity.domain.visibility import NOBODY, ViewerScope, scope_of
from shelfhold.infrastructure.schema import acquisition, identity

__all__ = ["FamilyProfile", "family_profile", "family_source_keys", "reader_scope"]


def reader_scope(source: Engine | Connection, user_id: str | None) -> ViewerScope:
    if not isinstance(user_id, str) or not user_id:
        return NOBODY
    query = sa.select(identity.user.c.role, identity.user.c.max_age_rating).where(
        identity.user.c.id == user_id
    )
    if isinstance(source, Engine):
        with source.connect() as connection:
            row = connection.execute(query).first()
    else:
        row = source.execute(query).first()
    return NOBODY if row is None else scope_of(row.role, row.max_age_rating)


@dataclass(frozen=True)
class FamilyProfile:
    """What the family search needs of a reader, read in one statement: the scope, the mode "hide
    the unrated remote results" and the words the profile added. Raw: the rules read them."""

    scope: ViewerScope
    hide_unrated_remote: object
    subject_filter: object


_UNKNOWN = FamilyProfile(NOBODY, True, [])


def family_profile(source: Engine | Connection, user_id: str | None) -> FamilyProfile:
    """The family search settings of a profile, from the database at each request; a profile that
    is not there sees nothing and hides everything."""
    if not isinstance(user_id, str) or not user_id:
        return _UNKNOWN
    user = identity.user
    query = sa.select(
        user.c.role, user.c.max_age_rating, user.c.hide_unrated_remote, user.c.subject_filter
    ).where(user.c.id == user_id)
    if isinstance(source, Engine):
        with source.connect() as connection:
            row = connection.execute(query).first()
    else:
        row = source.execute(query).first()
    if row is None:
        return _UNKNOWN
    return FamilyProfile(
        scope_of(row.role, row.max_age_rating), row.hide_unrated_remote, row.subject_filter
    )


def family_source_keys(
    connection: Connection, user_id: str, role: str, keys: Iterable[str]
) -> frozenset[str]:
    """Of these sources, the ones a child may search: marked "family" by the admin (D13) and not
    closed to her by her policy (the rule is `effective_source_policy`, read each time, so a source
    taken off the family list closes its door at once)."""
    wanted = sorted(set(keys))
    if not wanted:
        return frozenset()
    config = acquisition.source_config
    policy = identity.profile_source_policy
    rows = connection.execute(
        sa.select(config.c.plugin_id, config.c.family_safe, policy.c.policy)
        .select_from(
            config.outerjoin(
                policy,
                sa.and_(policy.c.source_key == config.c.plugin_id, policy.c.user_id == user_id),
            )
        )
        .where(config.c.plugin_id.in_(wanted))
    ).all()
    return frozenset(
        r.plugin_id
        for r in rows
        if r.family_safe is True
        and effective_source_policy(role, r.policy, family=True) != "forbidden"
    )
