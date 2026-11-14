"""M3 L48 (EF-BIB-021): the list behind the "Corbeille" screen, read from the real bin index of a
real deletion: one line per deletion and Unit, the term, the size, whether the Unit is
irreplaceable now. Never a path, never a snapshot."""

import pytest

from shelfhold.library.adapters.trash import list_binned
from shelfhold.library.public import DeletionLevel
from tests.deletion_env import make_deletion, make_irreplaceable, spread_unit
from tests.export_env import export_env

pytestmark = pytest.mark.req("EF-BIB-021")


def test_trash_listing_groups_one_deletion_per_line_and_flags_irreplaceable() -> None:
    with export_env() as env:
        d = make_deletion(env)
        plain = spread_unit(env, env.new_series("Series A"))
        rare = spread_unit(env, env.new_series("Series B"))
        d.apply(DeletionLevel.DELETE_CONTENT, [plain, rare])
        make_irreplaceable(env, rare)
        with env.engine.connect() as connection:
            listed = list_binned(connection)
        by_unit = {line["unit_id"]: line for line in listed}
        assert set(by_unit) == {plain, rare}
        assert by_unit[rare]["irreplaceable"] is True
        assert by_unit[plain]["irreplaceable"] is False
        for line in listed:
            assert line["files"] >= 1 and line["purge_after"] > line["deleted_at"]
            assert set(line) == {
                "deletion_id", "unit_id", "level", "files", "bytes", "deleted_at", "purge_after",
                "irreplaceable",
            }  # no path, no snapshot


def test_trash_listing_of_an_empty_bin_is_empty() -> None:
    with export_env() as env, env.engine.connect() as connection:
        assert list_binned(connection) == []
