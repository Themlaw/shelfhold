"""L45, ENF-SEC-003: a CBZ over the default entry cap is refused by the real receiving path."""

import pytest

from shelfhold.library.public import PassthroughStore
from tests.library_env import cbz_bytes, lib_env
from tests.null_tombstones import NONE_PURGED


@pytest.mark.req("ENF-SEC-003")
def test_passthrough_store_refuses_a_cbz_of_2500_entries() -> None:
    pages = tuple((f"{n:05d}.png", b"\x89PNG\r\n\x1a\n" + bytes(8)) for n in range(2_500))
    with lib_env() as lib:
        unit_id, variant_id = lib.new_unit()
        dest = lib.manga / "Series Alpha" / "ch-2500.cbz"
        store = PassthroughStore(
            lib.engine, lib.fsops, lib.locate, lib.data, tombstones=NONE_PURGED
        )
        with pytest.raises(Exception, match="entries"):
            store.ingest(
                unit_id=unit_id,
                variant_id=variant_id,
                staged=lib.stage(cbz_bytes(pages)),
                dest=str(dest),
            )
        assert not dest.exists()
