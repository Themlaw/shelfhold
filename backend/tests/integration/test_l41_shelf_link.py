"""L41: the first registration of the roots, and `shelf create` linking a Kavita library with the
audit first (EF-INT-024, EF-EXP-002, EF-EXP-055)."""

import asyncio

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.export.application.root_bootstrap import register_roots
from shelfhold.export.domain.path_map import PathMode
from shelfhold.export.targets.kavita import AdminKey, KavitaClient
from shelfhold.export.targets.kavita.adopt import AdoptionRefused
from shelfhold.export.targets.kavita.audit import AccessAuditor
from shelfhold.export.targets.kavita.linking import ShelfLinker
from shelfhold.infrastructure.schema import export, platform
from tests.export_env import export_env
from tests.fakes.kavita import API_KEY, BASE_URL, FakeKavita, build_fetcher
from tests.fakes.kavita.server import member
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
    with headless(settings, store_dir=None) as env:
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
        assert roots == {str(root): "rw", str(ro): "adopt_ro"}
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
