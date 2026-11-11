"""Metadata revisions through the Kavita API, and the suspect-revision guard at the upgrade door
(EF-BIB-043, EF-BIB-024)."""

import pytest
import sqlalchemy as sa

from shelfhold.export.application.upgrade import UpgradeOutcome
from shelfhold.export.targets.kavita.series import SeriesFields
from shelfhold.identity.public import Allowed
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export
from tests.export_env import export_env
from tests.family_env import family_env, rate
from tests.integration.test_kavita_age_push import index
from tests.kavita_env import run
from tests.kavita_export_env import wire
from tests.upgrade_env import better_copy, export_one, request, service


def _summary_posts(w) -> list:  # type: ignore[no-untyped-def]
    return [r for r in w.fake.requests_to("/Series/metadata") if r.method == "POST"]


def _wired_series():  # type: ignore[no-untyped-def]
    return family_env(13)


def _indexed(env):  # type: ignore[no-untyped-def]
    w = wire(env)
    series = env.new_series()
    env.add_chapter(series, "1")
    rate(env, series, 10)
    env.pipeline.run(env.destination_id, job_id=new_ulid())
    sid = index(w)
    fields = SeriesFields(env.engine, env.clock, w.events)
    return w, sid, env.rows(export.export_series)[0].id, fields


def _push(env, w, fields, series_id: str, sid: int, field: str, value: str) -> bool:  # type: ignore[no-untyped-def]
    return bool(run(fields.push(w.client, env.destination_id, series_id, sid, field, value)))


@pytest.mark.req("EF-BIB-043")
def test_metadata_only_revision_applied_via_kavita_api_field_guard() -> None:
    with _wired_series() as env:
        w, sid, series_id, fields = _indexed(env)
        files_before = sorted(r.file_hash for r in env.rows(export.export_record))
        assert _push(env, w, fields, series_id, sid, "summary", "A new summary")
        assert w.fake.ages[sid]["summary"] == "A new summary"
        assert w.fake.ages[sid]["summaryLocked"] is True
        posts = len(_summary_posts(w))
        # Shelfhold's own value is already there: no second request
        assert _push(env, w, fields, series_id, sid, "summary", "A new summary")
        assert len(_summary_posts(w)) == posts
        # a field the API does not take is never sent
        assert not _push(env, w, fields, series_id, sid, "title", "x")
        assert len(_summary_posts(w)) == posts
        # no file was written by any of it
        assert sorted(r.file_hash for r in env.rows(export.export_record)) == files_before


@pytest.mark.req("EF-BIB-043")
def test_metadata_locked_by_a_third_party_is_never_rewritten() -> None:
    with _wired_series() as env:
        w, sid, series_id, fields = _indexed(env)
        w.fake.ages[sid].update(summary="Mine", summaryLocked=True)  # the admin's own lock
        assert not _push(env, w, fields, series_id, sid, "summary", "Another one")
        assert w.fake.ages[sid]["summary"] == "Mine"
        assert _summary_posts(w) == []


def _quarantine(env, content_id: str) -> None:  # type: ignore[no-untyped-def]
    with env.engine.begin() as connection:
        variant = connection.execute(
            sa.text("SELECT variant_id FROM content WHERE id = :c"), {"c": content_id}
        ).scalar_one()
        connection.execute(
            sa.text(
                "INSERT INTO variant_revision (variant_id, revision, seen_at, page_count, "
                "quarantined_at, quarantine_reason) VALUES (:v, 1, :t, 1, :t, 'fewer_pages')"
            ),
            {"v": variant, "t": env.clock.now()},
        )


@pytest.mark.req("EF-BIB-024")
def test_suspect_revision_never_promoted_by_an_upgrade_without_the_reserved_grant() -> None:
    with export_env() as env:
        exported = export_one(env)
        old_bytes = exported.target.read_bytes()
        content_id = better_copy(env, exported)
        _quarantine(env, content_id)

        refused = service(env).upgrade(request(env, exported, content_id))
        assert refused.outcome is UpgradeOutcome.SUSPECT_REVISION
        assert exported.target.read_bytes() == old_bytes  # the stored version stays served

        # whatever else a request says, only the grant of the reserved action lifts it
        forged = request(env, exported, content_id, automatic=False, typed="yes")
        assert service(env).upgrade(forged).outcome is UpgradeOutcome.SUSPECT_REVISION
        assert exported.target.read_bytes() == old_bytes

        granted = request(env, exported, content_id, automatic=False)
        object.__setattr__(granted, "suspect_grant", Allowed())
        assert service(env).upgrade(granted).outcome is UpgradeOutcome.UPGRADED
        assert exported.target.read_bytes() != old_bytes


def _receive(env, exported, variant: str, pages: int, tag: bytes) -> str:  # type: ignore[no-untyped-def]
    """A chapter archive of `pages` pages received for `variant`, through the real store."""
    from shelfhold.export.domain.physical_roots import RECEIVED_DIR
    from shelfhold.library.public import PassthroughStore
    from tests.library_env import cbz_bytes
    from tests.locks_env import work_lock
    from tests.null_tombstones import NONE_PURGED

    entries = tuple(
        (f"{i + 1:03d}.png", b"\x89PNG\r\n\x1a\n" + tag + bytes([i]) + b"\x00" * 32)
        for i in range(pages)
    )
    PassthroughStore(
        env.engine, env.lib.fsops, env.lib.locate, env.lib.data,
        tombstones=NONE_PURGED, hold_work=work_lock(env.engine),
    ).ingest(
        unit_id=exported.unit_id, variant_id=variant, staged=env.lib.stage(cbz_bytes(entries)),
        dest=str(env.lib.manga / RECEIVED_DIR / f"{exported.unit_id}-{new_ulid()}.cbz"),
    )  # fmt: skip
    with env.engine.connect() as connection:
        return str(
            connection.execute(
                sa.text(
                    "SELECT id FROM content WHERE variant_id = :v ORDER BY created_at DESC, id DESC"
                ),
                {"v": variant},
            ).scalar_one()
        )


@pytest.mark.req("EF-BIB-024")
def test_a_truncated_reupload_is_quarantined_at_reception_and_never_promoted() -> None:
    """The production path end to end: the real store receives a whole chapter is served, then
    another copy cut to a fraction arrives; no source said anything about pages."""
    with export_env() as env:
        exported = export_one(env)
        from tests.integration.test_schema_catalog import add_variant

        with env.engine.begin() as connection:
            variant = add_variant(
                connection, exported.unit_id, exported.series.binding_id, f"ch-{new_ulid()}"
            )
        with env.engine.begin() as connection:  # the version served is a whole chapter
            connection.execute(
                sa.text("UPDATE content SET page_count = 10 WHERE unit_id = :u"),
                {"u": exported.unit_id},
            )
        truncated = _receive(env, exported, variant, 2, b"cut")

        with env.engine.connect() as connection:
            rows = connection.execute(
                sa.text(
                    "SELECT revision, page_count, quarantine_reason FROM variant_revision "
                    "WHERE variant_id = :v ORDER BY revision"
                ),
                {"v": variant},
            ).all()
            events = connection.execute(
                sa.text("SELECT count(*) FROM event WHERE type = 'unit.revision_suspect'")
            ).scalar_one()
            pending = connection.execute(
                sa.text("SELECT nature, state FROM revision_pending WHERE variant_id = :v"),
                {"v": variant},
            ).all()
        assert [r.quarantine_reason for r in rows[-1:]] == ["fewer_pages"]
        assert events == 1 and [tuple(p) for p in pending] == [("rewrite", "open")]

        # whatever asks for the truncated file to replace the served one is refused
        old_bytes = exported.target.read_bytes()
        refused = service(env).upgrade(request(env, exported, truncated))
        assert refused.outcome is UpgradeOutcome.SUSPECT_REVISION
        assert exported.target.read_bytes() == old_bytes
