"""Second counter-review of L30 (M3, EF-EXP-031), after the author's answers: red proofs.

Each test states the expected behaviour and fails on the branch as it is.
"""

from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.export.application.detach import full_detach_phrase
from shelfhold.export.targets.kavita.dto import HIDDEN_DIR_PATTERNS
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export
from tests.detach_env import ADMIN, DetachEnv, detach_env
from tests.fakes.kavita import API_KEY
from tests.rename_env import export_chapters

pytestmark = pytest.mark.req("EF-EXP-031")

ROOT_REMOTE = "/manga"  # the remote side of the physical root (path map `/manga`)


@pytest.fixture
def d() -> Iterator[DetachEnv]:
    with detach_env() as made:
        export_chapters(made.env, "Dr Stone", ["1", "2"])
        made.revoker.fake = made.wired.fake
        yield made


def _clean_bins(d: DetachEnv) -> None:
    import shutil

    for name in ("recycle", "received"):
        shutil.rmtree(d.root / ".shelfhold" / name, ignore_errors=True)
    shutil.rmtree(d.env.lib.manga / ".shelfhold", ignore_errors=True)


def _detach(d: DetachEnv) -> Any:
    target = d.env.target_id
    return d.detacher.apply(
        target, principal=ADMIN, typed=full_detach_phrase(target), client=d.wired.client,
        revoker=d.revoker, credential="the account password",
    )  # fmt: skip


def _hidden_pattern_kept(d: DetachEnv) -> bool:
    return any(p in HIDDEN_DIR_PATTERNS for p in d.kavita_patterns())


# -- N1: a kept `.shelfhold/` inside a Kavita library loses its exclusion -------------------------


def test_a_kept_reserved_folder_inside_the_library_stays_excluded_root_shared(
    d: DetachEnv,
) -> None:
    """The library reads the whole root (its folder is the root itself). Another target still uses
    the root, so its `.shelfhold/` stays (`root_shared`); the detachment still takes Shelfhold's
    pattern away, and Kavita now indexes the bin and the received archives."""
    _clean_bins(d)
    d.wired.fake.libraries[0]["folders"] = [ROOT_REMOTE]
    with d.env.engine.begin() as conn:
        conn.execute(
            sa.insert(export.export_target).values(
                id=new_ulid(), kind="folder", name="Another", path_map=[
                    {"local": str(d.env.lib.library), "remote": "/lib", "mode": "rw"}
                ],
            )
        )  # fmt: skip
    report = _detach(d)
    assert (d.root / ".shelfhold").exists()  # kept, as it must be
    assert "root_shared" in {line.code for line in report.leftovers}
    assert _hidden_pattern_kept(d), "the kept reserved folder is no longer excluded by Kavita"


def test_a_kept_reserved_folder_inside_the_library_stays_excluded_library_root(
    d: DetachEnv,
) -> None:
    """Same, for a root Shelfhold's own library lives on (the M4 answer keeps its `.shelfhold/`)."""
    _clean_bins(d)
    d.wired.fake.libraries[0]["folders"] = [ROOT_REMOTE]
    d.library_roots.add(d.env.lib.library_root)
    _detach(d)
    assert (d.root / ".shelfhold").exists()
    assert _hidden_pattern_kept(d), "the kept reserved folder is no longer excluded by Kavita"


# -- N2: a dedicated key already revoked by hand blocks the detachment forever ---------------------


def test_a_key_already_revoked_by_hand_does_not_block_the_detachment(d: DetachEnv) -> None:
    """The admin deleted the dedicated key in Kavita before detaching. What the detachment wants
    (the key no longer opens Kavita) is already true, but nothing ever completes: the settings
    step answers 401 with the dead key, and the revocation finds no key to name. There is no
    flag to get past it."""
    _clean_bins(d)
    d.wired.fake.auth_keys = [k for k in d.wired.fake.auth_keys if k["key"] != API_KEY]
    first = _detach(d)
    second = _detach(d)
    assert first.complete or second.complete, [line.code for line in second.leftovers]
