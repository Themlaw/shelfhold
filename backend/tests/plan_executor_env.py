"""The destination plan executor over a real database and a real library (M4 L04).

A destination here is a plain function that returns a plan; the executor never trusts it. The
content the host owns is a dictionary, the instance probe answers what a test says. Nothing else is
faked: the intents, the files and the pause of the target are the real ones.
"""

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import sqlalchemy as sa

from shelfhold.export.application.intents import IntentJournal
from shelfhold.export.application.plan_executor import (
    PlanBinding,
    PlanExecutor,
    PlannedLedger,
)
from shelfhold.export.domain.trees import TreeFolder
from shelfhold.export.targets.kavita.destination import KavitaDestination
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export
from shelfhold.sdk.fsops import ContentRef, PlannedContent
from tests.export_env import ROOT_SUBPATH, ExportEnv

BOOK_A = "01JBZ3X5QH8M2N4P6R7S9T0V1A"
BOOK_B = "01JBZ3X5QH8M2N4P6R7S9T0V1B"
BYTES = {BOOK_A: b"volume one " * 200, BOOK_B: b"volume two " * 200}


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def ref(item: str) -> ContentRef:
    return ContentRef(kind="export_item", ref=item)


class FixedContents:
    """The bytes the host owns, whatever the binding (the executor decides who may cite them)."""

    def __init__(self, data: dict[str, bytes] | None = None) -> None:
        self.data = dict(BYTES if data is None else data)
        self.reads: list[tuple[str, str]] = []

    def read(self, destination_id: str, content: ContentRef) -> bytes:
        self.reads.append((destination_id, content.ref))
        return self.data[content.ref]


class Probe:
    """What the server answers about itself right now."""

    def __init__(self, fingerprint: str | None) -> None:
        self.current = fingerprint

    def fingerprint(self, target_id: str) -> str | None:
        return self.current


@dataclass
class ExecEnv:
    env: ExportEnv
    executor: PlanExecutor
    ledger: PlannedLedger
    contents: FixedContents
    probe: Probe
    journal: IntentJournal
    binding: PlanBinding
    tree: TreeFolder
    calls: list[str] = field(default_factory=list)

    @property
    def root(self) -> Path:
        return self.env.root

    def plan_for(
        self,
        call_id: str,
        *refs: str,
        binding: PlanBinding | None = None,
        egress: str = "foyer",
        watermarked: frozenset[str] = frozenset(),
    ) -> None:
        target = binding or self.binding
        self.ledger.plan(
            call_id,
            target.destination_id,
            PlannedContent(
                planned_refs=frozenset(refs),
                egress=egress,  # type: ignore[arg-type]
                watermarked_refs=watermarked,
            ),
        )


FINGERPRINT = "sha256:" + "a" * 64


def tree_row(
    env: ExportEnv,
    *,
    subpath: str,
    role: str = "primary",
    destination_id: str | None = None,
    target_id: str | None = None,
) -> TreeFolder:
    """A `tree` and its `tree_indexer` for a destination of `env` (its own by default)."""
    root_id = env.lib.library_root
    tree_id = new_ulid()
    with write_transaction(env.engine) as connection:
        connection.execute(
            sa.insert(export.tree).values(id=tree_id, root_id=root_id, root_subpath=subpath)
        )
        connection.execute(
            sa.insert(export.tree_indexer).values(
                tree_id=tree_id,
                shelf_destination_id=destination_id or env.destination_id,
                target_id=target_id or env.target_id,
                role=role,
            )
        )
    return TreeFolder(root_id=root_id, subpath=subpath, tree_id=tree_id)


def make_exec_env(
    env: ExportEnv, *, role: str = "primary", fingerprint: str | None = None
) -> ExecEnv:
    tree = tree_row(env, subpath=f"manga/{ROOT_SUBPATH}", role=role)
    journal = IntentJournal(env.engine, env.lib.fsops, clock=env.clock)
    ledger, contents = PlannedLedger(), FixedContents()
    probe = Probe(fingerprint or FINGERPRINT)
    executor = PlanExecutor(
        env.engine, env.lib.fsops, journal, ledger=ledger, contents=contents, probe=probe,
        clock=env.clock,
    )  # fmt: skip
    binding = PlanBinding(
        destination_id=env.destination_id,
        target_id=env.target_id,
        capabilities=KavitaDestination.capabilities,
    )
    return ExecEnv(env, executor, ledger, contents, probe, journal, binding, tree)


def second_binding(ex: ExecEnv, *, new_target: bool) -> PlanBinding:
    """Another binding of the library: on the same server (another shelf) or on another server."""
    env = ex.env
    target_id, shelf_id, destination_id = (new_ulid(), new_ulid(), new_ulid())
    (env.lib.manga / "Other").mkdir(exist_ok=True)
    with write_transaction(env.engine) as connection:
        if new_target:
            connection.execute(
                sa.insert(export.export_target).values(
                    id=target_id, kind="kavita", name="Second server", path_map=[]
                )
            )
        else:
            target_id = env.target_id
        connection.execute(
            sa.insert(export.shelf).values(
                id=shelf_id, name=f"Shelf {shelf_id[-6:]}", phrase="Other", max_age_rating=99
            )
        )
        connection.execute(
            sa.insert(export.shelf_destination).values(
                id=destination_id, shelf_id=shelf_id, target_id=target_id, transport="tree",
                egress="foyer", root_subpath="Other", template="{series}",
                template_origin="default@1",
            )
        )  # fmt: skip
    tree_row(env, subpath="manga/Other", destination_id=destination_id, target_id=target_id)
    return PlanBinding(
        destination_id=destination_id,
        target_id=target_id,
        capabilities=KavitaDestination.capabilities,
    )
