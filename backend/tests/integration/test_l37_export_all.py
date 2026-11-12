"""L37: full open export `shelfhold-export/1` and round-trip import (EF-INT-016, EF-INT-014,
EF-BIB-053 (5)). No content under a tombstone, no tombstone that would name a title; import only
into a fresh instance."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.infrastructure.schema import catalog
from tests.export_env import export_env
from tests.purge_env import ADMIN, TITLE, make_purge_env, spread

pytestmark = pytest.mark.req("EF-INT-016")


def _read_all_text(root: Path) -> str:
    parts: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            parts.append(path.read_text(encoding="utf-8"))
        except UnicodeDecodeError:
            parts.append(path.read_bytes().decode("utf-8", errors="ignore"))
    return "\n".join(parts)


def _load_manifest(out: Path) -> dict:
    return json.loads((out / "manifest.json").read_text(encoding="utf-8"))


@pytest.mark.req("EF-BIB-053", "EF-INT-027")
def test_export_all_after_purge_contains_no_title(tmp_path: Path) -> None:
    from shelfhold.platform.export_all import export_all

    with export_env() as env:
        series = env.new_series(TITLE)
        spread(env, series)
        purge = make_purge_env(env.lib)
        purge.purge.purge_work(
            series.work_id, principal=ADMIN, typed=series.work_id, now=purge.clock.now()
        )

        out = tmp_path / "out"
        export_all(env.engine, env.lib.data, env.lib.library, env.lib.store, out)

        dumped = _read_all_text(out)
        assert TITLE not in dumped
        # no tombstone payload that would name the title
        for name in ("tombstone", "tombstones", "content_tombstone"):
            assert not (out / name).exists()


def test_export_all_schema_versioned_and_documented(tmp_path: Path) -> None:
    from shelfhold.platform.export_all import export_all

    with export_env() as env:
        env.new_series("One Piece")
        out = tmp_path / "out"
        export_all(env.engine, env.lib.data, env.lib.library, env.lib.store, out)

        manifest = _load_manifest(out)
        assert manifest["schema_version"] == "shelfhold-export/1"
        assert manifest.get("format") == "shelfhold-export/1" or "schema_version" in manifest

        doc = Path("docs/reference/format-export.md")
        if not doc.exists():
            doc = Path(__file__).resolve().parents[3] / "docs/reference/format-export.md"
        assert doc.exists(), "format-export.md must document the export format"
        text = doc.read_text(encoding="utf-8")
        assert "shelfhold-export/1" in text


def test_open_format_export_roundtrip(tmp_path: Path) -> None:
    from shelfhold.platform.export_all import export_all
    from shelfhold.platform.import_all import NonEmptyInstanceError, import_all

    with export_env() as env:
        series = env.new_series("Berserk")
        env.add_chapter(series, "1")
        env.pipeline.run(env.destination_id, job_id="01J0000000000000000000JOB0")

        out = tmp_path / "out"
        export_all(env.engine, env.lib.data, env.lib.library, env.lib.store, out)

        fresh = tmp_path / "fresh"
        fresh.mkdir()
        fresh_db = fresh / "shelfhold.db"
        # fresh empty database initialized by import
        import_all(fresh_db, out)

        engine = sa.create_engine(f"sqlite:///{fresh_db}")
        with engine.connect() as connection:
            titles = {row[0] for row in connection.execute(sa.select(catalog.work.c.title)).all()}
        engine.dispose()
        assert "Berserk" in titles

        # importing into a non-empty base is refused
        with pytest.raises(NonEmptyInstanceError):
            import_all(fresh_db, out)
