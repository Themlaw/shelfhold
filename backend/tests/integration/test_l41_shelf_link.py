"""L41: the first registration of the roots, and `shelf create` linking a Kavita library with the
audit first (EF-INT-024, EF-EXP-002, EF-EXP-055)."""

import asyncio

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.export.application.root_bootstrap import RootPolicy, register_roots
from shelfhold.export.domain.bindings import KomgaPrimaryUnsupportedError
from shelfhold.export.domain.path_map import PathMode
from shelfhold.export.targets.kavita import AdminKey, KavitaClient
from shelfhold.export.targets.kavita.adopt import AdoptionRefused
from shelfhold.export.targets.kavita.audit import AccessAuditor
from shelfhold.export.targets.kavita.linking import ShelfLinker
from shelfhold.headless.adapters import HttpKomgaProbe
from shelfhold.headless.errors import ShelfExposedToChildren
from shelfhold.headless.wiring import _ShelfLinker
from shelfhold.infrastructure.schema import export, platform
from shelfhold.infrastructure.secrets.diagnostics import boot_check
from tests.export_env import export_env
from tests.fakes.kavita import API_KEY, BASE_URL, FakeKavita, build_fetcher
from tests.fakes.kavita.server import member
from tests.fakes.komga import (
    API_KEY as KOMGA_KEY,
)
from tests.fakes.komga import (
    BASE_URL as KOMGA_URL,
)
from tests.fakes.komga import (
    FakeKomga,
)
from tests.fakes.komga import (
    build_fetcher as komga_fetcher,
)
from tests.headless_env import headless
from tests.kavita_env import RecordingEvents

PASSWORD = "synthetic"  # noqa: S105
pytestmark = pytest.mark.req("EF-INT-024", "EF-EXP-002", "EF-EXP-055")


def test_target_add_and_shelf_create_register_the_instance_and_the_roots(
    settings: Settings, tmp_path
) -> None:
    root = tmp_path / "library" / "manga"
    root.mkdir(parents=True)
    ro = tmp_path / "upstream"
    ro.mkdir()
    with headless(
        settings, roots=RootPolicy(tmp_path / "library", settings.data_dir, settings.store_dir)
    ) as env:
        env.admin.target_add_kavita(
            name="k", url="http://kavita.internal:5000", username="u", password=PASSWORD,
            accept_admin_risk=True,
            path_map=[
                {"local": str(root), "remote": "/manga", "mode": "rw"},
                {"local": str(ro), "remote": "/ro", "mode": "adopt_ro"},
            ],
        )  # fmt: skip
        env.admin.shelf_create(name="Adultes", max_age="18+", target="k", subpath="Kavita")
        with env.engine.connect() as c:
            instances = c.execute(sa.select(platform.instance.c.instance_id)).all()
            roots = {r.path_entry: r.mode for r in c.execute(sa.select(platform.physical_root))}
        assert len(instances) == 1
        assert roots == {str(root): "rw", str(ro): "adopt_ro", str(settings.store_dir): "rw"}
        assert (root / ".shelfhold-mount-id").is_file()  # the writable root is stamped
        assert not (ro / ".shelfhold-mount-id").exists()  # a read-only root never is
        again = register_roots(env.engine, [(str(root), PathMode.RW)])
        assert again == []  # idempotent


def _client(fake: FakeKavita, clock) -> KavitaClient:  # type: ignore[no-untyped-def]
    return KavitaClient(
        build_fetcher(fake), BASE_URL, AdminKey(API_KEY), events=RecordingEvents(), clock=clock
    )


def _link(env, fake: FakeKavita) -> str:  # type: ignore[no-untyped-def]
    linker = ShelfLinker(
        env.engine,
        env.clock,
        AccessAuditor(env.engine, env.clock, RecordingEvents()),
    )
    return asyncio.run(
        linker.link(_client(fake, env.clock), env.destination_id, library_name="Adultes")
    )


def test_a_library_covering_the_folder_is_linked_not_created() -> None:
    with export_env() as env:
        with env.engine.begin() as c:
            c.execute(sa.delete(export.shelf_destination_remote_library))
            c.execute(sa.delete(export.remote_library))
        fake = FakeKavita()  # library 7 watches /manga, the parent of the shelf's folder
        ref = _link(env, fake)
        assert ref == "7"
        assert not [r for r in fake.requests if "Library/create" in r.path]
        with env.engine.connect() as c:
            assert c.execute(sa.select(export.remote_library.c.remote_ref)).scalar_one() == "7"


def test_without_a_library_one_is_created_over_the_folder_then_audited() -> None:
    with export_env() as env:
        with env.engine.begin() as c:
            c.execute(sa.delete(export.shelf_destination_remote_library))
            c.execute(sa.delete(export.remote_library))
        fake = FakeKavita(libraries=[])
        ref = _link(env, fake)
        assert fake.requests_to("Library/create")
        assert [lib["id"] for lib in fake.libraries] == [int(ref)]
        assert fake.libraries[0]["folders"] == ["/manga/Kavita"]
        users = [i for i, r in enumerate(fake.requests) if r.path.endswith("/Users")]
        link = max(i for i, r in enumerate(fake.requests) if "Library/create" in r.path)
        assert users and max(users) > link  # the audit was replayed after the link


def test_a_library_a_restricted_account_reads_is_refused_before_any_link() -> None:
    with export_env() as env:
        with env.engine.begin() as c:
            c.execute(sa.delete(export.shelf_destination_remote_library))
            c.execute(sa.delete(export.remote_library))
        fake = FakeKavita()
        fake.users.append(member("kid", libraries=(7,), age_rating=13))
        with pytest.raises(AdoptionRefused) as refused:
            _link(env, fake)
        assert refused.value.accounts == ("kid",)
        with env.engine.connect() as c:
            assert (
                c.execute(
                    sa.select(sa.func.count()).select_from(export.remote_library)
                ).scalar_one()
                == 0
            )
        assert not fake.writes  # nothing written in Kavita


def test_a_relative_store_dir_does_not_stop_the_start(
    settings: Settings, tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """The front E2E starts the server with relative data and store folders and no target."""
    from fastapi.testclient import TestClient

    from shelfhold.infrastructure.migrate import migrate
    from shelfhold.main import create_app

    monkeypatch.chdir(tmp_path)
    (tmp_path / "store").mkdir()
    relative = settings.model_copy(update={"store_dir": __import__("pathlib").Path("store")})
    migrate(relative)
    with TestClient(create_app(relative)) as client:
        assert client.get("/healthz").status_code == 200
    from shelfhold.infrastructure.db import create_db_engine

    engine = create_db_engine(relative.database_path)
    try:
        with engine.connect() as c:
            assert c.execute(sa.select(platform.physical_root.c.path_entry)).scalars().all() == [
                str(tmp_path / "store")
            ]
    finally:
        engine.dispose()


def test_the_jurisdiction_follows_the_locale_until_the_admin_sets_it(
    settings: Settings, tmp_path
) -> None:
    """EF-SRC-024: `instance.jurisdiction`, default deduced from the locale, set by `config set`."""
    from shelfhold.export.application.root_bootstrap import default_jurisdiction

    assert default_jurisdiction({"LANG": "en_GB.UTF-8"}) == "GB"
    assert default_jurisdiction({"LC_ALL": "fr_CA.UTF-8", "LANG": "en_US"}) == "CA"
    assert default_jurisdiction({"LANG": "C.UTF-8"}) == "FR"  # the locale names no country
    with headless(settings) as env:
        assert env.admin.get("instance.jurisdiction").value == default_jurisdiction()
        env.admin.target_add_kavita(
            name="k", url="http://kavita.internal:5000", username="u", password=PASSWORD,
            accept_admin_risk=True,
        )  # fmt: skip
        env.admin.set("instance.jurisdiction", "be")
        assert env.admin.get("instance.jurisdiction").value == "BE"
        with pytest.raises(Exception, match="two-letter"):
            env.admin.set("instance.jurisdiction", "belgium")


# -- `shelf create` on a target that is not Kavita (J-HL-1) ------------------------------------


@pytest.mark.req("EF-EXP-020")
def test_shelf_create_on_a_komga_target_is_refused_with_its_own_code(
    settings: Settings, tmp_path
) -> None:
    """A Komga binding never owns a tree (J-SIMP-4), and `shelf create` binds a primary
    destination: on a Komga target the command answers the typed refusal, never the crash of the
    Kavita client it used to open for a target that is not Kavita, and no folder is left behind."""
    root = tmp_path / "library" / "manga"
    root.mkdir(parents=True)
    fake = FakeKomga(keys={KOMGA_KEY: True})
    probe = HttpKomgaProbe(lambda _url: komga_fetcher(fake))
    boot_check(settings, announce=False)  # the first start creates the keyring, as in production
    with headless(settings, komga=probe, real_linker=True, roots=_roots(settings, tmp_path)) as env:
        env.admin.target_add_komga(
            name="komga", url=KOMGA_URL, api_key=KOMGA_KEY,
            path_map=[{"local": str(root), "remote": "/manga", "mode": "rw"}],
        )  # fmt: skip
        asked = len(fake.requests)  # the probe of `target add` was the last ask of Komga
        with pytest.raises(KomgaPrimaryUnsupportedError) as refused:
            env.admin.shelf_create(name="Adultes", max_age="18+", target="komga", subpath="K")
        assert refused.value.code == "binding.komga_primary_unsupported"
        assert env.count("shelf") == 1  # the shelf is there; its binding is not
        assert env.count("shelf_destination") == 0
        assert not (root / "K").exists()  # no folder was made for a refused binding
        assert len(fake.requests) == asked


@pytest.mark.req("EF-EXP-010")
def test_shelf_create_on_a_native_folder_target_links_no_kavita_library(
    settings: Settings, tmp_path
) -> None:
    """The same for the folder Mihon reads: the shelf, its binding and its folder, no library."""
    root = tmp_path / "library" / "manga"
    root.mkdir(parents=True)
    boot_check(settings, announce=False)  # the first start creates the keyring, as in production
    with headless(settings, real_linker=True, roots=_roots(settings, tmp_path)) as env:
        env.admin.target_add_native_folder(
            name="mihon",
            path_map=[{"local": str(root), "remote": "/manga", "mode": "rw"}],
        )
        created = env.admin.shelf_create(name="Adultes", max_age="18+", target="mihon", subpath="K")
        assert created.created
        assert (root / "K").is_dir()
        assert env.count("shelf") == 1 and env.count("shelf_destination") == 1


def test_a_shelf_create_whose_link_fails_leaves_no_folder_behind(
    settings: Settings, tmp_path
) -> None:
    """The audit that refuses a shelf unbinds it: the folder it had made goes too, or a later
    binding would adopt a folder no destination owns."""
    root = tmp_path / "library" / "manga"
    root.mkdir(parents=True)
    roots = _roots(settings, tmp_path)
    boot_check(settings, announce=False)  # the first start creates the keyring, as in production

    class Refusing(_ShelfLinker):
        def link(self, target_id: str, destination_id: str, *, library_name: str) -> str:
            raise ShelfExposedToChildren(("kid8",))

    with headless(settings, roots=roots) as probe:
        linker = Refusing(settings, probe.engine)
    with headless(settings, roots=roots, linker=linker) as env:
        env.admin.target_add_kavita(
            name="k", url="http://kavita.internal:5000", username="u", password=PASSWORD,
            accept_admin_risk=True,
            path_map=[{"local": str(root), "remote": "/manga", "mode": "rw"}],
        )  # fmt: skip
        with pytest.raises(ShelfExposedToChildren):
            env.admin.shelf_create(name="Famille", max_age="13+", target="k", subpath="famille")
        assert not (root / "famille").exists()
        assert env.count("shelf") == 0 and env.count("shelf_destination") == 0


def _roots(settings: Settings, tmp_path) -> RootPolicy:
    return RootPolicy(tmp_path / "library", settings.data_dir, settings.store_dir)
