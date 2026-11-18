"""ENF-OPS-021, EF-LEC-005, ADR-0055: the named migration of the derivatives from
`xhtml-neutralizer@v1` to `@v2` (`shelfhold content reneutralize`, plan then confirmation).

The v1 derivative is produced by the real pipeline with the one difference between the releases
(v1 wrote nothing where it removed an element): a release never rewrites anything by itself, and
the migration brings each EPUB to the shipped release without touching its original.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from shelfhold.export.application.locks import work_lock_for
from shelfhold.infrastructure.clock import SystemClock
from shelfhold.infrastructure.content_trust import DERIVED_BY, neutralize_epub, xhtml_neutralizer
from shelfhold.library.public import Reneutralizer, StalePlanError, UntrustedEpubStore
from shelfhold.sdk.testing.corpus.trapped_epubs import CHAPTER, epub_bytes, xhtml
from tests.integration.test_untrusted_epub_pipeline import content_row, ingest, sha, store
from tests.library_env import lib_env
from tests.null_tombstones import NONE_PURGED

pytestmark = pytest.mark.req("ENF-OPS-021", "EF-LEC-005")

V1 = "xhtml-neutralizer@v1"
BOOK = epub_bytes(
    xhtml("<p>one</p><script>alert(1)</script><p>two</p><form><input/></form><p>three</p>")
)


def neutralize_v1(source: Any, out: Any, **kw: Any) -> Any:
    """What release 1 wrote: the removed elements leave nothing at their rank."""
    original = xhtml_neutralizer._inert
    xhtml_neutralizer._inert = lambda *_a, **_k: ""  # type: ignore[attr-defined]
    try:
        return replace(neutralize_epub(source, out, **kw), derived_by=V1)
    finally:
        xhtml_neutralizer._inert = original  # type: ignore[attr-defined]


def chapter(path: Path) -> str:
    with zipfile.ZipFile(io.BytesIO(path.read_bytes())) as archive:
        return archive.read(CHAPTER).decode()


def migrator(env: Any, **kw: Any) -> Reneutralizer:
    return Reneutralizer(store(env), engine=env.engine, **kw)  # the shipped release, by default


def test_neutralizer_v2_migration_regenerates_derivatives() -> None:
    with lib_env() as env:
        legacy = _ingest_with(env, neutralize_v1)
        row = content_row(env)
        assert row.derived_by == V1
        before = row.derived_hash
        assert "<span" not in chapter(legacy)  # v1: ranks shifted, nothing in place of the script

        plan = migrator(env).plan()
        (item,) = plan.items
        assert (item.source, item.from_by, item.to_by) == ("original", V1, DERIVED_BY)
        assert DERIVED_BY == "xhtml-neutralizer@v2"
        assert content_row(env).derived_by == V1  # a plan changes nothing
        with pytest.raises(StalePlanError):
            migrator(env).apply("0" * 64)

        report = migrator(env).apply(plan.fingerprint)
        assert (report.regenerated, report.blocked, report.failed) == (1, 0, 0)
        row = content_row(env)
        assert row.derived_by == DERIVED_BY and row.derived_hash != before
        assert [e["derived_by"] for e in row.derived_chain] == [V1]  # the lineage is kept
        assert row.original_sha256 == sha(BOOK)
        (kept,) = env.files_under(env.store)
        assert kept.read_bytes() == BOOK  # the original is never touched

        page = chapter(legacy)
        assert page.count("<span></span>") == 2  # a script and a form, each kept at its rank
        assert "<script" not in page and "<form" not in page and "<input" not in page
        assert page.index("one") < page.index("<span></span>") < page.index("two")
        assert migrator(env).plan().items == ()  # up to date: nothing left to do


def test_neutralizer_v2_migration_without_original_keeps_the_v1_text() -> None:
    """From the stored derivative only: text and order are kept, the ranks cannot come back."""
    with lib_env() as env:
        legacy = _ingest_with(env, neutralize_v1, with_store=False)
        plan = Reneutralizer(store(env, with_store=False), engine=env.engine).plan()
        (item,) = plan.items
        assert item.source == "derived" and item.from_by == V1
        Reneutralizer(store(env, with_store=False), engine=env.engine).apply(plan.fingerprint)
        assert content_row(env).derived_by == DERIVED_BY
        page = chapter(legacy)
        assert page.index("one") < page.index("two") < page.index("three")


def test_a_v2_derivative_is_not_in_the_migration_plan() -> None:
    with lib_env() as env:
        ingest(env, BOOK)
        assert content_row(env).derived_by == DERIVED_BY
        assert migrator(env).plan().items == ()


def _ingest_with(env: Any, neutralize: Callable[..., Any], *, with_store: bool = True) -> Path:
    dest = str(env.manga / "Book" / "Book.epub")
    staged = env.stage(BOOK, "received.epub")
    UntrustedEpubStore(
        env.engine,
        env.fsops,
        env.locate,
        env.data,
        store_dir=str(env.store) if with_store else None,
        tombstones=NONE_PURGED,
        hold_work=work_lock_for(env.engine, clock=SystemClock(), timeout=0.0),
        neutralize=neutralize,
    ).ingest(unit_id=env.chain["unit"], variant_id=env.chain["variant"], staged=staged, dest=dest)
    return Path(dest)
