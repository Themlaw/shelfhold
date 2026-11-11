"""M2 L03, adverse review: each proof of concept of the review is a regression test."""

import io
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import shelfhold.library.adapters.epub_derivative as mod
from shelfhold.infrastructure.archives import safe_open_archive
from shelfhold.infrastructure.content_trust import (
    DERIVED_BY,
    epub_payload_hash,
    neutralize_epub,
)
from shelfhold.library.public import OriginalNotServableError, servable_epub
from shelfhold.sdk.testing.corpus.trapped_epubs.build import Member, epub_bytes, xhtml
from tests.integration.test_untrusted_epub_pipeline import (
    V2,
    ingest,
    reneutralizer,
    store,
)
from tests.library_env import lib_env

SCRIPT = "<p>x</p><script>alert(document.cookie)</script>"
META_EPUB = epub_bytes(
    xhtml("<p>chapter</p>"),
    opf_extra='<item id="evil" href="../META-INF/evil.xhtml" media-type="application/xhtml+xml"/>',
    extra=[Member("META-INF/evil.xhtml", xhtml(SCRIPT))],
)


def entries(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as z:
        return {n: z.read(n) for n in z.namelist()}


def test_a_page_listed_in_the_manifest_under_meta_inf_is_refused_not_copied_raw() -> None:
    """Blocker 1: nothing under META-INF/ reaches /library without being read."""
    with lib_env() as env, pytest.raises(Exception, match="meta_inf_item"):
        ingest(env, META_EPUB)
    with lib_env() as env:
        assert not env.files_under(env.library)


def test_a_derivative_with_an_active_meta_inf_page_is_not_restored_from_a_destination() -> None:
    with lib_env() as env:
        data = epub_bytes(xhtml("<p>c</p>"), extra=[Member("META-INF/evil.xhtml", xhtml(SCRIPT))])
        forged = env.stage(data, "found.epub")
        payload = epub_payload_hash(safe_open_archive(forged, name="x.epub"))
        with pytest.raises(OriginalNotServableError):
            store(env).restore_from_destination(
                unit_id=env.chain["unit"], variant_id=env.chain["variant"], found=forged,
                dest=str(env.manga / "Book" / "Book.epub"), expected_derived_hash=payload,
                derived_by=DERIVED_BY,
            )  # fmt: skip


def _mutate(source: Any) -> io.BytesIO:
    data = source.read() if hasattr(source, "read") else Path(source).read_bytes()
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as zin, zipfile.ZipFile(buf, "w") as zout:
        for info in zin.infolist():
            body = zin.read(info.filename)
            if info.filename.endswith("ch1.xhtml"):
                body = body.replace(b"<p>", b"<p class='v2'>", 1)
            zout.writestr(info, body)
    buf.seek(0)
    return buf


def test_regeneration_never_overwrites_a_file_the_user_put_in_the_library() -> None:
    """Blocker 2: the file at the derivative's path is not the one Shelfhold wrote."""
    with lib_env() as env:
        _r, library = ingest(env)
        mine = epub_bytes(xhtml("<p>my own corrected edition</p>"))
        library.write_bytes(mine)
        r = reneutralizer(env)
        plan = r.plan()
        (item,) = plan.items
        assert (item.source, item.reason) == ("blocked", "library_file_was_replaced")
        report = r.apply(plan.fingerprint)
        assert (report.regenerated, report.blocked) == (0, 1)
        assert library.read_bytes() == mine


def test_a_plan_is_stale_when_the_library_file_changes_after_it_was_shown() -> None:
    from shelfhold.library.public import StalePlanError

    with lib_env() as env:
        _r, library = ingest(env)
        r = reneutralizer(env)
        fingerprint = r.plan().fingerprint
        library.write_bytes(epub_bytes(xhtml("<p>another</p>")))
        with pytest.raises(StalePlanError):
            r.apply(fingerprint)


def test_a_crash_between_the_new_file_and_its_rows_never_blocks_the_unit() -> None:
    """Minor 3, without /store: the file landed, the replica rows were not committed."""
    with lib_env() as env:
        ingest(env, with_store=False)
        real = mod.record_library_rewrite

        def killed(*_a: Any, **_k: Any) -> None:
            raise SystemExit("killed")

        def v2(s: Any, o: Any, **kw: Any) -> Any:
            return replace(neutralize_epub(_mutate(s), o, **kw), derived_by=V2)

        mod.record_library_rewrite = killed
        try:
            r = reneutralizer(env, with_store=False, neutralize=v2)
            with pytest.raises(SystemExit):
                r.apply(r.plan().fingerprint)
        finally:
            mod.record_library_rewrite = real
        plan = reneutralizer(env, with_store=False, neutralize=v2).plan()
        assert all(i.source != "blocked" for i in plan.items)
        servable_epub(env.engine, env.fsops, env.chain["unit"])


def test_a_failed_write_restores_the_rows() -> None:
    with lib_env() as env:
        _r, library = ingest(env)
        before = library.read_bytes()

        class Boom(Exception):
            pass

        def refuse(*_a: Any, **_k: Any) -> Any:
            raise Boom

        real = env.fsops.write_atomic
        env.fsops.write_atomic = refuse  # type: ignore[method-assign]
        try:
            r = reneutralizer(env)
            report = r.apply(r.plan().fingerprint)
        finally:
            env.fsops.write_atomic = real  # type: ignore[method-assign]
        assert report.failed == 1
        assert library.read_bytes() == before
        servable_epub(env.engine, env.fsops, env.chain["unit"])


# -- minors ------------------------------------------------------------------------------------


def _found(env: Any, data: bytes) -> tuple[Path, str]:
    forged = env.stage(data, "found.epub")
    return forged, epub_payload_hash(safe_open_archive(forged, name="x.epub"))


def _clean_derivative() -> bytes:
    from tests.integration.test_untrusted_epub_pipeline import CLEAN

    out = io.BytesIO()
    neutralize_epub(io.BytesIO(CLEAN), out)
    return out.getvalue()


def test_restore_from_destination_refuses_a_second_epub_for_the_variant() -> None:
    from shelfhold.library.public import VariantAlreadyStoredError

    with lib_env() as env:
        ingest(env)
        forged, payload = _found(env, _clean_derivative())
        with pytest.raises(VariantAlreadyStoredError):
            store(env).restore_from_destination(
                unit_id=env.chain["unit"], variant_id=env.chain["variant"], found=forged,
                dest=str(env.manga / "Book" / "Other.epub"), expected_derived_hash=payload,
                derived_by=DERIVED_BY,
            )  # fmt: skip


def test_restore_from_destination_refuses_an_unknown_generation() -> None:
    with lib_env() as env:
        forged, payload = _found(env, _clean_derivative())
        with pytest.raises(OriginalNotServableError):
            store(env).restore_from_destination(
                unit_id=env.chain["unit"], variant_id=env.chain["variant"], found=forged,
                dest=str(env.manga / "Book" / "Book.epub"), expected_derived_hash=payload,
                derived_by="evil@v1",
            )  # fmt: skip


def test_a_destination_swapped_after_the_check_is_not_what_is_filed() -> None:
    """The file found is copied first: a swap of the original path changes nothing filed."""
    import shelfhold.library.adapters.epub_derivative as m
    from tests.integration.test_untrusted_epub_pipeline import TRAPPED

    with lib_env() as env:
        forged, payload = _found(env, _clean_derivative())
        real = m._is_clean

        def swapping(source: Any, **kw: Any) -> bool:
            ok = real(source, **kw)
            forged.write_bytes(TRAPPED)  # the other writer, after the check
            return ok

        m._is_clean = swapping
        try:
            dest = env.manga / "Book" / "Book.epub"
            store(env).restore_from_destination(
                unit_id=env.chain["unit"], variant_id=env.chain["variant"], found=forged,
                dest=str(dest), expected_derived_hash=payload, derived_by=DERIVED_BY,
            )  # fmt: skip
        finally:
            m._is_clean = real
        assert dest.read_bytes() != TRAPPED


def test_an_occupied_destination_leaves_nothing_in_store() -> None:
    from tests.integration.test_untrusted_epub_pipeline import TRAPPED

    with lib_env() as env:
        dest = env.manga / "Book" / "Book.epub"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"user file")
        staged = env.stage(TRAPPED, "received.epub")
        with pytest.raises(FileExistsError):
            store(env).ingest(
                unit_id=env.chain["unit"], variant_id=env.chain["variant"],
                staged=staged, dest=str(dest),
            )  # fmt: skip
        assert not env.files_under(env.store)
        assert dest.read_bytes() == b"user file"


def test_a_refused_second_variant_does_not_quarantine_a_unit_with_a_good_epub() -> None:
    import sqlalchemy as sa

    from shelfhold.infrastructure.schema import catalog
    from shelfhold.library.public import EpubRefusedError
    from tests.integration.test_schema_catalog import add_variant

    bad = epub_bytes(xhtml("<p>x</p>"), extra=[Member("OEBPS/app.js", b"alert(1)")])
    with lib_env() as env:
        ingest(env)
        with env.engine.begin() as c:
            c.execute(sa.update(catalog.unit).values(state="exported"))
            other = add_variant(c, env.chain["unit"], env.chain["binding"], "ch-alt")
        with pytest.raises(EpubRefusedError):
            store(env).ingest(
                unit_id=env.chain["unit"], variant_id=other,
                staged=env.stage(bad, "b.epub"), dest=str(env.manga / "Book" / "Alt.epub"),
            )  # fmt: skip
        with env.engine.connect() as c:
            state = c.execute(
                sa.select(catalog.unit.c.state).where(catalog.unit.c.id == env.chain["unit"])
            ).scalar()
        assert state == "exported"
        servable_epub(env.engine, env.fsops, env.chain["unit"])


def test_a_refusal_is_an_untrusted_entry_error() -> None:
    from shelfhold.infrastructure.content_trust.cbz_entries import UntrustedEntryError
    from shelfhold.library.public import EpubRefusedError

    assert issubclass(EpubRefusedError, UntrustedEntryError)


def test_an_epub_the_neutralizer_refuses_counts_as_active_content() -> None:
    from shelfhold.infrastructure.content_trust import find_active_content

    assert find_active_content(io.BytesIO(META_EPUB))
