"""M4 (L44): the journeys of the release image, each on its own stack of the M4 overlay.

Same method as the M1 to M3 journeys: the simulated Kavita and Suwayomi of `e2e/fakes` behind a
socket, the commands the administrator types and the requests the screens send, and an effect
observed on the disk, in the database the image holds or in what the simulated server shows.

- fan-out: the "Manga" shelf feeds Kavita and a native folder (Mihon or the local source of
  Suwayomi) from one export, the same chapter as a file in both places (EF-EXP-002, EF-EXP-010);
- progress both ways: a position written by Kavita's own API with a member's account reaches that
  member's "Where I am" (read by the e-reader routes), and a position left by an e-reader reaches
  Kavita (EF-EXP-026);
- a rename keeps every member's progress: two members read the series in Kavita, the naming
  template changes, and both are where they were (EF-EXP-017, EF-EXP-030);
- the hash vectors of ENF-QUAL-005 are green on the image itself.

The two journeys of the members' progress turn the switch of the binding on the way the
administrator does (`shelfhold target sync-progress <binding> --on`, M4 L46): without it
`progress.sync` answers `no_destination` and a rename carries nobody. Nothing leaves the network
namespace (`Journey.egress()` is read at the end of each journey).
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from urllib.parse import quote

import pytest
from journey import KAVITA_URL, Journey, wait_for
from test_m1b_journey import shelf_id
from test_m3_journey import kavita_titles, library_ids, make_profile, manga_install

VECTORS_FILE = (
    Path(__file__).resolve().parents[2]
    / "backend"
    / "tests"
    / "unit"
    / "test_document_hash.py"
)
# The M4 stack's own layout (EF-EXP-059): a folder Shelfhold writes is a root under /library and
# two roots never overlap, so Kavita's media folder and the native folder are two roots of the
# library: the Kavita target's map names /library/manga (the shelf sits at its root, the files
# land where the other journeys put them) and the native folder is /library/native, a volume of
# the overlay mounted there, as a mounted share would be.
M4_KAVITA_PATH_MAP = (
    '[{"local": "/library/manga", "remote": "/library/manga", "mode": "rw"}]'
)
NATIVE_ROOT = "/library/native"


# -- what the journeys read of the simulated servers --------------------------------


def native_files(j: Journey) -> list[str]:
    """The files of the native folder (the M4 overlay mounts its volume at /library/native)."""
    done = j.compose(
        "exec", "-T", "shelfhold", "find", NATIVE_ROOT, "-type", "f", check=False
    )
    return sorted(line for line in done.stdout.splitlines() if line)


def manga_install_m4(j: Journey, *, exported: bool = True) -> tuple[str, str]:
    """`manga_install` on the M4 layout: Kavita's map names /library/manga and the shelf sits at
    its root, so the native folder beside it is a root of its own and breaks no rule."""
    return manga_install(
        j, exported=exported, path_map=M4_KAVITA_PATH_MAP, subpath=None
    )


def turn_sync_on(j: Journey, destination: str) -> None:
    """The administrator turns the progress sync of the binding on, by the command (reserved: the
    host's command line), and the destinations screen's route says it is on."""
    done = j.shelfhold("target", "sync-progress", destination, "--on")
    assert done.code == 0, (done.out, done.err)
    [(code, listed)] = j.api(("GET", "/api/v1/destinations"))
    assert code == 200, listed
    [row] = [d for d in listed["items"] if d["id"] == destination]  # type: ignore[index]
    assert row["sync_progress"] is True and row["can_sync_progress"] is True, row


def fake_call(j: Journey, url: str) -> dict[str, object]:
    """A POST to the simulated Kavita's own window that reads the answer (`Journey.fake` drops
    the body of a POST: the window's answers are for the test, never for Shelfhold)."""
    code = (
        "import urllib.request as u,sys; "
        "print(u.urlopen(u.Request(sys.argv[1], data=b'{}', method='POST'), timeout=5)"
        ".read().decode())"
    )
    done = j.compose("exec", "-T", "fake-kavita", "python", "-c", code, url)
    return dict(json.loads(done.stdout))


def member_key(j: Journey, name: str) -> str:
    """A key of the Kavita account `name`, as the member pastes theirs on the key screen."""
    return str(fake_call(j, f"{KAVITA_URL}/_fake/member-key?name={name}")["key"])


def read_in_kavita(j: Journey, account: str, chapter: str, page: int) -> None:
    """What Kavita's own reader leaves behind: the account read `page` of the chapter of this
    file, stamped with Kavita's clock (what the pull of EF-EXP-026 reads)."""
    j.fake(
        "kavita",
        f"{KAVITA_URL}/_fake/read?name={account}&file={quote(chapter)}&page={page}",
        post=True,
    )


def chapter_of(j: Journey, path: str) -> int:
    """The chapter id the simulated Kavita gives this file (its own rule: series id * 100 +
    index + 1): what its progress routes are asked by."""
    files = j.kavita()["files"]
    for sid, held in files.items():  # type: ignore[union-attr]
        if path in held:
            return int(sid) * 100 + held.index(path) + 1  # type: ignore[union-attr,arg-type]
    raise AssertionError(f"the simulated Kavita does not index {path}")


def kavita_page(j: Journey, account: str, chapter: int) -> int | None:
    """The page the account is at in this chapter, as Kavita itself holds it (or None)."""
    for row in j.kavita()["progress"]:  # type: ignore[union-attr]
        if row["account"] == account and int(row["chapter"]) == chapter:
            return int(row["page"])
    return None


def document_of(j: Journey, path: str) -> str:
    """KOReader's partial MD5 of the exported file, computed on the image under test: the digest
    its e-reader routes are asked by (the exporter wrote it as the file's alias)."""
    code = (
        "import sys; from pathlib import Path; "
        "from shelfhold.reading.domain.document_hash import koreader_partial_md5; "
        "print(koreader_partial_md5(Path(sys.argv[1])))"
    )
    done = j.compose("exec", "-T", "shelfhold", "python", "-c", code, path)
    return done.stdout.strip()


KOSYNC_CLIENT = r"""
import hashlib, json, sys, urllib.request as u
mode, public_id, secret, document, page = sys.argv[1:6]
headers = {"x-auth-user": public_id, "x-auth-key": hashlib.md5(secret.encode()).hexdigest()}
if mode == "put":
    body = json.dumps({"document": document, "progress": page, "percentage": 0.45,
                       "device": "KOReader"}).encode()
    request = u.Request("http://127.0.0.1:8080/kosync/syncs/progress", data=body,
                        headers={**headers, "content-type": "application/json"}, method="PUT")
else:
    request = u.Request(f"http://127.0.0.1:8080/kosync/syncs/progress/{document}", headers=headers)
try:
    reply = u.urlopen(request, timeout=10)
    print(json.dumps([reply.status, reply.read().decode()]))
except u.HTTPError as error:
    print(json.dumps([error.code, error.read().decode()]))
"""


def kosync(
    j: Journey, mode: str, public_id: str, secret: str, document: str, page: str = "0"
) -> tuple[int, dict[str, object]]:
    """What an e-reader asks of the image: `put` leaves a position, `get` reads it back. The
    headers are KOReader's own (`x-auth-user`, `x-auth-key` = md5 of the secret)."""
    j.put("/tmp/kosync_client.py", KOSYNC_CLIENT)
    done = j.compose(
        "exec", "-T", "shelfhold", "python", "/tmp/kosync_client.py",
        mode, public_id, secret, document, page,
    )  # fmt: skip
    status, body = json.loads(done.stdout)
    return int(status), dict(json.loads(body) if body else {})


def link_member_key(
    j: Journey, who: tuple[str, str], user_id: str, destination: str, account: str
) -> None:
    """The member pastes their own Kavita key on the key screen (EF-USR-012)."""
    [(code, linked)] = j.api(
        ("PUT", f"/api/v1/profiles/{user_id}/destination-accounts/{destination}",
         {"token": member_key(j, account), "remote_user": account}),
        user=who,
    )  # fmt: skip
    assert code == 200, (code, linked)


def device_token(j: Journey, who: tuple[str, str]) -> tuple[str, str]:
    """The member pairs an e-reader on the device screen: the token shown once."""
    [(code, paired)] = j.api(
        ("POST", "/api/v1/device-tokens", {"name": "koreader"}), user=who
    )
    assert code == 201, (code, paired)
    return str(paired["public_id"]), str(paired["secret"])  # type: ignore[index]


# -- (1) one shelf, two destinations: Kavita and a native folder -------------------


@pytest.mark.e2e_timeout(600)
@pytest.mark.req("EF-EXP-002", "EF-EXP-010")
def test_m4_journey_manga_shelf_kavita_and_native_folder(journey_m4: Journey) -> None:
    """The "Manga" shelf feeds Kavita and a native folder (Mihon or the local source of
    Suwayomi) from one export: the chapter published upstream is a file in Kavita's library and a
    file in the folder Mihon reads, laid out as it reads (a folder per series, `Ch.<n> <title>`,
    a cover, a ComicInfo inside the archive), and nothing left the network namespace."""
    j = journey_m4
    manga_install_m4(
        j
    )  # the shelf, the chapter published upstream, delivered to Kavita
    # the folder Mihon reads, named by the administrator on the screens of the API (a folder that
    # is written is a reserved action: a fresh session of the admin)
    [(code, made)] = j.api(
        ("POST", "/api/v1/targets/native-folder",
         {"name": "mihon",
          "path_map": [{"local": NATIVE_ROOT, "remote": NATIVE_ROOT, "mode": "rw"}]}),
    )  # fmt: skip
    assert code == 201, (code, made)
    # the shelf takes a second binding, a folder of that target (fan-out, EF-EXP-002)
    [(code, bound)] = j.api(
        ("POST", f"/api/v1/shelves/{shelf_id(j, 'Manga')}/destinations",
         {"target_id": made["id"], "transport": "tree", "root_subpath": "manga"}),
    )  # fmt: skip
    assert code == 201, (code, bound)
    assert j.shelfhold("jobs", "run", "export.run").code == 0

    def visible() -> list[str]:
        # Shelfhold's own markers (its state folder, the sentinel of a root) are not what Mihon reads
        return [f for f in native_files(j) if "/.shelfhold" not in f]

    expected = [
        f"{NATIVE_ROOT}/manga/Series Alpha/Ch.0002 Chapter 2.cbz",
        f"{NATIVE_ROOT}/manga/Series Alpha/cover.png",
    ]
    assert wait_for(lambda: visible() == expected, 300), native_files(j)
    # the archive is one Mihon reads: pages, and the ComicInfo that names the chapter
    raw = j.read_bytes(f"{NATIVE_ROOT}/manga/Series Alpha/Ch.0002 Chapter 2.cbz")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = archive.namelist()
    assert "ComicInfo.xml" in names and any(n.endswith(".png") for n in names), names
    # Kavita still holds what it held: the same export fed two destinations
    assert kavita_titles(j) == ["Series Alpha"]
    assert j.kavita()["violations"] == []
    assert j.egress() == []


# -- (2) the members' progress, both ways -------------------------------------------


@pytest.mark.e2e_timeout(900)
@pytest.mark.req("EF-EXP-026")
def test_m4_journey_progress_both_ways(journey_m4: Journey) -> None:
    """A member reads in Kavita: the position Kavita's own API holds for their account reaches
    their "Where I am", read by the e-reader routes of their device token; and the position the
    e-reader leaves reaches Kavita. The screen of the sync says what was pulled and pushed."""
    j = journey_m4
    destination, library = manga_install_m4(j)
    turn_sync_on(j, destination)
    reader = ("reader", "synthetic-reader-password-1")
    user_id = make_profile(j, reader, "member")
    j.child_account(
        reader[0], library_ids(j)[library], age=-1
    )  # the member's Kavita account
    link_member_key(j, reader, user_id, destination, reader[0])
    public_id, secret = device_token(j, reader)
    chapter = next(
        f
        for f in j.library_files()
        if f.startswith("/library/manga/")
        and f.endswith(".cbz")
        and "/.shelfhold/" not in f
    )
    document = document_of(j, chapter)

    # Kavita to Shelfhold: the member read page 5 of the chapter in Kavita
    read_in_kavita(j, reader[0], chapter, 5)
    assert j.shelfhold("jobs", "run", "progress.sync").code == 0
    assert wait_for(
        lambda: kosync(j, "get", public_id, secret, document)[1].get("progress") == "5",
        300,
    ), "the position read in Kavita never reached the member's own progress"
    [(code, status)] = j.api(("GET", "/api/v1/sync-status"))
    assert code == 200, status
    [row] = [r for r in status["items"] if r["username"] == reader[0]]
    assert row["state"] == "linked" and row["last_pull_at"] is not None, row

    # Shelfhold to Kavita: the e-reader left the member at page 9
    code, stored = kosync(j, "put", public_id, secret, document, "9")
    assert code == 200 and stored["document"] == document, (code, stored)
    assert j.shelfhold("jobs", "run", "progress.sync").code == 0
    assert wait_for(
        lambda: kavita_page(j, reader[0], chapter_of(j, chapter)) == 9, 300
    ), "the position left by the e-reader never reached Kavita"
    assert j.kavita()["violations"] == []
    assert j.egress() == []


# -- (3) a rename keeps every member's progress -------------------------------------


@pytest.mark.e2e_timeout(900)
@pytest.mark.req("EF-EXP-017", "EF-EXP-030")
def test_m4_journey_rename_keeps_each_member_progress(journey_m4: Journey) -> None:
    """Two members read the chapter in Kavita, each at their own page; the administrator changes
    the naming template and the files take their new names. Both members are where they were:
    what they read is pulled before the move and pushed to the renamed chapter after it, and
    nothing is made "read" on the way (EF-EXP-017, EF-EXP-030)."""
    j = journey_m4
    destination, library = manga_install_m4(j)
    turn_sync_on(j, destination)
    chapter = next(
        f
        for f in j.library_files()
        if f.startswith("/library/manga/")
        and f.endswith(".cbz")
        and "/.shelfhold/" not in f
    )
    for name in ("alice", "bob"):
        who = (name, f"synthetic-{name}-password-1")
        user_id = make_profile(j, who, "member")
        j.child_account(name, library_ids(j)[library], age=-1)
        link_member_key(j, who, user_id, destination, name)
    read_in_kavita(j, "alice", chapter, 12)
    read_in_kavita(j, "bob", chapter, 7)

    template = {"chapter": "{series} - Chapter {number}"}
    accepted, job = 0, None
    # A periodic export can land between the preview and the apply and change the destination:
    # the plan is then stale (412), the product doing its job, and the journey reads it again.
    for _attempt in range(4):
        [(code, plan)] = j.api(
            ("POST", "/api/v1/plans/renames",
             {"shelf_destination_id": destination, "template": template}),
        )  # fmt: skip
        assert code == 200, plan
        assert plan["steps"] and plan["plan_id"], plan
        # the members whose key is held are carried: they lose nothing, so nothing is asked of
        # the administrator; a reader the carry does not know is a warning to confirm
        etag = {"If-Match": f'"{plan["base_version"]}"'}
        apply_path = f"/api/v1/plans/{plan['plan_id']}/apply"
        body = {"confirm_progress_loss": True} if plan["warnings"] else {}
        [(accepted, job)] = j.api(("POST", apply_path, body, etag))
        if accepted != 412:
            break
    assert accepted == 202, job
    assert wait_for(
        lambda: all(
            " - Chapter " in f
            for f in j.library_files()
            if f.startswith("/library/manga/")
            and f.endswith(".cbz")
            and "/.shelfhold/" not in f
        ),
        200,
    ), j.library_files()
    # Kavita's scanner reads the disk again (the plan asked for a scan; the simulated server
    # indexes its folder when its own window is asked, as a scan would)
    j.fake("kavita", f"{KAVITA_URL}/_fake/index?folder=/library/manga", post=True)
    assert wait_for(lambda: kavita_titles(j) == ["Series Alpha"], 60), kavita_titles(j)
    moved = next(
        f
        for f in j.library_files()
        if f.startswith("/library/manga/") and " - Chapter " in f and f.endswith(".cbz")
    )
    # each member is where they were, on the chapter the rename made
    assert wait_for(lambda: kavita_page(j, "alice", chapter_of(j, moved)) == 12, 300), (
        "alice's progress was not carried over the rename"
    )
    assert kavita_page(j, "bob", chapter_of(j, moved)) == 7, (
        "bob's progress was not carried"
    )
    assert j.kavita()["violations"] == []
    assert j.egress() == []


# -- (4) the hash vectors, green on the image --------------------------------------


def _vectors() -> list[dict[str, str]]:
    """The frozen vectors of the unit suite (ENF-QUAL-005), read from its file: the expected
    digests were made by the reference implementations, never by the module under test."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("document_hash_vectors", VECTORS_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return [
        {"size": str(size), "file": file_id, "koreader": koreader, "kavita": kavita}
        for size, file_id, koreader, kavita in module.VECTORS
    ]


HASH_RUNNER = r"""
import hashlib, json, struct, sys
from pathlib import Path
from shelfhold.reading.domain.document_hash import kavita_hash, koreader_partial_md5


def pattern(position):
    return (position * 167 + (position >> 10) * 13 + 5) & 0xFF


def windows():
    return [0] + [1024 << (2 * i) for i in range(11)]


def synthetic(path, size):
    with path.open("wb") as handle:
        handle.truncate(size)  # a hole for the rest: the 3 GiB file costs a few KiB
        if size <= 1 << 16:
            handle.write(bytes(pattern(p) for p in range(size)))
        else:
            for offset in windows():
                if offset < size:
                    handle.seek(offset)
                    count = min(1024, size - offset)
                    handle.write(bytes(pattern(offset + k) for k in range(count)))


def file_id(path, size):
    digest = hashlib.sha256(struct.pack(">Q", size))
    with path.open("rb") as handle:
        for offset in windows():
            handle.seek(offset)
            digest.update(handle.read(1024))
    return digest.hexdigest()


vectors = json.loads(Path(sys.argv[1]).read_text())
out = []
for vector in vectors:
    size = int(vector["size"])
    path = Path("/tmp") / f"vector-{size}.bin"
    synthetic(path, size)
    with path.open("rb") as stream:
        stream.seek(7)  # a stream is sampled by absolute offsets, wherever it stands
        from_stream = koreader_partial_md5(stream)
    out.append({"size": vector["size"], "file_id": file_id(path, size),
                "koreader": koreader_partial_md5(path), "koreader_stream": from_stream,
                "kavita": kavita_hash(path)})
    path.unlink()
print(json.dumps(out))
"""


@pytest.mark.e2e_timeout(600)
@pytest.mark.req("ENF-QUAL-005")
def test_m4_journey_hash_vectors_green(journey_m4: Journey) -> None:
    """The frozen vectors of KOReader's partial MD5 and Kavita's variant pass on the image
    itself: the package the image ships gives the digests the reference implementations made,
    from a file and from a stream, and the bytes are the ones the references hashed."""
    j = journey_m4
    assert j.shelfhold("status").code == 0
    vectors = _vectors()
    j.put("/tmp/hash_vectors.json", json.dumps(vectors))
    j.put("/tmp/hash_vectors.py", HASH_RUNNER)
    done = j.compose(
        "exec",
        "-T",
        "shelfhold",
        "python",
        "/tmp/hash_vectors.py",
        "/tmp/hash_vectors.json",
    )
    computed = {row["size"]: row for row in json.loads(done.stdout)}
    for vector in vectors:
        found = computed[vector["size"]]
        assert found["file_id"] == vector["file"], vector["size"]  # the same bytes
        assert found["koreader"] == vector["koreader"], vector["size"]
        assert found["koreader_stream"] == vector["koreader"], vector["size"]
        assert found["kavita"] == vector["kavita"], vector["size"]
    assert j.egress() == []
