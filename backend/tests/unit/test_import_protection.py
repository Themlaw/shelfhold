"""M2 L20: what a dropped file is, whether it is protected, whether it looks watermarked.

Detection only reads. A protected file is refused, never opened further; nothing in the code base
decrypts, strips or asks anyone for a key.
"""

import ast
import hashlib
import tomllib
from pathlib import Path

import pytest

from shelfhold.i18n import t
from shelfhold.infrastructure.content_trust.protection import (
    PROTECTED,
    PROTECTED_LICENCE,
    detect_protection,
    watermark_hint,
)
from shelfhold.library.adapters.import_scan import (
    NAME_COLLISION,
    UNSAFE_NAME,
    check_name,
    collision_key,
    display_path,
    import_kind,
    is_audio_head,
)
from shelfhold.library.adapters.pdf_store import NotAPdfError, check_pdf_shape
from shelfhold.sdk.testing.corpus.trapped_epubs.build import Member, zip_bytes
from tests.import_env import (
    ADOBE_FONTS,
    AES,
    CLEAN_EPUB,
    IDPF,
    MP3,
    PDF,
    encryption_xml,
    epub_with,
    personalised_opf,
)

SRC = Path(__file__).resolve().parents[2] / "src" / "shelfhold"
FONT = Member("OEBPS/fonts/a.ttf", b"\x00\x01\x00\x00" + b"\x00" * 12, deflate=False)


def at(tmp_path: Path, data: bytes, name: str = "book.bin") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


@pytest.mark.req("EF-SRC-012")
def test_protected_epub_refused_with_message(tmp_path: Path) -> None:
    """Given an EPUB whose encryption.xml encrypts a chapter, then it is protected, and the
    message says so in plain words."""
    book = epub_with(Member("META-INF/encryption.xml", encryption_xml(AES, "OEBPS/ch1.xhtml")))
    found = detect_protection(at(tmp_path, book), "epub")
    assert found is not None and found.code == PROTECTED == "import.protected"
    assert "protégé" in t("errors.import.protected.title")
    assert "dossier d'import" in t("errors.import.protected.action")


@pytest.mark.req("EF-SRC-012")
def test_rights_document_is_a_protection(tmp_path: Path) -> None:
    book = epub_with(Member("META-INF/rights.xml", b"<rights/>"))
    found = detect_protection(at(tmp_path, book), "epub")
    assert found is not None and found.code == PROTECTED


@pytest.mark.req("EF-SRC-012")
def test_protection_is_found_whatever_the_letter_case(tmp_path: Path) -> None:
    book = epub_with(Member("META-INF/Rights.XML", b"<rights/>"))
    assert detect_protection(at(tmp_path, book), "epub") is not None


@pytest.mark.req("EF-SRC-012")
def test_lcp_protected_file_gets_dedicated_message(tmp_path: Path) -> None:
    """A licence document, or an encryption.xml that names a licence, has its own answer: the
    way out is a loan or a purchase."""
    by_licence = epub_with(Member("META-INF/license.lcpl", b"{}"))
    found = detect_protection(at(tmp_path, by_licence), "epub")
    assert found is not None and found.code == PROTECTED_LICENCE == "import.protected_licence"
    marked = epub_with(
        Member(
            "META-INF/encryption.xml",
            encryption_xml(
                AES,
                "OEBPS/ch1.xhtml",
                '<KeyInfo><RetrievalMethod URI="x" '
                'Type="http://readium.org/2014/01/lcp#EncryptedContentKey"/></KeyInfo>',
            ),
        )
    )
    other = detect_protection(at(tmp_path, marked, "b.bin"), "epub")
    assert other is not None and other.code == PROTECTED_LICENCE
    title = t("errors.import.protected_licence.title")
    assert "licence" in title
    assert "médiathèque" in t("errors.import.protected_licence.action")
    assert title != t("errors.import.protected.title")


@pytest.mark.req("EF-SRC-012")
def test_licence_document_in_a_comic_archive_is_protected_too(tmp_path: Path) -> None:
    archive = zip_bytes(
        [Member("001.png", b"\x89PNG\r\n\x1a\n"), Member("META-INF/license.lcpl", b"{}")]
    )
    found = detect_protection(at(tmp_path, archive), "cbz")
    assert found is not None and found.code == PROTECTED_LICENCE


@pytest.mark.req("EF-SRC-012")
def test_font_obfuscation_not_treated_as_protection(tmp_path: Path) -> None:
    """Both font manglings of the EPUB standard, applied to fonts, are not a protection."""
    for algorithm in (IDPF, ADOBE_FONTS):
        book = epub_with(
            FONT, Member("META-INF/encryption.xml", encryption_xml(algorithm, "OEBPS/fonts/a.ttf"))
        )
        assert detect_protection(at(tmp_path, book, "f.bin"), "epub") is None, algorithm


@pytest.mark.req("EF-SRC-012")
def test_font_obfuscation_on_a_chapter_is_a_protection(tmp_path: Path) -> None:
    """The mangling name on something that is not a font hides content: refused as protected."""
    book = epub_with(Member("META-INF/encryption.xml", encryption_xml(IDPF, "OEBPS/ch1.xhtml")))
    found = detect_protection(at(tmp_path, book), "epub")
    assert found is not None and found.code == PROTECTED


@pytest.mark.req("EF-SRC-012")
def test_unreadable_encryption_document_is_refused_not_ignored(tmp_path: Path) -> None:
    book = epub_with(Member("META-INF/encryption.xml", b"<encryption><broken"))
    found = detect_protection(at(tmp_path, book), "epub")
    assert found is not None and found.code == PROTECTED


@pytest.mark.req("EF-SRC-012")
def test_clean_files_are_not_protected(tmp_path: Path) -> None:
    assert detect_protection(at(tmp_path, CLEAN_EPUB), "epub") is None
    assert detect_protection(at(tmp_path, PDF, "a.pdf"), "pdf") is None


@pytest.mark.req("EF-SRC-012")
def test_encrypted_pdf_is_a_protection_wherever_the_entry_sits(tmp_path: Path) -> None:
    trailer = b"trailer\n<< /Root 1 0 R /Encrypt 7 0 R >>\n%%EOF\n"
    assert detect_protection(at(tmp_path, PDF + trailer, "a.pdf"), "pdf") is not None
    inline = b"trailer\n<< /Encrypt << /Filter /Standard >> >>\n"
    assert detect_protection(at(tmp_path, PDF + inline, "b.pdf"), "pdf") is not None
    # padding cannot push it out of a window: it sits after 5 MiB of filler
    padded = PDF + b"\n%" + b"x" * (5 * 1024 * 1024) + trailer
    assert detect_protection(at(tmp_path, padded, "c.pdf"), "pdf") is not None
    # a word that only starts like it is not it
    assert detect_protection(at(tmp_path, PDF + b"/EncryptMetadata false", "d.pdf"), "pdf") is None


@pytest.mark.req("EF-SRC-012", "ENF-LEG-009")
def test_never_attempts_to_lift_protection(tmp_path: Path) -> None:
    """Detection leaves the file byte for byte as it was, and no module that reads a protection
    can decrypt, strip or unlock: no such import, no such call, in the whole package."""
    book = epub_with(Member("META-INF/encryption.xml", encryption_xml(AES, "OEBPS/ch1.xhtml")))
    path = at(tmp_path, book)
    before = (hashlib.sha256(book).hexdigest(), path.stat().st_mtime_ns)
    detect_protection(path, "epub")
    assert (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns) == before

    banned_imports = {"cryptography", "Crypto", "Cryptodome", "pikepdf", "pypdf", "PyPDF2", "fitz"}
    banned_calls = ("decrypt", "unlock", "unprotect", "strip_drm", "remove_drm", "crack", "bypass")
    for module in ("infrastructure/content_trust/protection.py", "library/adapters/import_scan.py",
                   "library/adapters/pdf_store.py"):  # fmt: skip
        tree = ast.parse((SRC / module).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import | ast.ImportFrom):
                names = [a.name for a in node.names] + [getattr(node, "module", "") or ""]
                assert not {n.split(".")[0] for n in names} & banned_imports, module
            if isinstance(node, ast.FunctionDef | ast.Call | ast.Attribute):
                label = (
                    node.name if isinstance(node, ast.FunctionDef)
                    else getattr(node, "attr", getattr(getattr(node, "func", None), "id", ""))
                )  # fmt: skip
                assert not any(word in str(label).lower() for word in banned_calls), (module, label)


@pytest.mark.req("EF-SRC-012")
def test_the_messages_name_no_tool_and_no_site() -> None:
    """The user-facing texts of the import say « fichier protégé » and nothing else."""
    text = (SRC / "locales" / "fr.toml").read_text(encoding="utf-8")
    table = tomllib.loads(text)["errors"]
    for code, entry in table.items():
        if code.startswith("import."):
            words = (entry["title"] + " " + entry["action"]).lower()
            for brand in ("adobe", "adept", "readium", "kindle", "kobo", "drm", "lcp"):
                assert brand not in words, (code, brand)


# -- the watermark hint --------------------------------------------------------------------------


@pytest.mark.req("EF-BIB-041")
def test_watermark_heuristic_sets_flag(tmp_path: Path) -> None:
    """The three signs a personalised copy typically carries; each gives a closed word."""
    extra = epub_with(Member("META-INF/customer.dat", b"x"))
    assert watermark_hint(at(tmp_path, extra, "a.bin"), "epub") == "extra_meta_inf_entry"
    meta = epub_with(opf_bytes=personalised_opf())
    assert watermark_hint(at(tmp_path, meta, "b.bin"), "epub") == "personalised_metadata"
    named = epub_with(Member("OEBPS/watermark.xhtml", b"<html/>"))
    assert watermark_hint(at(tmp_path, named, "c.bin"), "epub") == "entry_name"
    comic = zip_bytes([Member("001.png", b"\x89PNG"), Member("watermark.png", b"\x89PNG")])
    assert watermark_hint(at(tmp_path, comic, "d.bin"), "cbz") == "entry_name"


@pytest.mark.req("EF-BIB-041")
def test_watermark_heuristic_is_quiet_on_an_ordinary_book(tmp_path: Path) -> None:
    ordinary = epub_with(Member("META-INF/calibre_bookmarks.txt", b"x"))
    assert watermark_hint(at(tmp_path, ordinary), "epub") is None
    assert watermark_hint(at(tmp_path, CLEAN_EPUB, "b.bin"), "epub") is None
    assert watermark_hint(at(tmp_path, PDF, "c.bin"), "pdf") is None


# -- what a file is ------------------------------------------------------------------------------


@pytest.mark.req("EF-SRC-012", "EF-BIB-051")
def test_the_kind_is_read_in_the_bytes_never_in_the_name(tmp_path: Path) -> None:
    assert import_kind(at(tmp_path, CLEAN_EPUB, "a.cbz")) == "epub"
    assert import_kind(at(tmp_path, PDF, "b.epub")) == "pdf"
    assert import_kind(at(tmp_path, MP3, "c.cbz")) == "audio"
    assert import_kind(at(tmp_path, zip_bytes([Member("1.png", b"x")]), "d.epub")) == "cbz"
    assert import_kind(at(tmp_path, b"hello", "e.pdf")) == "unknown"
    assert import_kind(at(tmp_path, b"", "f.cbz")) == "unknown"
    # audio by its name when its bytes are nothing else: still audio, never silently "unknown"
    assert import_kind(at(tmp_path, b"\x00" * 40, "g.m4b"), "g.m4b") == "audio"


@pytest.mark.req("EF-SRC-012")
def test_import_rejects_audio_until_audio_edition_key() -> None:
    heads = [
        b"ID3\x03" + b"\x00" * 20, b"\xff\xfb\x90\x00" + b"\x00" * 20, b"OggS" + b"\x00" * 20,
        b"fLaC" + b"\x00" * 20, b"RIFF\x00\x00\x00\x00WAVEfmt ", b"\x00\x00\x00\x20ftypM4B \x00",
        b"\x00\x00\x00\x20ftypM4A \x00", b"FORM\x00\x00\x00\x00AIFFCOMM",
    ]  # fmt: skip
    for head in heads:
        assert is_audio_head(head), head
    assert not is_audio_head(b"PK\x03\x04" + b"\x00" * 20)
    assert not is_audio_head(PDF[:32])
    assert "audio" in t("errors.import.audio_unsupported.title").lower()


@pytest.mark.req("EF-SRC-012")
def test_a_pdf_that_is_also_an_archive_is_not_a_pdf(tmp_path: Path) -> None:
    check_pdf_shape(at(tmp_path, PDF, "ok.pdf"))
    with pytest.raises(NotAPdfError):
        check_pdf_shape(at(tmp_path, b"\x00" + PDF, "shifted.pdf"))  # signature not first
    polyglot = PDF + zip_bytes([Member("a.js", b"alert(1)")])
    with pytest.raises(NotAPdfError):
        check_pdf_shape(at(tmp_path, polyglot, "poly.pdf"))


# -- names ---------------------------------------------------------------------------------------


@pytest.mark.req("EF-SRC-012", "ENF-SEC-002")
@pytest.mark.parametrize(
    "name",
    [
        "..", ".", "", "a/b.cbz", "a\\b.cbz", "a\x00b.cbz", "a\x01b.cbz", "a\nb.cbz", "a\x7fb.cbz",
        "evil\u202egnp.cbz", "a\u200bb.cbz", "book.cbz ", "book.", " book.cbz", "x" * 300 + ".cbz",
        "lone\udcffsurrogate.cbz", "a\u2028b.cbz",
    ],
)  # fmt: skip
def test_a_trapped_name_is_refused(name: str) -> None:
    assert check_name(name) == UNSAFE_NAME


@pytest.mark.req("EF-SRC-012")
@pytest.mark.parametrize(
    "name",
    ["Book 1.cbz", "vol. 01 (2020).epub", "Café.pdf", "日本語 第1巻.cbz", "a..b.cbz", "-x.cbz"],
)
def test_an_ordinary_name_is_accepted(name: str) -> None:
    assert check_name(name) is None


@pytest.mark.req("EF-SRC-012")
def test_unicode_variants_of_one_name_collide() -> None:
    composed, decomposed = "café.cbz", "café.cbz"
    assert composed != decomposed and collision_key(composed) == collision_key(decomposed)
    assert collision_key("Book.cbz") == collision_key("BOOK.CBZ")
    assert collision_key("ﬁle.cbz") == collision_key("file.cbz")  # a ligature
    assert NAME_COLLISION == "import.name_collision"


@pytest.mark.req("EF-SRC-012")
def test_a_shown_path_carries_no_control_character() -> None:
    shown = display_path("a\x1b[31m/\u202ename.cbz")
    assert shown.isascii() and "\x1b" not in shown and "\u202e" not in shown


PDF_HEAD = (
    b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n2 0 obj\n<< /Filter /Standard >>\nendobj\n"
)


def _pdf_with(trailer: bytes) -> bytes:
    return PDF_HEAD + b"trailer\n" + trailer + b"\n%%EOF\n"


@pytest.mark.req("EF-SRC-012", "ENF-LEG-009")
@pytest.mark.parametrize(
    "trailer",
    [
        b"<< /Root 1 0 R /Encr#79pt 2 0 R >>",  # a name spelled with an escape
        b"<< /Root 1 0 R /#45ncrypt 2 0 R >>",
        b"<< /Root 1 0 R /Encrypt%c\n2 0 R >>",  # a comment between the key and its value
        b"<< /Root 1 0 R /Encrypt 2%c\r0 R >>",
        b"<< /Root 1 0 R /Encrypt\x00\x00<< /Filter /Standard >> >>",
    ],
)
def test_pdf_protection_in_any_spelling_is_found(tmp_path: Path, trailer: bytes) -> None:
    found = detect_protection(at(tmp_path, _pdf_with(trailer), "x.pdf"), "pdf")
    assert found is not None and found.code == PROTECTED


@pytest.mark.req("EF-SRC-012", "ENF-LEG-009")
def test_pdf_protection_padded_across_blocks_is_found(tmp_path: Path) -> None:
    """A long run of blanks cannot push the value out of the window that is read."""
    body = PDF_HEAD + b"%" + b"x" * ((1 << 20) - len(PDF_HEAD) - 60)
    body += b"\ntrailer\n<< /Root 1 0 R /Encrypt" + b" " * 100 + b"2 0 R >>\n%%EOF\n"
    assert detect_protection(at(tmp_path, body, "x.pdf"), "pdf") is not None


@pytest.mark.req("EF-SRC-012")
def test_pdf_without_encrypt_is_clean(tmp_path: Path) -> None:
    assert detect_protection(at(tmp_path, _pdf_with(b"<< /Root 1 0 R >>"), "x.pdf"), "pdf") is None
