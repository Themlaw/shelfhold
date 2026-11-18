"""M4 milestone review (contradicteur de jalon M4), axis "secrets and paths": end-to-end holes
that no single lot could see. Each test is red today and kept as a strict xfail until the fix.

J-FS-1: a writable root is judged by its spelling, not by where it leads. `RootPolicy.check`
(`export/application/root_bootstrap.py`) compares `os.path.abspath` strings with the library,
`/data` and `/store`, while `register_roots` then stamps the sentinel through the link
(`Path.is_dir`, `ConfinedRoot.open` follow it), and every later write of the root goes there.
The M4 doors that add a folder target at run time (native folder, CWA drop box, Komga) register
their folders as roots through this check: a folder of the library that is a link to the data
folder (or anywhere off the library) becomes a writable root of Shelfhold.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.export.application.root_bootstrap import RootPolicy
from shelfhold.headless import HeadlessError
from shelfhold.headless.admin import HeadlessAdmin
from tests.headless_env import headless

SENTINEL = ".shelfhold-mount-id"


def _add_native(admin: HeadlessAdmin, local: Path) -> object:
    path_map = [{"local": str(local), "remote": "/mihon", "mode": "rw"}]
    return admin.target_add_native_folder(name="Mihon", path_map=path_map)


def _add_cwa(admin: HeadlessAdmin, local: Path) -> object:
    path_map = [{"local": str(local), "remote": "/ingest", "mode": "rw"}]
    return admin.target_add_cwa(name="Drop", path_map=path_map)


def _registered(engine: sa.Engine) -> list[str]:
    with engine.connect() as connection:
        return [str(p) for p in connection.execute(sa.text("SELECT path_entry FROM physical_root"))]


@pytest.mark.req("EF-EXP-059", "ENF-SEC-012")
@pytest.mark.parametrize("add", [_add_native, _add_cwa], ids=["native_folder", "cwa"])
@pytest.mark.parametrize("leads_to", ["data", "elsewhere"])
def test_a_root_that_links_out_of_the_library_is_refused(
    settings: Settings,
    tmp_path: Path,
    add: Callable[[HeadlessAdmin, Path], object],
    leads_to: str,
) -> None:
    """A folder of the library that is a symbolic link to a folder of `/data` (or to any folder
    outside the library) is refused as a writable root, as the same folder named directly is:
    nothing is stamped there and no root is registered. Today the string check passes, the
    sentinel is written through the link and the folder becomes a root Shelfhold writes into."""
    library = tmp_path / "library"
    library.mkdir()
    real = settings.data_dir / "inner" if leads_to == "data" else tmp_path / "elsewhere"
    real.mkdir(parents=True)
    link = library / "mihon"
    link.symlink_to(real, target_is_directory=True)
    policy = RootPolicy(library, settings.data_dir, settings.store_dir)
    with headless(settings, roots=policy) as env:
        with pytest.raises(HeadlessError):
            add(env.admin, link)
        assert not (real / SENTINEL).exists()  # nothing stamped where the link leads
        assert str(link) not in _registered(env.engine)
