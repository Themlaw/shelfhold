"""M2 L04 (EF-SRC-035, EF-EXP-056): no stored reference, journal line or snapshot holds a secret.

The service is the simulated Suwayomi whose chapter URLs carry a key in the path: the shape of an
OPDS feed that puts the API key there. The key is built at run time (no literal in the sources).
"""

import asyncio
import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
import sqlalchemy as sa
import structlog

from shelfhold.acquisition.application.upstream import (
    SnapshotRefusedError,
    UpstreamSnapshots,
    validate_payload,
)
from shelfhold.infrastructure.clock import SystemClock
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.logging import configure_logging, register_secret_value
from shelfhold.infrastructure.schema import acquisition, platform
from shelfhold.infrastructure.secrets.keyring import Keyring
from shelfhold.infrastructure.secrets.vault import SecretVault
from shelfhold.infrastructure.write_guard import SecretInWriteError
from shelfhold.library.adapters.meta_projection import MetaProjection
from shelfhold.sdk.connected import ConnectedCapability
from shelfhold.sdk.refs import SeriesRef
from tests.backup_env import backup_bench, read_members
from tests.follow_env import Bench, follow_bench
from tests.library_env import lib_env
from tests.redaction_support import forget_secret_values, synthetic_text

PLACEHOLDER = "{secret}"
KEY_A = synthetic_text("opds-a", specials=True)
KEY_B = synthetic_text("opds-b", specials=True)


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    forget_secret_values()
    yield
    forget_secret_values()


@pytest.fixture
def captured(capsys: pytest.CaptureFixture[str]) -> Iterator[Any]:
    configure_logging(level="DEBUG", log_format="json")
    capsys.readouterr()

    def drain() -> str:
        return capsys.readouterr().out

    yield drain
    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, "_shelfhold_handler", False)]:
        root.removeHandler(handler)
    root.setLevel(logging.WARNING)
    structlog.reset_defaults()


def _forms(key: str) -> list[str]:
    return [key, quote(key, safe=""), quote(quote(key, safe=""), safe="")]


def _dump(engine: sa.Engine) -> str:
    raw = engine.raw_connection()
    try:
        return "\n".join(raw.driver_connection.iterdump())  # type: ignore[attr-defined]
    finally:
        raw.close()


def _leaks(text: str, key: str) -> list[str]:
    return [form for form in _forms(key) if form in text]


def _carry_key(bench: Bench, key: str) -> None:
    """The simulated server now puts the key in every chapter URL, as a feed would."""
    for number, chapter in enumerate(bench.alpha()["chapters"], start=1):
        chapter["url"] = f"/series-alpha/{quote(key, safe='')}/ch-{number:03d}"


def _remote_ids(bench: Bench) -> list[str]:
    """The identifiers of the followed series (the bench's library also holds an unrelated one)."""
    rows = bench.rows(
        "SELECT remote_unit_id FROM unit_variant WHERE remote_unit_id LIKE 'suwayomi:%'"
    )
    return [r["remote_unit_id"] for r in rows]


@pytest.mark.req("EF-SRC-035", "EF-SRC-023")
def test_remote_refs_never_contain_secret_values(tmp_path: Path, captured: Any) -> None:
    """The base, `unit.json`, the log and the mirror's input hold the key under no encoding."""
    register_secret_value(KEY_A)
    with follow_bench() as bench:
        bench.fetcher.ref_template = PLACEHOLDER  # type: ignore[attr-defined]
        _carry_key(bench, KEY_A)
        report = bench.run()
        assert report.discovered == 2 and report.paused is None
        ids = _remote_ids(bench)
        assert ids and all(PLACEHOLDER in i for i in ids)  # rewritten by the template

        assert _leaks(_dump(bench.lib.engine), KEY_A) == []  # the whole database
        projection = MetaProjection(bench.lib.engine, bench.lib.data)
        units = [r["id"] for r in bench.rows("SELECT id FROM unit")]
        for unit_id in units:
            projection.project(unit_id)
        files = [p for p in bench.lib.data.rglob("*.json")]
        assert files, "the projection wrote nothing: the test would prove nothing"
        for path in files:
            assert _leaks(path.read_text(encoding="utf-8"), KEY_A) == [], path.name
        assert _leaks(captured(), KEY_A) == []  # the logs, DEBUG included


@pytest.mark.req("EF-SRC-035")
def test_remote_refs_never_contain_secret_values_in_the_mirror_input() -> None:
    """The provenance mirror copies `remote_unit_id` out of the database: a row that predates the
    guard (a legacy value) is still written without the key."""
    from shelfhold.export.application.mirror import ProvenanceMirror

    with lib_env() as lib:
        legacy = f"/old/{quote(KEY_A, safe='')}/ch-1"
        with lib.engine.begin() as connection:  # before the key is known: nothing to refuse
            connection.execute(
                sa.text("UPDATE unit_variant SET remote_unit_id = :r WHERE id = :v"),
                {"r": legacy, "v": lib.chain["variant"]},
            )
        register_secret_value(KEY_A)
        coverage = {"item": [{"unit": lib.chain["unit"]}]}
        with lib.engine.connect() as connection:
            ProvenanceMirror._sources(connection, coverage)
        written = json.dumps(coverage)
        assert _leaks(written, KEY_A) == [] and "remote_unit_id" in written


@pytest.mark.req("EF-SRC-035")
def test_remote_refs_never_contain_secret_values_in_a_backup(tmp_path: Path) -> None:
    register_secret_value(KEY_A)
    with backup_bench(tmp_path) as bench:
        with bench.engine.begin() as connection, pytest.raises(SecretInWriteError):
            connection.execute(  # the raw form never reaches the database
                sa.insert(acquisition.source_item).values(
                    id=new_ulid(), plugin_id="p", source_key="k", remote_id=f"/api/opds/{KEY_A}/s",
                )
            )  # fmt: skip
        with bench.engine.begin() as connection:
            connection.execute(
                sa.insert(acquisition.source_item).values(
                    id=new_ulid(), plugin_id="p", source_key="k",
                    remote_id=f"/api/opds/{PLACEHOLDER}/s",
                )
            )  # fmt: skip
        members = read_members(bench.backups / bench.service.run().name)
    assert members, "an empty backup proves nothing"
    for name, data in members.items():
        text = data.decode("latin-1")
        assert _leaks(text, KEY_A) == [], name
    assert any(PLACEHOLDER.encode() in data for data in members.values())


@pytest.mark.req("EF-SRC-035")
def test_api_key_rotation_keeps_remote_ids() -> None:
    """A new key in the URLs: same identifiers, so no new Variant (EF-BIB-050)."""
    register_secret_value(KEY_A)
    with follow_bench() as bench:
        bench.fetcher.ref_template = PLACEHOLDER  # type: ignore[attr-defined]
        _carry_key(bench, KEY_A)
        assert bench.run().discovered == 2
        before = sorted(_remote_ids(bench))
        register_secret_value(KEY_B)  # the administrator rotated the key
        _carry_key(bench, KEY_B)
        again = bench.run()
        assert again.discovered == 0 and again.paused is None
        assert sorted(_remote_ids(bench)) == before
        assert len(_remote_ids(bench)) == 2
        assert _leaks(_dump(bench.lib.engine), KEY_A) == []
        assert _leaks(_dump(bench.lib.engine), KEY_B) == []


@pytest.mark.req("EF-SRC-035")
def test_a_reference_with_the_key_and_no_template_is_quarantined(captured: Any) -> None:
    """No template declared: the references are refused as a contract error. The Units are
    quarantined (the service is never paused for it, review of L04); nothing with the key is
    stored."""
    register_secret_value(KEY_A)
    with follow_bench() as bench:
        _carry_key(bench, KEY_A)
        report = bench.run()
        assert report.paused is None and report.discovered == 0
        assert "quarantine" in str(bench.rows("SELECT health FROM source_config"))
        assert _remote_ids(bench) == []
        assert _leaks(_dump(bench.lib.engine), KEY_A) == []
        assert _leaks(captured(), KEY_A) == []  # the refusal does not quote the reference


@pytest.mark.req("EF-SRC-035")
def test_the_database_itself_refuses_a_secret_in_a_reference_or_a_journal() -> None:
    """The guard works below the plugin boundary: whoever writes, the engine refuses."""
    register_secret_value(KEY_A)
    with lib_env() as lib:
        item = {"plugin_id": "p", "source_key": "k"}
        for statement in (
            sa.insert(acquisition.source_item).values(
                id=new_ulid(), remote_id=f"/o/{KEY_A}", **item
            ),
            sa.insert(acquisition.source_item).values(
                id=new_ulid(), remote_id=f"/o/{quote(KEY_A, safe='')}", **item
            ),
            sa.update(acquisition.source_item).values(remote_id=f"/x?token={KEY_A}"),
            sa.insert(platform.event).values(
                id=1, type="t", schema_version=1, visibility="ops",
                payload={"url": f"https://h.example/{KEY_A}"}, occurred_at=datetime.now(UTC),
            ),
        ):  # fmt: skip
            with pytest.raises(SecretInWriteError), lib.engine.begin() as connection:
                connection.execute(statement)
        with lib.engine.connect() as connection:
            count = connection.execute(sa.text("SELECT count(*) FROM source_item")).scalar_one()
        assert count == 1  # only the seeded one: the refused writes left nothing


# -- upstream_restore -----------------------------------------------------------------------------


class _Service:
    """A connected service with the `upstream_restore` capability and a payload we choose."""

    connected = ConnectedCapability.UPSTREAM_RESTORE

    def __init__(self, payload: dict[str, object], sensitive: tuple[str, ...] = ()) -> None:
        self.payload = payload
        self.snapshot_sensitive_fields = sensitive

    async def snapshot_upstream(self, f: Any, ref: SeriesRef) -> dict[str, object]:
        return dict(self.payload)


def _take(snapshots: UpstreamSnapshots, service: _Service) -> bool:
    ref = SeriesRef(source_key="svc:1", remote_id="/series-alpha", title="")
    return asyncio.run(
        snapshots.take(service, object(), connected_service_id="svc", ref=ref)  # type: ignore[arg-type]
    )


@pytest.mark.req("EF-EXP-056", "EF-SRC-035")
def test_upstream_snapshot_payload_schema_has_no_secret() -> None:
    """The payload is validated at write: plain, small, and free of any secret (a registered
    value, a userinfo, a token-shaped value). Nothing is stored when it is refused."""
    register_secret_value(KEY_A)
    token = synthetic_text("jwt")
    bad: list[dict[str, Any]] = [
        {"password": "x"},
        {"a": {"api_key": "x"}},
        {"note": f"see https://h.example/o/{KEY_A}/x"},
        {"note": f"see https://h.example/o/{quote(KEY_A, safe='')}/x"},
        {"endpoint": "https://user:pw@h.example/x"},
        {"hint": f"eyJ{token[:12]}.{token[:14]}.{token[14:]}"},
        {"list": [{"deep": f"Bearer {token}"}]},
    ]
    for payload in bad:
        with pytest.raises(SnapshotRefusedError):
            validate_payload(payload)
    assert validate_payload({"categories": [{"id": 1, "name": "Reading"}], "update_strategy": "X"})
    with lib_env() as lib:
        snapshots = UpstreamSnapshots(lib.engine, clock=SystemClock())
        for payload in bad:
            with pytest.raises(SnapshotRefusedError):
                _take(snapshots, _Service(payload))
        assert snapshots.count() == 0


@pytest.mark.req("EF-EXP-056", "ENF-SEC-005")
def test_a_sensitive_snapshot_field_is_sealed_in_the_envelope() -> None:
    """A field the service declares sensitive is not refused: it is sealed (ENF-SEC-005) with the
    row as associated data, and comes back as it was for the restoration."""
    secret_text = synthetic_text("sealed-field")
    payload: dict[str, object] = {"categories": [], "hint": secret_text}
    with lib_env() as lib:
        plain = UpstreamSnapshots(lib.engine, clock=SystemClock())
        with pytest.raises(SnapshotRefusedError):  # no vault: nothing sensitive is kept
            _take(plain, _Service(payload, sensitive=("hint",)))
        vault = SecretVault(Keyring.generate(datetime.now(UTC)))
        snapshots = UpstreamSnapshots(lib.engine, clock=SystemClock(), vault=vault)
        assert _take(snapshots, _Service(payload, sensitive=("hint",))) is True
        with lib.engine.connect() as connection:
            stored = connection.execute(
                sa.text("SELECT payload FROM upstream_snapshot")
            ).scalar_one()
        assert secret_text not in stored and "hint" not in json.loads(stored)
        got = snapshots.get("svc", "svc:1|/series-alpha")
        assert got is not None and got["hint"] == secret_text and got["categories"] == []
        # the seal is bound to its row: moved to another row it does not open
        with lib.engine.begin() as connection:
            connection.execute(sa.text("UPDATE upstream_snapshot SET id = :n"), {"n": new_ulid()})
        with pytest.raises(Exception):  # noqa: B017 - any failure to open is the point
            snapshots.get("svc", "svc:1|/series-alpha")
