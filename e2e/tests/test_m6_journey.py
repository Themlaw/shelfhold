"""M6 on the image under test: the plugin, the repository and the devices a household uses.

The stack is the M5 one plus the M6 overlay (`e2e/m6.yaml`): the plugin repository and the
synthetic catalogue of the example plugin answer through the simulated sources, over TLS, under
names the stack gives an address that exists on the loopback of the namespace only. Every key is
drawn by the test that needs one, every file is made here, and `egress-watch` ends each journey
as empty as it began.

The journeys, one runner each (`e2e/shards.py`):

1. the example plugin of `docs/plugins/exemple`, vouched for by an index signed with a key made
   for the run, is installed from it, enabled by the reserved action, and runs in the companion;
2. an index of a lower version is refused, and so is a wheel that is not the one the index signed;
3. a plugin that breaks leaves the core answering and goes to the error state alone;
4. the SDK wheel, installed alone in a clean environment, passes the conformance kit;
5. a child profile cannot link a tracker;
6. a simulated Kobo initializes and syncs page by page, with the scope of its profile and the
   token that continues; no reading state is sent (that side is not delivered).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from journey import Journey, wait_for
from m6_support import (
    CASSETTE,
    EXAMPLE,
    INDEX_URL,
    REPO,
    SOURCES,
    dist_of,
    entry_for,
    example_entry,
    example_project,
    example_wheel,
    make_key,
    publish_catalogue,
    publish_index,
    publish_wheel,
    small_plugin_wheel,
)
from test_m3_journey import KID, make_profile
from test_m4_journey import device_token
from test_m5_journey import MEMBER

E2E = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(E2E / "fakes" / "m2"))
from books import book  # noqa: E402

KOBO = (E2E / "fakes" / "kobo" / "client.py").read_text(encoding="utf-8")
EXAMPLE_KEY, EXAMPLE_TARGET = example_entry()
EXAMPLE_DIST = str(example_project()["name"])
EXAMPLE_VERSION = str(example_project()["version"])

PROCESSES = r"""
import json, os
found = []
for pid in os.listdir('/proc'):
    if pid.isdigit():
        try:
            found.append(open(f'/proc/{pid}/cmdline', 'rb').read().replace(b'\0', b' ').decode())
        except OSError:
            pass
print(json.dumps(found))
"""


def processes(j: Journey, service: str) -> list[str]:
    """The command lines of the processes of a container of the stack."""
    done = j.compose("exec", "-T", service, "python", "-c", PROCESSES)
    return [str(line) for line in json.loads(done.stdout)]


def restart_core(j: Journey) -> None:
    """The core reads the grants once, at start-up: it is restarted, as an administrator would
    after enabling a plugin. The companion keeps running; the core connects to it again."""
    j.compose("restart", "shelfhold")
    assert wait_for(lambda: j.shelfhold("status").code == 0, 180), "the core did not come back"


def search(j: Journey, key: str, query: str) -> tuple[int, object]:
    [(code, body)] = j.api(("GET", f"/api/v1/sources/{key}/search?q={query}"))
    return code, body


def served(j: Journey) -> list[str]:
    """What the simulated repository was asked, `host METHOD path` by line."""
    return [str(line) for line in j.fake(SOURCES, "http://127.0.0.1/_fake/state")["seen"]]  # type: ignore[union-attr]


def audited(j: Journey, like: str) -> list[str]:
    return [str(row[0]) for row in j.sql(f"select action from audit where action like '{like}' order by id")]


def plugins_listed(j: Journey) -> dict[str, dict[str, object]]:
    [(code, listed)] = j.api(("GET", "/api/v1/plugins"))
    assert code == 200, listed
    return {str(item["name"]): item for item in listed["items"]}  # type: ignore[index]


def pin_repository(j: Journey, signer_public: str) -> None:
    done = j.shelfhold("plugin", "index", "add", INDEX_URL, "--pubkey", signer_public)
    assert done.code == 0, (done.out, done.err)


# -- (1) the example plugin, from a signed index, isolated -------------------------------------


@pytest.mark.e2e_timeout(780)
@pytest.mark.req("EF-SRC-016", "EF-SRC-017", "EF-SRC-020", "EF-USR-024")
def test_m6_journey_example_plugin_from_signed_index_isolated(journey_m6: Journey) -> None:
    """The administrator pins a repository by its address and its signer's public key, installs
    the example plugin from the index that signer made for the run (nothing runs yet), enables it
    (a reserved action), restarts the core, and the plugin answers: in the companion, not in the
    core. Everything it was given came from the stack's own names."""
    j = journey_m6
    j.create_admin()
    signer = make_key()
    wheel = example_wheel()
    publish_wheel(j, wheel)
    publish_index(j, signer, 1, [entry_for(EXAMPLE_KEY, EXAMPLE_VERSION, wheel)])
    publish_catalogue(j)

    pin_repository(j, signer.public_b64)
    listed = j.shelfhold("plugin", "index", "list")
    assert listed.code == 0 and INDEX_URL in listed.out and signer.key_id in listed.out, listed

    done = j.shelfhold("plugin", "install", EXAMPLE_KEY, "--index", INDEX_URL)
    assert done.code == 0, (done.out, done.err)
    assert any(f"GET /wheels/{wheel.name}" in line for line in served(j)), served(j)
    installed = j.sql(
        f"select plugin_id, version from plugin_install where plugin_id = '{EXAMPLE_KEY}'"
    )
    assert installed == [(EXAMPLE_KEY, EXAMPLE_VERSION)], installed
    # installed, never run: the plugin is disabled, in the isolated mode, and nobody started it
    before = plugins_listed(j)[EXAMPLE_DIST]
    assert before["status"] == "disabled" and before["mode"] == "isolated", before
    assert not [p for p in processes(j, "plugin-runner") if "plugin_exemple" in p]
    assert search(j, EXAMPLE_KEY, "libre")[0] == 404  # not a source of the instance yet

    enabled = j.shelfhold("plugin", "enable", EXAMPLE_DIST)
    assert enabled.code == 0, (enabled.out, enabled.err)
    after = plugins_listed(j)[EXAMPLE_DIST]
    assert after["status"] == "enabled" and after["mode"] == "isolated", after

    restart_core(j)
    assert wait_for(lambda: search(j, EXAMPLE_KEY, "libre")[0] == 200, 120), search(
        j, EXAMPLE_KEY, "libre"
    )
    code, found = search(j, EXAMPLE_KEY, "libre")
    titles = [item["title"] for item in found["items"]]  # type: ignore[index,call-overload]
    assert titles == ["Un livre libre"], found  # the title of the plugin's own catalogue

    # the plugin runs in the companion: its process is there, with the entry point it was given,
    # and the core never imported it
    runner = processes(j, "plugin-runner")
    assert any("plugin_side" in p and EXAMPLE_TARGET in p for p in runner), runner
    assert not [p for p in processes(j, "shelfhold") if "plugin_side" in p or "plugin_exemple" in p]
    # the catalogue came through the host's Fetcher, from the stack's own name
    assert any(line == "catalogue.exemple.invalid GET /catalogue.json" for line in served(j))
    # each step is written to the audit log
    steps = audited(j, "plugin.%")
    assert {"plugin.index_add", "plugin.install", "plugin.enable"} <= set(steps), steps
    assert j.egress() == []


# -- (2) a rolled-back index, a wheel that is not the signed one -------------------------------


@pytest.mark.e2e_timeout(780)
@pytest.mark.req("EF-SRC-017")
def test_m6_journey_index_rollback_refused(journey_m6: Journey) -> None:
    """The index moves forward only: a validly signed index of a lower version is refused, at the
    refresh and at an install, and the pin keeps the version it knew. A wheel whose sha256 is not
    the one the index signed is refused and nothing of it is installed. The same repository then
    installs the right wheel: the refusals were not a closed door."""
    j = journey_m6
    j.create_admin()
    signer = make_key()
    wheel = example_wheel()
    entry = [entry_for(EXAMPLE_KEY, EXAMPLE_VERSION, wheel)]
    publish_wheel(j, wheel)
    publish_index(j, signer, 5, entry)
    pin_repository(j, signer.public_b64)
    refreshed = j.shelfhold("plugin", "index", "refresh", INDEX_URL)
    assert refreshed.code == 0, (refreshed.out, refreshed.err)
    assert j.sql("select last_version from plugin_repo") == [(5,)]

    # (a) the replay: version 3, signed by the right key
    publish_index(j, signer, 3, entry)
    replay = j.shelfhold("plugin", "index", "refresh", INDEX_URL)
    assert replay.code != 0 and "rollback" in replay.err, (replay.out, replay.err)
    install = j.shelfhold("plugin", "install", EXAMPLE_KEY, "--index", INDEX_URL)
    assert install.code != 0 and "rollback" in install.err, (install.out, install.err)
    assert j.sql("select last_version from plugin_repo") == [(5,)]  # the floor did not move
    assert j.sql("select count(*) from plugin_install") == [(0,)]
    assert EXAMPLE_DIST not in plugins_listed(j)

    # (b) a wheel that is not the signed one: the index vouches for the real bytes, the
    # repository serves others
    publish_index(j, signer, 6, entry)
    publish_wheel(j, wheel, data=example_wheel(tamper=True).data)
    forged = j.shelfhold("plugin", "install", EXAMPLE_KEY, "--index", INDEX_URL)
    assert forged.code != 0 and "wheel_hash_mismatch" in forged.err, (forged.out, forged.err)
    assert j.sql("select count(*) from plugin_install") == [(0,)]
    assert EXAMPLE_DIST not in plugins_listed(j)
    leftovers = j.compose(
        "exec", "-T", "shelfhold", "sh", "-c", "ls /data/plugins/site-packages 2>/dev/null | wc -l"
    ).stdout.strip()
    assert leftovers == "0", leftovers  # nothing of the refused wheel stayed on the volume

    # (c) the refusals are written, and the right wheel still installs
    refused = audited(j, "plugin.%refused")
    assert "plugin.index_refresh_refused" in refused and "plugin.install_refused" in refused, refused
    publish_wheel(j, wheel)
    good = j.shelfhold("plugin", "install", EXAMPLE_KEY, "--index", INDEX_URL)
    assert good.code == 0, (good.out, good.err)
    assert j.sql("select count(*) from plugin_install") == [(1,)]
    assert j.egress() == []


# -- (3) a plugin that breaks ------------------------------------------------------------------


@pytest.mark.e2e_timeout(780)
@pytest.mark.req("EF-SRC-016")
def test_m6_journey_plugin_crash_host_survives(journey_m6: Journey) -> None:
    """Two plugins run in the companion. One of them exits in the middle of a call: the core goes
    on answering (liveness, the API, the other plugin, the companion), and the plugin that broke
    is out of the sources while the other is not."""
    j = journey_m6
    j.create_admin()
    for key in ("steady", "crashy"):
        wheel = small_plugin_wheel(key)
        j.put_bytes(f"/tmp/{wheel.name}", wheel.data, old=False)
        done = j.shelfhold("plugin", "install", f"/tmp/{wheel.name}", "--sha256", wheel.sha256)
        assert done.code == 0, (done.out, done.err)
        assert j.shelfhold("plugin", "enable", dist_of(key)).code == 0
    restart_core(j)
    for key in ("steady", "crashy"):
        assert wait_for(lambda k=key: search(j, k, "hello")[0] == 200, 120), search(j, key, "hello")
    assert search(j, "crashy", "hello")[1]["items"][0]["title"] == "echo:hello"  # type: ignore[index,call-overload]
    core = j.container_id()

    code, _ = search(j, "crashy", "crash")  # the plugin's process exits in the middle of the call
    assert code != 200

    # the core: alive and ready, the same container, the API answers
    assert j.api(("GET", "/healthz"), mode="anon")[0][0] == 200
    assert j.api(("GET", "/readyz"), mode="anon")[0][0] == 200
    assert j.container_id() == core
    assert j.shelfhold("status").code == 0
    # the plugin that broke is out of the sources, whatever it is asked again...
    gone_code, gone = search(j, "crashy", "hello")
    assert gone_code == 404 and "plugin.unknown" in json.dumps(gone), (gone_code, gone)
    # ...and the other one answers, in the same companion, still running
    assert search(j, "steady", "still")[1]["items"][0]["title"] == "echo:still"  # type: ignore[index,call-overload]
    runner = processes(j, "plugin-runner")
    assert any("e2e_steady" in p for p in runner), runner
    assert not any("e2e_crashy" in p for p in runner), runner  # its process is gone with it
    assert j.shelfhold("plugin-runner", "--healthcheck", service="plugin-runner").code == 0
    # both are still installed and enabled: it is the process that failed, not the grant
    listed = plugins_listed(j)
    assert listed[dist_of("crashy")]["status"] == "enabled", listed
    assert j.egress() == []


# -- (4) the SDK wheel, alone ---------------------------------------------------------------------

PROBE = r"""
import asyncio, importlib.util, json, sys
sys.path.insert(0, sys.argv[1])  # the example plugin's own sources: all that is added
host_importable = importlib.util.find_spec("shelfhold") is not None
import shelfhold_sdk
from shelfhold_sdk.testing import FakeFetcher, run_source_conformance
from pathlib import Path
from plugin_exemple.source import ExampleSource

fetcher = FakeFetcher.from_cassette(Path(sys.argv[2]))
report = asyncio.run(run_source_conformance(ExampleSource(), fetcher, reinstall=ExampleSource))
print(json.dumps({
    "host_importable": host_importable,
    "sdk_file": shelfhold_sdk.__file__,
    "ok": report.ok,
    "failures": [f"{c.name}: {c.detail}" for c in report.failures],
    "checks": len(report.checks),
    "unscripted": fetcher.unscripted,
}))
"""
KIT_TEST = (
    "def test_the_kit_fixtures_are_injected(offline_fetcher, check_conformance):\n"
    "    assert offline_fetcher is not None and callable(check_conformance)\n"
)


@pytest.mark.e2e_timeout(780)
@pytest.mark.req("EF-SRC-020")
def test_m6_journey_sdk_wheel_alone(journey_m6: Journey, tmp_path: Path) -> None:
    """A plugin author has the wheel of the SDK and nothing of the host: it is built from the
    repository, installed alone in a clean environment (no `shelfhold` package, pydantic its one
    dependency), and the conformance kit is green on the example plugin, as a library and as the
    pytest plugin it registers. The image carries the very same version of the SDK."""
    j = journey_m6
    out = tmp_path / "dist"
    built = subprocess.run(  # noqa: S603
        ["uv", "build", "--project", str(REPO / "sdk"), "--wheel", "--out-dir", str(out)],  # noqa: S607
        capture_output=True, text=True, cwd=REPO, check=False, timeout=300,
    )  # fmt: skip
    assert built.returncode == 0, built.stderr
    [wheel] = sorted(out.glob("shelfhold_sdk-*.whl"))
    version = wheel.name.split("-")[1]

    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True)  # noqa: S603
    python = str(venv / "bin" / "python")
    installed = subprocess.run(  # noqa: S603
        [python, "-m", "pip", "install", "--disable-pip-version-check", str(wheel), "pytest"],
        capture_output=True, text=True, check=False, timeout=600,
    )  # fmt: skip
    assert installed.returncode == 0, installed.stderr
    frozen = subprocess.run(  # noqa: S603
        [python, "-m", "pip", "freeze"], capture_output=True, text=True, check=True, timeout=120
    ).stdout
    assert f"shelfhold_sdk=={version}" in frozen.splitlines(), frozen
    assert not [line for line in frozen.splitlines() if line.lower().startswith("shelfhold==")]

    probe = tmp_path / "probe.py"
    probe.write_text(PROBE, encoding="utf-8")
    ran = subprocess.run(  # noqa: S603
        [python, "-I", str(probe), str(EXAMPLE / "src"), str(CASSETTE)],
        capture_output=True, text=True, check=False, timeout=300, cwd=tmp_path,
    )  # fmt: skip
    assert ran.returncode == 0, ran.stderr
    report = json.loads(ran.stdout.strip().splitlines()[-1])
    assert report["host_importable"] is False
    assert str(venv) in report["sdk_file"]  # the wheel's SDK, not the repository's
    assert report["ok"], report["failures"]
    assert report["checks"] > 0 and report["unscripted"] == []

    (tmp_path / "test_kit_plugin.py").write_text(KIT_TEST, encoding="utf-8")
    under_pytest = subprocess.run(  # noqa: S603
        [python, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(tmp_path / "test_kit_plugin.py")],
        capture_output=True, text=True, check=False, timeout=300, cwd=tmp_path,
    )  # fmt: skip
    assert under_pytest.returncode == 0, under_pytest.stdout + under_pytest.stderr

    # the image under test carries the SDK of that very wheel
    shipped = j.compose(
        "exec", "-T", "shelfhold", "python", "-c",
        "import importlib.metadata as m; print(m.version('shelfhold-sdk'))",
    ).stdout.strip()  # fmt: skip
    assert shipped == version, (shipped, version)
    assert j.egress() == []


# -- (5) a tracker, refused to a child ----------------------------------------------------------


@pytest.mark.e2e_timeout(780)
@pytest.mark.req("EF-INT-010", "EF-USR-024")
def test_m6_journey_tracker_refused_to_child(journey_m6: Journey) -> None:
    """A child profile cannot link a tracker: refused by name, whatever token she pastes, nothing
    stored, and she cannot give herself the allowance that would lift it. An adult is not refused
    for that reason (a tracker that is not installed answers otherwise), and none of it reaches
    the network."""
    j = journey_m6
    j.create_admin()
    kid_id = make_profile(j, KID, "child", 8)
    mum_id = make_profile(j, MEMBER, "member")
    token = "synthetic-remote-token-0001"
    link = f"/api/v1/profiles/{kid_id}/trackers/e2e-tracker"

    [(code, body)] = j.api(("PUT", link, {"token": token}), user=KID)
    assert code == 403 and body["code"] == "tracker_child_refused", (code, body)  # type: ignore[index]
    assert token not in json.dumps(body)
    # she cannot lift it herself: the allowance is the administrator's
    [(code, body)] = j.api(("PUT", link + "/allowance", {"allowed": True}), user=KID)
    assert code == 403, (code, body)
    [(code, body)] = j.api(("PUT", link, {"token": token}), user=KID)
    assert code == 403 and body["code"] == "tracker_child_refused", (code, body)  # type: ignore[index]
    # her list says nothing, and nothing was stored
    [(code, listed)] = j.api(("GET", f"/api/v1/profiles/{kid_id}/trackers"), user=KID)
    assert code == 200 and listed["items"] == [], (code, listed)  # type: ignore[index]
    assert j.sql("select count(*) from tracker_link") == [(0,)]
    # the profile of another is not hers to link either
    [(code, _)] = j.api(("PUT", f"/api/v1/profiles/{mum_id}/trackers/e2e-tracker", {"token": token}), user=KID)
    assert code in (403, 404)

    # an adult's attempt is judged on its own: the refusal is not the child's
    [(code, body)] = j.api(
        ("PUT", f"/api/v1/profiles/{mum_id}/trackers/e2e-tracker", {"token": token}), user=MEMBER
    )
    assert code != 200 and body.get("code") != "tracker_child_refused", (code, body)  # type: ignore[union-attr]
    assert j.sql("select count(*) from tracker_link") == [(0,)]
    assert j.egress() == []


# -- (6) the Kobo's sync -----------------------------------------------------------------------

BOOKS = 27  # a page holds 25 entitlements: two pages


def kobo_device(j: Journey, profile: str) -> tuple[str, str]:
    """The administrator pairs a Kobo for a profile: the secret on the command's output, the
    identifier in its message."""
    import re

    done = j.shelfhold(
        "admin", "device-tokens", "create", "--profile", profile, "--name", "liseuse", "--kind", "kobo"
    )
    assert done.code == 0, (done.out, done.err)
    found = re.search(r"identifiant (\S+?)\)", done.err)
    assert found is not None, done.err
    return found.group(1), done.out.strip()


def kobo_path_token(public_id: str, secret: str) -> str:
    import base64

    return base64.urlsafe_b64encode(f"{public_id}:{secret}".encode()).decode().rstrip("=")


def kobo(j: Journey, spec: dict[str, object]) -> dict[str, object]:
    """What the simulated Kobo of `fakes/kobo` saw."""
    j.put("/tmp/kobo_client.py", KOBO)
    done = j.compose("exec", "-T", "shelfhold", "python", "/tmp/kobo_client.py", json.dumps(spec))
    report: dict[str, object] = json.loads(done.stdout)
    return report


def pages_of(report: dict[str, object]) -> list[dict[str, object]]:
    return report["pages"]  # type: ignore[return-value]


def new_titles(report: dict[str, object]) -> list[str]:
    return [title for page in pages_of(report) for title in page["new"]]  # type: ignore[union-attr]


@pytest.mark.e2e_timeout(780)
@pytest.mark.req("EF-INT-012")
def test_m6_journey_kobo_sync(journey_m6: Journey) -> None:
    """The sync emulation, minimal surface: a Kobo initializes (the resources of this server and
    no other) and syncs page by page, the continuation token asking for each next page; a second
    sync with the token that ended the first is told only what changed. The scope is the profile
    of the token: a child's Kobo is given none of the books of an adult's, and no other credential
    opens the route. The device sends no reading state, and none is stored."""
    j = journey_m6
    j.create_admin()
    make_profile(j, MEMBER, "member")
    make_profile(j, KID, "child", 8)
    titles = [f"Le Livre numero {n:02d}" for n in range(BOOKS)]
    for title in titles:
        j.put_bytes(f"/import/{title}.epub", book(title))
    scan = j.shelfhold("import", "scan")
    assert scan.code == 0, (scan.out, scan.err)
    assert wait_for(lambda: j.sql("select count(*) from edition") == [(BOOKS,)], 600), j.sql(
        "select count(*) from edition"
    )

    mum = kobo_device(j, MEMBER[0])
    mum_path = kobo_path_token(*mum)

    # the device wakes up: initialization, activation, then the sync, page by page
    report = kobo(j, {"token": mum_path})
    init = report["init"]
    assert init["status"] == 200 and init["no_store"] is True, init  # type: ignore[index]
    hrefs = [r["Href"] for r in init["body"]["Resources"]]  # type: ignore[index]
    assert hrefs and all(h.startswith(f"/kobo/{mum_path}/") for h in hrefs), hrefs
    assert init["body"]["User"]["DisplayName"] == MEMBER[0]  # type: ignore[index]
    assert report["auth"] == 200
    pages = pages_of(report)
    assert [p["status"] for p in pages] == [200, 200], pages
    assert [p["continue"] for p in pages] == [True, False], pages  # two pages, told to continue once
    assert [len(p["new"]) for p in pages] == [25, BOOKS - 25], pages  # type: ignore[arg-type]
    assert sorted(new_titles(report)) == sorted(titles)  # all of them, none twice
    assert all(p["reading_states"] == 0 and p["deleted"] == 0 for p in pages)
    assert pages[0]["token"] != pages[1]["token"] and report["final_token"] == pages[1]["token"]

    # the same sync again from the token that ended it: nothing changed; then one more book
    again = kobo(j, {"token": mum_path, "sync_token": report["final_token"]})
    assert new_titles(again) == [] and not pages_of(again)[-1]["continue"], again
    extra = "Le Livre ajoute plus tard"
    j.put_bytes(f"/import/{extra}.epub", book(extra))
    assert j.shelfhold("import", "scan").code == 0
    assert wait_for(lambda: j.sql("select count(*) from edition") == [(BOOKS + 1,)], 300)
    delta = kobo(j, {"token": mum_path, "sync_token": report["final_token"]})
    assert new_titles(delta) == [extra], delta  # only what changed

    # the scope is the token's profile: a child's Kobo is given none of the adult's books
    kid = kobo_device(j, KID[0])
    kid_path = kobo_path_token(*kid)
    kid_report = kobo(j, {"token": kid_path})
    assert kid_report["init"]["status"] == 200  # type: ignore[index]
    assert new_titles(kid_report) == [], kid_report
    assert all(title not in json.dumps(kid_report) for title in [*titles, extra])

    # no other credential opens the route: nobody, a session, a token of another kind, a wrong one
    nobody = kobo(j, {"token": "a" * 46, "only": "init"})
    assert nobody["init"]["status"] == 401, nobody  # type: ignore[index]
    [(code, _)] = j.api(("GET", f"/kobo/{mum_path}/v1/library/sync"), user=MEMBER)
    assert code == 401
    reader = device_token(j, MEMBER)  # a token of the device screen: not of kind `kobo`
    other_kind = kobo(j, {"token": kobo_path_token(*reader), "only": "init"})
    assert other_kind["init"]["status"] == 401, other_kind  # type: ignore[index]

    # the device only reads: no reading state came in, none is stored, no export, no setting
    assert j.sql("select count(*) from reading_position") == [(0,)]
    assert j.egress() == []
