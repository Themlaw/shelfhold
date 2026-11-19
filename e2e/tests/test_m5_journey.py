"""M5 on the image under test (L24): the journeys of the integrated reader, the e-reader and the
sign-in by a provider, each on its own stack of the M5 overlay.

Same method as the M1 to M4 journeys: the simulated servers of `e2e/fakes` behind a socket, the
commands the administrator types and the requests the screens send, each profile signing in as
itself. The journeys that need a browser (the reader, `/lite` with JavaScript off, the offline
copy) are played by Playwright (`m5_journeys.spec.ts`, `playwright.image.config.ts`) on the stack
of the M5 overlay; this module brings the stack, seeds it, runs the spec and reads the outcome.
The simulated KOReader (`fakes/koreader`) and the person's browser at the simulated provider
(`fakes/oidc/browser.py`) are small scripts run inside the stack. Synthetic data only; nothing
leaves the network namespace (`Journey.egress()` is read at the end of each journey).

Requirements: EF-LEC-001, EF-LEC-003, EF-LEC-005 (the reader and what it may be given),
EF-LEC-009 (kosync), EF-LEC-010 (`/lite`), EF-LEC-012 (offline), EF-USR-004 and EF-USR-024 (OIDC),
ENF-A11Y-002 (the reader by keyboard, in the specs).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from journey import ADMIN_PASSWORD, ADMIN_USER, Journey, wait_for
from test_m1b_journey import ids_of
from test_m2_journey import library_epubs, members
from test_m3_journey import destination_of, library_ids, make_profile, playwright
from test_m4_journey import (
    device_token,
    document_of,
    link_member_key,
    manga_install_m4,
    turn_sync_on,
)

E2E = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(E2E / "fakes" / "m5"))
from m5_books import BEACON, multi_chapter_epub  # noqa: E402

MEMBER = ("mum", "synthetic-mum-password-1")
OTHER = ("dad", "synthetic-dad-password-1")
KID = ("kid8", "synthetic-kid-password-1")
KOREADER = (E2E / "fakes" / "koreader" / "client.py").read_text(encoding="utf-8")
OIDC_BROWSER = (E2E / "fakes" / "oidc" / "browser.py").read_text(encoding="utf-8")
BOOK = "Le Carnet de Route"
CHAPTERS = 10


def drop_book(j: Journey, title: str = BOOK, *, trap: bool = False) -> None:
    """The administrator drops a book in the import folder: it is stored, verified and kept, and
    the Work it makes is the administrator's to file (nothing is exported)."""
    j.put_bytes(f"/import/{title}.epub", multi_chapter_epub(title, chapters=CHAPTERS, trap=trap))
    scan = j.shelfhold("import", "scan")
    assert scan.code == 0, (scan.out, scan.err)


def koreader(j: Journey, token: tuple[str, str], path: str, **extra: str) -> list[list[object]]:
    """What the simulated KOReader of `fakes/koreader` saw, for the book at `path`."""
    j.put("/tmp/koreader_client.py", KOREADER)
    spec = {
        "public_id": token[0], "secret": token[1], "file": path,
        "progress": "/body/DocFragment[3]/body/div/p[4]/text().12", "percentage": "0.31",
        "device": "KOReader", "device_id": "A1B2C3D4E5F60718293A4B5C6D7E8F90", **extra,
    }  # fmt: skip
    done = j.compose(
        "exec", "-T", "shelfhold", "python", "/tmp/koreader_client.py", json.dumps(spec)
    )
    return [list(step) for step in json.loads(done.stdout)]


def saw(steps: list[list[object]], name: str) -> tuple[int, object]:
    [step] = [s for s in steps if s[0] == name]
    return int(step[1]), step[2]  # type: ignore[call-overload]


# -- what the browser journeys are given ---------------------------------------------------------


def kept_book(j: Journey, title: str, *, trap: bool = False) -> dict[str, str]:
    """A book dropped in the import folder and kept (nothing is exported: no target, no shelf):
    the identifiers its sheet and its reader are opened by."""
    drop_book(j, title, trap=trap)
    assert wait_for(
        lambda: j.sql(f"select count(*) from work where title = '{title}'") == [(1,)], 120
    ), j.sql("select title from work")
    edition, _work = ids_of(j, title)
    [(code, sheet)] = j.api(("GET", f"/api/v1/editions/{edition}/sheet"))
    assert code == 200 and sheet["first_unit_id"], sheet  # type: ignore[index]
    unit = str(sheet["first_unit_id"])  # type: ignore[index]
    [(code, published)] = j.api(("GET", f"/api/reader/unit/{unit}/publication"))
    assert code == 200, published
    return {
        "title": title, "series_id": edition, "unit_id": unit,
        "carrier_id": str(published["carrier_id"]),  # type: ignore[index]
        "marker": f"Marqueur {title}",
    }  # fmt: skip


def browser_world(j: Journey, **parts: object) -> dict[str, str]:
    admin = {"username": ADMIN_USER, "password": ADMIN_PASSWORD}
    return {"E2E_M5": json.dumps({"admin": admin, **parts})}


def spine_of(published: dict[str, object]) -> list[str]:
    return [str(e["href"]) for e in published["spine"]]  # type: ignore[attr-defined,index]


# -- (1) an excerpt without exporting, then the whole book -------------------------------------------


@pytest.mark.e2e_timeout(780)
@pytest.mark.req("EF-LEC-001", "EF-LEC-003", "EF-LEC-005", "ENF-A11Y-002")
def test_m5_journey_read_excerpt_without_export(journey_m5: Journey) -> None:
    """A book kept and never exported is read in a browser as an excerpt (a fifth of it, cut by the
    server, ending on its own screen and writing no position), then in full, by the keyboard alone,
    and the book is then one to continue. The server keeps its word on the way: the excerpt's ticket
    opens the excerpt's chapters and no other, a child, another credential and a stranger get the
    one answer of a thing that is not there, and nothing was exported."""
    j = journey_m5
    j.create_admin()
    book = kept_book(j, BOOK)
    unit = book["unit_id"]
    make_profile(j, KID, "child", 8)

    excerpt, full = j.api(
        ("GET", f"/api/reader/unit/{unit}/publication?scope=excerpt"),
        ("GET", f"/api/reader/unit/{unit}/publication"),
    )
    assert excerpt[0] == 200 and full[0] == 200, (excerpt, full)
    assert excerpt[1]["scope"] == "excerpt" and full[1]["scope"] == "full"  # type: ignore[index]
    assert len(spine_of(excerpt[1])) == 2 and len(spine_of(full[1])) == CHAPTERS  # type: ignore[arg-type]
    # (the ticket is bound to a session: what it opens is asked in the browser's, by the spec)
    # an excerpt writes no position (the reader asks; the server refuses)
    [(refused, _)] = j.api(
        ("PUT", "/api/reader/positions",
         {"carrier_type": "content", "carrier_id": excerpt[1]["carrier_id"],  # type: ignore[index]
          "locator": {"v": 1, "kind": "epub", "spine": 0}, "client_ts": "2026-01-01T00:00:00Z",
          "device_id": "e2e", "scope": "excerpt", "ticket": "x"}),
    )  # fmt: skip
    assert refused in (409, 422), refused
    # the attacks: a child below the book's rating, a key of the API, nobody
    assert j.api(("GET", f"/api/reader/unit/{unit}/publication"), user=KID)[0][0] == 404
    assert j.api(("GET", f"/api/reader/unit/{unit}/publication"), mode="anon")[0][0] == 401
    key = re.search(r"shk_\S+", j.shelfhold("admin", "keys", "create", "--name", "e2e").out)
    assert key is not None
    assert j.api(("GET", f"/api/reader/unit/{unit}/publication"), mode=f"bearer:{key.group(0)}")[0][0] == 403

    done = playwright(
        j, "test_m5_journey_read_excerpt_without_export", 600, browser_world(j, book=book)
    )
    assert done.returncode == 0, "the reader journey failed (see the output of the spec)"
    # nothing was exported, nothing is in Kavita's library
    assert j.sql("select count(*) from export_item") == [(0,)]
    assert library_epubs(j) == []
    assert j.kavita()["series"] == []
    assert j.egress() == []


# -- (2) a booby-trapped EPUB ------------------------------------------------------------------------


@pytest.mark.e2e_timeout(780)
@pytest.mark.req("EF-LEC-003", "EF-LEC-005", "ENF-SEC-004")
def test_m5_journey_epub_script_blocked(journey_m5: Journey) -> None:
    """A book whose first chapter carries every way a script gets run (a `script` element, an event
    attribute, a `javascript:` link) is read in the browser: its text is shown, and
    nothing of it runs. The reader is given the neutralised derivative, never the original, which
    the store keeps as it came; the browser's own policy (a sandboxed `blob:` document with no
    script) is what the spec watches."""
    j = journey_m5
    j.create_admin()
    title = "Le Carnet Piege"
    book = kept_book(j, title, trap=True)
    # the original is kept in the store as it came, trap included, and never served
    kept = [
        f for f in j.compose("exec", "-T", "shelfhold", "find", "/store", "-type", "f").stdout.split()
    ]
    assert any(
        b"<script" in b"".join(members(j.read_bytes(f)).values()) for f in kept if f.endswith(".epub")
    ), kept
    done = playwright(
        j, "test_m5_journey_epub_script_blocked", 600,
        browser_world(j, trapped={"title": title, "unit_id": book["unit_id"], "beacon": BEACON}),
    )  # fmt: skip
    assert done.returncode == 0, "the trapped-book journey failed (see the output of the spec)"
    assert j.egress() == []


# -- (3) a simulated KOReader pairs, sends its position and reads it back ---------------------------


@pytest.mark.e2e_timeout(900)
@pytest.mark.req("EF-LEC-009", "EF-LEC-014", "EF-USR-022", "EF-USR-008")
def test_m5_journey_koreader_sync(journey_m5: Journey) -> None:
    """A member pairs an e-reader (a device token, shown once) and KOReader, replayed request by
    request from the vectors of its source, logs in, finds nothing for a file it has never sent,
    sends the page it is at and reads it back. The series is then in "Continuer la lecture" of that
    member and of nobody else. Registering is closed, a wrong key is refused, another member's token
    neither reads nor completes the position, and a revoked token writes nothing."""
    j = journey_m5
    destination, library = manga_install_m4(j)  # the chapter, exported to Kavita
    member_id = make_profile(j, MEMBER, "member")
    make_profile(j, OTHER, "member")
    # the member reads in their own Kavita account (EF-USR-012): the series is one to continue there
    turn_sync_on(j, destination)
    j.child_account(MEMBER[0], library_ids(j)[library], age=-1)
    link_member_key(j, MEMBER, member_id, destination, MEMBER[0])
    [path] = [
        f for f in j.library_files()
        if f.startswith("/library/manga/") and f.endswith(".cbz") and "/.shelfhold/" not in f
    ]  # fmt: skip
    token = device_token(j, MEMBER)
    other_token = device_token(j, OTHER)

    steps = koreader(j, token, path, progress="5", percentage="0.45")
    document = str(saw(steps, "document")[1])
    assert document == document_of(j, path), "KOReader's digest and the image's are not the same"
    assert saw(steps, "login")[0] == 200
    assert saw(steps, "register")[0] == 402  # the "Register" button of the plugin: closed
    assert saw(steps, "login_wrong_key")[0] == 401
    assert saw(steps, "pull_on_open") == (200, {})  # nothing yet: "no progress found"
    assert saw(steps, "push")[0] == 200
    code, back = saw(steps, "pull_back")
    assert code == 200 and isinstance(back, dict), (code, back)
    assert back["document"] == document and back["progress"] == "5", back
    assert back["percentage"] == pytest.approx(0.45)
    assert saw(steps, "pull_unknown") == (200, {})

    # "Continuer la lecture": the series, for this member, where the file was sent (Kavita)
    [(code, mine)] = j.api(("GET", "/api/reader/continue"), user=MEMBER)
    assert code == 200, mine
    found = [i for i in mine["items"] if i["title"] == "Series Alpha"]  # type: ignore[index]
    assert len(found) == 1, mine
    assert found[0]["state"] == "in_progress" and found[0]["target"] == "kavita", found
    # nobody else's: another member has read nothing, a device token opens no session route
    [(code, theirs)] = j.api(("GET", "/api/reader/continue"), user=OTHER)
    assert code == 200 and "Series Alpha" not in json.dumps(theirs), theirs
    assert j.api(("GET", "/api/reader/continue"), mode="bearer:" + token[1])[0][0] in (401, 403)

    # another member's token: the same file, no position of mine, and mine is not touched by theirs
    other = koreader(j, other_token, path, progress="9", percentage="0.9")
    assert saw(other, "pull_on_open") == (200, {})
    again = koreader(j, token, path, progress="5", percentage="0.45")
    assert saw(again, "pull_back")[1]["progress"] == "5"  # type: ignore[index]

    # a revoked token: refused, and what it sent is not stored
    [(gone, _)] = j.api(("DELETE", f"/api/v1/device-tokens/{token[0]}"), user=MEMBER)
    assert gone == 204, gone
    late = koreader(j, token, path, progress="12", percentage="0.99")
    assert saw(late, "login")[0] == 401 and saw(late, "push")[0] == 401
    assert j.kavita()["violations"] == []
    assert j.egress() == []


# -- (4) `/lite`, JavaScript off -------------------------------------------------------------------


def child_device(j: Journey, profile: str) -> tuple[str, str]:
    """A token an administrator pairs for a child (a child pairs nothing herself): the command's
    secret on its output, the identifier in its message."""
    done = j.shelfhold("admin", "device-tokens", "create", "--profile", profile, "--name", "lite")
    assert done.code == 0, (done.out, done.err)
    found = re.search(r"identifiant (\S+?)\)", done.err)
    assert found is not None, done.err
    return found.group(1), done.out.strip()


@pytest.mark.e2e_timeout(900)
@pytest.mark.req("EF-LEC-010", "ENF-COMP-002", "EF-USR-022")
def test_m5_journey_lite_without_javascript(journey_m5: Journey) -> None:
    """The member's e-reader, which runs no JavaScript, signs in on `/lite` with its token's
    identifier and secret, browses to a series and downloads it; the page carries no script. The
    child's token sees none of it, a session of the SPA opens nothing there, and nobody gets in
    without signing in."""
    j = journey_m5
    manga_install_m4(j)  # the series, exported: its block is what `/lite` offers
    make_profile(j, MEMBER, "member")
    make_profile(j, KID, "child", 8)
    member = device_token(j, MEMBER)
    child = child_device(j, KID[0])
    title = "Series Alpha"

    # the doors that stay shut: nobody, and a session of the SPA (the one credential is a token's)
    nobody = j.api(("GET", "/lite/series"), mode="anon")[0]
    assert title not in str(nobody[1]), nobody
    spa = j.api(("GET", "/lite/series"), user=MEMBER)[0]
    assert title not in str(spa[1]), spa

    done = playwright(
        j, "test_m5_journey_lite_without_javascript", 600,
        browser_world(j, lite={
            "identifier": member[0], "secret": member[1], "title": title,
            "child": {"identifier": child[0], "secret": child[1]},
        }),
    )  # fmt: skip
    assert done.returncode == 0, "the /lite journey failed (see the output of the spec)"
    assert j.kavita()["violations"] == []
    assert j.egress() == []


# -- (5) offline, reclassified, back online, evicted ------------------------------------------------


@pytest.mark.e2e_timeout(900)
@pytest.mark.req("EF-LEC-012")
def test_m5_journey_offline_eviction(journey_m5: Journey) -> None:
    """A copy kept for offline reading is read with the network cut; the Work leaves the profile's
    view meanwhile; the network back, the copy is evicted by the `?since=` of the change feed.
    A child's view (reclassified 18+) is played on the feed itself: the device is the administrator's."""
    j = journey_m5
    j.create_admin()
    title = "Le Livre Hors Ligne"
    book = kept_book(j, title)  # the administrator's, kept, exported nowhere
    edition, work = ids_of(j, title)
    make_profile(j, KID, "child", 8)
    [(code, shelf)] = j.api(
        ("POST", "/api/v1/shelves", {"name": "Famille", "phrase": "Famille", "max_age_rating": 12})
    )
    assert code == 201, shelf
    rated, filed = j.api(
        ("POST", f"/api/v1/works/{work}/rating", {"age": "0"}),
        ("POST", f"/api/v1/editions/{edition}/shelf", {"shelf_id": shelf["id"]}),  # type: ignore[index]
    )  # fmt: skip
    assert rated[0] == 200 and filed == (200, {"filed": True, "reason": None}), (rated, filed)

    # hers, in full; reclassified 18+, the change feed names the Work in her removals (what her
    # device evicts by) and the server refuses her the book; set back, it is hers again
    [(code, hers)] = j.api(("GET", f"/api/reader/unit/{book['unit_id']}/publication"), user=KID)
    assert code == 200 and hers["scope"] == "full", hers  # type: ignore[index]
    [(code, feed)] = j.api(("GET", "/api/v1/works?limit=1"), user=KID)
    assert code == 200 and feed["since"], feed  # type: ignore[index]
    assert j.api(("POST", f"/api/v1/works/{work}/rating", {"age": "18+"}))[0][0] == 200
    [(code, gone)] = j.api(("GET", f"/api/v1/works?limit=200&since={feed['since']}"), user=KID)  # type: ignore[index]
    assert code == 200 and work in gone["removed"], gone  # type: ignore[index]
    assert j.api(("GET", f"/api/reader/unit/{book['unit_id']}/publication"), user=KID)[0][0] == 404
    assert j.api(("POST", f"/api/v1/works/{work}/rating", {"age": "0"}))[0][0] == 200

    # in the browser: "Lire hors ligne" is a button of the series sheet, which is the administrator's
    # (the sheet is 403 for anyone else): she keeps the copy, reads it with the network cut, the Work
    # is purged meanwhile, the network back, the copy is evicted
    done = playwright(
        j, "test_m5_journey_offline_eviction", 600,
        browser_world(j, offline={
            "title": title, "series_id": book["series_id"], "marker": book["marker"],
            "work_id": work,
        }),
    )  # fmt: skip
    assert done.returncode == 0, "the offline journey failed (see the output of the spec)"
    assert j.sql(f"select count(*) from work where id = '{work}'") == [(0,)]
    assert j.egress() == []


# -- (6) sign-in by the simulated provider, a reserved action with `max_age` ------------------------


def browser(j: Journey, *steps: list[object]) -> list[dict[str, object]]:
    """What the person's browser of `fakes/oidc/browser.py` saw, one step at a time."""
    j.put("/tmp/oidc_browser.py", OIDC_BROWSER)
    done = j.compose(
        "exec", "-T", "shelfhold", "python", "/tmp/oidc_browser.py", json.dumps({"steps": steps})
    )
    return list(json.loads(done.stdout))


@pytest.mark.e2e_timeout(600)
@pytest.mark.req("EF-USR-004", "EF-USR-024")
def test_m5_journey_oidc_login(journey_m5_oidc: Journey) -> None:
    """The person signs in at the simulated provider: refused while no account is linked to them
    (no account is ever made by the provider), then admitted once the administrator linked their
    identity. The session the provider opened is not fresh: a reserved action asks the provider
    again (`prompt=login`, `max_age`), and goes through only on its proof. A callback with a forged
    state, and one with no flow cookie, open nothing."""
    j = journey_m5_oidc
    sub = j.extra_env["E2E_OIDC_SUB"]
    j.create_admin()
    password = {"username": "admin", "password": "synthetic-admin-password-1"}

    # nobody is linked to this identity: the provider's word makes no account and opens no session
    [first] = browser(j, ["login"])
    assert first["status"] == 303 and str(first["location"]).startswith("/login"), first
    assert not any(c.endswith("sid") for c in first["cookies"]), first  # type: ignore[attr-defined]
    forged = browser(j, ["request", "GET", "/api/v1/auth/oidc/callback?code=forged&state=forged"])
    assert forged[0]["status"] == 303 and str(forged[0]["location"]).startswith("/login")

    # the administrator links the identity to their own account (a fresh session of theirs)
    [_, who] = browser(
        j, ["request", "POST", "/api/v1/auth/login", password], ["request", "GET", "/api/v1/me"]
    )
    admin_id = str(who["body"]["id"])  # type: ignore[index]
    [_, put] = browser(
        j,
        ["request", "POST", "/api/v1/auth/login", password],
        ["request", "PUT", f"/api/v1/profiles/{admin_id}/oidc", {"sub": sub}],
    )
    assert put["status"] == 204, put

    # the person signs in by the provider: a session of their own account
    [login, me, refused, reauth, after] = browser(
        j,
        ["login"],
        ["request", "GET", "/api/v1/me"],
        ["request", "POST", "/api/v1/auth/sessions/revoke-others", {}],
        ["reauth"],
        ["request", "POST", "/api/v1/auth/sessions/revoke-others", {}],
    )
    assert login["status"] == 303 and login["location"] == "/", login
    assert me["status"] == 200 and me["body"]["username"] == "admin", me  # type: ignore[index]
    # the session is not fresh: the reserved action asks to prove oneself again, by the provider
    assert refused["status"] == 401 and refused["body"]["code"] == "reauth_required", refused  # type: ignore[index]
    assert refused["body"]["reauth_methods"][0] == "oidc"  # type: ignore[index]
    asked = reauth["asked"]
    assert asked["prompt"] == "login" and asked["max_age"] == "300", asked  # type: ignore[index]
    assert asked["code_challenge_method"] == "S256" and asked["nonce"] and asked["state"]  # type: ignore[index]
    assert reauth["status"] == 303, reauth
    assert str(reauth["location"]).endswith("oidc=reauth"), reauth
    assert after["status"] == 200, after  # the proof, now: the reserved action goes through
    assert parse_qs(urlsplit(str(reauth["location"])).query) == {"oidc": ["reauth"]}
    assert re.search(r"https://idp\.e2e\.test:9443", json.dumps(refused)) is None
    assert j.egress() == []
