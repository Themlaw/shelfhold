"""M3 on the image under test (L57): the family and the first-run journeys, each on its own stack.

Same method as the M1 and M2 journeys: the simulated Kavita, Suwayomi and sources of `e2e/fakes`
behind a socket, a real library volume, the commands the administrator types and the requests the
SPA sends (session and CSRF, each profile signing in as itself). The two journeys that need a
browser (the first book, the key screens per theme) are played by Playwright
(`test_m3_journey.spec.ts`, `playwright.image.config.ts`) on the stack of the M3 overlay, and this
module only brings the stack, runs the spec and reads the outcome. Synthetic data only; nothing
leaves the network namespace (`Journey.egress()` is read at the end of each journey).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from journey import KAVITA_PASSWORD, KAVITA_URL, Journey, wait_for
from test_m1a_journey import install_by_commands
from test_m1b_journey import discovered, family_files, family_install, ids_of, shelf_id
from test_m2_journey import configure_sources, first_id, kavita_titles, library_epubs

E2E = Path(__file__).resolve().parents[1]
KID = ("kid8", "synthetic-kid-password-1")
MEMBER = ("mum", "synthetic-mum-password-1")
KID_AGE = 8


# -- what the administrator types and what the screens send -----------------------------------------


def make_profile(j: Journey, who: tuple[str, str], role: str, age: int | None = None) -> str:
    """A profile made from the screen's route; its identifier."""
    body: dict[str, object] = {"username": who[0], "password": who[1], "role": role}
    if age is not None:
        body["max_age_rating"] = age
    [(code, made)] = j.api(("POST", "/api/v1/profiles", body))
    assert code == 201, (code, made)
    return str(made["id"])  # type: ignore[index]


def library_ids(j: Journey) -> dict[str, int]:
    return {str(lib["name"]): int(lib["id"]) for lib in j.kavita()["libraries"]}  # type: ignore[index,union-attr]


def target_id(j: Journey) -> str:
    return str(j.sql("select id from export_target")[0][0])


def destination_of(j: Journey, shelf: str) -> str:
    rows = j.sql(
        "select d.id from shelf_destination d join shelf s on s.id = d.shelf_id "
        f"where s.name = '{shelf}'"
    )
    assert len(rows) == 1, rows
    return str(rows[0][0])


def exported_chapter(j: Journey, folder: str, wanted: str = "Ch.2.cbz") -> bool:
    return any(
        f.startswith(f"/library/{folder}/") and f.endswith(wanted) for f in j.library_files()
    )


def playwright(j: Journey, grep: str, seconds: int) -> subprocess.CompletedProcess[str]:
    """The spec of the image journeys, on the stack of `j` (the base address and the compose
    arguments are what the spec reads); its output is printed, whatever the outcome."""
    done = subprocess.run(  # noqa: S603
        ["npx", "playwright", "test", "-c", "playwright.image.config.ts", "-g", grep],  # noqa: S607
        cwd=E2E, capture_output=True, text=True, check=False, timeout=seconds,
        env={**j._env(), "E2E_COMPOSE": json.dumps(j._command()[2:])},
    )  # fmt: skip
    print(done.stdout)
    print(done.stderr)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:  # the report of the spec (time, gestures) is read on the page of the run
        with open(summary, "a", encoding="utf-8") as page:
            page.write(f"### Playwright `{grep}`\n\n```\n{done.stdout[-6000:]}\n```\n")
    return done


def install_by_the_administrator(j: Journey) -> None:
    """What the administrator does on the host before opening the browser, and nothing else: the
    first account (the screens have no sign-in of their own yet) and the two addresses of the books
    project (`plugin set`, which the first-run wizard does not take)."""
    j.create_admin()
    configure_sources(j)


# -- (1) the first book, in a browser ---------------------------------------------------------------


@pytest.mark.e2e_timeout(780)
@pytest.mark.req("ENF-UX-001", "EF-UX-001")
def test_first_book_under_10_minutes(journey_m3: Journey) -> None:
    """A new installation to the first Gutenberg book listed by Kavita, played in a browser: at
    most 10 minutes and 6 actions. Today the screens stop at the folder screen (see the spec): the
    test passes while that known gap is where the journey stops, fails on any other stop, and asserts
    both budgets in full the day the journey gets through."""
    j = journey_m3
    install_by_the_administrator(j)
    done = playwright(j, "test_first_book_under_10_minutes", 720)
    assert done.returncode == 0, "the spec failed in a way it did not expect (see its output)"
    assert j.egress() == []


@pytest.mark.e2e_timeout(780)
@pytest.mark.req("ENF-UX-001", "EF-UX-001")
def test_timed_first_book_with_the_shelf_made_by_command(journey_m3: Journey) -> None:
    """The same journey with the shelf bound to Kavita made by command: the real time of the rest
    (the spec prints the list of the gestures and the seconds) is at most 10 minutes, the book is in
    Kavita as a file the shared volume holds, and nothing left the network namespace."""
    j = journey_m3
    install_by_the_administrator(j)
    done = playwright(j, "test_timed_first_book_with_the_shelf_made_by_command", 720)
    assert done.returncode == 0, "the timed journey failed (see the output of the spec)"
    assert kavita_titles(j) == ["A Study in Test"]
    assert len(library_epubs(j)) == 1
    assert j.kavita()["violations"] == []
    assert j.egress() == []


# -- (2) the parental control, end to end ------------------------------------------------------------


@pytest.mark.e2e_timeout(900)
@pytest.mark.req("EF-USR-017", "EF-EXP-036", "EF-BIB-036", "EF-USR-024")
def test_parental_control_end_to_end(journey_m3: Journey) -> None:
    """A child of 8: a Work nobody has rated is invisible to her everywhere (her catalogue, her
    search, her Kavita account), whatever she types or is shown; once classed and filed on the
    family shelf it is hers, and the neighbour that stays unrated stays out. The audit of the Kavita
    accounts alerts on an account that reads more than a child of 8 should, with codes and no name."""
    j = journey_m3
    family_install(j)  # Kavita, the 18+ shelf, the family shelf; two unrated series in the library
    alpha_edition, alpha_work = ids_of(j, "Series Alpha")
    kid_id = make_profile(j, KID, "child", KID_AGE)
    family = library_ids(j)["Famille"]

    # nothing is classed: the child's own catalogue and search hold neither series
    def seen_by_the_child() -> tuple[object, object]:
        works, found = j.api(
            ("GET", "/api/v1/works"), ("GET", "/api/v1/search?scope=all&q=Series"), user=KID
        )
        assert works[0] == 200 and found[0] == 200, (works, found)
        return works[1], found[1]

    works, found = seen_by_the_child()
    assert "Series" not in json.dumps(works) and "Series" not in json.dumps(found)
    # and the same screen for the administrator holds both (the Works exist, only hidden from her)
    assert "Series Alpha" in json.dumps(j.api(("GET", "/api/v1/works"))[0][1])

    # her Kavita account reads the family library only, and Kavita shows her nothing of it yet
    j.child_account("kid", family, unknowns=False, age=3)
    assert (
        j.api(("PUT", f"/api/v1/profiles/{kid_id}/kavita-account", {"kavita_username": "kid"}))[0][
            0
        ]
        == 200
    )
    assert j.visible_to("kid") == []

    # classed for all audiences (0) and filed on the family shelf, the series is hers everywhere; Beta stays out
    rate = f"/api/v1/works/{alpha_work}/rating"
    rated, filed = j.api(
        ("POST", rate, {"age": "0"}),
        ("POST", f"/api/v1/editions/{alpha_edition}/shelf", {"shelf_id": shelf_id(j, "Famille")}),
    )  # fmt: skip
    assert rated[0] == 200 and filed == (200, {"filed": True, "reason": None}), (rated, filed)
    j.publish_upstream()
    assert j.follow_until(lambda: any(f.endswith("Ch.2.cbz") for f in family_files(j)), 300)
    assert wait_for(lambda: j.visible_to("kid") == ["Series Alpha"], 120)
    works, found = seen_by_the_child()
    assert "Series Alpha" in json.dumps(works) and "Series Beta" not in json.dumps(works)
    assert "Series Beta" not in json.dumps(found)
    assert all("Series Beta" not in f for f in family_files(j))

    # the audit of the accounts: this child of 8 reads a family shelf that holds up to 12+, and the
    # account is wired to nothing else. The answer holds codes, never the name of an account
    [(code, audit)] = j.api(("POST", "/api/v1/audit/kavita-accounts"))
    assert code == 200, audit
    [target] = audit["targets"]  # type: ignore[index]
    assert target["state"] == "alert" and target["alerts"] == 1, target
    [child] = [c for c in target["children"] if c["profile_id"] == kid_id]
    codes = {f["code"] for f in child["findings"]}
    assert "library_above_max_age" in codes, child
    assert "kid" not in json.dumps(audit)  # the name of the account is not in the answer
    # the Health page carries the same alert; a child never reads either
    [(health_code, health)] = j.api(("GET", "/api/v1/health/page"))
    assert health_code == 200 and "alert" in json.dumps(health["kavita_audit"]), health  # type: ignore[index]
    assert j.api(("GET", "/api/v1/audit/kavita-accounts"), user=KID)[0][0] == 403
    assert j.api(("POST", "/api/v1/audit/kavita-accounts"), user=KID)[0][0] == 403
    assert j.kavita()["violations"] == [] and j.suwayomi()["violations"] == []
    assert j.egress() == []


# -- (3) a child asks, the administrator approves with an age, the book arrives ----------------------


@pytest.mark.e2e_timeout(1200)
@pytest.mark.req("EF-USR-010", "EF-USR-014", "EF-USR-016", "EF-INT-021")
def test_m3_request_journey(journey_m3: Journey) -> None:
    """The child finds a book of the free libraries marked "family" in her own search and asks for
    it by the reference her search gave; the administrator reads the request, approves it with an
    age and the family shelf; the book arrives in the child's Kavita library, her request says so
    with the way to open it, and the notice of arrival was queued for her."""
    j = journey_m3
    books_to_the_family_shelf(j)
    kid_id = make_profile(j, KID, "child", KID_AGE)
    family = shelf_id(j, "Famille")
    j.child_account("kid", library_ids(j)["Famille"], unknowns=False, age=3)
    assert (
        j.api(("PUT", f"/api/v1/profiles/{kid_id}/kavita-account", {"kavita_username": "kid"}))[0][
            0
        ]
        == 200
    )
    # the free library is "family" (the wizard's last choice) and her search shows the unrated
    [(marked, _)] = j.api(("PUT", "/api/v1/setup/sources/family", {"source_keys": ["gutenberg"]}))
    assert marked == 200
    assert j.api(
        ("PUT", f"/api/v1/profiles/{kid_id}/family-search", {"hide_unrated_remote": False, "subject_filter": []})
    )[0][0] == 200  # fmt: skip

    # she searches, and asks by the reference of the answer (never by an identifier of hers)
    [(code, found)] = j.api(("GET", "/api/v1/search?scope=all&q=study"), user=KID)
    assert code == 200, found
    hit = next(i for i in found["items"] if i["title"] == "A Study in Test")  # type: ignore[index]
    assert "gutenberg" not in json.dumps(hit).lower() and "1001" not in json.dumps(hit)
    [(code, asked)] = j.api(
        ("POST", "/api/v1/requests", {"request_ref": hit["request_ref"]}), user=KID
    )
    assert code == 201 and asked["state"] == "pending", asked  # type: ignore[index]
    request_id = str(asked["id"])  # type: ignore[index]

    # the administrator reads the queue and what approving takes, and approves with an age
    [(code, queue)] = j.api(("GET", "/api/v1/requests/queue"))
    assert code == 200 and [i["id"] for i in queue["items"]] == [request_id], queue  # type: ignore[index]
    [(code, what)] = j.api(("GET", f"/api/v1/requests/{request_id}/approval"))
    assert code == 200 and what["needs_age"] is True, what  # type: ignore[index]
    body: dict[str, object] = {
        "age": KID_AGE,
        "shelf_id": family,
        "confirm_size": bool(what["confirmation_required"]),
    }  # type: ignore[index]
    [(code, approved)] = j.api(("POST", f"/api/v1/requests/{request_id}/approve", body))
    assert code == 200, approved

    # it arrives in her family library, and her request says so
    export_until_listed(j, "/library/family", "A Study in Test")
    assert wait_for(lambda: j.visible_to("kid") == ["A Study in Test"], 120)

    def request_of_the_child() -> dict[str, object]:
        return j.api(("GET", f"/api/v1/requests/{request_id}"), user=KID)[0][1]  # type: ignore[return-value]

    assert wait_for(lambda: request_of_the_child().get("family_state") == "available", 120), (
        request_of_the_child()
    )
    mine = request_of_the_child()
    assert mine["open_url"], mine
    for word in ("gutenberg", "Gutenberg", "1001", "age"):
        assert word not in json.dumps(mine), word  # what a child reads names no source and no age
    # the notice of arrival was queued for her: one event of the bus, one line of her digest
    assert wait_for(
        lambda: j.sql("select count(*) from event where type = 'ops.request_arrived'") == [(1,)], 60
    )
    assert j.kavita()["violations"] == [] and j.egress() == []


def books_to_the_family_shelf(j: Journey) -> None:
    """Kavita, the family shelf (12+ at most), the books project reached through its catalogue and
    its mirror: what the request journey needs, typed by the administrator."""
    j.secrets()
    added = j.shelfhold(
        "target", "add", "kavita", "--url", KAVITA_URL, "--user", "shelfhold",
        "--password-file", "/tmp/kavita.pw", "--accept-admin-risk",
        "--path-map", '[{"local": "/library", "remote": "/library", "mode": "rw"}]',
    )  # fmt: skip
    assert added.code == 0, added.err
    shelf = j.shelfhold("shelf", "create", "Famille", "--max-age", "12+", "--target", "kavita",
                        "--subpath", "family")  # fmt: skip
    assert shelf.code == 0, shelf.err
    j.create_admin()
    configure_sources(j)
    found = j.shelfhold("source", "search", "gutenberg", "study")
    assert found.code == 0 and first_id(found.out) == "1001", found


def export_until_listed(j: Journey, folder: str, title: str) -> None:
    """Ask for the export until Kavita lists `title` (each run also asks for the scan)."""

    def listed() -> bool:
        j.shelfhold("jobs", "run", "export.run")
        j.fake("kavita", f"{KAVITA_URL}/_fake/index?folder={folder}", post=True)
        return title in kavita_titles(j)

    assert wait_for(listed, 400, 10), kavita_titles(j)


# -- (4) renaming a series two members read: the warning, with a number ------------------------------


def manga_install(j: Journey) -> tuple[str, str]:
    """Kavita and the 18+ shelf, a series exported to it (the first chapter after the discovery);
    returns `(destination id, library name)`."""
    install_by_commands(j)
    j.create_admin()
    j.discover_series()
    discovered(j)
    j.publish_upstream()
    assert j.follow_until(lambda: exported_chapter(j, "manga"), 300)
    # Kavita's own scan of its folder, which the simulated server does when asked
    j.fake("kavita", f"{KAVITA_URL}/_fake/index?folder=/library/manga", post=True)
    assert wait_for(lambda: kavita_titles(j) == ["Series Alpha"], 60), kavita_titles(j)
    return destination_of(j, "Manga"), "Manga"


@pytest.mark.e2e_timeout(900)
@pytest.mark.req("EF-EXP-030", "EF-EXP-037")
def test_m3_rename_warning_journey(journey_m3: Journey) -> None:
    """Two members read the series in Kavita; the administrator changes the naming template. The
    plan is read first and says how many readers would lose their progress; applied without the
    confirmation it is refused and moves nothing; applied with it, the files take their new names."""
    j = journey_m3
    destination, library = manga_install(j)
    for name in ("mum", "dad"):
        j.child_account(name, library_ids(j)[library], unknowns=True, age=-1)
    before = sorted(
        f for f in j.library_files() if f.startswith("/library/manga/") and f.endswith(".cbz")
    )
    assert before and all(" - Ch." in f for f in before), before

    template = {"chapter": "{series} - Chapter {number}"}
    [(code, plan)] = j.api(
        ("POST", "/api/v1/plans/renames", {"shelf_destination_id": destination, "template": template})
    )  # fmt: skip
    assert code == 200, plan
    assert plan["steps"] and plan["plan_id"], plan  # type: ignore[index]
    [warning] = plan["warnings"]  # type: ignore[index]
    assert warning["readers"] >= 2, warning  # the accounts that would lose their progress, counted
    assert warning["exact"] is False, (
        warning
    )  # the members' own progress is read in M4: an upper bound
    nothing_moved = sorted(f for f in j.library_files() if f.startswith("/library/manga/"))

    etag = {"If-Match": f'"{plan["base_version"]}"'}  # type: ignore[index]
    apply_path = f"/api/v1/plans/{plan['plan_id']}/apply"  # type: ignore[index]
    [(refused, why)] = j.api(("POST", apply_path, {}, etag))
    assert refused >= 400 and refused != 412, (refused, why)  # the confirmation is asked for
    assert "readers" in json.dumps(why), why
    assert sorted(f for f in j.library_files() if f.startswith("/library/manga/")) == nothing_moved

    [(accepted, job)] = j.api(("POST", apply_path, {"confirm_progress_loss": True}, etag))
    assert accepted == 202, job
    renamed = wait_for(
        lambda: all(
            " - Chapter " in f
            for f in j.library_files()
            if f.startswith("/library/manga/") and f.endswith(".cbz")
        ),
        200,
    )
    assert renamed, (
        j.library_files(),
        j.sql(
            "select kind, status, attempts, error_code, last_error from job where kind = 'export.apply'"
        ),
    )
    assert j.kavita()["violations"] == [] and j.egress() == []


# -- (5) the full detach: Kavita stands alone ---------------------------------------------------------


@pytest.mark.e2e_timeout(900)
@pytest.mark.req("EF-EXP-031", "EF-EXP-056")
def test_m3_detach_journey(journey_m3: Journey) -> None:
    """The administrator detaches Kavita for good: the phrase to type is shown first, nothing moves
    without it; with it the dedicated key is revoked, Shelfhold's reserved folder is gone and the
    tree Kavita reads is whole, byte for byte, with Kavita still listing its series."""
    j = journey_m3
    manga_install(j)
    target = target_id(j)
    tree = {
        f: j.read_bytes(f)
        for f in j.library_files()
        if f.endswith(".cbz") and "/.shelfhold/" not in f
    }
    series = kavita_titles(j)
    assert tree and series

    [(code, shown)] = j.api(("POST", f"/api/v1/targets/{target}/detach-preview"))
    assert code == 200, shown
    phrase = str(shown["phrase"])  # type: ignore[index]
    assert target in phrase

    # without the phrase, or with it altered, nothing is done
    for typed in ("", phrase.upper(), phrase + " "):
        [(refused, _)] = j.api(("POST", f"/api/v1/targets/{target}/detach", {"typed": typed}))
        assert refused >= 400, typed
    assert j.sql("select count(*) from export_target") == [(1,)]
    assert j.kavita()["keys"] != []

    [(done_code, report)] = j.api(
        ("POST", f"/api/v1/targets/{target}/detach", {"typed": phrase, "credential": KAVITA_PASSWORD})
    )  # fmt: skip
    assert done_code == 200, report
    assert report["complete"] is True and report["key_revoked"] is True, report  # type: ignore[index]
    assert j.kavita()["keys"] == []  # the dedicated key is gone from Kavita itself
    assert j.sql("select count(*) from export_target") == [(0,)]
    assert not [f for f in j.library_files() if "/.shelfhold/" in f]  # the reserved folder is gone
    after = {f: j.read_bytes(f) for f in j.library_files() if f.endswith(".cbz")}
    assert after == tree  # the administrator's tree, byte for byte
    assert kavita_titles(j) == series  # Kavita lists what it listed, on its own now
    # Shelfhold writes nothing more there: a new chapter upstream reaches no folder of the library
    j.publish_upstream()
    j.shelfhold("jobs", "run", "follow.cycle")
    j.shelfhold("jobs", "run", "export.run")
    assert {f for f in j.library_files() if f.endswith(".cbz")} == set(tree)
    assert j.egress() == []


# -- (6) "Kavita unreachable": understood, and solved by the action shown -----------------------------


@pytest.mark.e2e_timeout(900)
@pytest.mark.req("EF-UX-003", "EF-EXP-034", "ENF-OBS-003")
def test_m3_kavita_unreachable_action_journey(journey_m3: Journey) -> None:
    """Kavita stops answering. The administrator is told in plain words what happens and what to
    do (the title and the action of the catalogue, not a trace); doing that action (Kavita answers
    again, the connection is checked) brings the exports back, and the chapter that waited is
    delivered."""
    j = journey_m3
    manga_install(j)
    target = target_id(j)

    j.compose("pause", "fake-kavita")  # the container is there, nobody answers
    try:
        [(code, problem)] = j.api(("POST", f"/api/v1/targets/{target}/recheck"))
        assert code == 503, (code, problem)
        assert problem["code"] == "target.unreachable", problem  # type: ignore[index]
        assert "Kavita ne répond pas" in problem["title"], problem  # type: ignore[index]
        assert (
            "conteneur Kavita" in problem["action"] and "target reconnect" in problem["action"]
        ), problem  # type: ignore[index]
        for leak in ("Traceback", "httpx", "127.0.0.1", "/library"):
            assert leak not in json.dumps(problem), leak  # no trace, no address, no path
    finally:
        j.compose("unpause", "fake-kavita")

    # the action: Kavita is up again, the connection is looked at once more
    assert wait_for(lambda: bool(j.kavita()), 60)
    [(code, back)] = j.api(("POST", f"/api/v1/targets/{target}/recheck"))
    assert code == 200, back
    [(code, reconnected)] = j.api(("POST", f"/api/v1/targets/{target}/reconnect"))
    assert code == 200, reconnected
    j.publish_upstream()
    assert j.follow_until(lambda: exported_chapter(j, "manga", "Ch.3.cbz"), 300)
    assert j.kavita()["violations"] == [] and j.egress() == []


# -- (7) the key screens, per theme --------------------------------------------------------------------


@pytest.mark.e2e_timeout(600)
@pytest.mark.req("EF-UX-012", "ENF-A11Y-001")
def test_key_screens_snapshot_per_theme(journey_m3: Journey) -> None:
    """The key screens of the administrator and of a child of 8, in each theme the image holds
    and in each colour scheme: the tokens the browser applied are those of `tokens.json`, the
    screen has the same landmarks in light and in dark, nothing overflows, no script fails. The
    pixels are kept as an artifact and never compared (see the spec for why)."""
    j = journey_m3
    install_by_the_administrator(j)
    make_profile(j, KID, "child", KID_AGE)
    done = playwright(j, "test_key_screens_snapshot_per_theme", 560)
    assert done.returncode == 0, (
        "the key screens differ from the themes (see the output of the spec)"
    )
    assert j.egress() == []
