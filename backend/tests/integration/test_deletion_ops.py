"""EF-BIB-021 (L25) through the operating surface of a real instance (composition, roots, lease,
simulated Kavita): the preview reads, the application and the restore write, and the refusals are
the catalog's codes."""

from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.identity.public import Credential, Principal
from shelfhold.ops.errors import CodedRefusal
from shelfhold.ops.library import LibraryOps
from tests.l50_env import Seeded, seeded
from tests.purge_env import ADMIN, KEY, READER

TYPED_ADMIN = ADMIN
MEMBER = Principal("01J00000000000000000000MEM", "member", "member", Credential.LOCAL_CLI)


def served(env: Seeded) -> list[Path]:
    return env.exported_files()


@pytest.mark.req("EF-BIB-021")
def test_ops_preview_apply_and_restore_on_a_real_instance(
    settings: Settings, tmp_path: Path
) -> None:
    with seeded(settings, tmp_path) as env:
        series = env.new_series("Dr Stone")
        unit = env.add_chapter(series, "1")
        env.export()
        ops = LibraryOps(env.inst.comp)
        [file] = served(env)
        preview = ops.delete_preview(level="remove_destinations", unit_ids=[unit])
        assert preview["files"] == 1 and preview["blocked"] is False
        assert preview["units"][0]["exports"] == 1 and file.exists()
        [shown] = preview["units"][0]["files"]  # each file, by its path in its root (relative)
        assert not shown["path"].startswith("/") and file.as_posix().endswith(shown["path"])
        assert str(file) not in str(preview)
        with pytest.raises(CodedRefusal) as stale:
            ops.delete_apply(level="remove_destinations", unit_ids=[unit], if_match="nope",
                             typed={}, principal=ADMIN)  # fmt: skip
        assert stale.value.code == "plan.stale" and file.exists()
        for who in (MEMBER, KEY, READER):
            with pytest.raises(CodedRefusal) as refused:
                ops.delete_apply(level="remove_destinations", unit_ids=[unit],
                                 if_match=preview["version"], typed={}, principal=who)  # fmt: skip
            assert refused.value.code == "forbidden" and file.exists()
        done = ops.delete_apply(level="remove_destinations", unit_ids=[unit],
                                if_match=preview["version"], typed={}, principal=ADMIN)  # fmt: skip
        assert done["moved"] == 1 and not file.exists()
        assert (
            done["folders_removed"] == 1 and not (env.inst.library / "Kavita" / "Dr Stone").exists()
        )
        with pytest.raises(CodedRefusal) as unknown:
            ops.delete_preview(level="delete_content", unit_ids=["01J000000000000000000UNKNO"])
        assert unknown.value.code == "ops.unknown_subject"
        assert ops.delete_restore(unit_id=unit, principal=ADMIN)["restored"] == 1
        assert file.exists()
        with pytest.raises(CodedRefusal) as nothing:
            ops.delete_restore(unit_id=unit, principal=ADMIN)
        assert nothing.value.code == "deletion.blocked"


@pytest.mark.req("EF-BIB-021", "EF-BIB-031")
def test_ops_content_deletion_of_an_irreplaceable_unit_asks_for_its_word(
    settings: Settings, tmp_path: Path
) -> None:
    with seeded(settings, tmp_path) as env:
        unit = env.add_chapter(env.new_series("Dr Stone"), "1")
        env.export()
        with env.inst.engine.begin() as connection:
            connection.execute(sa.text("UPDATE unit SET availability = 'removed'"))
        ops = LibraryOps(env.inst.comp)
        preview = ops.delete_preview(level="delete_content", unit_ids=[unit])
        assert preview["confirmations"] == [unit]
        args = {"level": "delete_content", "unit_ids": [unit], "if_match": preview["version"]}
        with pytest.raises(CodedRefusal) as refused:
            ops.delete_apply(typed={unit: "wrong"}, principal=ADMIN, **args)
        assert refused.value.code == "unit.confirmation_required"
        assert len(served(env)) == 1
        done = ops.delete_apply(typed={unit: unit}, principal=ADMIN, **args)
        assert done["moved"] == 2 and served(env) == []
