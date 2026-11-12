"""M3 L39: the first-run wizard, part 1 (EF-UX-001 screens 1 to 3, EF-UX-002 UI part).

The routes are the real ones of `create_app()`, at their real place. Behind them: a real
`Composition` whose only reachable server is the simulated Kavita, and the real services.

Every "never" of the lot is a test that tries to break it: the Kavita credentials are exchanged
for a dedicated key and are never stored, logged, answered, cached nor put in the diagnostic
bundle; the wizard routes are refused to a member, a child, a key and a stale session; the probe
file is written by fsops and removed whatever Kavita did.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.export.targets.kavita import probe_read
from shelfhold.headless.admin import KEY_DAYS, KEY_NAME
from shelfhold.infrastructure.logging import configure_logging
from shelfhold.infrastructure.schema import export
from shelfhold.ops.wiring import open_ops
from tests.auth_env import AuthEnv, auth_env, db_dump, run_cli
from tests.diagnostic_env import Traps, leaks
from tests.fakes.kavita import API_KEY
from tests.headless_env import KAVITA_KEY, KAVITA_PASSWORD, KAVITA_URL, headless
from tests.integration.test_auth_keys_reserved import _bearer, _create_key
from tests.l41_env import Instance, instance
from tests.library_env import cbz_bytes
from tests.visibility_env import add_profile

SCREENS = Path(__file__).resolve().parents[3] / "frontend/src/routes/assistant"
LOCAL_SUB, REMOTE_FOLDER = "Kavita", "/manga/Kavita"
PROBE_NAME = ".shelfhold-probe-0123456789abcdef.epub"
EXPECTED_HINT = f"services:\n  kavita:\n    volumes:\n      - <host path>:{REMOTE_FOLDER}:ro\n"
PROBE_FILES = re.compile(r"\.shelfhold-probe-[0-9a-f]{16}(\.epub)?")

WIZARD_ROUTES = (
    ("GET", "/api/v1/setup/state", None),
    ("POST", "/api/v1/setup/probe", {"destination_id": "d"}),
    ("POST", "/api/v1/setup/adoption/preview", {"destination_id": "d"}),
    ("POST", "/api/v1/setup/adoption/confirm", {"destination_id": "d", "fingerprint": "f"}),
    ("POST", "/api/v1/setup/library", {"destination_id": "d", "name": "Livres"}),
)


# -- the screens -------------------------------------------------------------------------------


@pytest.mark.req("EF-UX-001")
def test_wizard_max_5_screens() -> None:
    """The wizard has at most 5 screens in all; this part delivers the first three."""
    screens = sorted(
        p.name for p in SCREENS.iterdir() if p.is_dir() and (p / "+page.svelte").exists()
    )
    assert {"compte", "kavita", "dossier"} <= set(screens)
    assert len(screens) <= 5


@pytest.mark.req("EF-UX-001")
def test_kavita_screen_warns_that_an_administrator_account_is_required() -> None:
    """The warning is a text of the catalog, shown before the credentials are typed."""
    page = (SCREENS / "kavita" / "+page.svelte").read_text("utf-8")
    catalog = json.loads((SCREENS.parents[1] / "lib" / "i18n" / "fr.json").read_text("utf-8"))
    assert "wizard.kavita.adminWarning" in page
    assert "administrateur" in catalog["wizard.kavita.adminWarning"].lower()


# -- the world ---------------------------------------------------------------------------------


@dataclass
class World:
    inst: Instance
    api: AuthEnv
    destination_id: str

    @property
    def folder(self) -> Path:
        return self.inst.library / LOCAL_SUB

    def probe_files(self) -> list[str]:
        return sorted(p.name for p in self.folder.rglob("*") if PROBE_FILES.fullmatch(p.name))

    def tree(self) -> list[str]:
        return sorted(str(p.relative_to(self.folder)) for p in self.folder.rglob("*"))

    def dest(self) -> dict[str, Any]:
        with self.inst.engine.connect() as connection:
            row = connection.execute(sa.select(export.shelf_destination)).mappings().one()
            linked = connection.execute(
                sa.select(sa.func.count()).select_from(export.shelf_destination_remote_library)
            ).scalar_one()
        return {"adopted": bool(row["adopted"]), "links": linked}

    def post(self, path: str, body: dict[str, Any] | None = None, **extra: Any) -> Any:
        return self.api.post(path, json={"destination_id": self.destination_id, **(body or {})})


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path) -> Iterator[World]:
    monkeypatch.setattr(probe_read, "PAUSE_S", 0.0)  # Kavita is a simulation: no need to wait
    with instance(settings, tmp_path) as inst:
        inst.configure()
        ops = open_ops(settings, engine=inst.engine, composition=inst.comp)
        with auth_env(monkeypatch, settings, headless_admin=inst.admin, ops_services=ops) as api:
            assert api.login().status_code == 200
            with inst.engine.connect() as connection:
                dest = connection.execute(sa.select(export.shelf_destination.c.id)).scalar_one()
            yield World(inst, api, str(dest))


def _fix_probe_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """The probe draws its name at random: the simulated Kavita must know the one it will see."""
    from shelfhold.infrastructure.fsops import emitters, probe

    def named(*, epub: bool = False) -> str:
        return PROBE_NAME if epub else emitters.new_probe_name()

    monkeypatch.setattr(probe, "new_probe_name", named)


def _kavita_sees_probe(world: World) -> None:
    world.inst.kavita.index_after_scans(
        "probe", REMOTE_FOLDER, [f"{REMOTE_FOLDER}/{PROBE_NAME}"], library_id=7
    )


# -- the probe, read by Kavita -----------------------------------------------------------------


@pytest.mark.req("EF-UX-002")
def test_mount_probe_read_by_kavita_and_yaml_hint_on_failure(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kavita reads the probe: the folder is right. It does not: the exact compose block."""
    _fix_probe_name(monkeypatch)
    _kavita_sees_probe(world)
    ok = world.post("/api/v1/setup/probe")
    assert ok.status_code == 200, ok.text
    assert ok.json()["ok"] is True and ok.json()["kavita_read"] is True
    assert ok.json()["yaml_hint"] is None
    assert world.inst.kavita.scans == [REMOTE_FOLDER]

    world.inst.kavita.never_indexed.add(REMOTE_FOLDER)  # Kavita does not see the folder
    world.inst.kavita.index_after_scans(
        "probe-2", REMOTE_FOLDER, [f"{REMOTE_FOLDER}/{PROBE_NAME}"], library_id=7
    )
    refused = world.post("/api/v1/setup/probe")
    assert refused.status_code == 200
    assert refused.json()["ok"] is False and refused.json()["kavita_read"] is False
    assert refused.json()["yaml_hint"] == EXPECTED_HINT  # exact, not a paraphrase
    assert world.probe_files() == []


@pytest.mark.req("EF-UX-002")
def test_probe_is_a_valid_epub_written_before_kavita_is_asked_and_named_as_documented(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At the time Kavita scans, the file is in the folder, named `.shelfhold-probe-<id>.epub`,
    and is a real EPUB (`mimetype` first, stored)."""
    seen: list[tuple[list[str], bytes]] = []
    kavita = world.inst.kavita
    scan = type(kavita)._scan_folder

    def spying(self: Any, recorded: Any) -> Any:
        names = [p.name for p in world.folder.iterdir() if PROBE_FILES.fullmatch(p.name)]
        data = (world.folder / names[0]).read_bytes() if names else b""
        seen.append((names, data))
        return scan(self, recorded)

    monkeypatch.setattr(type(kavita), "_scan_folder", spying)
    assert world.post("/api/v1/setup/probe").status_code == 200
    names, data = seen[0]
    assert len(names) == 1 and names[0].endswith(".epub")
    with zipfile.ZipFile(io.BytesIO(data)) as book:
        first = book.infolist()[0]
        assert first.filename == "mimetype" and first.compress_type == zipfile.ZIP_STORED
        assert book.read("mimetype") == b"application/epub+zip"
        assert book.testzip() is None
    assert world.probe_files() == []


@pytest.mark.req("EF-UX-002")
def test_probe_file_is_removed_when_kavita_cannot_be_asked(world: World) -> None:
    """Kavita asleep: the answer is a refusal with its code, and nothing is left in the folder."""
    world.inst.kavita.asleep = True
    answer = world.post("/api/v1/setup/probe")
    assert answer.status_code >= 400, answer.text
    assert world.probe_files() == []
    assert world.tree() == []


@pytest.mark.req("EF-UX-002")
def test_probe_removes_its_file_whatever_the_library_check_raises(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unexpected failure after the write is still cleaned up (the `finally`, not the happy
    path): the file is not left for Kavita to index."""

    async def boom(*_a: Any, **_k: Any) -> bool:
        raise RuntimeError("unexpected")

    monkeypatch.setattr("shelfhold.ops.wizard.kavita_reads", boom)
    with pytest.raises(RuntimeError):
        world.post("/api/v1/setup/probe")
    assert world.probe_files() == []


@pytest.mark.req("EF-UX-002")
def test_probe_without_a_kavita_library_says_so_and_is_not_a_failure(world: World) -> None:
    world.inst.kavita.libraries.clear()
    answer = world.post("/api/v1/setup/probe")
    assert answer.json()["ok"] is True and answer.json()["kavita_read"] == "no_library"
    assert world.probe_files() == []


@pytest.mark.req("EF-UX-002")
def test_probe_of_an_unknown_destination_touches_nothing(world: World) -> None:
    before = world.tree()
    answer = world.api.post("/api/v1/setup/probe", json={"destination_id": "nope"})
    assert answer.status_code == 404
    assert world.tree() == before and world.inst.kavita.scans == []


# -- adoption ----------------------------------------------------------------------------------


def _existing_series(world: World, name: str = "Dr Stone") -> None:
    folder = world.folder / name
    folder.mkdir(parents=True)
    file = f"{name} - Ch.1.cbz"
    (folder / file).write_bytes(cbz_bytes((("1.png", b"\x89PNG\r\n\x1a\n"),)))
    world.inst.kavita.add_series(
        name, f"{REMOTE_FOLDER}/{name}", [f"{REMOTE_FOLDER}/{name}/{file}"], library_id=7
    )


@pytest.mark.req("EF-UX-001", "EF-EXP-055")
def test_wizard_adoption_links_library(world: World) -> None:
    """Preview writes nothing; confirming on its fingerprint adopts the files and links the
    library, and the state the screens read says so."""
    _existing_series(world)
    before = world.tree()
    seen = world.post("/api/v1/setup/adoption/preview")
    assert seen.status_code == 200, seen.text
    assert seen.json()["series"] == 1 and seen.json()["files"] == 1
    assert world.dest() == {"adopted": False, "links": 0} and world.tree() == before

    done = world.post("/api/v1/setup/adoption/confirm", {"fingerprint": seen.json()["fingerprint"]})
    assert done.status_code == 200, done.text
    assert done.json()["adopted_files"] == 1 and done.json()["linked"] is True
    assert world.dest() == {"adopted": True, "links": 1}
    state = world.api.client.get("/api/v1/setup/state").json()
    assert state["destinations"] == [
        {"id": world.destination_id, "target_id": state["targets"][0]["id"], "adopted": True,
         "linked": True}
    ]  # fmt: skip


@pytest.mark.req("EF-UX-001", "EF-EXP-055")
def test_adoption_goes_ahead_only_on_what_the_preview_showed(world: World) -> None:
    """A fingerprint the preview did not give (or a library that changed since) adopts nothing."""
    _existing_series(world)
    seen = world.post("/api/v1/setup/adoption/preview").json()
    _existing_series(world, "Another Series")  # the library moved after the admin looked
    stale = world.post("/api/v1/setup/adoption/confirm", {"fingerprint": seen["fingerprint"]})
    assert stale.status_code == 409 and stale.json()["code"] == "ops.preview_stale"
    forged = world.post("/api/v1/setup/adoption/confirm", {"fingerprint": "0" * 64})
    assert forged.status_code == 409
    assert world.dest() == {"adopted": False, "links": 0}


@pytest.mark.req("EF-UX-001", "EF-EXP-051")
def test_adoption_preview_carries_the_verdict_on_the_download_folder(world: World) -> None:
    _existing_series(world)
    under = world.post(
        "/api/v1/setup/adoption/preview", {"download_dir": str(world.inst.library / "dl")}
    ).json()
    assert under["download_dir"]["kind"] == "under_library"
    assert under["download_dir"]["copy_needed"] is False
    outside = world.post(
        "/api/v1/setup/adoption/preview", {"download_dir": "/elsewhere/downloads"}
    ).json()
    assert outside["download_dir"]["kind"] == "outside_library"
    assert outside["download_dir"]["copy_needed"] is True
    assert world.post("/api/v1/setup/adoption/preview").json()["download_dir"] is None
    assert world.dest() == {"adopted": False, "links": 0}  # a verdict changes nothing


@pytest.mark.req("EF-UX-001", "EF-EXP-059")
def test_adoption_preview_lists_the_physical_roots(world: World) -> None:
    _existing_series(world)
    roots = world.post("/api/v1/setup/adoption/preview").json()["roots"]
    assert roots and all({"path", "present"} == set(r) for r in roots)
    assert all(r["present"] for r in roots)


@pytest.mark.req("EF-UX-001")
def test_new_library_is_made_over_the_folder_and_linked(world: World) -> None:
    world.inst.kavita.libraries.clear()
    made = world.post("/api/v1/setup/library", {"name": "Mangas"})
    assert made.status_code == 201, made.text
    assert made.json()["linked"] is True
    libraries = world.inst.kavita.libraries
    assert [lib["folders"] for lib in libraries] == [[REMOTE_FOLDER]]
    assert world.dest()["links"] == 1


@pytest.mark.req("EF-UX-001")
def test_new_library_with_an_empty_or_huge_name_is_refused_before_kavita(world: World) -> None:
    for name in ("", "x" * 129):
        assert world.post("/api/v1/setup/library", {"name": name}).status_code == 422
    assert world.inst.kavita.requests == [] or all(
        "library/create" not in r.path for r in world.inst.kavita.requests
    )


# -- who may use the wizard --------------------------------------------------------------------


def _call(client: Any, method: str, path: str, body: Any, headers: dict[str, str] | None = None):
    kwargs: dict[str, Any] = {"headers": headers or {}}
    if body is not None:
        kwargs["json"] = body
    return client.request(method, path, **kwargs)


def _csrf(world: World, client: Any) -> dict[str, str]:
    return {"X-CSRF-Token": world.api.csrf(client)}


@pytest.mark.req("EF-UX-001", "EF-USR-024")
@pytest.mark.parametrize(("method", "path", "body"), WIZARD_ROUTES)
def test_wizard_routes_are_refused_to_a_member_and_to_a_child(
    world: World, method: str, path: str, body: Any
) -> None:
    effects_before = (world.tree(), world.dest(), world.inst.kavita.scans[:])
    for role, ip in (("member", "10.0.0.5"), ("child", "10.0.0.6")):
        client = add_profile(
            world.api, role, role, ip, **({"max_age_rating": 10} if role == "child" else {})
        )
        answer = _call(client, method, path, body, _csrf(world, client))
        assert answer.status_code == 403, (role, answer.text)
        assert answer.json()["code"] == "forbidden"
    assert (world.tree(), world.dest(), world.inst.kavita.scans) == effects_before


@pytest.mark.req("EF-UX-001", "EF-USR-024")
@pytest.mark.parametrize(("method", "path", "body"), WIZARD_ROUTES)
def test_wizard_routes_are_refused_to_an_api_key_even_an_admin_one(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    method: str,
    path: str,
    body: Any,
) -> None:
    key = _create_key(monkeypatch, capsys, world.api.settings)
    answer = _call(world.api.other_client("10.0.0.9"), method, path, body, _bearer(key))
    assert answer.status_code in {401, 403}, answer.text
    assert answer.json()["code"] in {"credential_not_allowed", "forbidden"}
    assert world.inst.kavita.scans == [] and world.probe_files() == []


@pytest.mark.req("EF-UX-001", "EF-USR-024")
@pytest.mark.parametrize(("method", "path", "body"), WIZARD_ROUTES)
def test_wizard_routes_need_a_fresh_session_and_a_session(
    world: World, method: str, path: str, body: Any
) -> None:
    anonymous = _call(world.api.other_client("10.0.0.8"), method, path, body)
    assert anonymous.status_code == 401
    world.api.clock.advance(timedelta(minutes=20))  # the password was typed 20 minutes ago
    stale = _call(world.api.client, method, path, body, _csrf(world, world.api.client))
    assert stale.status_code == 401 and stale.json()["code"] == "reauth_required"
    assert world.inst.kavita.scans == [] and world.probe_files() == []


@pytest.mark.req("EF-UX-001", "EF-USR-024")
def test_wizard_writes_leave_a_line_in_the_audit(world: World) -> None:
    _existing_series(world)
    seen = world.post("/api/v1/setup/adoption/preview").json()
    world.post("/api/v1/setup/adoption/confirm", {"fingerprint": seen["fingerprint"]})
    with world.inst.engine.connect() as connection:
        actions = connection.execute(sa.text("SELECT action FROM audit")).scalars().all()
    assert "admin_risk.accept" in actions


# -- the Kavita credentials: exchanged, never kept ---------------------------------------------
#
# The wizard connects Kavita through `POST /api/v1/targets`. Each test below tries one way for the
# password to survive the exchange.


def _target(api: AuthEnv, **fields: Any) -> Any:
    body = {
        "kind": "kavita", "name": "kavita", "url": KAVITA_URL, "username": "kavita-admin",
        "password": KAVITA_PASSWORD, "accept_admin_risk": True, **fields,
    }  # fmt: skip
    return api.post("/api/v1/targets", json={k: v for k, v in body.items() if v is not None})


def _connect(api: AuthEnv) -> Any:
    return api.post(
        "/api/v1/targets",
        json={"kind": "kavita", "name": "kavita", "url": KAVITA_URL, "username": "kavita-admin",
              "password": KAVITA_PASSWORD, "accept_admin_risk": True},
        headers={"Idempotency-Key": "wizard-connect-1"},
    )  # fmt: skip


@pytest.fixture
def connected(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, capsys: pytest.CaptureFixture[str]
):
    with (
        headless(settings) as env,
        auth_env(monkeypatch, settings, headless_admin=env.admin) as api,
    ):
        assert api.login().status_code == 200
        configure_logging(level="DEBUG", log_format="json")  # type: ignore[arg-type]
        capsys.readouterr()
        yield env, api, _connect(api)


@pytest.mark.req("EF-UX-001")
def test_kavita_credentials_exchanged_for_dedicated_key_not_stored(connected: Any) -> None:
    """The credentials go to Kavita once, for a key named and expiring; only that key is kept."""
    env, api, made = connected
    assert made.status_code == 201, made.text
    [call] = env.kavita.calls
    assert call["password"] == KAVITA_PASSWORD and call["username"] == "kavita-admin"
    assert call["key_name"] == KEY_NAME and 0 < call["expires_days"] <= KEY_DAYS <= 365
    with env.engine.connect() as connection:
        target = connection.execute(sa.select(export.export_target)).mappings().one()
    assert target["config"]["key_name"] == KEY_NAME and target["config"]["key_expires_at"]
    assert "password" not in json.dumps(target["config"]).lower()
    dump = db_dump(api.settings)
    assert KAVITA_PASSWORD not in dump  # not in any table, column, or journal
    assert KAVITA_KEY not in dump  # the key is sealed, not in clear
    assert not any(
        "password" in str(c).lower()
        for t in export.metadata.tables.values()
        for c in t.columns
        if t.name == "export_target"
    )


@pytest.mark.req("EF-UX-001", "ENF-SEC-005")
def test_kavita_credentials_are_never_logged(
    connected: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    _env, api, made = connected
    assert made.status_code == 201
    api.client.get("/api/v1/setup/state")
    refused = _target(api, name="k2", accept_admin_risk=False)
    assert refused.status_code == 409  # no accepted risk: stopped before anything left
    out = capsys.readouterr()
    text = out.out + out.err
    assert text  # debug level: something was said
    for secret in (KAVITA_PASSWORD, KAVITA_KEY, API_KEY):
        assert secret not in text


@pytest.mark.req("EF-UX-001", "ENF-SEC-005")
def test_kavita_credentials_are_never_sent_back(connected: Any) -> None:
    """Not in the answer to the connection, not in the next reads, not in a validation error."""
    _env, api, made = connected
    seen = [made.text, str(made.headers)]
    for path in ("/api/v1/setup/state", "/api/v1/config", "/api/v1/status"):
        answer = api.client.get(path)
        seen.append(answer.text + str(answer.headers))
    invalid = _target(api, name="k3", username=None)  # no user: a 422
    assert invalid.status_code == 422
    seen.append(invalid.text)
    wrong_kind = _target(api, name="k4", kind="other")
    seen.append(wrong_kind.text)
    blob = "\n".join(seen)
    for secret in (KAVITA_PASSWORD, KAVITA_KEY, API_KEY):
        assert secret not in blob


@pytest.mark.req("EF-UX-001", "ENF-SEC-005")
def test_kavita_credentials_are_never_cached_for_a_replay(connected: Any) -> None:
    """The idempotency store keeps answers to replay: not this one (the action is reserved), and
    a replay with the same key runs the guard again instead of being served from a store."""
    _env, api, made = connected
    assert made.status_code == 201 and "Idempotent-Replayed" not in made.headers
    assert KAVITA_PASSWORD not in db_dump(api.settings)
    again = _connect(api)  # the very same key and body
    assert "Idempotent-Replayed" not in again.headers
    api.clock.advance(timedelta(minutes=20))
    stale = _connect(api)  # a stale session gets no replay of the earlier success
    assert stale.status_code == 401 and stale.json()["code"] == "reauth_required"


@pytest.mark.req("EF-UX-001", "ENF-LEG-009")
def test_kavita_credentials_are_not_in_the_diagnostic_bundle(
    connected: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _env, api, made = connected
    assert made.status_code == 201
    out = tmp_path / "out" / "bundle.zip"
    out.parent.mkdir()
    assert run_cli(monkeypatch, api.settings, "diagnostic", "bundle", "--out", str(out)) == 0
    traps = Traps(
        values={"kavita_password": KAVITA_PASSWORD, "kavita_key": KAVITA_KEY, "api_key": API_KEY}
    )
    assert leaks(out.read_bytes(), traps) == []


@pytest.mark.req("EF-UX-001")
def test_the_key_expiry_is_what_the_screen_may_show_never_the_key(connected: Any) -> None:
    _env, api, _made = connected
    [target] = api.client.get("/api/v1/setup/state").json()["targets"]
    assert set(target) == {"id", "name", "key_expires_at"}
    assert target["key_expires_at"]
