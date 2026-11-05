"""independent_copies counts by content_hash, whatever the file hash says (EF-BIB-045, EF-BIB-048)."""

import pytest
import sqlalchemy as sa
from hypothesis import given
from hypothesis import strategies as st

from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import storage
from shelfhold.library.public import independent_copies
from tests.library_env import lib_env

A, B = "sha256:" + "a" * 64, "sha256:" + "b" * 64
FILE_HASHES = ["sha256:" + c * 64 for c in "0123456789"]

rows = st.lists(
    st.fixed_dictionaries(
        {
            "content_hash": st.sampled_from([A, B, None]),
            "file_hash": st.sampled_from(FILE_HASHES + [None]),
            "independent": st.booleans(),
            "inode": st.integers(1, 4),  # few inodes: collisions are hard links
            "location": st.sampled_from(["library", "store", "adopted_ro"]),
        }
    ),
    max_size=8,
)


def expected(model: list[dict[str, object]]) -> int:
    """The rule, restated: independent copies of the reference payload, one per distinct inode."""
    return len({r["inode"] for r in model if r["independent"] and r["content_hash"] == A})


@pytest.mark.req("EF-BIB-045", "EF-BIB-048")
@given(model=rows)
def test_independent_copies_counts_by_content_hash(model: list[dict[str, object]]) -> None:
    with lib_env() as env:
        unit = env.chain["unit"]
        with env.engine.begin() as c:
            c.execute(
                sa.insert(storage.content).values(
                    id=new_ulid(), unit_id=unit, variant_id=env.chain["variant"], kind="images",
                    media_type="application/vnd.comicbook+zip", format="cbz",
                    retention="passthrough", original_sha256=A, content_hash=A,
                    store_provenance="received", size=10,
                )
            )  # fmt: skip
            for i, r in enumerate(model):
                c.execute(
                    sa.insert(storage.replica).values(
                        id=new_ulid(), unit_id=unit, location=r["location"],
                        physical_root_id=env.library_root if r["location"] != "store" else None,
                        rel_path=f"f{i}.cbz", dev=1, ino=r["inode"],
                        content_hash=r["content_hash"], file_hash=r["file_hash"],
                        independent=r["independent"],
                    )
                )  # fmt: skip
        with env.engine.connect() as c:
            assert independent_copies(c, unit) == expected(model)
            assert independent_copies(c, new_ulid()) == 0  # an unknown Unit has none


@pytest.mark.req("EF-BIB-045", "EF-BIB-048")
def test_file_hash_never_enters_the_count() -> None:
    with lib_env() as env:
        unit = env.chain["unit"]
        with env.engine.begin() as c:
            c.execute(
                sa.insert(storage.content).values(
                    id=new_ulid(), unit_id=unit, variant_id=env.chain["variant"], kind="images",
                    media_type="application/vnd.comicbook+zip", format="cbz",
                    retention="passthrough", original_sha256=A, content_hash=A,
                    store_provenance="received", size=10,
                )
            )  # fmt: skip
            for i, file_hash in enumerate((B, None, A, FILE_HASHES[3])):  # four renderings
                c.execute(
                    sa.insert(storage.replica).values(
                        id=new_ulid(), unit_id=unit, location="library",
                        physical_root_id=env.library_root, rel_path=f"d{i}/x.cbz",
                        dev=1, ino=10 + i, content_hash=A, file_hash=file_hash, independent=True,
                    )
                )  # fmt: skip
        with env.engine.connect() as c:
            assert independent_copies(c, unit) == 4
