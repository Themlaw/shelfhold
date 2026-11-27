"""The profiles register of the admin: how many Works each ceiling shows, and per profile how many
it sees, its last reading and its e-readers (EF-USR-005, EF-USR-006, EF-USR-008, EF-USR-016).

* Every count is `count_visible_works` under a `ViewerScope` built by `scope_of`, the very count of
  the library list: the rule of the profiles screen can never promise more than the list shows. A
  mark of the rule counts what a child at that ceiling sees with no shelf reserved to her, or, for
  one child (`profile_id`), with the shelves reserved to her (D-P3).
* The last reading is the latest position written by the profile (a reader or a pull), with the
  Work it is in. What a profile reads is shown to the others only when its visibility says so
  (`household`, always for a child): the reading of a `private` adult is hers, and is `None` here
  for anyone but herself.
* The e-readers are the devices of the profile that are not retired (`revoked_at`).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

import sqlalchemy as sa

from shelfhold.catalog.public import count_visible_works
from shelfhold.identity.public import effective_visibility, scope_of
from shelfhold.infrastructure.schema import catalog, export, identity, reading, storage

__all__ = [
    "DEFAULT_CEILINGS",
    "LastRead",
    "NotAChildError",
    "OverviewDevice",
    "ProfileOverview",
    "ProfilesOverview",
    "profiles_overview",
]

DEFAULT_CEILINGS: Final = (0, 10, 12, 13, 16, 18)  # the marks of the rule of the profiles screen
_PROFILE_ROLES: Final = ("admin", "member", "child")


class NotAChildError(ValueError):
    """`profile_id` names no child profile: only a child's own shelves can be counted."""


@dataclass(frozen=True)
class LastRead:
    at: datetime
    work_id: str
    edition_id: str
    title: str


@dataclass(frozen=True)
class OverviewDevice:
    id: str
    label: str | None
    kind: str
    last_seen_at: datetime | None


@dataclass(frozen=True)
class ProfileOverview:
    profile_id: str
    username: str
    role: str
    max_age_rating: int | None
    works: int
    last_read: LastRead | None
    devices: list[OverviewDevice]


@dataclass(frozen=True)
class ProfilesOverview:
    total_works: int
    steps: list[tuple[int, int]]  # (ceiling, works)
    profiles: list[ProfileOverview]


def profiles_overview(
    connection: sa.Connection,
    *,
    viewer_id: str,
    ceilings: Sequence[int] = DEFAULT_CEILINGS,
    profile_id: str | None = None,
) -> ProfilesOverview:
    """The register as the admin `viewer_id` reads it (the route checks the role)."""
    user = identity.user
    rows = connection.execute(
        sa.select(user.c.id, user.c.username, user.c.role, user.c.max_age_rating,
                  user.c.visibility)
        .where(user.c.role.in_(_PROFILE_ROLES))
        .order_by(user.c.username, user.c.id)
    ).all()  # fmt: skip
    if profile_id is not None and not any(r.id == profile_id and r.role == "child" for r in rows):
        raise NotAChildError(profile_id)
    steps = [
        (c, count_visible_works(connection, scope_of("child", c, profile_id))) for c in ceilings
    ]
    devices = _devices(connection)
    profiles = [
        ProfileOverview(
            profile_id=str(r.id), username=str(r.username), role=str(r.role),
            max_age_rating=r.max_age_rating,
            works=count_visible_works(connection, scope_of(r.role, r.max_age_rating, r.id)),
            last_read=(
                _last_read(connection, str(r.id))
                if r.id == viewer_id or effective_visibility(r.role, r.visibility) == "household"
                else None
            ),
            devices=devices.get(str(r.id), []),
        )
        for r in rows
    ]  # fmt: skip
    return ProfilesOverview(
        total_works=count_visible_works(connection, scope_of("admin", None)),
        steps=steps,
        profiles=profiles,
    )


def _devices(connection: sa.Connection) -> dict[str, list[OverviewDevice]]:
    device = identity.device
    found: dict[str, list[OverviewDevice]] = {}
    for r in connection.execute(
        sa.select(device)
        .where(device.c.revoked_at.is_(None))
        .order_by(device.c.subject_user_id, device.c.created_at, device.c.id)
    ):
        found.setdefault(str(r.subject_user_id), []).append(
            OverviewDevice(id=str(r.id), label=r.label, kind=str(r.kind),
                           last_seen_at=r.last_seen_at)
        )  # fmt: skip
    return found


def _last_read(connection: sa.Connection, user_id: str) -> LastRead | None:
    position = reading.reading_position
    latest = connection.execute(
        sa.select(position.c.carrier_type, position.c.carrier_id, position.c.server_ts)
        .where(position.c.user_id == user_id)
        .order_by(position.c.server_ts.desc(), position.c.id.desc())
        .limit(1)
    ).first()
    if latest is None:
        return None
    edition = catalog.edition
    if latest.carrier_type == "content":
        edition_of = (
            sa.select(catalog.unit.c.edition_id)
            .select_from(
                storage.content.join(catalog.unit, catalog.unit.c.id == storage.content.c.unit_id)
            )
            .where(storage.content.c.id == latest.carrier_id)
            .scalar_subquery()
        )
    else:
        edition_of = (
            sa.select(export.export_item.c.edition_id)
            .where(export.export_item.c.id == latest.carrier_id)
            .scalar_subquery()
        )
    work = connection.execute(
        sa.select(edition.c.id, catalog.work.c.id.label("work_id"), catalog.work.c.title)
        .select_from(edition.join(catalog.work, catalog.work.c.id == edition.c.work_id))
        .where(edition.c.id == edition_of)
    ).first()
    if work is None:  # the file is gone since: the moment is still hers, the Work is not known
        return None
    return LastRead(
        at=latest.server_ts, work_id=str(work.work_id), edition_id=str(work.id),
        title=str(work.title),
    )  # fmt: skip
