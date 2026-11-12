"""M2 L03: the derivative pipeline of an `untrusted` EPUB (EF-BIB-051, EF-BIB-006, EF-BIB-045).

Real files, a real database, the real neutralizer (`xhtml-neutralizer@v1`) over EPUB built by the
conformance kit. The invariants under test:

- no `untrusted` EPUB leaves Shelfhold except as its neutralized derivative;
- the original is kept in /store (the only file there in passthrough mode) and is never served;
- a security update of the neutralizer regenerates from the original, else from the stored
  derivative, by an explicit plan then confirmation, and never deletes before the new file is
  verified;
- a refused EPUB produces neither a derivative nor an export, and says why.
"""

import hashlib
import io
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.export.application.locks import work_lock_for
from shelfhold.infrastructure.archives import safe_open_archive
from shelfhold.infrastructure.clock import SystemClock
from shelfhold.infrastructure.content_trust import DERIVED_BY, epub_payload_hash, neutralize_epub
from shelfhold.infrastructure.schema import catalog, storage
from shelfhold.library.public import (
    EpubRefusedError,
    OriginalNotServableError,
    Reneutralizer,
    StalePlanError,
    UntrustedEpubStore,
    independent_copies,
    servable_epub,
)
from shelfhold.sdk.testing.corpus.trapped_epubs.build import (
    Member,
    epub_bytes,
    xhtml,
    zip_bytes,
)
from shelfhold.sdk.testing.corpus.trapped_epubs.standard import standard_epub3
from tests.library_env import LibEnv, lib_env
from tests.null_tombstones import NONE_PURGED

pytestmark = pytest.mark.req("EF-BIB-051")

TRAPPED = epub_bytes(xhtml("<p>chapter</p><script>alert(1)</script>"))
CLEAN = epub_bytes(xhtml("<p>chapter</p>"))
V2 = "xhtml-neutralizer@v2"


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def store(env: LibEnv, *, with_store: bool = True, tombstones: Any = NONE_PURGED) -> Any:
    return UntrustedEpubStore(
        env.engine, env.fsops, env.locate, env.data,
        store_dir=str(env.store) if with_store else None, tombstones=tombstones,
        hold_work=work_lock_for(env.engine, clock=SystemClock(), timeout=0.0),
    )  # fmt: skip


def neutralize_v2(source: Path, out: Any, **kw: Any) -> Any:
    """A later release of the neutralizer: same output here, another `derived_by`."""
    return replace(neutralize_epub(source, out, **kw), derived_by=V2)


def reneutralizer(env: LibEnv, *, with_store: bool = True, neutralize: Any = neutralize_v2) -> Any:
    return Reneutralizer(
        store(env, with_store=with_store), engine=env.engine, neutralize=neutralize, target=V2
    )


def file_in(env: LibEnv, data: bytes, name: str = "Book.epub") -> tuple[str, Path]:
    return str(env.manga / "Book" / name), env.stage(data, "received.epub")


def ingest(env: LibEnv, data: bytes = TRAPPED, **kw: Any) -> Any:
    dest, staged = file_in(env, data)
    result = store(env, **kw).ingest(
        unit_id=env.chain["unit"], variant_id=env.chain["variant"], staged=staged, dest=dest
    )
    return result, Path(dest)


def content_row(env: LibEnv) -> Any:
    with env.engine.connect() as connection:
        return connection.execute(sa.select(storage.content)).one()


def replicas(env: LibEnv) -> list[Any]:
    with env.engine.connect() as connection:
        return list(connection.execute(sa.select(storage.replica).order_by(storage.replica.c.id)))


def set_binding_trust(env: LibEnv, value: str) -> None:
    binding = catalog.unit_variant.metadata.tables["source_binding"]
    with env.engine.begin() as connection:
        connection.execute(sa.update(binding).values(content_trust=value))


def has_active_content(data: bytes) -> bool:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return any(b"<script" in zf.read(n) for n in zf.namelist())


# -- the original stays in /store, the derivative goes to /library --------------------------------


def test_passthrough_keeps_untrusted_original_in_store() -> None:
    """The original is the only file of /store in passthrough mode; /library has the derivative."""
    with lib_env() as env:
        result, library = ingest(env)
        (kept,) = env.files_under(env.store)
        assert kept.read_bytes() == TRAPPED  # the bytes as received, never rewritten
        assert kept.relative_to(env.store).parts[0] == "untrusted"
        assert not has_active_content(library.read_bytes())  # what /library holds is derived
        assert library.read_bytes() != TRAPPED
        row = content_row(env)
        assert (row.retention, row.store_provenance, row.format) == ("keep", "received", "epub")
        assert row.path == kept.relative_to(env.store).as_posix()
        assert row.original_sha256 == sha(TRAPPED)
        assert (row.derived_by, row.derived_hash_by) == (DERIVED_BY, "content-hash@2")
        assert row.derived_hash == result.derived_hash
        assert row.derived_hash == epub_payload_hash(safe_open_archive(library, name="x.epub"))
        by_place = {r.location: r for r in replicas(env)}
        assert by_place["library"].fidelity == "derived"
        assert by_place["store"].fidelity == "original"
        assert kept.stat().st_ino != library.stat().st_ino  # never one inode under two names


def test_untrusted_epub_without_changes_is_still_a_derivative() -> None:
    """A clean EPUB goes through the neutralizer all the same: `derived_hash` is always set."""
    with lib_env() as env:
        ingest(env, CLEAN)
        row = content_row(env)
        assert row.derived_hash is not None and row.derived_by == DERIVED_BY


def test_publisher_epub_filed_as_received_without_derivative() -> None:
    """Only the manifest's `publisher` (or an admin attestation) skips the neutralizer."""
    with lib_env() as env:
        set_binding_trust(env, "publisher")
        _result, library = ingest(env, CLEAN)
        assert library.read_bytes() == CLEAN
        row = content_row(env)
        assert (row.derived_hash, row.derived_by, row.retention) == (None, None, "passthrough")
        assert env.files_under(env.store) == []


def test_unknown_trust_is_untrusted() -> None:
    """The default is `untrusted`: a Variant whose binding cannot be read gets the neutralizer."""
    with lib_env() as env:
        _result, library = ingest(env)
        assert not has_active_content(library.read_bytes())


def test_heuristic_watermark_does_not_bypass_the_pipeline() -> None:
    """`watermarked` from the heuristic is a suggestion: the pipeline never reads it."""
    with lib_env() as env:
        _result, library = ingest(env, epub_bytes(xhtml("<p>x</p><script>1</script>")))
        assert not has_active_content(library.read_bytes())
        assert content_row(env).watermark_origin == "none"


def test_untrusted_epub_without_store_warns_and_degrades() -> None:
    """No /store: warned at ingest, derivative only, nothing else is kept (02 section 5.8)."""
    with lib_env() as env:
        result, library = ingest(env, with_store=False)
        assert "content.original_not_kept" in result.warnings
        assert result.degraded is True
        assert env.files_under(env.store) == []
        assert not has_active_content(library.read_bytes())
        row = content_row(env)
        assert (row.retention, row.path) == ("passthrough", None)
        assert row.derived_hash is not None


def test_binding_warns_when_untrusted_and_no_store() -> None:
    from shelfhold.library.public import binding_warning

    assert binding_warning("untrusted", store_available=False) == "content.original_not_kept"
    assert binding_warning("untrusted", store_available=True) is None
    assert binding_warning("publisher", store_available=False) is None


# -- a refused EPUB: no derivative, no export, a visible reason ----------------------------------


def test_refused_epub_has_no_derivative_and_a_visible_reason() -> None:
    bad = epub_bytes(xhtml("<p>x</p>"), extra=[Member("OEBPS/app.js", b"alert(1)")])
    with lib_env() as env:
        dest, staged = file_in(env, bad)
        with pytest.raises(EpubRefusedError) as refused:
            store(env).ingest(
                unit_id=env.chain["unit"],
                variant_id=env.chain["variant"],
                staged=staged,
                dest=dest,
            )
        assert refused.value.code == "content.untrusted_entry"
        assert refused.value.reason
        assert not Path(dest).exists()
        assert env.files_under(env.store) == []  # not even the original: nothing to correct from
        with env.engine.connect() as connection:
            assert (
                connection.execute(
                    sa.select(sa.func.count()).select_from(storage.content)
                ).scalar_one()
                == 0
            )
            state = connection.execute(
                sa.select(catalog.unit.c.state).where(catalog.unit.c.id == env.chain["unit"])
            ).scalar_one()
            reason = connection.execute(
                sa.select(catalog.variant_revision.c.quarantine_reason)
            ).scalar_one()
        assert state == "quarantined"
        assert "content.untrusted_entry" in reason and refused.value.reason in reason


def test_a_replacement_source_leaves_the_refused_state() -> None:
    bad = epub_bytes(xhtml("<p>x</p>"), extra=[Member("OEBPS/app.js", b"alert(1)")])
    with lib_env() as env:
        dest, staged = file_in(env, bad)
        with pytest.raises(EpubRefusedError):
            store(env).ingest(
                unit_id=env.chain["unit"],
                variant_id=env.chain["variant"],
                staged=staged,
                dest=dest,
            )
        ingest(env, TRAPPED)  # the source was replaced by a file that passes
        with env.engine.connect() as connection:
            state = connection.execute(
                sa.select(catalog.unit.c.state).where(catalog.unit.c.id == env.chain["unit"])
            ).scalar_one()
        assert state != "quarantined"


# -- never served: the original, whatever the road -----------------------------------------------


@pytest.mark.req("EF-BIB-006", "EF-BIB-045")
def test_untrusted_original_never_served_to_pull_destination() -> None:
    """What a `pull` destination serves is the derivative: never the file of /store."""
    with lib_env() as env:
        _result, library = ingest(env)
        (kept,) = env.files_under(env.store)
        served = servable_epub(env.engine, env.fsops, env.chain["unit"])
        assert Path(served.path) == library
        assert Path(served.path) != kept
        assert not has_active_content(Path(served.path).read_bytes())


def test_original_in_library_is_never_served_whatever_put_it_there() -> None:
    """Re-export after a restore, adoption, repair: if /library holds the original bytes, nothing
    is served; the way out is the regeneration from the original, never a copy of it."""
    with lib_env() as env:
        _result, library = ingest(env)
        library.write_bytes(TRAPPED)  # what a restore or an adoption could leave there
        with pytest.raises(OriginalNotServableError):
            servable_epub(env.engine, env.fsops, env.chain["unit"])
        repaired = store(env).regenerate(env.chain["unit"])  # repair: from the original
        assert repaired.source == "original"
        served = servable_epub(env.engine, env.fsops, env.chain["unit"])
        assert not has_active_content(Path(served.path).read_bytes())


def test_epub_without_derivative_is_not_servable() -> None:
    """An `untrusted` content with no `derived_hash` (adopted in place, say) serves nothing."""
    with lib_env() as env:
        _result, _library = ingest(env)
        with env.engine.begin() as connection:
            connection.execute(
                sa.update(storage.content).values(
                    derived_hash=None, derived_by=None, derived_hash_by=None
                )
            )
        with pytest.raises(OriginalNotServableError):
            servable_epub(env.engine, env.fsops, env.chain["unit"])


def test_library_file_that_is_not_the_derivative_is_not_servable() -> None:
    with lib_env() as env:
        _result, library = ingest(env)
        library.write_bytes(CLEAN)  # some other EPUB under the same name
        with pytest.raises(OriginalNotServableError):
            servable_epub(env.engine, env.fsops, env.chain["unit"])


def test_publisher_epub_is_servable_as_filed() -> None:
    with lib_env() as env:
        set_binding_trust(env, "publisher")
        _result, library = ingest(env, CLEAN)
        assert Path(servable_epub(env.engine, env.fsops, env.chain["unit"]).path) == library


# -- extracted_derived ----------------------------------------------------------------------------


def _rendered_for_destination(derived: bytes) -> bytes:
    """What an export wrote: the derivative with another OPF (the only part it may rewrite)."""
    members = []
    with zipfile.ZipFile(io.BytesIO(derived)) as zf:
        for info in zf.infolist():
            data = zf.read(info.filename)
            if info.filename.endswith(".opf"):
                data = data.replace(b"Synthetic", b"Rendered")
            members.append(Member(info.filename, data, deflate=info.compress_type != 0))
    return zip_bytes(members)


def test_restore_from_destination_marks_extracted_derived() -> None:
    """Only the derivative is left (the original is lost): the file taken back from a destination
    is verified by `derived_hash` and recorded `extracted_derived`."""
    with lib_env() as env:
        original_result, library = ingest(env)
        found = _rendered_for_destination(library.read_bytes())
    with lib_env() as env:  # a fresh instance: nothing but the destination's file
        dest, staged = file_in(env, found)
        got = store(env).restore_from_destination(
            unit_id=env.chain["unit"],
            variant_id=env.chain["variant"],
            found=staged,
            dest=dest,
            expected_derived_hash=original_result.derived_hash,
            derived_by=DERIVED_BY,
        )
        row = content_row(env)
        assert row.store_provenance == "extracted_derived"
        assert row.derived_hash == original_result.derived_hash == got.derived_hash
        assert row.derived_by == DERIVED_BY
        assert env.files_under(env.store) == []  # there is no original to put in /store
        (replica,) = replicas(env)
        assert (replica.location, replica.fidelity) == ("library", "derived")
        with env.engine.connect() as connection:
            assert independent_copies(connection, env.chain["unit"]) == 1  # a readable copy
        assert not has_active_content(Path(dest).read_bytes())


def test_restore_from_destination_refuses_a_file_that_is_not_the_derivative() -> None:
    with lib_env() as env:
        original_result, _library = ingest(env)
        wanted = original_result.derived_hash
    with lib_env() as env:
        dest, staged = file_in(env, TRAPPED)  # the destination's file was swapped for the original
        with pytest.raises(OriginalNotServableError):
            store(env).restore_from_destination(
                unit_id=env.chain["unit"],
                variant_id=env.chain["variant"],
                found=staged,
                dest=dest,
                expected_derived_hash=wanted,
                derived_by=DERIVED_BY,
            )
        assert not Path(dest).exists()
        with env.engine.connect() as connection:
            assert (
                connection.execute(
                    sa.select(sa.func.count()).select_from(storage.content)
                ).scalar_one()
                == 0
            )


def test_irreplaceable_is_evaluated_without_the_original() -> None:
    """`independent_copies` counts the readable derivative once the original is gone."""
    with lib_env() as env:
        ingest(env)
        with env.engine.connect() as connection:
            assert independent_copies(connection, env.chain["unit"]) == 2  # original and derivative


# -- security update of the neutralizer -----------------------------------------------------------


def test_neutralizer_security_update_regenerates_from_original() -> None:
    with lib_env() as env:
        _result, library = ingest(env)
        before = library.stat().st_ino
        plan = reneutralizer(env).plan()
        (item,) = plan.items
        assert (item.source, item.from_by, item.to_by) == ("original", DERIVED_BY, V2)
        report = reneutralizer(env).apply(plan.fingerprint)
        assert report.regenerated == 1 and not report.blocked
        row = content_row(env)
        assert row.derived_by == V2
        assert [e["derived_by"] for e in row.derived_chain] == [DERIVED_BY]  # the lineage is kept
        assert row.original_sha256 == sha(TRAPPED)
        (kept,) = env.files_under(env.store)
        assert kept.read_bytes() == TRAPPED  # the original is never touched
        assert library.stat().st_ino != before  # an atomic replacement, a new inode
        assert not has_active_content(library.read_bytes())


def test_regeneration_without_original_restarts_from_stored_derivative_and_keeps_chain() -> None:
    with lib_env() as env:
        _result, library = ingest(env, with_store=False)
        plan = reneutralizer(env, with_store=False).plan()
        (item,) = plan.items
        assert item.source == "derived"
        reneutralizer(env, with_store=False).apply(plan.fingerprint)
        row = content_row(env)
        assert row.derived_by == V2
        assert [e["derived_by"] for e in row.derived_chain] == [DERIVED_BY]
        assert not has_active_content(library.read_bytes())

        # and a second update stacks on the first: the chain keeps every generation
        def v3(s: Any, o: Any, **kw: Any) -> Any:
            return replace(neutralize_epub(s, o, **kw), derived_by="xhtml-neutralizer@v3")

        again = Reneutralizer(
            store(env, with_store=False),
            engine=env.engine,
            neutralize=v3,
            target="xhtml-neutralizer@v3",
        )
        again.apply(again.plan().fingerprint)
        assert [e["derived_by"] for e in content_row(env).derived_chain] == [DERIVED_BY, V2]


def test_regeneration_refuses_a_stored_derivative_that_no_longer_verifies() -> None:
    """No original and a library file that is not the recorded derivative: blocked, not guessed."""
    with lib_env() as env:
        _result, library = ingest(env, with_store=False)
        library.write_bytes(TRAPPED)
        plan = reneutralizer(env, with_store=False).plan()
        (item,) = plan.items
        assert item.source == "blocked" and item.reason
        report = reneutralizer(env, with_store=False).apply(plan.fingerprint)
        assert report.regenerated == 0 and report.blocked == 1
        assert library.read_bytes() == TRAPPED  # untouched: nothing was replaced


def test_reneutralize_plan_then_confirmation() -> None:
    with lib_env() as env:
        _result, _library = ingest(env)
        plan = reneutralizer(env).plan()
        assert content_row(env).derived_by == DERIVED_BY  # a plan changes nothing
        with pytest.raises(StalePlanError):
            reneutralizer(env).apply("0" * 64)  # not the plan the admin saw
        assert content_row(env).derived_by == DERIVED_BY  # nothing moved
        reneutralizer(env).apply(plan.fingerprint)
        assert content_row(env).derived_by == V2
        assert reneutralizer(env).plan().items == ()  # up to date: nothing to do


def test_reneutralize_never_deletes_before_the_new_file_is_verified() -> None:
    """A neutralizer that fails (or writes nothing worth keeping) leaves the old file and rows."""

    def broken(source: Path, out: Any, **kw: Any) -> Any:
        out.write(b"not an epub")
        raise RuntimeError("the new release crashed")

    with lib_env() as env:
        _result, library = ingest(env)
        before = (library.read_bytes(), library.stat().st_ino)
        r = reneutralizer(env, neutralize=broken)
        report = r.apply(r.plan().fingerprint)
        assert report.regenerated == 0 and report.failed == 1
        assert (library.read_bytes(), library.stat().st_ino) == before
        assert content_row(env).derived_by == DERIVED_BY
        assert [p for p in env.files_under(env.library) if p != library] == []  # no stray file


def test_reneutralize_leaves_current_derivatives_alone() -> None:
    with lib_env() as env:
        ingest(env)
        r = Reneutralizer(store(env), engine=env.engine)  # the shipped release: already current
        assert r.plan().items == ()


# -- adoption -------------------------------------------------------------------------------------


def test_adoption_report_flags_active_content_in_untrusted_epub() -> None:
    from tests.adopt_env import adoption
    from tests.export_env import export_env

    with export_env() as env:
        a = adoption(env)
        a.add_series(
            "Shelf", {"Book.epub": TRAPPED, "Clean.epub": CLEAN, "Std.epub": standard_epub3()}
        )
        from tests.kavita_env import run

        report = run(a.adopter.preview(a.wired.client, env.destination_id))
        flagged = {p.rsplit("/", 1)[-1] for p in report.active_content}
        assert flagged == {"Book.epub"}  # the clean one is not reported, nothing is rewritten


# -- governance -----------------------------------------------------------------------------------


def test_replacements_in_library_are_classed() -> None:
    """Every code site that replaces a file in place (`replace=True`) is classed, as the deletion
    emitters are: a new one is a security decision."""
    import ast

    from shelfhold.identity.public import REPLACEMENT_SITES

    src = Path(__file__).resolve().parents[2] / "src" / "shelfhold"
    found: set[str] = set()

    class Scan(ast.NodeVisitor):
        def __init__(self, module: str) -> None:
            self.module, self.stack = module, []

        def _enter(self, node: Any) -> None:
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _enter

        def visit_Call(self, node: ast.Call) -> None:
            name = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if name in {"write_atomic", "copy_independent", "link_replaceable", "rename"} and any(
                k.arg == "replace" and isinstance(k.value, ast.Constant) and k.value.value is True
                for k in node.keywords
            ):
                found.add(f"{self.module}::{'.'.join(self.stack)}")
            self.generic_visit(node)

    for path in sorted(src.rglob("*.py")):
        module = "shelfhold/" + path.relative_to(src).as_posix()
        if "/infrastructure/fsops/" in module:
            continue
        Scan(module).visit(ast.parse(path.read_text(encoding="utf-8")))
    assert found == set(REPLACEMENT_SITES), (
        f"unclassed: {sorted(found - set(REPLACEMENT_SITES))}; "
        f"stale: {sorted(set(REPLACEMENT_SITES) - found)}"
    )
    assert all(REPLACEMENT_SITES[site] for site in found)


def test_content_reneutralize_is_a_command() -> None:
    import argparse

    from shelfhold.interfaces.cli import content

    parser = argparse.ArgumentParser()
    content.register(parser.add_subparsers())
    args = parser.parse_args(["content", "reneutralize", "--confirm", "abc"])
    assert args.confirm == "abc"
    assert parser.parse_args(["content", "reneutralize"]).confirm is None
