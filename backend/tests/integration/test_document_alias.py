"""ENF-QUAL-005 / EF-LEC-009: `document_alias` gets the KOReader and Kavita hashes of a file at
every rewrite, and the work of the hooks never touches the export that caused it.

The hashes themselves are proved against external vectors in `tests/unit/test_document_hash.py`;
here the expected values are taken from the bytes the pipeline wrote, so what is proved is the
wiring: the right file, the right item, the right binding, once per generation of the file.
"""

import hashlib
import io

import pytest
import sqlalchemy as sa

from shelfhold.export.application import render_hooks
from shelfhold.export.application.upgrade import UpgradeOutcome
from shelfhold.infrastructure.schema import export
from shelfhold.reading.domain.document_hash import kavita_hash, koreader_partial_md5
from tests.export_env import export_env
from tests.upgrade_env import (
    better_copy,
    bin_files,
    digest,
    export_one,
    record_of,
    request,
    service,
)

pytestmark = pytest.mark.req("ENF-QUAL-005")


def _aliases(env: object, item_id: str) -> dict[str, sa.Row[tuple[object, ...]]]:
    with env.engine.connect() as connection:  # type: ignore[attr-defined]
        rows = connection.execute(
            sa.select(export.document_alias).where(
                export.document_alias.c.export_item_id == item_id
            )
        )
        return {str(row.file_hash): row for row in rows}


def test_document_alias_written_on_each_rewrite() -> None:
    with export_env() as env:
        exported = export_one(env)
        generations = [exported.target.read_bytes()]
        for tag in (b"second", b"third"):
            result = service(env).upgrade(
                request(env, exported, better_copy(env, exported, tag=tag))
            )
            assert result.outcome is UpgradeOutcome.UPGRADED
            generations.append(exported.target.read_bytes())
        assert len({hashlib.sha256(data).digest() for data in generations}) == 3

        aliases = _aliases(env, exported.export_item_id)
        for data in generations:  # the file as first exported, and each rewrite of it
            row = aliases["sha256:" + hashlib.sha256(data).hexdigest()]
            assert row.koreader_partial_md5 == "md5:" + koreader_partial_md5(io.BytesIO(data))
            # stored like every digest of the schema (`<algorithm>:<lowercase hex>`)
            assert row.kavita_md5 == "md5:" + kavita_hash(io.BytesIO(data)).lower()
            assert row.shelf_destination_id == env.destination_id


@pytest.mark.parametrize("broken", ["koreader_partial_md5", "kavita_hash", "write_transaction"])
def test_alias_failure_never_fails_nor_rolls_back_an_export(
    monkeypatch: pytest.MonkeyPatch, broken: str
) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("the alias hook is broken")

    with export_env() as env:
        exported = export_one(env)
        old_bytes = exported.target.read_bytes()
        content_id = better_copy(env, exported)
        monkeypatch.setattr(render_hooks, broken, boom)
        scans: list[str] = []

        result = service(env, scans=scans).upgrade(request(env, exported, content_id))

        assert result.outcome is UpgradeOutcome.UPGRADED  # the export succeeded, and stays so
        assert exported.target.read_bytes() != old_bytes
        row = record_of(env, exported)
        assert row.file_hash == digest(exported.target) and row.state == "written"
        assert [p.read_bytes() for p in bin_files(env)] == [old_bytes]
        assert scans  # the rest of the run went on
        aliases = _aliases(env, exported.export_item_id)  # the transaction's own aliases are there
        assert exported.old_file_hash in aliases and row.file_hash in aliases
        # the rewrite's own alias has no hashes (the hook failed); the first file's was hashed by
        # the export that placed it (M4 L47), before the hook broke
        written = aliases[row.file_hash]
        assert written.koreader_partial_md5 is None and written.kavita_md5 is None
