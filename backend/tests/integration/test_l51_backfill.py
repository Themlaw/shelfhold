"""L51 (EF-EXP-053): `backfill --gaps|--all` and its route show an estimate first and, above 100
Units or 5 GiB, start only on a confirmation that carries the figures the caller saw."""

import json
import re
from collections.abc import Callable
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.schema import acquisition, platform
from tests.auth_env import run_cli
from tests.integration.test_schema_catalog import add_unit, add_variant
from tests.l50_env import Seeded, Series
from tests.l51_env import live

pytestmark = pytest.mark.req("EF-EXP-053")
UNITS = 150


def followed_with(count: int) -> Callable[[Seeded, Series], None]:
    """A followed series whose watermark is at `count` and of which nothing was ever fetched."""

    def seed(env: Seeded, series: Series) -> None:
        with env.inst.engine.begin() as connection:
            connection.execute(
                sa.insert(acquisition.series_follow).values(
                    edition_id=series.edition_id, work_id=series.work_id, follow_from_segment="",
                    follow_from_number=str(count), follow_from_norm_by="number@1",
                )
            )  # fmt: skip
            for n in range(1, count + 1):
                add_variant(
                    connection, add_unit(connection, series.edition_id, str(n)),
                    series.binding_id, f"synthetic:{n}",
                )  # fmt: skip

    return seed


def mode_of(env: Seeded, series: Series) -> str:
    (row,) = env.rows(acquisition.series_follow)
    assert row.edition_id == series.edition_id
    return str(row.backfill_mode)


def test_backfill_requires_estimate_and_confirmation_over_100_units_or_5gib(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with live(monkeypatch, settings, tmp_path, chapters=(), seed=followed_with(UNITS)) as api:
        edition = api.series.edition_id
        url = f"/api/v1/editions/{edition}/backfill"

        # the route: the estimate first (count, size, free space) ...
        estimate = api.client.get(url, params={"mode": "all"})
        assert estimate.status_code == 200, estimate.text
        figures = estimate.json()
        assert (figures["mode"], figures["units"]) == ("all", UNITS)
        assert figures["needs_confirmation"] is True
        assert figures["size_bytes"] > 0 and figures["free_bytes"] > 0
        assert mode_of(api.env, api.series) == "none"  # an estimate starts nothing

        # ... nothing leaves without the figures that were seen ...
        refused = api.api.post(url, json={"mode": "all"})
        assert refused.status_code == 409
        assert refused.json()["type"] == "/api/v1/problems/backfill.confirmation-required"
        assert refused.json()["estimate"]["units"] == UNITS  # the fresh estimate is shown again
        short = api.api.post(url, json={"mode": "all", "confirm_units": 100, "confirm_bytes": 1})
        assert short.status_code == 409
        assert mode_of(api.env, api.series) == "none"

        # ... and a confirmation of exactly those figures starts it.
        started = api.api.post(
            url,
            json={"mode": "all", "confirm_units": UNITS, "confirm_bytes": figures["size_bytes"]},
        )
        assert started.status_code == 200, started.text
        assert started.json()["units"] == UNITS
        assert mode_of(api.env, api.series) == "all"
        bad = api.api.post(url, json={"mode": "everything"})
        assert bad.status_code == 422
        unknown = api.client.get("/api/v1/editions/01JNOSUCHEDITION000000000A/backfill")
        assert unknown.status_code == 404

        # the command: same rule, same figures, and the exact command to run to confirm
        with api.env.inst.engine.begin() as connection:
            connection.execute(sa.update(acquisition.series_follow).values(backfill_mode="none"))
        capsys.readouterr()
        assert run_cli(monkeypatch, settings, "backfill", "--all", edition, "--dry-run") == 0
        shown = capsys.readouterr().out
        assert str(UNITS) in shown and "all" in shown
        assert mode_of(api.env, api.series) == "none"

        assert run_cli(monkeypatch, settings, "backfill", "--all", edition) == 3
        out = capsys.readouterr()
        assert str(UNITS) in out.out  # the estimate is shown before anything is asked
        assert f"--confirm-units {UNITS}" in out.err  # and the way to confirm it
        assert mode_of(api.env, api.series) == "none"

        size = re.search(r"--confirm-bytes (\d+)", out.err)
        assert size
        assert (
            run_cli(
                monkeypatch, settings, "backfill", "--all", edition,
                "--confirm-units", str(UNITS), "--confirm-bytes", size.group(1),
            )
            == 0
        )  # fmt: skip
        assert mode_of(api.env, api.series) == "all"
        assert run_cli(monkeypatch, settings, "backfill", edition) == 2  # --gaps or --all


def test_a_small_backfill_needs_no_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with live(monkeypatch, settings, tmp_path, chapters=(), seed=followed_with(40)) as api:
        capsys.readouterr()
        edition = api.series.edition_id
        assert run_cli(monkeypatch, settings, "backfill", "--all", edition) == 0
        assert mode_of(api.env, api.series) == "all"
        started = api.api.post(f"/api/v1/editions/{edition}/backfill", json={"mode": "gaps"})
        assert started.status_code == 200 and started.json()["needs_confirmation"] is False
        missing = run_cli(monkeypatch, settings, "backfill", "--all", "01JNOSUCHEDITION000000000A")
        assert missing != 0


def test_inflated_confirmation_is_refused_and_the_ceiling_is_the_estimate(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    """(review) A confirmation larger than the estimate is not a confirmation of what was shown:
    it would be stored as the ceiling and let the series grow without any further question."""
    with live(monkeypatch, settings, tmp_path, chapters=(), seed=followed_with(UNITS)) as api:
        edition = api.series.edition_id
        url = f"/api/v1/editions/{edition}/backfill"
        figures = api.client.get(url, params={"mode": "all"}).json()

        for units, size in (
            (10**9, figures["size_bytes"]),
            (UNITS, 10**15),
            (UNITS + 1, figures["size_bytes"]),
        ):
            inflated = api.api.post(
                url, json={"mode": "all", "confirm_units": units, "confirm_bytes": size}
            )
            assert inflated.status_code == 409, inflated.text
            assert inflated.json()["estimate"]["units"] == UNITS
        capsys.readouterr()
        assert (
            run_cli(
                monkeypatch, settings, "backfill", "--all", edition,
                "--confirm-units", str(10**9), "--confirm-bytes", str(10**15),
            )
            == 3
        )  # fmt: skip
        assert mode_of(api.env, api.series) == "none"
        with api.env.inst.engine.connect() as connection:
            keys = connection.execute(sa.select(platform.app_setting.c.key)).scalars().all()
            assert not [k for k in keys if "backfill" in k]  # no ceiling was stored

        started = api.api.post(
            url,
            json={"mode": "all", "confirm_units": UNITS, "confirm_bytes": figures["size_bytes"]},
        )
        assert started.status_code == 200
        with api.env.inst.engine.connect() as connection:
            stored = connection.execute(
                sa.select(platform.app_setting.c.value).where(
                    platform.app_setting.c.key.like("%backfill%")
                )
            ).scalar_one()
        assert json.loads(stored) == {"units": UNITS, "bytes": figures["size_bytes"]}
