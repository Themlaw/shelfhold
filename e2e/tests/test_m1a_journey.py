"""M1a from the installation to the first chapter in Kavita, headless (EF-INT-024), the redeploy
(ENF-OPS-017) and the detach (EF-EXP-056), on the image under test.

Kavita and Suwayomi are the simulated servers of backend/tests/fakes behind a socket
(e2e/fakes); the library volume is real, so "readable in Kavita" means "indexed from the files
Shelfhold wrote". Every step is a `shelfhold` command typed in the container: no SQL.
"""

import subprocess
import time

import pytest

from journey import KAVITA_URL, PATH_MAP, SUWAYOMI_URL, Journey, wait_for

SHELFHOLD_TOML = f"""\
schema_version = 1

[source]
check_mode = "always_on"

[[targets]]
kind = "kavita"
name = "kavita"
url = "{KAVITA_URL}"
user = "shelfhold"
password_file = "/tmp/kavita.pw"
accept_admin_risk = true
path_map = [{{ local = "/library", remote = "/library", mode = "rw" }}]

[[sources]]
kind = "suwayomi"
url = "{SUWAYOMI_URL}"
secret_file = "/tmp/suwayomi.secret"

[[shelves]]
name = "Manga"
max_age = "18+"
target = "kavita"
subpath = "manga"
"""


def install_by_commands(j: Journey) -> None:
    """target add, source add, shelf create: what the administrator types, in that order."""
    j.secrets()
    refused = j.shelfhold(
        "target", "add", "kavita", "--url", KAVITA_URL, "--user", "shelfhold",
        "--password-file", "/tmp/kavita.pw",
    )  # fmt: skip
    assert refused.code != 0 and "administrat" in refused.err.lower()  # stops on the warning
    assert j.kavita()["keys"] == []  # nothing was sent
    added = j.shelfhold(
        "target", "add", "kavita", "--url", KAVITA_URL, "--user", "shelfhold",
        "--password-file", "/tmp/kavita.pw", "--accept-admin-risk",
        "--path-map", '[{"local": "/library", "remote": "/library", "mode": "rw"}]',
    )  # fmt: skip
    assert added.code == 0, added.err
    source = j.shelfhold("source", "add", "suwayomi", "--url", SUWAYOMI_URL,
                         "--secret-file", "/tmp/suwayomi.secret")  # fmt: skip
    assert source.code == 0, source.err
    shelf = j.shelfhold("shelf", "create", "Manga", "--max-age", "18+", "--target", "kavita",
                        "--subpath", "manga")  # fmt: skip
    assert shelf.code == 0, shelf.err


@pytest.mark.e2e_timeout(120)
@pytest.mark.req("EF-INT-024")
def test_m1a_headless_config_apply_declarative_idempotent(journey: Journey) -> None:
    """`config apply -f shelfhold.toml` brings a new installation to its configuration: dry run
    first (nothing written), then applied, then idempotent."""
    j = journey
    j.secrets()
    j.put("/tmp/shelfhold.toml", SHELFHOLD_TOML)
    dry = j.shelfhold("config", "apply", "-f", "/tmp/shelfhold.toml", "--dry-run")
    assert dry.code == 0, dry.err
    assert j.kavita()["keys"] == []  # a dry run reaches no server
    applied = j.shelfhold("config", "apply", "-f", "/tmp/shelfhold.toml")
    assert applied.code == 0, applied.err
    keys = j.kavita()["keys"]
    assert isinstance(keys, list) and len(keys) == 1  # one named key, the admin's own key unread
    assert "POST /api/graphql" in j.suwayomi()["requests"]  # the authentication mode was read
    again = j.shelfhold("config", "apply", "-f", "/tmp/shelfhold.toml")
    assert again.code == 0, again.err
    assert len(j.kavita()["keys"]) == 1  # idempotent: no second key
    listing = j.shelfhold("config", "list").out
    assert "path_map.kavita" in listing and "source.check_mode = always_on" in listing
    assert j.kavita()["violations"] == [] and j.suwayomi()["violations"] == []
    assert PATH_MAP[0]["local"] in listing


@pytest.mark.e2e_timeout(420)
@pytest.mark.req("EF-INT-024", "EF-SRC-008", "EF-SRC-036")
def test_m1a_headless_install_to_first_chapter_in_kavita(journey: Journey) -> None:
    j = journey
    install_by_commands(j)
    j.discover_series()  # the series of the library are filed; what is visible now is not asked
    j.publish_upstream()  # a new chapter upstream, after the follow was set up
    # the follow runs at the next tick (at most a minute), then the export waits 30 s of calm
    # before the scan (EF-EXP-034): ask for the cycle now instead of waiting a quarter hour
    assert j.follow_until(lambda: bool(j.kavita()["series"]), 300), (
        "aucun job de suivi enregistré : Kavita n'a indexé aucune série"
    )
    files = j.library_files()
    exported = next(f for f in files if f.endswith("Ch.2.cbz"))
    # EF-EXP-006: the process runs under umask 077, the library does not: 0644 (SHELFHOLD_UMASK
    # defaults to 022), so a Kavita under another uid can read the file.
    mode = j.compose("exec", "-T", "shelfhold", "stat", "-c", "%a", exported).stdout.strip()
    assert mode == "644", (exported, mode)
    kavita = j.kavita()
    assert kavita["violations"] == [] and j.suwayomi()["violations"] == []
    assert not any(f.split("/")[-1].split(" - ")[-1][:1].isdigit() and "5002" in f for f in files)


@pytest.mark.e2e_timeout(300)
@pytest.mark.req("ENF-OPS-017")
def test_redeploy_recreates_container_without_loss(journey: Journey) -> None:
    """A redeploy is `up --force-recreate`: the container is new, the migration service
    ran again, SIGTERM was handled inside `stop_grace_period`, nothing was written outside the
    volumes, and everything the administrator set is still there."""
    j = journey
    install_by_commands(j)
    before_config = j.shelfhold("config", "list").out
    old = j.container_id()
    started = time.monotonic()
    j.compose("stop", "shelfhold")
    assert time.monotonic() - started < 30  # inside stop_grace_period, not killed
    state = subprocess.run(  # noqa: S603
        ["docker", "inspect", "-f", "{{.State.ExitCode}} {{.State.OOMKilled}}", old],  # noqa: S607
        capture_output=True, text=True, check=True,
    ).stdout.split()  # fmt: skip
    # SIGTERM handled, not SIGKILL: uvicorn finishes its shutdown, then re-raises the signal (143)
    assert state[0] in ("0", "143") and state[1] == "false"
    assert "Application shutdown complete" in j.compose("logs", "--no-color", "shelfhold").stdout
    diff = subprocess.run(  # noqa: S603
        ["docker", "diff", old],
        capture_output=True,
        text=True,
        check=True,  # noqa: S607
    ).stdout.split("\n")
    runtime = {"/usr", "/usr/sbin", "/usr/sbin/docker-init", "/library", "/data", "/store", "/tmp"}
    written = [line for line in diff if line and line.split(" ", 1)[1] not in runtime]
    assert written == []  # read_only: only the runtime's own init and mount points differ
    migrated = j.container_id("shelfhold-migrate")

    j.compose("up", "-d", "--wait", "--force-recreate", timeout=600)

    assert j.container_id() not in ("", old)
    assert j.container_id("shelfhold-migrate") != migrated  # the migration service ran again
    after_config = j.shelfhold("config", "list")
    assert after_config.out == before_config, (after_config.code, after_config.err)
    again = j.shelfhold("shelf", "create", "Manga", "--max-age", "18+", "--target", "kavita",
                        "--subpath", "manga")  # fmt: skip
    assert again.code == 0 and "Manga" in again.out  # the shelf exists: said so, not duplicated
    assert j.shelfhold("status").code == 0
    ready = j.compose("exec", "-T", "shelfhold", "python", "-c",
                      "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8080/readyz').read().decode())")  # fmt: skip
    assert '"ready"' in ready.stdout


def open_intents(j: Journey) -> int:
    """Intents still open in the volume (a read-only look at the database)."""
    done = j.compose(
        "exec", "-T", "shelfhold", "python", "-c",
        "import sqlite3; print(sqlite3.connect('file:/data/shelfhold.db?mode=ro', uri=True)"
        ".execute(\"select count(*) from fs_intent where state in ('pending','applying')\")"
        ".fetchone()[0])",
    )  # fmt: skip
    return int(done.stdout.strip())


@pytest.mark.e2e_timeout(420)
@pytest.mark.req("ENF-OPS-017")
def test_redeploy_during_running_export_finishes_without_open_intent(journey: Journey) -> None:
    """The container is recreated while a chapter is being received and exported: SIGTERM is
    handled inside `stop_grace_period`, no intent stays open after the restart, and the chapter
    arrives (the work that was cut is taken up again, not lost, not written twice)."""
    j = journey
    install_by_commands(j)
    j.discover_series()
    j.publish_upstream()
    old = j.container_id()
    assert j.shelfhold("jobs", "run", "follow.cycle").code == 0  # the download is asked for
    time.sleep(2)  # the second look of the service collects it: the work is under way
    assert j.shelfhold("jobs", "run", "follow.cycle").code == 0
    started = time.monotonic()
    j.compose("stop", "shelfhold")  # Komodo's "Redeploy", step one: SIGTERM, then wait
    assert time.monotonic() - started < 60  # inside stop_grace_period, never SIGKILL
    exit_code = subprocess.run(  # noqa: S603
        ["docker", "inspect", "-f", "{{.State.ExitCode}}", old],  # noqa: S607
        capture_output=True, text=True, check=True,
    ).stdout.strip()  # fmt: skip
    assert exit_code in ("0", "143")

    # Only the service is recreated (the simulated servers keep what was published upstream).
    j.compose("up", "-d", "--wait", "--no-deps", "--force-recreate", "shelfhold", timeout=600)

    assert wait_for(lambda: open_intents(j) == 0, 60), "an intent stayed open after the redeploy"
    assert j.follow_until(lambda: any(f.endswith("Ch.2.cbz") for f in j.library_files()), 300), (
        "le chapitre n'est pas arrivé après le redéploiement"
    )
    files = j.library_files()
    assert len([f for f in files if f.endswith("Ch.2.cbz")]) == 1  # written once
    assert not any("/." in f.removeprefix("/library") and ".shelfhold" not in f for f in files)
    assert open_intents(j) == 0
    assert j.kavita()["violations"] == [] and j.suwayomi()["violations"] == []


@pytest.mark.e2e_timeout(420)
@pytest.mark.req("EF-EXP-056", "EF-INT-024")
def test_detach_restores_suwayomi_updates_and_leaves_files(journey: Journey) -> None:
    """After a first chapter, `detach --restore-upstream` gives the series back to Suwayomi's own
    updater and leaves every exported file where and what it was."""
    j = journey
    install_by_commands(j)
    j.discover_series()
    j.publish_upstream()
    assert j.follow_until(lambda: any(f.endswith(".cbz") for f in j.library_files()), 300), (
        "aucun job de suivi enregistré : aucun chapitre exporté"
    )
    before = j.compose("exec", "-T", "shelfhold", "sh", "-c",
                       "cd /library && find . -path ./.shelfhold -prune -o -type f -exec sha256sum {} +").stdout  # fmt: skip
    detached = j.shelfhold("detach", "--restore-upstream")
    assert detached.code == 0, detached.err
    after = j.compose("exec", "-T", "shelfhold", "sh", "-c",
                      "cd /library && find . -path ./.shelfhold -prune -o -type f -exec sha256sum {} +").stdout  # fmt: skip
    assert after == before  # .shelfhold/ is excluded: the lease is rewritten by its heartbeat
    state = j.suwayomi()
    assert "violations" in state and state["violations"] == []


FOREIGN = "foreign-writer.cbz"


@pytest.mark.e2e_timeout(480)
@pytest.mark.req("EF-INT-023", "EF-EXP-043", "EF-INT-024")
def test_exits_work_after_target_add_on_the_running_server(journey: Journey) -> None:
    """`target add` is typed while the server runs: the roots it registers are read by the exits
    that follow (`series resume`, `target recheck`), not the ones known at start (L60)."""
    j = journey
    install_by_commands(j)  # the server was started before any root existed
    j.discover_series()
    j.publish_upstream()
    assert j.follow_until(lambda: any(f.endswith("Ch.2.cbz") for f in j.library_files()), 300)
    exported = next(f for f in j.library_files() if f.endswith("Ch.2.cbz"))
    folder = exported.rsplit("/", 1)[0]
    j.put(f"{folder}/{FOREIGN}", "not ours")  # another writer, in the folder Shelfhold manages
    assert j.shelfhold("jobs", "run", "reconcile.cycle").code == 0
    assert wait_for(lambda: "paused_foreign_writer" in j.waiting(), 90), j.shelfhold("status").out
    (item,) = j.waiting()["paused_foreign_writer"]
    assert str(item["command"]).startswith("shelfhold series resume ")
    resumed = j.shelfhold("series", "resume", str(item["subject"]))
    assert resumed.code == 0, resumed.err + resumed.out
    assert "OutsidePathMapError" not in resumed.err
    target = j.db_value("select id from export_target")
    rechecked = j.shelfhold("target", "recheck", target)
    assert rechecked.code == 0, rechecked.err + rechecked.out
    assert "OutsidePathMapError" not in rechecked.err


def exported_cbz(j: Journey) -> list[str]:
    """The files in the series folders of the shelf (the received ones wait under .shelfhold)."""
    return [f for f in j.library_files() if f.endswith(".cbz") and "/.shelfhold/" not in f]


@pytest.mark.e2e_timeout(480)
@pytest.mark.req("EF-EXP-032", "EF-INT-023")
def test_two_series_with_one_title_wait_for_a_name_then_export(journey: Journey) -> None:
    """The same title in `en` and in `fr`: nothing is written silently under one name. `status`
    names both Editions and the command; the name given, both series are exported (L60)."""
    j = journey
    install_by_commands(j)
    j.twin_titles()
    j.discover_series()
    j.publish_upstream()
    assert j.follow_until(lambda: "series_name_collision" in j.waiting(), 300), (
        j.shelfhold("status").out
    )
    items = j.waiting()["series_name_collision"]
    assert len(items) == 2
    assert exported_cbz(j) == []  # no silent skip, no overwrite (received files stay in .shelfhold)
    command = str(items[0]["command"])
    assert command.startswith("shelfhold edition set-series-name ")
    named = j.shelfhold("edition", "set-series-name", str(items[0]["subject"]), "Series Alpha (fr)")
    assert named.code == 0, named.err + named.out
    assert j.shelfhold("jobs", "run", "export.run").code == 0
    assert wait_for(
        lambda: len(exported_cbz(j)) >= 2
        and "series_name_collision" not in j.waiting(),
        120,
    ), j.shelfhold("status").out
    folders = {f.split("/")[3] for f in exported_cbz(j)}
    assert len(folders) == 2  # two series, two names


@pytest.mark.e2e_timeout(480)
@pytest.mark.req("EF-EXP-032", "EF-INT-023")
def test_name_taken_by_a_series_of_another_source_in_kavita_waits_for_a_name(
    journey: Journey,
) -> None:
    """Kavita already shows "Series Alpha", written by someone else: nothing is exported under
    that name, `status` lists the Edition, the exit refuses the same name and takes another."""
    j = journey
    install_by_commands(j)
    j.foreign_series("Series Alpha")
    j.discover_series()
    j.publish_upstream()
    assert j.follow_until(lambda: "series_name_collision" in j.waiting(), 300), (
        j.shelfhold("status").out
    )
    (item,) = j.waiting()["series_name_collision"]
    assert not any("Series Alpha/" in f for f in exported_cbz(j))
    taken = j.shelfhold("edition", "set-series-name", str(item["subject"]), "series  alpha")
    assert taken.code != 0  # the foreign series bears that name (normalized)
    named = j.shelfhold("edition", "set-series-name", str(item["subject"]), "Series Alpha (mine)")
    assert named.code == 0, named.err + named.out
    assert j.shelfhold("jobs", "run", "export.run").code == 0
    assert wait_for(
        lambda: any("Series Alpha (mine)/" in f for f in exported_cbz(j))
        and "series_name_collision" not in j.waiting(),
        120,
    ), j.shelfhold("status").out
