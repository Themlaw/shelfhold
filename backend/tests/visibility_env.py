"""M3 L09: a small catalogue that holds every way a Work can be hidden from a child.

Titles share the word `Tale` so that one query matches all of them: what the child's search
returns is then exactly the visible ones, and anything else in the answer is a leak.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.identity.public import LocalAuth
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, export
from tests.auth_env import PASSWORD, AuthEnv

RATED = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
VISIBLE_TITLES = {
    "ok": "Tale of Orchard",
    "twelve": "Tale of Twelve Rivers",
    "adult_shelf": "Tale of Quiet Shelf",
    "mixed": "Tale of Mixed Editions",
}
HIDDEN_TITLES = {
    "above": "Tale of Harbor Sixteen",
    "x": "Tale of Xylophone",
    "flagged": "Tale of Flagged Lantern",
    "unrated": "Tale of Unrated Lantern",
    "orphan": "Tale of Orphan Age",
    "adult_edition": "Tale of Grove Adult Edition",
    "misfiled": "Tale of Misfiled Family",
}


@dataclass(frozen=True)
class Seed:
    works: dict[str, str]  # key -> work id
    editions: dict[str, str]  # key -> edition id
    shelves: dict[str, str]
    engine: Engine

    @property
    def visible_ids(self) -> set[str]:
        return {self.works[k] for k in VISIBLE_TITLES if k != "twelve"}

    def title_of(self, key: str) -> str:
        return {**VISIBLE_TITLES, **HIDDEN_TITLES}[key]


def _work(
    connection: sa.Connection, kind: str, title: str, *, age: int | None, x: bool = False,
    rated: bool = True, adult: bool = False,
) -> str:  # fmt: skip
    work_id = new_ulid()
    connection.execute(
        sa.insert(catalog.work).values(
            id=work_id,
            kind=kind,
            title=title,
            sort_title=title.lower(),
            age_rating=age,
            age_x=x,
            rated_at=RATED if rated else None,
            is_adult=adult,
        )
    )
    return work_id


def _edition(
    connection: sa.Connection, work_id: str, *, age: int | None = None, rated: bool = True,
    shelf_id: str | None = None, lang: str = "en",
) -> str:  # fmt: skip
    edition_id = new_ulid()
    connection.execute(
        sa.insert(catalog.edition).values(
            id=edition_id,
            work_id=work_id,
            lang=lang,
            translation_type="official",
            media="images",
            age_rating=age,
            rated_at=RATED if rated else None,
            shelf_id=shelf_id,
        )
    )
    return edition_id


def _shelf(connection: sa.Connection, name: str, ceiling: int | None, allowed: list[str]) -> str:
    shelf_id = new_ulid()
    connection.execute(
        sa.insert(export.shelf).values(
            id=shelf_id,
            name=name,
            phrase=name.lower(),
            max_age_rating=ceiling,
            allowed_profiles=allowed,
        )
    )
    return shelf_id


def seed_catalogue(env: AuthEnv, *, other_profile: str = "someone-else") -> Seed:
    engine = create_db_engine(env.settings.database_path)
    with engine.begin() as connection:
        kind = str(connection.execute(sa.text("SELECT key FROM work_kind LIMIT 1")).scalar_one())
        family = _shelf(connection, "Family", 13, [other_profile])  # not the kid's own id
        adult = _shelf(connection, "Adults", None, [])
        works: dict[str, str] = {}
        editions: dict[str, str] = {}

        def add(key: str, **kw: object) -> None:
            works[key] = _work(connection, kind, {**VISIBLE_TITLES, **HIDDEN_TITLES}[key], **kw)  # type: ignore[arg-type]

        add("ok", age=6)
        add("twelve", age=12)
        add("adult_shelf", age=6)
        add("mixed", age=6)
        add("above", age=16)
        add("x", age=18, x=True)
        add("flagged", age=6, adult=True)
        add("unrated", age=None, rated=False)
        add("orphan", age=6, rated=False)  # an age but never rated as a whole (a merge's leftover)
        add("adult_edition", age=6)
        add("misfiled", age=None, rated=False)
        editions["ok"] = _edition(connection, works["ok"], shelf_id=family)
        editions["twelve"] = _edition(connection, works["twelve"])
        editions["adult_shelf"] = _edition(connection, works["adult_shelf"], shelf_id=adult)
        editions["mixed_rated"] = _edition(connection, works["mixed"], shelf_id=adult)
        editions["mixed_new"] = _edition(connection, works["mixed"], rated=False, lang="fr")
        editions["above"] = _edition(connection, works["above"])
        editions["adult_edition"] = _edition(connection, works["adult_edition"], age=18)
        editions["misfiled"] = _edition(connection, works["misfiled"], rated=False, shelf_id=family)
        # one rated Work on two shelves: it must still be counted and listed once
        editions["ok_second"] = _edition(connection, works["ok"], shelf_id=adult, lang="fr")
    return Seed(works, editions, {"family": family, "adult": adult}, engine)


def add_profile(env: AuthEnv, username: str, role: str, ip: str, **kwargs: object):  # type: ignore[no-untyped-def]
    auth: LocalAuth = env.app.state.auth_gate.auth  # type: ignore[attr-defined]
    auth.profile_add(username, PASSWORD, role, **kwargs)  # type: ignore[arg-type]
    client = env.other_client(ip)
    assert env.login(username, client=client).status_code == 200
    return client
