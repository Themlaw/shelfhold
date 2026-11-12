"""M2 on the image under test (L32): the books and web-novel journeys, each on its own stack.

Same method as the M1 journeys: the simulated Kavita and the simulated servers of `e2e/fakes`
behind a socket, a real library volume, the commands the administrator types and the requests the
SPA sends. Synthetic data only; the names of the sources resolve to an address of the stack's own
loopback (`m2.yaml`) and nothing leaves the network namespace (`Journey.egress()` is read at the
end of each journey).
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import pytest

from journey import KAVITA_URL, Journey, wait_for
from test_m1a_journey import install_by_commands

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fakes" / "m2"))
from books import Member, book, pdf_encrypted  # noqa: E402

BOOKS_SHELF = "Livres"


def books_install(j: Journey) -> None:
    """Kavita and its 18+ books shelf, as an administrator sets them up (no comic source)."""
    j.secrets()
    added = j.shelfhold(
        "target", "add", "kavita", "--url", "http://127.0.0.1:5000", "--user", "shelfhold",
        "--password-file", "/tmp/kavita.pw", "--accept-admin-risk",
        "--path-map", '[{"local": "/library", "remote": "/library", "mode": "rw"}]',
    )  # fmt: skip
    assert added.code == 0, added.err
    shelf = j.shelfhold("shelf", "create", BOOKS_SHELF, "--max-age", "18+", "--target", "kavita",
                        "--subpath", "livres")  # fmt: skip
    assert shelf.code == 0, shelf.err
    j.create_admin()


def library_epubs(j: Journey) -> list[str]:
    return [
        f for f in j.library_files() if f.endswith(".epub") and "/.shelfhold/" not in f
    ]


def kavita_titles(j: Journey) -> list[str]:
    return sorted(str(s["name"]) for s in j.kavita()["series"])  # type: ignore[attr-defined]


def sources_seen(j: Journey) -> list[str]:
    done = j.fake("sources", "http://127.0.0.1/_fake/state")
    return list(done["seen"])  # type: ignore[arg-type]


AES = "http://www.w3.org/2001/04/xmlenc#aes128-cbc"
ENCRYPTION = (
    '<?xml version="1.0"?><encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
    'xmlns:enc="http://www.w3.org/2001/04/xmlenc#"><enc:EncryptedData>'
    f'<enc:EncryptionMethod Algorithm="{AES}"/><enc:CipherData>'
    '<enc:CipherReference URI="OEBPS/ch1.xhtml"/></enc:CipherData></enc:EncryptedData></encryption>'
).encode()
PERSONAL = "<dc:rights>Licensed to Some Person, order 12345</dc:rights>"


def members(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {n: archive.read(n) for n in archive.namelist()}


def exported_epub(j: Journey, title: str) -> bytes:
    path = next(f for f in library_epubs(j) if "/livres/" in f and title in f)
    return j.read_bytes(path)


@pytest.mark.e2e_timeout(600)
@pytest.mark.req("EF-SRC-012", "EF-BIB-051", "EF-BIB-041", "EF-SRC-024")
def test_m2_import_folder_journey(journey_m2: Journey) -> None:
    """(3) An EPUB dropped in the import folder is stored, verified, removed from the folder and
    readable in Kavita (neutralized copy in the library, original in /store). (5) A protected EPUB
    and a protected PDF are refused with their message and stay where they are, untouched.
    (6) A watermarked purchase is kept as it is: every entry but the package document is
    identical, byte for byte, to what was dropped."""
    j = journey_m2
    books_install(j)
    hostile = book(
        "Le Livre du Foyer", lang="fr", body="<p>texte</p><script>alert(1)</script>"
    )
    protected = book(
        "Le Verrouillé", extra=(Member("META-INF/encryption.xml", ENCRYPTION),)
    )
    locked_pdf = pdf_encrypted()
    bought = book("L'Achat Tatoué", metadata=PERSONAL)
    j.put_bytes("/import/Le Livre du Foyer.epub", hostile)
    j.put_bytes("/import/locked.epub", protected)
    j.put_bytes("/import/locked.pdf", locked_pdf)
    j.put_bytes("/import/achats/L'Achat Tatoué.epub", bought)

    dry = j.shelfhold("import", "scan", "--dry-run")
    assert dry.code == 0 and "Prêt" in dry.out, dry
    assert wait_for(lambda: len(j.library_files()) <= 2, 1)  # a dry run moved nothing
    scan = j.shelfhold("import", "scan", "--purchase")
    assert scan.code == 0, scan.err
    assert scan.out.count("Fichier protégé") == 2 and "drapeau tatoué" in scan.out, (
        scan.out
    )
    # the refused files are still in the folder, bit for bit; the stored ones are gone from it
    assert j.read_bytes("/import/locked.epub") == protected
    assert j.read_bytes("/import/locked.pdf") == locked_pdf
    left = j.compose(
        "exec", "-T", "shelfhold", "find", "/import", "-type", "f"
    ).stdout.split()
    assert sorted(left) == ["/import/locked.epub", "/import/locked.pdf"], left

    assert j.shelfhold("jobs", "run", "export.run").code == 0
    assert wait_for(lambda: len(kavita_titles(j)) == 2, 180), kavita_titles(j)
    assert kavita_titles(j) == ["L'Achat Tatoué", "Le Livre du Foyer"]

    # (3) the library holds the neutralized copy, /store the original as it came
    served = exported_epub(j, "Le Livre du Foyer")
    assert b"<script" not in b"".join(members(served).values())
    kept = [
        f
        for f in j.compose(
            "exec", "-T", "shelfhold", "find", "/store", "-type", "f"
        ).stdout.split()
    ]
    assert any(j.read_bytes(f) == hostile for f in kept), kept

    # (6) the purchase: everything but the package document is the very same bytes
    got, sent = members(exported_epub(j, "Achat")), members(bought)
    opf = "OEBPS/content.opf"
    assert {n: b for n, b in got.items() if n != opf} == {
        n: b for n, b in sent.items() if n != opf
    }
    assert j.kavita()["violations"] == []
    assert j.egress() == []


def configure_sources(j: Journey) -> None:
    """What the administrator types to point the switched-on sources at their servers."""
    set_ = j.shelfhold(
        "plugin", "set", "gutenberg",
        'catalog_url="http://catalog.example/cache/epub/feeds/pg_catalog.csv"',
        'mirror_url="http://aleph.gutenberg.org"',
    )  # fmt: skip
    assert set_.code == 0, set_.err


def first_id(out: str) -> str:
    """The identifier on the first line of `source search`: `<source>  <id>  <title>`."""
    return out.strip().splitlines()[0].split()[1]


def run_job(j: Journey, kind: str) -> bool:
    """`jobs run <kind>`; False when the server was busy with its own write (database locked)."""
    return j.shelfhold("jobs", "run", kind).code == 0


def export_until(j: Journey, wanted: int) -> None:
    """Ask for the export until Kavita lists `wanted` series (each run also asks for the scan)."""

    def listed() -> bool:
        run_job(j, "export.run")
        # Kavita's own periodic scan of its folders (the export asks for one after a quiet moment)
        j.fake("kavita", f"{KAVITA_URL}/_fake/index?folder=/library/livres", post=True)
        return len(kavita_titles(j)) >= wanted

    assert wait_for(listed, 400, 10), kavita_titles(j)


def wait_epubs(j: Journey, wanted: int) -> None:
    assert wait_for(lambda: len(library_epubs(j)) >= wanted, 420), library_epubs(j)


def store_files(j: Journey) -> list[str]:
    """The Unit copies kept in /store. `/store/INDEX.jsonl` (the index of what /store says about
    itself, rewritten by the `store.index` job) is a legitimate non-archive file there, so it is
    no copy. Other non-archive files can sit there too (the journey still failed after this
    filter), so callers that open archives keep only the files with the ZIP signature."""
    done = j.compose(
        "exec", "-T", "shelfhold", "find", "/store", "-type", "f", check=False
    )
    return [f for f in done.stdout.split() if f.rsplit("/", 1)[-1] != "INDEX.jsonl"]


@pytest.mark.e2e_timeout(900)
@pytest.mark.req(
    "EF-SRC-007", "ENF-LEG-003", "EF-BIB-049", "EF-SRC-024", "ENF-PRIV-001"
)
def test_m2_gutenberg_to_kavita_books_journey(journey_m2: Journey) -> None:
    """(1) A novel of the books project, through the catalogue and the mirror the administrator
    named (never the website), reaches Kavita's Books shelf as a neutralized copy; the original
    is kept in /store. (7) On an instance of France, a book that may still be protected there
    is added all the same, with a warning that blocks nothing."""
    j = journey_m2
    books_install(j)
    configure_sources(j)
    found = j.shelfhold("source", "search", "gutenberg", "study")
    assert found.code == 0 and first_id(found.out) == "1001", found
    # the website is no source: nothing is asked of it, not even its name
    assert j.shelfhold("acquire", "https://www.gutenberg.org/ebooks/1001").code != 0
    assert (
        "1001" not in j.shelfhold("source", "search", "gutenberg", "audio").out
    )  # a sound is no book
    assert "5001" not in j.shelfhold("source", "search", "gutenberg", "audio").out
    assert j.shelfhold("source", "acquire", "gutenberg", "1001").code == 0
    wait_epubs(j, 1)
    export_until(j, 1)
    assert kavita_titles(j) == ["A Study in Test"]
    served = exported_epub(j, "A Study in Test")
    assert b"<script" not in b"".join(members(served).values())  # the neutralized copy
    kept = {f: j.read_bytes(f) for f in store_files(j)}
    archives = [data for data in kept.values() if data[:4] == b"PK\x03\x04"]
    assert any(b"<script" in b"".join(members(data).values()) for data in archives), sorted(kept)
    assert (
        j.shelfhold("status").out.count("peut encore être protégée") == 0
    )  # died 1920

    # (7) the instance is in France; a book whose author died in 1960 is added, with a warning
    assert j.shelfhold("config", "set", "instance.jurisdiction", "FR").code == 0
    assert j.shelfhold("source", "acquire", "gutenberg", "3001").code == 0
    wait_epubs(j, 2)
    export_until(j, 2)  # not blocked
    assert "peut encore être protégée" in j.shelfhold("status").out

    seen = sources_seen(j)
    assert {line.split()[0] for line in seen} <= {
        "catalog.example",
        "aleph.gutenberg.org",
    }, seen
    assert j.kavita()["violations"] == []
    assert j.egress() == []


@pytest.mark.e2e_timeout(900)
@pytest.mark.req("EF-SRC-007", "EF-SRC-024", "ENF-LEG-003", "ENF-PRIV-001")
def test_m2_wikisource_attribution_journey(journey_m2: Journey) -> None:
    """(2) A book of the collaborative library arrives with its attribution: the licence and the
    contributors travel in the file's package document, and no rights warning is raised (a text
    under a licence is not "public domain"). A book of the ebook publisher comes by its address."""
    j = journey_m2
    books_install(j)
    assert j.shelfhold("config", "set", "instance.jurisdiction", "FR").code == 0
    found = j.shelfhold("source", "search", "wikisource", "Comte")
    assert found.code == 0 and "Le Comte de Test" in found.out, found
    asked = j.shelfhold("acquire", "https://fr.wikisource.org/wiki/Le_Comte_de_Test")
    assert asked.code == 0, asked.err
    wait_epubs(j, 1)
    publisher = j.shelfhold(
        "acquire", "https://standardebooks.org/ebooks/jane-synth/le-test"
    )
    assert publisher.code == 0, publisher.err
    wait_epubs(j, 2)
    export_until(j, 2)
    assert kavita_titles(j) == ["Le Comte de Test", "Le Test"]
    package = members(exported_epub(j, "Le Comte de Test"))[
        "OEBPS/content.opf"
    ].decode()
    assert (
        "CC BY-SA" in package and "Alice Synth" in package and "Bruno Synth" in package
    )
    assert "wikisource" in package  # a link to the origin
    assert "peut encore être protégée" not in j.shelfhold("status").out
    hosts = {line.split()[0] for line in sources_seen(j)}
    assert hosts <= {
        "fr.wikisource.org",
        "ws-export.wmcloud.org",
        "standardebooks.org",
    }, hosts
    assert j.kavita()["violations"] == []
    assert j.egress() == []


def edition_of(j: Journey, title: str) -> tuple[str, str]:
    """`(edition id, work id)` of the Work called `title`."""
    rows = j.sql(
        "select e.id, w.id from edition e join work w on w.id = e.work_id "
        f"where w.title = '{title}'"
    )
    assert len(rows) == 1, rows
    return str(rows[0][0]), str(rows[0][1])


@pytest.mark.e2e_timeout(900)
@pytest.mark.req("EF-EXP-033", "EF-EXP-040", "EF-BIB-001", "EF-USR-015")
def test_m2_epub_age_only_adult_library_journey(journey_m2: Journey) -> None:
    """(4) DoD 03: an EPUB classed 16+ carries no age in its file; the age of its series is pushed
    through Kavita's API and locked; the book is in the adult library only, and the child account,
    which reads the family library alone, does not see it."""
    j = journey_m2
    books_install(j)
    family = j.shelfhold("shelf", "create", "Famille", "--max-age", "12+", "--target", "kavita",
                         "--subpath", "famille")  # fmt: skip
    assert family.code == 0, family.err
    original = book("Le Roman Seize", lang="fr")
    j.put_bytes("/import/Le Roman Seize.epub", original)
    assert j.shelfhold("import", "scan").code == 0
    edition, work = edition_of(j, "Le Roman Seize")
    # the language of the file is not read from it: the book waits, and the administrator says it
    assert "language_unknown" in j.waiting(), j.waiting()
    assert j.shelfhold("edition", "set-lang", edition, "fr").code == 0
    adults = str(j.sql(f"select id from shelf where name = '{BOOKS_SHELF}'")[0][0])
    filed = j.api(("POST", f"/api/v1/editions/{edition}/shelf", {"shelf_id": adults}))
    assert filed == [(200, {"filed": True, "reason": None})], (filed, j.waiting())
    refused = j.api(("POST", f"/api/v1/editions/{edition}/shelf",
                     {"shelf_id": str(j.sql("select id from shelf where name = 'Famille'")[0][0])}))  # fmt: skip
    assert refused == [
        (200, {"filed": False, "reason": "unrated_edition"})
    ]  # no age, no family
    classed = j.shelfhold("rate", "16+", "--work", work)
    assert classed.code == 0, classed.err
    libraries = {lib["name"]: lib["id"] for lib in j.kavita()["libraries"]}  # type: ignore[index,union-attr]
    from shelfhold.export.domain.kavita_access import rating_for_age

    # the child reads BOTH libraries, capped at 12+, unknowns hidden: only the age keeps it out
    j.child_account(
        "kid", int(libraries["Famille"]), more=(int(libraries[BOOKS_SHELF]),),
        age=int(rating_for_age(12, x=False)), unknowns=False,
    )  # fmt: skip
    # once classed 16+, the family shelf refuses the book (a later filing is refused too)
    later = j.api(("POST", f"/api/v1/editions/{edition}/shelf",
                   {"shelf_id": str(j.sql("select id from shelf where name = 'Famille'")[0][0])}))  # fmt: skip
    assert later[0][1]["filed"] is False, later

    export_until(j, 1)

    def pushed() -> bool:
        run_job(j, "export.run")
        ages = j.kavita()["ages"]
        return any(a["ageRating"] != 0 and a["ageRatingLocked"] for a in ages.values())  # type: ignore[attr-defined]

    assert wait_for(pushed, 240, 5), j.kavita()["ages"]
    (series,) = j.kavita()["series"]  # type: ignore[misc]
    assert series["libraryId"] == libraries[BOOKS_SHELF]  # the adult library
    (age,) = j.kavita()["ages"].values()  # type: ignore[attr-defined]
    assert (
        age["ageRating"] == int(rating_for_age(16, x=False)) and age["ageRatingLocked"]
    ), age
    assert (
        j.visible_to("kid") == []
    )  # it reads both libraries: its ceiling alone hides the book
    package = members(exported_epub(j, "Le Roman Seize"))["OEBPS/content.opf"].decode()
    assert (
        "16" not in package and "dc:subject" not in package
    )  # no age anywhere in the file
    assert [n for n in members(exported_epub(j, "Le Roman Seize"))] == [
        n for n in members(original)
    ]
    assert j.kavita()["violations"] == []
    assert j.egress() == []


@pytest.mark.e2e_timeout(900)
@pytest.mark.req("EF-SRC-006", "EF-SRC-023")
def test_m2_opds_anti_echo_journey(journey_m2: Journey) -> None:
    """(9) The catalogue of a household server serves back what Shelfhold exported: the file is a
    replica of its own export, never a new Variant of a Work."""
    j = journey_m2
    books_install(j)
    j.put_bytes("/import/Le Livre du Foyer.epub", book("Le Livre du Foyer", lang="fr"))
    assert j.shelfhold("import", "scan").code == 0
    export_until(j, 1)
    before = j.sql(
        "select (select count(*) from unit_variant), (select count(*) from content)"
    )
    set_ = j.shelfhold("plugin", "set", "opds", 'base_url="http://opds.example/opds"')
    assert set_.code == 0, set_.err
    found = j.shelfhold("source", "search", "opds", "foyer")
    assert found.code == 0 and found.out.strip(), found
    asked = j.shelfhold(
        "source", "acquire", "opds", first_id(found.out), "--unit", "urn:e2e:book:1"
    )
    assert asked.code == 0, asked.err
    assert wait_for(lambda: any("/opds/file/" in s for s in sources_seen(j)), 180)
    assert j.shelfhold("jobs", "run", "export.run").code == 0
    assert wait_for(lambda: j.shelfhold("status").code == 0, 30)
    after = j.sql(
        "select (select count(*) from unit_variant), (select count(*) from content)"
    )
    assert after == before, (before, after)  # a replica: no Variant, no new content
    assert len(library_epubs(j)) == 1
    assert j.kavita()["violations"] == []
    assert j.egress() == []


@pytest.mark.e2e_timeout(900)
@pytest.mark.req("EF-SRC-010", "EF-SRC-033", "EF-BIB-051", "EF-BIB-040")
def test_m2_lncrawl_neutralized_journey(journey_m2: Journey) -> None:
    """(8) The administrator's own instance of the chapter crawler is followed: the closed volume
    is asked for as one range (one job, one EPUB), the file it brings is untrusted, the library
    holds its neutralized derivative, the original stays as received under /data, and Kavita lists
    the volume. The
    instance is asked nothing outside its contract, and nothing leaves the stack."""
    j = journey_m2
    books_install(j)
    j.put("/tmp/lncrawl.key", "synthetic-test-key-not-a-real-one\n")
    assert (
        j.shelfhold(
            "plugin", "set", "lncrawl", 'base_url="http://lncrawl.lan:8090"'
        ).code
        == 0
    )
    keyed = j.shelfhold(
        "plugin",
        "set-secret",
        "lncrawl",
        "api_key",
        "--secret-file",
        "/tmp/lncrawl.key",
    )
    assert keyed.code == 0, keyed.err

    def instance() -> dict[str, object]:
        return j.fake("lncrawl", "http://127.0.0.1:8090/_fake/state")

    def asked() -> bool:
        run_job(j, "follow.cycle")
        return bool(instance()["jobs"])

    assert wait_for(asked, 240, 5), instance()

    def exported() -> bool:
        run_job(j, "follow.cycle")
        # the instance gives no language for a volume: the administrator says it once
        for entry in j.waiting().get("language_unknown", []):  # type: ignore[union-attr]
            assert j.shelfhold("edition", "set-lang", entry["subject"], "en").code == 0
        run_job(j, "export.run")
        return len(library_epubs(j)) >= 1

    assert wait_for(exported, 600, 10), instance()
    assert wait_for(lambda: len(kavita_titles(j)) >= 1, 240), kavita_titles(j)
    assert j.sql("select title from work") == [("Novel Alpha",)]
    # the file a range brings is kept as received under /data (never in the library), for the export
    kept = j.compose(
        "exec", "-T", "shelfhold", "sh", "-c", "ls /data/ranged/*.epub"
    ).stdout.split()
    assert kept and any(
        b"<script" in b"".join(members(j.read_bytes(f)).values()) for f in kept
    ), kept
    served = members(j.read_bytes(library_epubs(j)[0]))
    assert b"<script" not in b"".join(served.values())  # the derivative, neutralized
    state = instance()
    assert state["violations"] == [], state
    assert all(r.split()[1].startswith("/api/v1/") for r in state["requests"]), state  # type: ignore[attr-defined]
    assert j.egress() == []


def comic_archive(number: int) -> bytes:
    """A chapter as the download folder of a manga server keeps it: pages in a CBZ."""
    from shelfhold.sdk.testing.corpus.trapped_epubs.build import TINY_PNG

    info = (
        '<?xml version="1.0" encoding="utf-8"?><ComicInfo><Series>Series Alpha</Series>'
        f"<Number>{number}</Number></ComicInfo>"
    )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("001.png", TINY_PNG + bytes([number]))
        archive.writestr("ComicInfo.xml", info)
    return out.getvalue()


def tree_listing(j: Journey) -> str:
    done = j.compose(
        "exec", "-T", "shelfhold", "sh", "-c",
        "cd /tree && find . -type f -exec sha256sum {} + | sort && find . | sort",
    )  # fmt: skip
    return done.stdout


@pytest.mark.e2e_timeout(900)
@pytest.mark.req("EF-INT-009", "EF-SRC-012", "EF-BIB-051")
def test_m2_suwayomi_tree_journey(journey_m2: Journey) -> None:
    """An old download tree of the manga server is imported without a download: a chapter the
    follow already knew gets its file, a chapter whose pages are not all images is refused whole,
    and the tree is read only (not one name, byte or date changed)."""
    j = journey_m2
    install_by_commands(j)
    j.create_admin()
    j.discover_series()
    assert wait_for(lambda: j.sql("select count(*) from edition") == [(2,)], 120)
    sub = j.sql("select name, lang from sub_source")
    assert sub, "no sub-source"
    folder = f"/tree/mangas/{sub[0][0]} ({str(sub[0][1]).upper()})/Series Alpha"
    j.put_bytes(f"{folder}/Chapter 1.cbz", comic_archive(1))
    j.put_bytes(
        f"{folder}/Chapter 2/001.png", comic_archive(2)[:0] + b"\x89PNG\r\n\x1a\n-"
    )
    j.put_bytes(f"{folder}/Chapter 2/notes.txt", b"not a page")
    before = tree_listing(j)
    downloads = j.suwayomi()["requests"]

    dry = j.shelfhold("import", "tree", "/tree", "--dry-run")
    assert dry.code == 0, dry
    assert tree_listing(j) == before
    stored = j.sql("select count(*) from content")
    done = j.shelfhold("import", "tree", "/tree")
    assert done.code == 0, done
    print(done.out)
    assert tree_listing(j) == before  # read only, whatever happened
    assert (
        j.sql("select count(*) from content")[0][0] == stored[0][0] + 1
    )  # Chapter 1 only
    assert j.suwayomi()["requests"] == downloads  # nothing was asked of the service
    again = j.shelfhold("import", "tree", "/tree")
    assert again.code == 0
    assert (
        j.sql("select count(*) from content")[0][0] == stored[0][0] + 1
    )  # not stored twice
    assert j.egress() == []
