"""Synthetic base of the workshop harness: three profiles (admin, member, a child of 8) and a
few Works that wait in "À classer". Everything is invented here; nothing reaches the network.

Run after `shelfhold migrate` and `shelfhold admin create`, before `shelfhold serve`:
    python seed.py <data-dir>
"""

import sys
from pathlib import Path

import sqlalchemy as sa

from shelfhold.identity.adapters.passwords import Argon2Hasher
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, export, identity

PASSWORD = "synthetic-profile-password-1"  # noqa: S105 (invented, for the synthetic base)
TITLES = ("Le Jardin des brumes", "Chroniques du phare", "Atlas des marées")


def main(data_dir: Path) -> None:
    engine = create_db_engine(data_dir / "shelfhold.db")
    digest = Argon2Hasher().hash(PASSWORD)
    with engine.begin() as db:
        # A child always has an age ceiling (migration 0046): 7 for the child of 8.
        for name, role, ceiling in (("membre", "member", None), ("enfant8", "child", 7)):
            db.execute(
                sa.insert(identity.user).values(
                    id=new_ulid(),
                    username=name,
                    password_hash=digest,
                    role=role,
                    can_rate=False,
                    max_age_rating=ceiling,
                )
            )
        shelf_id = new_ulid()
        db.execute(
            sa.insert(export.shelf).values(
                id=shelf_id, name="Famille", phrase="Pour toute la maison", max_age_rating=13
            )
        )
        for title in TITLES:
            work_id = new_ulid()
            db.execute(
                sa.insert(catalog.work).values(
                    id=work_id, kind="manga", title=title, sort_title=title.casefold()
                )
            )
            db.execute(
                sa.insert(catalog.edition).values(
                    id=new_ulid(),
                    work_id=work_id,
                    lang="fr",
                    translation_type="official",
                    media="images",
                    shelf_id=shelf_id,
                )
            )


if __name__ == "__main__":
    main(Path(sys.argv[1]))
