"""M1b on the image under test (L30): the family shelf and the parental barrier, the safeguard
before an upstream purge, the piloted wake, the backup restored on another path, Suwayomi in
`ui_login`, the purge, and the way back to the previous image (ENF-OPS-008).

Same method as the M1a journey: the simulated Kavita, Suwayomi and Sablier of `e2e/fakes` behind
a socket, a real library volume, the commands the administrator types and the requests the SPA
sends (session and CSRF). Synthetic data only; nothing leaves the network namespace of the stack
(`Journey.egress()` is read at the end of each journey).
"""

import json
import os
import re
import subprocess
from collections.abc import Iterator

import pytest

from journey import Journey, wait_for
from test_m1a_journey import install_by_commands

FAMILY_CEILING = "12+"


def ids_of(j: Journey, series: str) -> tuple[str, str]:
    """`(edition id, work id)` of the series of the simulated Suwayomi called `series`."""
    rows = j.sql(
        "select e.id, w.id from edition e join work w on w.id = e.work_id "
        f"where w.title = '{series}'"
    )
    assert len(rows) == 1, rows
    return str(rows[0][0]), str(rows[0][1])


def shelf_id(j: Journey, name: str) -> str:
    return str(j.sql(f"select id from shelf where name = '{name}'")[0][0])


def family_install(j: Journey) -> None:
    install_by_commands(j)
    j.create_admin()
    done = j.shelfhold("shelf", "create", "Famille", "--max-age", FAMILY_CEILING,
                       "--target", "kavita", "--subpath", "family")  # fmt: skip
    assert done.code == 0, done.err
    j.discover_series()
    discovered(j)
    assert j.shelfhold("jobs", "run", "export.run").code == 0  # the export looks at the shelves
    assert wait_for(lambda: len(j.waiting().get("rating_pending", [])) == 2, 150)


def discovered(j: Journey, editions: int = 2) -> None:
    """The first follow cycle filed every series of the library (it lists them one by one)."""
    assert wait_for(lambda: j.sql("select count(*) from edition") == [(editions,)], 120)


def family_files(j: Journey) -> list[str]:
    return [f for f in j.library_files() if f.startswith("/library/family/") and f.endswith(".cbz")]


@pytest.mark.e2e_timeout(600)
@pytest.mark.req("EF-EXP-046", "EF-USR-015", "EF-BIB-036", "EF-BIB-044", "EF-USR-024")
def test_m1b_family_shelf_journey(journey_m1b: Journey) -> None:
    """(1) An unrated Work is refused on the family shelf, classed from the screen (session and
    CSRF), then arrives in the family library, which a child account alone reads; the shelf's
    ceiling lowered withdraws what no longer fits, only with the plan that was read."""
    j = journey_m1b
    family_install(j)
    alpha_edition, alpha_work = ids_of(j, "Series Alpha")
    beta_edition, _ = ids_of(j, "Series Beta")
    family = shelf_id(j, "Famille")

    # the unrated Work is refused on the family shelf, and listed in "À classer"
    refused = j.api(("POST", f"/api/v1/editions/{alpha_edition}/shelf", {"shelf_id": family}))
    assert refused == [(200, {"filed": False, "reason": "unrated_edition"})]
    waiting = j.api(("GET", "/api/v1/to-rate"))[0][1]
    assert {i["title"] for i in waiting["items"]} == {"Series Alpha", "Series Beta"}  # type: ignore[index]

    # attacks on the right to rate: no session, a session without the CSRF header, an API key
    body = {"age": "10+"}
    rate_path = f"/api/v1/works/{alpha_work}/rating"
    assert j.api(("POST", rate_path, body), mode="anon")[0][0] == 401
    assert j.api(("POST", rate_path, body), mode="nocsrf")[0][0] in (400, 403)
    key = re.search(r"shk_\S+", j.shelfhold("admin", "keys", "create", "--name", "e2e").out)
    assert key is not None
    assert j.api(("POST", rate_path, body), mode=f"bearer:{key.group(0)}")[0][0] == 403
    assert j.api(("GET", "/api/v1/to-rate"))[0][1]["items"]  # type: ignore[index]  # still unrated

    # classed from the screen, the Work can be filed; the other one stays out
    rated, filed = j.api(("POST", rate_path, body),
                         ("POST", f"/api/v1/editions/{alpha_edition}/shelf", {"shelf_id": family}))  # fmt: skip
    assert rated[0] == 200 and filed == (200, {"filed": True, "reason": None})
    assert j.api(("POST", f"/api/v1/editions/{beta_edition}/shelf", {"shelf_id": family}))[0][1] == {
        "filed": False, "reason": "unrated_edition",
    }  # fmt: skip

    j.adopted_series("Adult Title")  # the administrator's own, in the 18+ library
    j.publish_upstream()
    assert j.follow_until(lambda: any(f.endswith("Ch.2.cbz") for f in family_files(j)), 300)
    assert all("Series Beta" not in f for f in family_files(j))
    libraries = {lib["name"]: lib["id"] for lib in j.kavita()["libraries"]}  # type: ignore[index,union-attr]
    j.child_account("kid", int(libraries["Famille"]))
    assert wait_for(lambda: j.visible_to("kid") == ["Series Alpha"], 120)  # classed, never 18+

    # the ceiling is lowered under what is on the shelf: the plan is read, then applied with it
    assert j.api(("POST", rate_path, {"age": "16+"}))[0][0] == 200
    plan = j.shelfhold("shelf", "withdraw")
    assert plan.code == 0 and "above_ceiling" in plan.out and family_files(j)  # nothing moved yet
    version = re.search(r"--confirm (\w+)", plan.out)
    assert version is not None
    assert j.shelfhold("shelf", "withdraw", "--apply").code != 0
    assert j.shelfhold("shelf", "withdraw", "--apply", "--confirm", "0" * 32).code != 0
    assert family_files(j)  # a refused apply withdrew nothing
    applied = j.shelfhold("shelf", "withdraw", "--apply", "--confirm", version.group(1))
    assert applied.code == 0, applied.err
    assert family_files(j) == []  # out of the children's library, into the root's recycle bin
    assert any("/.shelfhold/recycle/" in f and f.endswith("Ch.2.cbz") for f in j.library_files())
    assert j.kavita()["violations"] == [] and j.suwayomi()["violations"] == []
    assert j.egress() == []


@pytest.mark.e2e_timeout(600)
@pytest.mark.req("EF-BIB-038", "EF-BIB-027", "EF-SRC-009", "EF-BIB-042")
def test_m1b_safeguard_journey(journey_m1b: Journey) -> None:
    """(2) The copy kept in /store is independent (another inode, no link), and the upstream
    purge comes only after it: the service is asked to erase the download once /store holds it,
    never before."""
    j = journey_m1b
    install_by_commands(j)
    j.create_admin()
    j.discover_series()
    discovered(j)
    alpha_edition, _ = ids_of(j, "Series Alpha")
    # a shelf in `keep` mode (created from the screen's route), bound to the Kavita target
    created = j.api(("POST", "/api/v1/shelves", {"name": "Archive", "phrase": "Archive",
                                                 "max_age_rating": 18, "retention_mode": "keep"}))  # fmt: skip
    assert created[0][0] == 201
    assert j.shelfhold("shelf", "create", "Archive", "--max-age", "18+", "--target", "kavita",
                       "--subpath", "archive").code == 0  # fmt: skip
    archive = shelf_id(j, "Archive")
    assert j.api(("POST", f"/api/v1/editions/{alpha_edition}/shelf", {"shelf_id": archive}))[0][1] == {
        "filed": True, "reason": None,
    }  # fmt: skip
    assert j.shelfhold("shelf", "purge-upstream", "--on", "Archive").code == 0

    j.publish_upstream()
    assert j.follow_until(lambda: any("/archive/" in f and f.endswith("Ch.2.cbz")
                                      for f in j.library_files()), 300)  # fmt: skip
    exported = next(f for f in j.library_files() if "/archive/" in f and f.endswith("Ch.2.cbz"))
    # nothing was asked of the service yet: no copy of the download is in /store
    assert j.suwayomi()["deleted"] == []

    assert j.shelfhold("jobs", "run", "store.keep_catchup").code == 0  # the window job, now
    assert wait_for(lambda: bool(kept_files(j)), 150), "no kept copy in /store"
    assert j.shelfhold("jobs", "run", "library.safeguard").code == 0
    assert wait_for(lambda: bool(j.suwayomi()["deleted"]), 150), (
        "the upstream download is still there"
    )

    kept = kept_files(j)[0]
    assert j.stat(kept)[0] != j.stat(exported)[0]  # an independent copy: another inode
    assert j.stat(kept)[1] == 1 and j.stat(exported)[1] == 1  # no hard link between the two
    erased = j.suwayomi()["erased_with_store"]
    assert erased and all(e["store_files"] for e in erased)  # type: ignore[attr-defined]
    assert j.stat(exported)  # the file Kavita reads is still there
    assert j.kavita()["violations"] == [] and j.suwayomi()["violations"] == []
    assert j.egress() == []


def kept_files(j: Journey) -> list[str]:
    done = j.compose("exec", "-T", "shelfhold", "find", "/store", "-name", "*.cbz", check=False)
    return sorted(line for line in done.stdout.splitlines() if line)


@pytest.mark.e2e_timeout(600)
@pytest.mark.req("EF-INT-022", "EF-SRC-025", "EF-SRC-027", "ENF-OPS-011", "EF-BIB-042")
def test_m1b_piloted_wake_journey(journey_m1b: Journey) -> None:
    """(5) Suwayomi was stopped by Sablier and a chapter came out upstream that it does not know:
    the piloted cycle wakes it first, asks it to refresh the series, and sees the next chapter on
    waking. (3) An authentication error counts as nothing: three cycles, no candidate."""
    j = journey_m1b
    install_by_commands(j)
    assert j.shelfhold("config", "set", "source.check_mode", "piloted").code == 0
    j.discover_series()
    discovered(j)
    known = j.units_known()
    j.suwayomi_do("publish_hidden")  # published upstream, unknown to Suwayomi until it refreshes
    j.suwayomi_do("sleep")  # a day later, Sablier has stopped it
    woken_before = len(j.sablier()["calls"])  # the discovery may already have woken it

    def chapter_exported() -> bool:
        return any(f.endswith("Ch.2.cbz") for f in j.library_files())

    assert j.follow_until(chapter_exported, 300), "le chapitre n'est pas vu au réveil"
    assert j.units_known() > known
    calls = j.sablier()["calls"]
    assert len(calls) > woken_before  # woken again after it went to sleep
    assert all(c["names"] == ["fake-suwayomi"] for c in calls)  # by name, through the hook
    assert j.suwayomi()["refreshed"]  # asked to refresh the series, then read

    # (3) the credentials stop working: three cycles answered 401, and nothing is counted
    j.suwayomi_do("expire")
    j.publish_upstream()
    for _ in range(3):
        assert j.shelfhold("jobs", "run", "follow.cycle").code == 0
        assert wait_for(lambda: "auth_expired" in j.shelfhold("status").out, 90)
    assert j.sql("select count(*) from source_failure") == [(0,)]  # never counted as content
    assert j.sql("select count(*) from download_request where state != 'fetched'") == [(0,)]
    out = j.shelfhold("status").out.lower()
    assert "irremplaçable" not in out and "candidate" not in out  # no candidate for the safeguard
    assert j.kavita()["violations"] == []
    assert j.egress() == []


@pytest.mark.e2e_timeout(600)
@pytest.mark.req("ENF-OPS-001", "EF-INT-015", "EF-INT-026", "EF-INT-016")
def test_m1b_backup_restore_other_path_journey(journey_m1b: Journey) -> None:
    """(6) `backup run`, then a restore on another path with no `/store`: the restored instance is
    behind the files, waits, and `reconcile --adopt-after-restore` deletes nothing."""
    j = journey_m1b
    install_by_commands(j)
    j.create_admin()
    j.discover_series()
    discovered(j)
    ran = j.shelfhold("backup", "run")
    assert ran.code == 0, ran.err
    archive = j.compose(
        "exec", "-T", "shelfhold", "sh", "-c", "ls /backups/*.tar.age"
    ).stdout.split()[0]
    assert j.shelfhold("backup", "verify").code == 0
    # a chapter is filed after the backup: the library is now ahead of the saved database
    j.publish_upstream()
    assert j.follow_until(lambda: any(f.endswith("Ch.2.cbz") for f in j.library_files()), 300)
    before = j.library_files()

    refused = j.shelfhold("backup", "restore", "--from", archive, "--to", "/data")
    assert refused.code != 0  # never over the data in service
    assert j.shelfhold("backup", "restore", "--from", archive, "--to", "/tmp/restored").code == 0
    restored = {
        "SHELFHOLD_DATA_DIR": "/tmp/restored",
        "SHELFHOLD_STORE_DIR": "/tmp/restored-store",
    }
    status = j.shelfhold("status", env=restored)
    assert status.code == 0 and "restored_credentials" in status.out
    assert (
        "restored_behind" in status.out
        or "restored_behind"
        in j.shelfhold("reconcile", "--adopt-after-restore", "--dry-run", env=restored).out
    )  # the library is ahead of the saved database: the instance waits
    # the preview first (it writes nothing), then the confirmation of exactly what it showed
    preview = j.shelfhold("reconcile", "--adopt-after-restore", "--dry-run", env=restored)
    assert preview.code == 0, preview.err
    assert j.shelfhold("reconcile", "--adopt-after-restore", env=restored).code != 0  # unconfirmed
    assert j.library_files() == before
    fingerprint = re.search(r"Empreinte de l'aperçu\s: (\w+)", preview.out)
    assert fingerprint is not None, preview.out
    adopted = j.shelfhold(
        "reconcile",
        "--adopt-after-restore",
        "--confirm",
        fingerprint.group(1),
        env=restored,
    )
    assert adopted.code == 0, adopted.err
    assert j.library_files() == before  # nothing was deleted, nothing was moved
    assert j.shelfhold("restore", "acknowledge", env=restored).code in (0, 2, 3)
    assert j.kavita()["violations"] == [] and j.suwayomi()["violations"] == []
    assert j.egress() == []


@pytest.mark.e2e_timeout(480)
@pytest.mark.req("EF-SRC-008", "EF-SRC-036", "EF-INT-019")
def test_m1b_ui_login_service_account_journey(journey_ui_login: Journey) -> None:
    """(8) Suwayomi in `ui_login` is followed by the service account: one login, the JWT pair,
    one entry per followed series (the second ask adds nothing), in the Shelfhold category that
    the scheduled downloads skip, and only the library of that account is read."""
    j = journey_ui_login
    install_by_commands(j)
    j.discover_series()
    discovered(j)
    state = j.suwayomi()
    assert state["login_users"] and set(state["login_users"]) == {"shelfhold"}  # type: ignore[arg-type]
    first = j.shelfhold("follow", "suwayomi:7003", "/series-gamma")
    assert first.code == 0, first.err
    again = j.shelfhold("follow", "suwayomi:7003", "/series-gamma")
    assert again.code == 0 and "déjà" in again.out  # the same request adds no second entry
    state = j.suwayomi()
    assert [c["name"] for c in state["categories"]] == ["Shelfhold"]  # type: ignore[attr-defined]
    assert state["categories"][0]["includeInDownload"] == "EXCLUDE"  # type: ignore[index]
    assert state["categories"][0]["includeInUpdate"] != "EXCLUDE"  # type: ignore[index]
    assert state["assigned"]["/series-gamma"] == [state["categories"][0]["id"]]  # type: ignore[index]
    assert sum(1 for ids in state["assigned"].values() if ids) == 1  # type: ignore[attr-defined]
    assert state["violations"] == []
    assert j.egress() == []


@pytest.mark.e2e_timeout(480)
@pytest.mark.req("EF-BIB-045", "EF-BIB-052", "EF-BIB-054", "ENF-LEG-003")
def test_m1b_purge_journey(journey_m1b: Journey) -> None:
    """The purge: it needs the identifier typed back, it leaves no file of the Work and no
    readable copy in /store, and the Work stays erased."""
    j = journey_m1b
    install_by_commands(j)
    j.create_admin()
    j.discover_series()
    discovered(j)
    j.publish_upstream()
    assert j.follow_until(lambda: any(f.endswith("Ch.2.cbz") for f in j.library_files()), 300)
    _, work = ids_of(j, "Series Alpha")
    # attacks: no session, a wrong confirmation, the identifier of another Work
    assert j.api(("POST", f"/api/v1/works/{work}/purge", {"typed": work}), mode="anon")[0][0] == 401
    wrong = j.shelfhold("purge", "--work", work, "--confirm", "x" * 26, "--now")
    assert wrong.code != 0
    assert any(f.endswith("Ch.2.cbz") for f in j.library_files())  # nothing was erased
    done = j.shelfhold("purge", "--work", work, "--confirm", work, "--now")
    assert done.code == 0, done.err
    assert not any(f.endswith(".cbz") and "Series Alpha" in f for f in j.library_files())
    assert not any(work in f or "Series Alpha" in f for f in kept_files(j))
    assert j.kavita()["violations"] == [] and j.suwayomi()["violations"] == []
    assert j.egress() == []


# the way back: the previous image on the volumes of this one ---------------------------------
PREVIOUS = os.environ.get("SHELFHOLD_IMAGE_PREVIOUS")
needs_previous = pytest.mark.skipif(
    not PREVIOUS,
    reason="SHELFHOLD_IMAGE_PREVIOUS (the image of the last release) is not set",
)
SENTINELS = "cat /library/.shelfhold-mount-id; echo; cat /store/.shelfhold-mount-id"


def sentinels(j: Journey) -> list[dict[str, object]]:
    """The two mount sentinels (/library, /store) as the container reads them."""
    text = j.compose("exec", "-T", "shelfhold", "sh", "-c", SENTINELS).stdout
    decoder, found, rest = json.JSONDecoder(), [], text.lstrip()
    while rest:
        document, end = decoder.raw_decode(rest)
        found.append(document)
        rest = rest[end:].lstrip()
    return found


@pytest.fixture(scope="module")
def rollback_world() -> Iterator[dict[str, object]]:
    """One journey for both rollback tests (it is the long one): the previous release installs and
    files a chapter; the image under test takes over the volumes (migration, then a purge, which
    makes M1b write its sentinel); then the way back, as the documentation says it: the database
    of the pre-migration backup under the previous image, `restored_behind`, then the adoption."""
    if not PREVIOUS or not os.environ.get("SHELFHOLD_IMAGE"):
        pytest.skip("SHELFHOLD_IMAGE and SHELFHOLD_IMAGE_PREVIOUS are needed")
    j = Journey(app_image=PREVIOUS)
    j.compose("down", "-v", "-t", "1", check=False)
    try:
        j.compose("up", "--wait", "-d")
        yield run_rollback(j)
    finally:
        print(j.compose("logs", "--no-color", check=False).stdout)
        j.compose("down", "-v", "-t", "1", check=False)


def run_rollback(j: Journey) -> dict[str, object]:
    install_by_commands(j)  # the commands of 0.2, on the 0.2 image
    j.discover_series()
    discovered(j)
    j.publish_upstream()
    assert j.follow_until(lambda: any(f.endswith("Ch.2.cbz") for f in j.library_files()), 300)
    sentinels_m1a = sentinels(j)
    assert {s["schema_version"] for s in sentinels_m1a} == {"1.0"}

    j.switch_app_image(os.environ["SHELFHOLD_IMAGE"])  # the migration runs, with its backup
    backups = j.compose("exec", "-T", "shelfhold", "sh", "-c", "ls /data/backups/pre-migration")
    backup = backups.stdout.split()[0]
    j.create_admin()
    beta_work = ids_of(j, "Series Beta")[1]
    # B3: the Work carries an external identifier, so its purge also lays a `work` tombstone, which
    # cites the normalizer `work_id@1` that the previous image does not have
    j.compose(
        "exec", "-T", "shelfhold", "python", "-c",
        "import sqlite3; c = sqlite3.connect('/data/shelfhold.db'); "
        "c.execute(\"insert into external_id (entity_type, entity_id, provider, value, origin) "
        f"values ('work', '{beta_work}', 'anilist', '30013', 'provider')\"); c.commit()",
    )  # fmt: skip
    purged = j.shelfhold("purge", "--work", beta_work, "--confirm", beta_work, "--now")
    assert purged.code == 0, purged.err  # a Work of no file: M1b writes its tombstone chain
    assert j.sql("select count(*) from content_tombstone where scope = 'work'") == [(1,)]
    sentinels_m1b = sentinels(j)
    assert {s["schema_version"] for s in sentinels_m1b} == {"1.2"}
    assert all("tombstone_chain" in s for s in sentinels_m1b)
    files_m1b = j.library_files()
    assert any(f.endswith("Ch.2.cbz") for f in files_m1b)

    # the way back: the saved database first (the newer one would be refused), then the release
    j.compose("stop", "shelfhold")
    j.compose(
        "run", "--rm", "--no-deps", "-T", "--entrypoint", "sh", "shelfhold-migrate", "-c",
        f"cp /data/backups/pre-migration/{backup} /data/shelfhold.db "
        "&& rm -f /data/shelfhold.db-wal /data/shelfhold.db-shm",
    )  # fmt: skip
    j.switch_app_image(PREVIOUS or "")
    # the adoption wants the volumes to itself, and every root mounted (the `shelfhold` service,
    # not the migration one, which has no /library: a root that is not mounted is not judged)
    j.compose("stop", "shelfhold")

    def offline(*args: str) -> subprocess.CompletedProcess[str]:
        return j.compose(
            "run", "--rm", "--no-deps", "-T", "--entrypoint", "shelfhold", "shelfhold",
            *args, check=False,
        )  # fmt: skip

    preview = offline("reconcile", "--adopt-after-restore", "--dry-run")
    # the previous release prints "racine(s)", the image under test an ICU plural
    late = re.search(r"racine(?:s|\(s\))? en retard", preview.stdout)
    assert late, (preview.stdout, preview.stderr)
    assert "Rien ne sera supprimé" in preview.stdout
    fingerprint = re.search(r"Empreinte de l'aperçu\s: (\w+)", preview.stdout)
    assert fingerprint is not None, (preview.stdout, preview.stderr)
    adopted = offline("reconcile", "--adopt-after-restore", "--confirm", fingerprint.group(1))
    assert adopted.returncode == 0, adopted.stderr
    j.compose("up", "-d", "--no-deps", "shelfhold")  # the simulated servers are already up
    assert wait_for(lambda: j.shelfhold("status").code == 0, 120)
    # B3: the previous image replays the journal, work tombstone included, and still serves
    work_tombstones = j.sql("select count(*) from content_tombstone where scope = 'work'")
    return {
        "work_tombstones_after": work_tombstones,
        "preview": preview.stdout,
        "roots": j.sql("select path_entry, generation from physical_root"),
        "waiting_after_adoption": j.shelfhold("status").out,
        "files_m1b": files_m1b,
        "files_after": j.library_files(),
        "sentinels_m1b": sentinels_m1b,
        "sentinels_after": sentinels(j),
        "meta_check": j.shelfhold("meta", "check").code,
    }


@needs_previous
@pytest.mark.e2e_timeout(900)
@pytest.mark.req("ENF-OPS-008", "EF-BIB-054")
def test_rollback_with_a_work_tombstone_starts_the_previous_release(
    rollback_world: dict[str, object],
) -> None:
    """B3: a `work` tombstone laid by the purge of a Work with an external identifier cites
    `work_id@1`; the previous release does not know it, and must still build its index."""
    assert rollback_world["work_tombstones_after"] == [(1,)]


@needs_previous
@pytest.mark.e2e_timeout(900)
@pytest.mark.req("ENF-OPS-008", "ENF-OPS-013", "EF-INT-026")
def test_rollback_after_lazy_format_migration_no_loss(
    rollback_world: dict[str, object],
) -> None:
    """Back from the image under test to the previous release, with the pre-migration backup: the
    previous release waits in `restored_behind` and nothing runs; the adoption deletes nothing
    (every file filed since is still there), leaves no waiting state, and the files of the
    projections written by the newer release are readable by the older one."""
    world = rollback_world
    assert "Rien ne sera supprimé" in world["preview"]  # type: ignore[operator]
    assert re.search(r"racine(?:s|\(s\))? en retard", world["preview"])  # type: ignore[arg-type]
    assert world["files_after"] == world["files_m1b"]  # nothing lost, nothing moved
    assert world["meta_check"] == 0  # unit.json, work.json and the mirror: read, no drift


@needs_previous
@pytest.mark.e2e_timeout(900)
@pytest.mark.req("ENF-OPS-008", "ENF-OPS-013", "EF-INT-026")
def test_rollback_reads_m1b_sentinel_without_field_loss(
    rollback_world: dict[str, object],
) -> None:
    """The sentinel M1b wrote (`1.2`, with the registry of tombstone keys and the chain) is read
    and stamped again by the previous release: no field of it is lost, the root is the same one,
    and the tombstone keys are the same."""
    before = {s["root_id"]: s for s in rollback_world["sentinels_m1b"]}  # type: ignore[attr-defined]
    after = {s["root_id"]: s for s in rollback_world["sentinels_after"]}  # type: ignore[attr-defined]
    assert after.keys() == before.keys()  # the same two roots
    for root, old in before.items():
        new = after[root]
        assert set(old) <= set(new), (root, set(old) - set(new))  # no field dropped
        assert new["tombstone_kids"] == old["tombstone_kids"]
        assert new["instance_id"] == old["instance_id"]


@needs_previous
@pytest.mark.e2e_timeout(900)
@pytest.mark.req("ENF-OPS-008", "EF-INT-026")
def test_rollback_adoption_clears_restored_behind(rollback_world: dict[str, object]) -> None:
    """After `reconcile --adopt-after-restore` by the previous release, no waiting state is left
    and every root is re-stamped forward."""
    assert "restored_behind" not in rollback_world["waiting_after_adoption"]  # type: ignore[operator]
    before = {s["root_id"]: s for s in rollback_world["sentinels_m1b"]}  # type: ignore[attr-defined]
    for sentinel in rollback_world["sentinels_after"]:  # type: ignore[attr-defined]
        assert int(sentinel["generation"]) > int(before[sentinel["root_id"]]["generation"])
