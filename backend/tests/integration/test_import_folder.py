"""M2 L20: the import folder (EF-SRC-012, EF-BIB-041, ENF-LEG-009).

Real files, a real database, the real stores. Every guard has a test that tries to break it:
the source is never removed before a hash-verified storage; a protected or audio file stays; a
file still being written waits; a link or a trapped name is refused; an EPUB or a PDF that tries
to get round the limits is stopped; a watermark suggestion never blocks and never exempts.
"""

import errno
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.i18n import t
from shelfhold.library.adapters import import_scan
from shelfhold.library.adapters.export_source import export_sources
from shelfhold.library.adapters.import_scan import (
    ImportFolder,
    ImportFolderOverlapError,
    ImportOutcome,
)
from shelfhold.ops.diagnostic import Vocabulary, _content
from shelfhold.plugin_host.adapters.native.import_folder import build_manifest
from shelfhold.sdk.source import SourceManifest
from shelfhold.sdk.testing.corpus.trapped_epubs.build import Member, epub_bytes, xhtml, zip_bytes
from tests.import_env import (
    ADOBE_FONTS,
    AES,
    CLEAN_EPUB,
    IDPF,
    MP3,
    OLD,
    PDF,
    SCRIPT_EPUB,
    ImportEnv,
    encryption_xml,
    epub_with,
    import_env,
    personalised_opf,
)
from tests.library_env import cbz_bytes

CBZ = cbz_bytes()
REPO = Path(__file__).resolve().parents[3]


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def outcome_of(env: ImportEnv, **kw: Any) -> tuple[Any, list[ImportOutcome]]:
    return env.folder.run(**kw)


def only(outcomes: list[ImportOutcome]) -> ImportOutcome:
    assert len(outcomes) == 1, outcomes
    return outcomes[0]


def assert_stays(env: ImportEnv, rel: str, data: bytes) -> None:
    assert (env.incoming / rel).read_bytes() == data
    assert env.contents() == []


class Crash(BaseException):
    """The process dies: nothing below it may catch it and carry on."""


# -- the happy road --------------------------------------------------------------------


@pytest.mark.req("EF-SRC-012")
def test_import_folder_cbz_epub_pdf() -> None:
    """A CBZ, an EPUB and a PDF each become a Unit; each source leaves once stored."""
    with import_env() as env:
        env.drop("one.cbz", CBZ)
        env.drop("sub/two.epub", CLEAN_EPUB)
        env.drop("three.pdf", PDF)
        scan, outcomes = outcome_of(env)
        assert not scan.refused and not scan.waiting
        assert sorted(o.kind or "" for o in outcomes) == ["cbz", "epub", "pdf"]
        assert all(o.state == "stored" and o.removed for o in outcomes), outcomes
        assert [p for p in env.incoming.rglob("*") if p.is_file()] == []
        formats = sorted(c.format for c in env.contents())
        assert formats == ["cbz", "epub", "pdf"]
        assert len({c.unit_id for c in env.contents()}) == 3
        for row in env.contents():  # what was stored is what was dropped
            assert row.original_sha256 in {sha(CBZ), sha(CLEAN_EPUB), sha(PDF)}
        assert {o.rel for o in outcomes} == {"one.cbz", "sub/two.epub", "three.pdf"}


@pytest.mark.req("EF-SRC-012", "EF-BIB-051")
def test_the_import_is_untrusted_unless_declared_a_purchase() -> None:
    with import_env() as env:
        env.drop("a.epub", SCRIPT_EPUB)
        outcome_of(env)
        assert [i.trust for i in env.items] == ["untrusted"]
        row = env.contents()[0]
        assert row.derived_hash is not None  # the neutralizer ran
        assert row.watermarked is False and row.watermark_origin == "none"


@pytest.mark.req("EF-SRC-012", "EF-BIB-041")
def test_a_declared_purchase_is_publisher_and_watermarked() -> None:
    with import_env() as env:
        env.drop("bought.epub", CLEAN_EPUB)
        _scan, outcomes = outcome_of(env, declared_purchase=True)
        assert [i.trust for i in env.items] == ["publisher"]
        row = env.contents()[0]
        assert (row.watermarked, row.watermark_origin) == (True, "user")
        assert row.derived_hash is None  # a publisher file is never rewritten (ADR-0033)
        stored = next(env.lib.manga.rglob("*.epub"))
        assert stored.read_bytes() == CLEAN_EPUB
        assert only(outcomes).watermark == "user"


@pytest.mark.req("EF-SRC-012")
def test_a_pdf_is_kept_and_never_exported_unless_declared() -> None:
    """D9: an untrusted PDF is stored as it came, not exported, and says why."""
    with import_env() as env:
        env.drop("a.pdf", PDF + b"\n% /JS (app.alert(1))\n")
        _scan, outcomes = outcome_of(env)
        assert only(outcomes).code == "content.untrusted_pdf"
        unit = env.contents()[0].unit_id
        with env.lib.engine.connect() as connection:
            assert export_sources(connection, [unit], formats=("cbz", "epub")) == {}
            assert unit in export_sources(connection, [unit], formats=("pdf",))
        assert "PDF" in t("errors.content.untrusted_pdf.title")


# -- protected files and audio --------------------------------------------------------------------


@pytest.mark.req("EF-SRC-012")
def test_protected_epub_refused_with_message() -> None:
    with import_env() as env:
        book = epub_with(Member("META-INF/encryption.xml", encryption_xml(AES, "OEBPS/ch1.xhtml")))
        env.drop("locked.epub", book)
        _scan, outcomes = outcome_of(env)
        refused = only(outcomes)
        assert (refused.state, refused.code, refused.removed) == (
            "refused", "import.protected", False,
        )  # fmt: skip
        assert "fichier protégé" in t(f"errors.{refused.code}.title").lower()
        assert_stays(env, "locked.epub", book)
        assert env.entries() == {"locked.epub": ("refused", "import.protected")}
        assert env.router.calls == 0  # the store never saw it


@pytest.mark.req("EF-SRC-012")
def test_lcp_protected_file_gets_dedicated_message() -> None:
    with import_env() as env:
        book = epub_with(Member("META-INF/license.lcpl", b"{}"))
        env.drop("loan.epub", book)
        refused = only(outcome_of(env)[1])
        assert refused.code == "import.protected_licence"
        assert t(f"errors.{refused.code}.title") != t("errors.import.protected.title")
        assert_stays(env, "loan.epub", book)


@pytest.mark.req("EF-SRC-012")
def test_font_obfuscation_not_treated_as_protection() -> None:
    """Mangled fonts are not a protection: not refused as one. An untrusted EPUB meets the
    neutralizer, which refuses any encryption document with its own reason; a declared purchase
    (publisher) is stored as it came."""
    font = Member("OEBPS/fonts/a.ttf", b"\x00\x01\x00\x00" + b"\x00" * 12, deflate=False)

    def book(algorithm: str) -> bytes:
        return epub_with(
            font, Member("META-INF/encryption.xml", encryption_xml(algorithm, "OEBPS/fonts/a.ttf"))
        )

    with import_env() as env:
        env.drop("a.epub", book(IDPF))
        refused = only(outcome_of(env)[1])
        assert refused.state == "refused" and refused.code == "import.archive_refused"
    with import_env() as env:
        env.drop("a.epub", book(IDPF))
        env.drop("b.epub", book(ADOBE_FONTS))
        _scan, outcomes = outcome_of(env, declared_purchase=True)
        assert [o.state for o in outcomes] == ["stored", "stored"], outcomes
        assert all(o.removed for o in outcomes)


@pytest.mark.req("EF-SRC-012", "ENF-LEG-009")
def test_never_attempts_to_lift_protection() -> None:
    """A protected file is not opened past the check, not copied into the library, not changed;
    asking again changes nothing, and the answer is the same without reading it again."""
    with import_env() as env:
        book = epub_with(Member("META-INF/rights.xml", b"<rights/>"))
        path = env.drop("locked.epub", book)
        before = path.stat()
        outcome_of(env)
        again = only(outcome_of(env)[1])
        assert again.code == "import.protected"
        after = path.stat()
        assert (after.st_ino, after.st_size, after.st_mtime_ns) == (
            before.st_ino, before.st_size, before.st_mtime_ns,
        )  # fmt: skip
        assert env.contents() == [] and not list(env.lib.manga.rglob("*.epub"))
        assert not list((env.lib.tmp / "staging").glob("import-*"))


@pytest.mark.req("EF-SRC-012")
def test_protected_pdf_is_refused_and_stays() -> None:
    with import_env() as env:
        data = PDF + b"trailer\n<< /Encrypt 9 0 R >>\n"
        env.drop("locked.pdf", data)
        assert only(outcome_of(env)[1]).code == "import.protected"
        assert_stays(env, "locked.pdf", data)


@pytest.mark.req("EF-SRC-012")
def test_import_rejects_audio_until_audio_edition_key() -> None:
    """Audio by its bytes, whatever it is called: refused with its message, left in place."""
    with import_env() as env:
        for name in ("book.mp3", "disguised.cbz", "disguised.epub", "disguised.pdf"):
            env.drop(name, MP3)
        _scan, outcomes = outcome_of(env)
        assert {o.code for o in outcomes} == {"import.audio_unsupported"}
        assert len(outcomes) == 4
        for name in ("book.mp3", "disguised.cbz", "disguised.epub", "disguised.pdf"):
            assert (env.incoming / name).read_bytes() == MP3
        assert env.contents() == [] and env.router.calls == 0


@pytest.mark.req("EF-SRC-012")
def test_an_unknown_file_is_refused_with_a_message() -> None:
    with import_env() as env:
        env.drop("notes.txt", b"just text")
        assert only(outcome_of(env)[1]).code == "import.unsupported_file"
        assert (env.incoming / "notes.txt").exists()


# -- watermark --------------------------------------------------------------------


@pytest.mark.req("EF-BIB-041")
def test_watermark_heuristic_sets_flag() -> None:
    with import_env() as env:
        env.drop("copy.epub", epub_with(opf_bytes=personalised_opf()))
        outcome = only(outcome_of(env)[1])
        assert (outcome.state, outcome.watermark) == ("stored", "heuristic")
        row = env.contents()[0]
        assert (row.watermarked, row.watermark_origin) == (True, "heuristic")


@pytest.mark.req("EF-BIB-041", "EF-BIB-051")
def test_the_heuristic_never_exempts_the_neutralizer() -> None:
    """A suggestion is not a verdict: the file is untrusted and still gets its derivative, with
    the script out."""
    with import_env() as env:
        trapped = epub_bytes(
            xhtml("<p>x</p><script>alert(1)</script>"), opf_bytes=personalised_opf()
        )
        env.drop("copy.epub", trapped)
        outcome = only(outcome_of(env)[1])
        assert outcome.watermark == "heuristic"
        assert [i.trust for i in env.items] == ["untrusted"]
        row = env.contents()[0]
        assert row.derived_hash is not None
        library = next(env.lib.manga.rglob("*.epub"))
        assert b"<script" not in library.read_bytes() and b"alert(1)" not in library.read_bytes()


@pytest.mark.req("EF-BIB-041")
def test_the_heuristic_never_blocks_and_always_says_so() -> None:
    """A false positive costs a restriction, never a refusal: the file is stored, the outcome
    carries the flag, and nothing is silent."""
    with import_env() as env:
        env.drop("odd.epub", epub_with(opf_bytes=personalised_opf()))
        outcome = only(outcome_of(env)[1])
        assert outcome.state == "stored" and outcome.code is None
        assert outcome.watermark == "heuristic" and outcome.removed
        assert env.entries()["odd.epub"][0] == "removed"


@pytest.mark.req("EF-BIB-041")
def test_a_user_flag_is_never_lowered_by_the_heuristic() -> None:
    with import_env() as env:
        env.drop("a.epub", epub_with(Member("META-INF/anything.bin", b"x")))
        outcome_of(env, declared_purchase=True)
        assert env.contents()[0].watermark_origin == "user"


@pytest.mark.req("ENF-LEG-009", "EF-BIB-041")
def test_watermarked_never_in_diagnostic_bundle() -> None:
    """A file flagged by the import is cited by hash and size in the inventory, never by a name."""
    data = epub_with(opf_bytes=personalised_opf())
    with import_env() as env:
        env.drop("Secret Title.epub", data)
        outcome_of(env)
        with env.lib.engine.connect() as connection:
            inventory = _content(connection, Vocabulary.load())
        assert inventory["watermarked"] == [
            {"hash": sha(data), "size": env.contents()[0].size}
        ] or inventory["watermarked"][0]["hash"] == sha(data)
        assert len(inventory["watermarked"]) == 1  # type: ignore[arg-type]
        text = json.dumps(inventory)
        assert "Secret Title" not in text and "Licensed to" not in text


# -- removal: only after a hash-verified storage ------------------------------------------


@pytest.mark.req("EF-SRC-012")
def test_import_removed_only_after_hash_verified_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    """At the moment the source is taken out, a copy with the very same hash is on the disk."""
    seen: list[tuple[bool, bool]] = []
    with import_env() as env:
        env.drop("a.cbz", CBZ)
        real = ImportFolder._remove_verified

        def spy(self: ImportFolder, root_fd: int, item: Any, digest: str) -> bool:
            stored = [p for p in env.lib.manga.rglob("*.cbz") if sha(p.read_bytes()) == digest]
            seen.append((bool(stored), (env.incoming / "a.cbz").exists()))
            return real(self, root_fd, item, digest)

        monkeypatch.setattr(ImportFolder, "_remove_verified", spy)
        only(outcome_of(env)[1])
        assert seen == [(True, True)]
        assert not (env.incoming / "a.cbz").exists()


@pytest.mark.req("EF-SRC-012")
def test_crash_between_the_copy_and_the_verification_loses_nothing() -> None:
    """The process dies after the store accepted the file and before the read-back: the source is
    still there; the next run finds the copy, verifies it, removes the source and files nothing
    twice."""
    with import_env() as env:
        env.drop("a.cbz", CBZ)
        real = ImportFolder._stored_verified

        def die(self: ImportFolder, unit_id: str, digest: str) -> bool:
            raise Crash

        ImportFolder._stored_verified = die  # type: ignore[method-assign]
        try:
            with pytest.raises(Crash):
                env.folder.run()
        finally:
            ImportFolder._stored_verified = real  # type: ignore[method-assign]
        assert (env.incoming / "a.cbz").read_bytes() == CBZ
        assert len(env.contents()) == 1
        _scan, outcomes = env.folder.run()
        assert only(outcomes).removed
        assert not (env.incoming / "a.cbz").exists()
        assert len(env.contents()) == 1  # the same Unit, never a second one
        assert len(env.items) == 1


@pytest.mark.req("EF-SRC-012")
def test_crash_right_after_the_store_call_loses_nothing() -> None:
    with import_env() as env:
        env.drop("a.cbz", CBZ)
        real = env.router.ingest

        def ingest_then_die(**kw: Any) -> Any:
            real(**kw)
            raise Crash

        env.router.ingest = ingest_then_die  # type: ignore[method-assign]
        with pytest.raises(Crash):
            env.folder.run()
        assert (env.incoming / "a.cbz").read_bytes() == CBZ
        env.router.ingest = real  # type: ignore[method-assign]
        assert only(env.folder.run()[1]).removed and len(env.contents()) == 1


@pytest.mark.req("EF-SRC-012")
def test_a_stored_copy_with_another_hash_keeps_the_source() -> None:
    """The copy on the disk is not what was dropped (damaged after the store accepted it): the
    source stays, and the answer is a mismatch, not a success."""
    with import_env() as env:
        env.drop("a.cbz", CBZ)
        real = env.router.ingest

        def ingest_then_damage(**kw: Any) -> Any:
            result = real(**kw)
            path = Path(kw["dest"])
            damaged = bytearray(path.read_bytes())
            damaged[-1] ^= 0xFF
            path.write_bytes(bytes(damaged))
            return result

        env.router.ingest = ingest_then_damage  # type: ignore[method-assign]
        outcome = only(outcome_of(env)[1])
        assert (outcome.state, outcome.code, outcome.removed) == (
            "stored", "import.hash_mismatch", False,
        )  # fmt: skip
        assert (env.incoming / "a.cbz").read_bytes() == CBZ
        assert env.entries()["a.cbz"][0] == "stored"


@pytest.mark.req("EF-SRC-012")
def test_the_store_answering_another_hash_stores_nothing() -> None:
    """The hash counted at the copy is checked by the store: a staged file that changed is
    refused before anything is placed."""
    with import_env() as env:
        env.drop("a.cbz", CBZ)
        real = env.router.ingest

        def lie(**kw: Any) -> Any:
            return real(**{**kw, "expected_sha256": sha(b"another file")})

        env.router.ingest = lie  # type: ignore[method-assign]
        outcome = only(outcome_of(env)[1])
        assert (outcome.state, outcome.code) == ("failed", "import.hash_mismatch")
        assert (env.incoming / "a.cbz").read_bytes() == CBZ
        assert env.contents() == [] and not list(env.lib.manga.rglob("*.cbz"))


@pytest.mark.req("EF-SRC-012")
def test_disk_full_while_copying_keeps_the_source(monkeypatch: pytest.MonkeyPatch) -> None:
    with import_env() as env:
        env.drop("a.cbz", CBZ)

        def full(self: ImportFolder, fd: int, sink: Any) -> Any:
            sink.write(b"partial")
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(ImportFolder, "_copy", full)
        outcome = only(outcome_of(env)[1])
        assert (outcome.state, outcome.code, outcome.removed) == (
            "failed", "import.disk_full", False,
        )  # fmt: skip
        assert_stays(env, "a.cbz", CBZ)
        assert not list((env.lib.tmp / "staging").glob("import-*"))  # no half file left behind
        assert "Disque plein" in t("errors.import.disk_full.title")


@pytest.mark.req("EF-SRC-012")
def test_disk_full_while_filing_keeps_the_source() -> None:
    with import_env() as env:
        env.drop("a.cbz", CBZ)

        def full(**kw: Any) -> Any:
            raise OSError(errno.ENOSPC, "No space left on device")

        env.router.ingest = full  # type: ignore[method-assign]
        outcome = only(outcome_of(env)[1])
        assert (outcome.state, outcome.code) == ("failed", "import.disk_full")
        assert_stays(env, "a.cbz", CBZ)


@pytest.mark.req("EF-SRC-012")
def test_a_source_deleted_before_the_copy_is_verified_is_not_an_error() -> None:
    """The person removes the file themselves between the store and the verification: the library
    holds it, nothing is owed, nothing breaks."""
    with import_env() as env:
        env.drop("a.cbz", CBZ)
        real = ImportFolder._stored_verified

        def verified_then_gone(self: ImportFolder, unit_id: str, digest: str) -> bool:
            ok = real(self, unit_id, digest)
            (env.incoming / "a.cbz").unlink()
            return ok

        ImportFolder._stored_verified = verified_then_gone  # type: ignore[method-assign]
        try:
            outcome = only(outcome_of(env)[1])
        finally:
            ImportFolder._stored_verified = real  # type: ignore[method-assign]
        assert (outcome.state, outcome.removed) == ("stored", False)
        assert len(env.contents()) == 1


@pytest.mark.req("EF-SRC-012")
def test_a_source_vanishing_before_the_copy_waits() -> None:
    with import_env() as env:
        env.drop("a.cbz", CBZ)
        scan = env.folder.scan()
        (env.incoming / "a.cbz").unlink()
        outcome = env.folder.import_one(scan.ready[0])
        assert (outcome.state, outcome.code) == ("waiting", "import.not_ready")
        assert env.contents() == []


@pytest.mark.req("EF-SRC-012")
def test_a_source_replaced_after_it_was_read_is_not_removed() -> None:
    """Same inode and size, other bytes: it is somebody's other file, not the one we stored."""
    with import_env() as env:
        env.drop("a.cbz", CBZ)
        real = ImportFolder._stored_verified

        def verified_then_swapped(self: ImportFolder, unit_id: str, digest: str) -> bool:
            ok = real(self, unit_id, digest)
            path = env.incoming / "a.cbz"
            with path.open("r+b") as stream:
                stream.write(b"X" * 8)  # in place: same inode, same size
            return ok

        ImportFolder._stored_verified = verified_then_swapped  # type: ignore[method-assign]
        try:
            outcome = only(outcome_of(env)[1])
        finally:
            ImportFolder._stored_verified = real  # type: ignore[method-assign]
        assert (outcome.state, outcome.removed) == ("stored", False)
        assert (env.incoming / "a.cbz").read_bytes().startswith(b"XXXXXXXX")


@pytest.mark.req("EF-SRC-012", "EF-BIB-045")
def test_an_untrusted_epub_whose_original_was_not_kept_stays() -> None:
    """Without /store only the derivative is held: removing the source would lose the original."""
    with import_env(with_store=False) as env:
        env.drop("a.epub", SCRIPT_EPUB)
        outcome = only(outcome_of(env)[1])
        assert (outcome.state, outcome.code, outcome.removed) == (
            "stored", "import.original_not_kept", False,
        )  # fmt: skip
        assert (env.incoming / "a.epub").read_bytes() == SCRIPT_EPUB


@pytest.mark.req("EF-SRC-012")
def test_an_untrusted_epub_with_its_original_kept_is_removed() -> None:
    with import_env(with_store=True) as env:
        env.drop("a.epub", SCRIPT_EPUB)
        assert only(outcome_of(env)[1]).removed
        kept = [p for p in env.lib.store.rglob("*.epub") if p.read_bytes() == SCRIPT_EPUB]
        assert len(kept) == 1  # the original is on the disk, under its hash


@pytest.mark.req("EF-SRC-012")
def test_the_same_file_dropped_twice_is_one_unit_and_both_leave() -> None:
    with import_env() as env:
        env.drop("a.cbz", CBZ)
        env.drop("copy of a.cbz", CBZ)
        _scan, outcomes = outcome_of(env)
        assert all(o.removed for o in outcomes) and len(outcomes) == 2
        assert len(env.contents()) == 1 and len(env.items) == 1


@pytest.mark.req("EF-SRC-012")
def test_a_tombstoned_content_is_refused_and_stays() -> None:
    class Purged:
        def check_content(self, hashes: Any = (), **_: Any) -> bool:
            return True

    with import_env() as env:
        env.router.stores[".cbz"]._tombstones = Purged()
        env.drop("a.cbz", CBZ)
        outcome = only(outcome_of(env)[1])
        assert (outcome.state, outcome.code) == ("refused", "import.tombstoned")
        assert_stays(env, "a.cbz", CBZ)


# -- a file still being written --------------------------------------------------------------------


@pytest.mark.req("EF-SRC-012")
def test_a_file_just_dropped_waits() -> None:
    with import_env() as env:
        env.drop("fresh.cbz", CBZ, age=0)
        scan, outcomes = outcome_of(env)
        assert outcomes == [] and [v.code for v in scan.waiting] == ["import.not_ready"]
        assert (env.incoming / "fresh.cbz").exists() and env.contents() == []


@pytest.mark.req("EF-SRC-012")
def test_a_file_written_during_the_look_waits() -> None:
    """Size and modification time are compared across two looks: a file that grows between them
    is not read."""
    with import_env() as env:
        path = env.drop("growing.cbz", CBZ)

        def writer(_seconds: float) -> None:
            with path.open("ab") as stream:
                stream.write(b"more")
            env.age(path, OLD)  # the time is made to look old again: only the size moved

        env.build(sleep=writer)
        scan, outcomes = outcome_of(env)
        assert outcomes == [] and [v.code for v in scan.waiting] == ["import.not_ready"]
        assert env.contents() == []


@pytest.mark.req("EF-SRC-012")
def test_a_file_rewritten_with_the_same_size_waits() -> None:
    """The modification time catches a rewrite that kept the size."""
    with import_env() as env:
        path = env.drop("same-size.cbz", CBZ)

        def rewrite(_seconds: float) -> None:
            data = path.read_bytes()
            path.write_bytes(data[:-1] + b"Z")
            env.age(path, OLD - 5)

        env.build(sleep=rewrite)
        scan, outcomes = outcome_of(env)
        assert outcomes == [] and [v.code for v in scan.waiting] == ["import.not_ready"]


@pytest.mark.req("EF-SRC-012")
def test_a_file_that_changes_while_it_is_copied_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    with import_env() as env:
        path = env.drop("a.cbz", CBZ)
        real = ImportFolder._copy

        def copy_then_append(self: ImportFolder, fd: int, sink: Any) -> Any:
            result = real(self, fd, sink)
            with path.open("ab") as stream:
                stream.write(b"tail")
            return result

        monkeypatch.setattr(ImportFolder, "_copy", copy_then_append)
        outcome = only(outcome_of(env)[1])
        assert (outcome.state, outcome.code) == ("waiting", "import.changed_while_reading")
        assert env.contents() == [] and path.exists()


@pytest.mark.req("EF-SRC-012")
@pytest.mark.parametrize("name", ["a.cbz.part", "a.crdownload", ".hidden.cbz", "a.tmp"])
def test_partial_download_names_wait(name: str) -> None:
    with import_env() as env:
        env.drop(name, CBZ)
        scan, outcomes = outcome_of(env)
        assert outcomes == [] and [v.code for v in scan.waiting] == ["import.not_ready"]


# -- links, hard links, names --------------------------------------------------------------------


@pytest.mark.req("EF-SRC-012", "ENF-SEC-002")
def test_a_symlink_to_a_file_outside_is_refused() -> None:
    with import_env() as env:
        outside = env.lib.tmp / "outside.cbz"
        outside.write_bytes(CBZ)
        (env.incoming / "link.cbz").symlink_to(outside)
        scan, outcomes = outcome_of(env)
        assert outcomes == [] and [v.code for v in scan.refused] == ["import.link_refused"]
        assert outside.read_bytes() == CBZ and (env.incoming / "link.cbz").is_symlink()
        assert env.contents() == []


@pytest.mark.req("EF-SRC-012", "ENF-SEC-002")
def test_a_symlinked_folder_is_not_followed() -> None:
    with import_env() as env:
        outside = env.lib.tmp / "outside"
        outside.mkdir()
        (outside / "in.cbz").write_bytes(CBZ)
        env.age(outside / "in.cbz", OLD)
        (env.incoming / "dir").symlink_to(outside, target_is_directory=True)
        scan, outcomes = outcome_of(env)
        assert outcomes == [] and [v.code for v in scan.refused] == ["import.link_refused"]
        assert (outside / "in.cbz").exists()


@pytest.mark.req("EF-SRC-012", "ENF-SEC-002")
def test_a_hard_link_to_a_file_outside_is_refused_and_the_outside_file_survives() -> None:
    with import_env() as env:
        outside = env.lib.tmp / "outside.cbz"
        outside.write_bytes(CBZ)
        env.age(outside, OLD)
        os.link(outside, env.incoming / "hard.cbz")
        scan, outcomes = outcome_of(env)
        assert outcomes == [] and [v.code for v in scan.refused] == ["import.link_refused"]
        assert outside.read_bytes() == CBZ and (env.incoming / "hard.cbz").exists()
        assert env.contents() == []


@pytest.mark.req("EF-SRC-012", "ENF-SEC-002")
def test_a_file_swapped_for_a_link_after_the_scan_is_not_followed() -> None:
    """The window between the scan and the read: the name now leads outside. Every step opens
    without following, so the outside file is neither read nor removed."""
    with import_env() as env:
        env.drop("a.cbz", CBZ)
        scan = env.folder.scan()
        outside = env.lib.tmp / "outside.cbz"
        outside.write_bytes(cbz_bytes((("9.png", b"\x89PNG\r\n\x1a\n" + b"\x01" * 24),)))
        (env.incoming / "a.cbz").unlink()
        (env.incoming / "a.cbz").symlink_to(outside)
        outcome = env.folder.import_one(scan.ready[0])
        assert (outcome.state, outcome.code) == ("refused", "import.link_refused")
        assert outside.exists() and env.contents() == []


@pytest.mark.req("EF-SRC-012", "ENF-SEC-002")
def test_a_folder_swapped_for_a_link_after_the_scan_is_not_followed() -> None:
    with import_env() as env:
        env.drop("d/a.cbz", CBZ)
        scan = env.folder.scan()
        outside = env.lib.tmp / "outside"
        outside.mkdir()
        (outside / "a.cbz").write_bytes(CBZ)
        (env.incoming / "d" / "a.cbz").unlink()
        (env.incoming / "d").rmdir()
        (env.incoming / "d").symlink_to(outside, target_is_directory=True)
        outcome = env.folder.import_one(scan.ready[0])
        assert outcome.code == "import.link_refused"
        assert (outside / "a.cbz").exists() and env.contents() == []


@pytest.mark.req("EF-SRC-012", "ENF-SEC-002")
def test_trapped_names_are_refused_and_never_reach_a_path() -> None:
    with import_env() as env:
        names = ["ctl\x01.cbz", "esc\x1b[2J.cbz", "bidi\u202egnp.cbz", "trail.cbz.", "sp .cbz "]
        for name in names:
            env.drop(name, CBZ)
        env.drop("fine.cbz", CBZ)
        scan, outcomes = outcome_of(env)
        assert {v.code for v in scan.refused} == {"import.unsafe_name"}
        assert len(scan.refused) == len(names)
        assert all(
            v.rel.isascii() and "\x1b" not in v.rel and "\x01" not in v.rel for v in scan.refused
        )
        assert [o.rel for o in outcomes] == ["fine.cbz"]
        for name in names:
            assert (env.incoming / name).read_bytes() == CBZ
        for path in env.lib.library.rglob("*"):  # no stored file carries any of those names
            assert path.name.isascii() and "\x1b" not in path.name


@pytest.mark.req("EF-SRC-012")
def test_a_trapped_folder_name_refuses_what_is_under_it() -> None:
    with import_env() as env:
        env.drop("evil\u202e/inner.cbz", CBZ)
        scan, outcomes = outcome_of(env)
        assert outcomes == [] and [v.code for v in scan.refused] == ["import.unsafe_name"]
        assert (env.incoming / "evil\u202e" / "inner.cbz").exists()


@pytest.mark.req("EF-SRC-012")
def test_two_names_that_collide_after_normalisation_are_both_refused() -> None:
    with import_env() as env:
        env.drop("café.cbz", CBZ)
        env.drop("café.cbz", cbz_bytes((("1.png", b"\x89PNG\r\n\x1a\n" + b"\x02" * 24),)))
        env.drop("CAFÉ.cbz", CBZ)
        scan, outcomes = outcome_of(env)
        assert outcomes == [] and {v.code for v in scan.refused} == {"import.name_collision"}
        assert len(scan.refused) == 3 and env.contents() == []


@pytest.mark.req("EF-SRC-012")
def test_the_title_comes_from_the_name_but_the_path_never_does() -> None:
    with import_env() as env:
        env.drop("<script>alert(1)</script> vol 1.cbz", CBZ)
        outcome_of(env)
        assert "<script" not in env.items[0].title
        stored = next(env.lib.manga.rglob("*.cbz"))
        assert stored.parent.name == "Imported" and "script" not in stored.name


# -- the hostile content --------------------------------------------------------------------


@pytest.mark.req("EF-SRC-012", "EF-BIB-051")
def test_an_epub_named_as_a_cbz_still_meets_the_neutralizer() -> None:
    with import_env() as env:
        env.drop("sneaky.cbz", SCRIPT_EPUB)
        outcome = only(outcome_of(env)[1])
        assert outcome.kind == "epub"
        library = next(env.lib.manga.rglob("*.epub"))
        assert b"<script" not in library.read_bytes()


@pytest.mark.req("EF-SRC-012", "EF-BIB-051")
def test_a_trapped_epub_is_refused_and_stays() -> None:
    """An entry no category admits (a script file): the whole EPUB is refused, nothing is stored,
    the source stays with its message."""
    with import_env() as env:
        trapped = epub_with(Member("OEBPS/run.js", b"alert(1)"))
        env.drop("trap.epub", trapped)
        outcome = only(outcome_of(env)[1])
        assert (outcome.state, outcome.code, outcome.removed) == (
            "refused", "import.archive_refused", False,
        )  # fmt: skip
        assert (env.incoming / "trap.epub").read_bytes() == trapped
        assert env.contents() == [] and not list(env.lib.manga.rglob("*.epub"))


@pytest.mark.req("EF-SRC-012", "EF-BIB-051")
def test_an_epub_with_a_remote_reference_in_its_package_is_refused() -> None:
    bad = epub_bytes(
        xhtml("<p>x</p>"),
        opf_extra='<item id="x" href="https://example.invalid/a.xhtml" '
        'media-type="application/xhtml+xml"/>',
    )
    with import_env() as env:
        env.drop("remote.epub", bad)
        outcome = only(outcome_of(env)[1])
        assert outcome.state == "refused" and not outcome.removed
        assert (env.incoming / "remote.epub").exists()


@pytest.mark.req("EF-SRC-012", "ENF-SEC-003")
def test_a_zip_bomb_is_refused_and_stays() -> None:
    bomb = zip_bytes([Member("1.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * (200 * 1024 * 1024))])
    with import_env() as env:
        env.drop("bomb.cbz", bomb)
        outcome = only(outcome_of(env)[1])
        assert outcome.state == "refused" and outcome.code == "import.archive_refused"
        assert (env.incoming / "bomb.cbz").exists() and env.contents() == []


@pytest.mark.req("EF-SRC-012", "ENF-SEC-003")
def test_a_cbz_with_a_script_entry_is_refused_and_stays() -> None:
    data = zip_bytes([Member("1.png", b"\x89PNG\r\n\x1a\n"), Member("run.sh", b"#!/bin/sh")])
    with import_env() as env:
        env.drop("a.cbz", data)
        outcome = only(outcome_of(env)[1])
        assert (outcome.state, outcome.code) == ("refused", "import.archive_refused")
        assert (env.incoming / "a.cbz").read_bytes() == data


@pytest.mark.req("EF-SRC-012", "ENF-SEC-003")
def test_a_pdf_over_its_limit_is_refused_and_stays() -> None:
    big = PDF + b"%" + b"x" * 4096
    with import_env(max_pdf=2048, max_bytes=2048) as env:
        env.drop("big.pdf", big)
        outcome = only(outcome_of(env)[1])
        assert (outcome.state, outcome.code) == ("refused", "import.too_large")
        assert_stays(env, "big.pdf", big)


@pytest.mark.req("EF-SRC-012", "ENF-SEC-003")
def test_a_pdf_that_is_also_an_archive_is_refused() -> None:
    polyglot = PDF + zip_bytes([Member("run.js", b"alert(1)")])
    with import_env() as env:
        env.drop("poly.pdf", polyglot)
        outcome = only(outcome_of(env)[1])
        assert (outcome.state, outcome.code) == ("refused", "import.unsupported_file")
        assert_stays(env, "poly.pdf", polyglot)


@pytest.mark.req("EF-SRC-012", "ENF-SEC-003")
def test_a_pdf_whose_store_limit_is_lower_than_the_folder_limit_stops_at_the_store() -> None:
    """The two limits are independent: the PDF store refuses what the folder let through."""
    big = PDF + b"%" + b"x" * 4096
    with import_env(max_pdf=2048) as env:
        env.drop("big.pdf", big)
        outcome = only(outcome_of(env)[1])
        assert (outcome.state, outcome.code, outcome.removed) == (
            "refused",
            "import.too_large",
            False,
        )
        assert_stays(env, "big.pdf", big)


@pytest.mark.req("EF-SRC-012")
def test_a_file_over_the_folder_limit_is_refused_before_it_is_read() -> None:
    with import_env(max_bytes=100) as env:
        env.drop("a.cbz", CBZ)
        outcome = only(outcome_of(env)[1])
        assert outcome.code == "import.too_large"
        assert not list((env.lib.tmp / "staging").glob("import-*"))


# -- rows and re-runs --------------------------------------------------------------------


@pytest.mark.req("EF-SRC-012")
def test_a_refused_file_unchanged_is_not_read_again() -> None:
    with import_env() as env:
        book = epub_with(Member("META-INF/rights.xml", b"<rights/>"))
        env.drop("locked.epub", book)
        first = only(outcome_of(env)[1])
        reads = []
        real = ImportFolder._copy

        def counting(self: ImportFolder, fd: int, sink: Any) -> Any:
            reads.append(1)
            return real(self, fd, sink)

        ImportFolder._copy = counting  # type: ignore[method-assign]
        try:
            second = only(outcome_of(env)[1])
        finally:
            ImportFolder._copy = real  # type: ignore[method-assign]
        assert second.code == first.code and reads == []
        # a changed file is looked at again
        env.drop("locked.epub", CLEAN_EPUB)
        assert only(outcome_of(env)[1]).state == "stored"


@pytest.mark.req("EF-SRC-012")
def test_the_import_entries_record_each_decision() -> None:
    with import_env() as env:
        env.drop("ok.cbz", CBZ)
        env.drop("notes.txt", b"text")
        env.drop("ctl\x01.cbz", CBZ)
        outcome_of(env)
        entries = env.entries()
        assert entries["ok.cbz"] == ("removed", None)
        assert entries["notes.txt"] == ("refused", "import.unsupported_file")
        assert entries["ctl\\x01.cbz".replace("\\", "%5C")] == ("refused", "import.unsafe_name")


@pytest.mark.req("EF-SRC-012", "ENF-SEC-002")
def test_an_import_folder_that_holds_or_sits_in_a_written_root_is_refused() -> None:
    """The echo: what Shelfhold exports must never be taken in again."""
    with import_env() as env:
        for folder in (env.lib.library, env.lib.manga, env.lib.library.parent, env.lib.store):
            with pytest.raises(ImportFolderOverlapError):
                ImportFolder(
                    folder, engine=env.lib.engine, fsops=env.lib.fsops, store=env.router,
                    staging_dir=env.lib.tmp / "staging", target=lambda i: ("", ""),
                    place=lambda *a: "", keep_apart=(env.lib.library, env.lib.store),
                )  # fmt: skip


@pytest.mark.req("EF-SRC-012", "ENF-LEG-003")
def test_the_manifest_is_offline_untrusted_and_the_one_on_disk() -> None:
    manifest = build_manifest()
    on_disk = SourceManifest.model_validate_json(
        (REPO / "sources" / "import_folder" / "manifest.json").read_text("utf-8")
    )
    assert manifest == on_disk
    assert manifest.permissions.domains == [] and manifest.permissions.lan_targets == []
    assert manifest.content_trust == "untrusted"


@pytest.mark.req("EF-SRC-012")
def test_every_code_the_folder_gives_has_its_message() -> None:
    for code in import_scan.REFUSED_CODES:
        assert t(f"errors.{code}.title") and t(f"errors.{code}.action"), code
    assert sa  # the module under test imports sqlalchemy for the rows it keeps
