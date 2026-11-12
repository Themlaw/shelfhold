"""A synthetic instance for the deletion tests (L25, EF-BIB-021): the exported library of
`export_env` (real files, real SQLite, real pipeline), one Unit with a copy in /library, in
/store and in the destination, and the real `DeletionService` over them. Nothing is mocked."""

import hashlib
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path

import sqlalchemy as sa

from shelfhold.export.application.deletion import DeletionService
from shelfhold.export.application.intents import IntentJournal
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.library.public import DeletionLevel, DeletionPlan, Safeguard, StoreIndex
from tests.export_env import ExportEnv, Series
from tests.null_tombstones import NONE_PURGED
from tests.purge_env import ADMIN


def file_hash(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass
class Deletion:
    env: ExportEnv
    service: DeletionService
    index: StoreIndex
    scans: list[str] = field(default_factory=list)
    order: list[str] = field(default_factory=list)  # `scan:<folder exists>` as it happened

    @property
    def now(self):  # type: ignore[no-untyped-def]
        return self.env.clock.now()

    def tree(self) -> dict[str, str]:
        """Every regular file of the two roots, bins included: path -> hash."""
        found: dict[str, str] = {}
        for root in (self.env.lib.library, self.env.lib.store):
            for path in sorted(root.rglob("*")):
                if path.is_file() and path.name not in {".shelfhold-mount-id", "INDEX.jsonl"}:
                    rel = path.relative_to(root).parts
                    if rel[:2] in {(".shelfhold", "tombstones"), (".shelfhold", "lease")}:
                        continue
                    found[f"{root.name}/{path.relative_to(root).as_posix()}"] = file_hash(path)
        return found

    def binned(self) -> list[Path]:
        return sorted(
            p
            for root in (self.env.lib.library, self.env.lib.store)
            for p in (root / ".shelfhold" / "recycle").rglob("*")
            if p.is_file()
        )

    def rows(self, sql: str, **params: object) -> list[tuple[object, ...]]:
        with self.env.engine.connect() as connection:
            return [tuple(r) for r in connection.execute(sa.text(sql), params)]

    def dump(self) -> str:
        """Every table that a deletion could touch, as text: the same before and after a preview."""
        parts = []
        for table in (
            "replica", "export_record", "export_item", "trash_item", "fs_intent", "audit",
            "unit", "content", "resource_lock",
        ):  # fmt: skip
            parts.append(repr(self.rows(f"SELECT * FROM {table} ORDER BY 1")))  # noqa: S608
        return "\n".join(parts)

    def apply(self, level: DeletionLevel, units: list[str], **kw: object) -> object:
        plan = kw.pop("plan", None) or self.service.preview(level, units)
        assert isinstance(plan, DeletionPlan)
        kw.setdefault("principal", ADMIN)
        kw.setdefault("typed", None)
        kw.setdefault("if_match", plan.version)
        return self.service.apply(level, units, now=self.now, **kw)  # type: ignore[arg-type]


def make_deletion(env: ExportEnv, *, fail_scan: bool = False) -> Deletion:
    holder: list[Deletion] = []

    def scan(remote: str) -> None:
        deletion = holder[0]
        deletion.scans.append(remote)
        # the folder a scan is asked about must still be there: nothing is removed before it
        local = env.lib.manga / remote.removeprefix("/manga/").strip("/")
        deletion.order.append(f"scan:{local.exists()}")
        if fail_scan:
            raise RuntimeError("Kavita is down")

    index = StoreIndex(
        env.lib.engine, env.lib.fsops, str(env.lib.store), NONE_PURGED,
        hold=nullcontext,
    )  # fmt: skip
    service = DeletionService(
        env.lib.engine, env.lib.fsops, clock=env.clock, locate=env.lib.locate,
        request_scan=scan, refresh_index=index.rewrite, fresh_auth_s=900,
    )  # fmt: skip
    deletion = Deletion(env, service, index)
    holder.append(deletion)
    return deletion


def spread_unit(env: ExportEnv, series: Series, number: str = "1") -> str:
    """One Unit with a copy in /library, one in /store and one served in the destination."""
    lib = env.lib
    unit = env.add_chapter(series, number)
    Safeguard(lib.engine, lib.fsops, lib.locate, str(lib.store), lib.data).run(unit)
    env.pipeline.run(env.destination_id, job_id=new_ulid())
    return unit


def make_irreplaceable(env: ExportEnv, unit: str) -> None:
    """The source no longer lists the Unit: nothing upstream can give it back (EF-BIB-031)."""
    with env.engine.begin() as connection:
        connection.execute(
            sa.text("UPDATE unit SET availability = 'removed' WHERE id = :u"), {"u": unit}
        )


def open_intents(env: ExportEnv) -> list[str]:
    return [i.id for i in IntentJournal(env.engine, env.lib.fsops).open_intents()]
