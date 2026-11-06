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


@pytest.mark.req("EF-INT-024", "EF-SRC-008", "EF-SRC-036")
def test_m1a_headless_install_to_first_chapter_in_kavita(journey: Journey) -> None:
    j = journey
    install_by_commands(j)
    j.publish_upstream()  # a new chapter upstream, after the follow was set up
    assert wait_for(lambda: bool(j.kavita()["series"]), 25), (
        "aucun job de suivi enregistré : Kavita n'a indexé aucune série"
    )
    files = j.library_files()
    assert any(f.endswith("Ch.2.cbz") for f in files), files
    kavita = j.kavita()
    assert kavita["violations"] == [] and j.suwayomi()["violations"] == []
    assert not any(f.split("/")[-1].split(" - ")[-1][:1].isdigit() and "5002" in f for f in files)


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
    assert j.shelfhold("config", "list").out == before_config
    again = j.shelfhold("shelf", "create", "Manga", "--max-age", "18+", "--target", "kavita",
                        "--subpath", "manga")  # fmt: skip
    assert again.code == 0 and "Manga" in again.out  # the shelf exists: said so, not duplicated
    assert j.shelfhold("status").code == 0
    ready = j.compose("exec", "-T", "shelfhold", "python", "-c",
                      "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8080/readyz').read().decode())")  # fmt: skip
    assert '"ready"' in ready.stdout


@pytest.mark.req("EF-EXP-056", "EF-INT-024")
def test_detach_restores_suwayomi_updates_and_leaves_files(journey: Journey) -> None:
    """After a first chapter, `detach --restore-upstream` gives the series back to Suwayomi's own
    updater and leaves every exported file where and what it was."""
    j = journey
    install_by_commands(j)
    j.publish_upstream()
    assert wait_for(lambda: bool(j.library_files()), 25), (
        "aucun job de suivi enregistré : aucun chapitre exporté"
    )
    before = j.compose("exec", "-T", "shelfhold", "sh", "-c",
                       "cd /library && find . -type f -exec sha256sum {} +").stdout  # fmt: skip
    detached = j.shelfhold("detach", "--restore-upstream")
    assert detached.code == 0, detached.err
    after = j.compose("exec", "-T", "shelfhold", "sh", "-c",
                      "cd /library && find . -type f -exec sha256sum {} +").stdout  # fmt: skip
    assert after == before
    state = j.suwayomi()
    assert "violations" in state and state["violations"] == []
