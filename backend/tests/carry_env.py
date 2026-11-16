"""Harness of the progress carried over a rename plan (M4 L16, EF-EXP-017, EF-EXP-030).

The real exported files and manifest of `export_env`, a simulated Kavita (`tests.fakes.kavita`)
that is re-indexed from the disk at each scan the plan asks for (a renamed folder is a new series
with new chapter ids, and the old one is deleted with every reading of every account), the real
sealed keys of the members, the production adapters (`KavitaProgress`, `KavitaCarry`). A member's
key is drawn at random by the simulated server: no literal key is written. The clock is moved by
hand and nothing sleeps.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from types import SimpleNamespace
from typing import Any

import sqlalchemy as sa

from shelfhold.export.application.rename_plan import ItemMove
from shelfhold.export.targets.kavita import AdminKey, KavitaClient
from shelfhold.export.targets.kavita.carry import KavitaCarry
from shelfhold.export.targets.kavita.progress import KavitaProgress
from shelfhold.identity.adapters.destination_accounts import DestinationAccountStore
from shelfhold.identity.application.ports import AuditEntry
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export
from shelfhold.infrastructure.secrets.keyring import Keyring
from shelfhold.infrastructure.secrets.vault import SecretVault
from shelfhold.reading.application.sync import MemberOutcome, sync_destination
from tests.export_env import ExportEnv, export_env
from tests.fakes.kavita import API_KEY, BASE_URL, FakeKavita, build_fetcher, member
from tests.intents_env import NOW
from tests.rename_env import export_chapters, item_ids_of
from tests.sync_env import store_open

LIBRARY = 1
ARGON = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA"
PAGES = 20


class _Events:
    def emit(self, code: str, **detail: Any) -> None:  # the port of the client: nobody listens
        return None


@dataclass
class CarryEnv:
    env: ExportEnv
    fake: FakeKavita
    store: DestinationAccountStore
    remote: KavitaProgress
    carry: KavitaCarry
    log: list[tuple[Any, ...]] = field(default_factory=list)  # what happened, in order
    keys: dict[str, str] = field(default_factory=dict)  # user id -> the member's key
    names: dict[str, str] = field(default_factory=dict)  # user id -> the Kavita account name
    sleeps: list[float] = field(default_factory=list)  # the waits the carry asked for
    on_sleep: Callable[[], None] | None = None  # what Kavita does meanwhile

    @property
    def engine(self) -> sa.Engine:
        return self.env.engine

    # ---------------------------------------------------------------- people
    def member(
        self, name: str, *, role: str = "member", link: bool = True, kavita: bool = True,
        library_access: bool = True, **extra: Any,
    ) -> str:  # fmt: skip
        """A profile; `kavita` declares its account (the declarative link), `link` gives it its own
        key on the destination, `library_access` lets that account read the library."""
        values: dict[str, Any] = {
            "id": new_ulid(), "username": name, "role": role, "can_rate": role == "admin",
            "password_hash": ARGON, **extra,
        }  # fmt: skip
        if role == "child" and "max_age_rating" not in extra:
            values["max_age_rating"] = 10
        if kavita:
            values["kavita_username"] = f"acct-{name}"
        with write_transaction(self.engine) as connection:
            connection.execute(sa.insert(_user()).values(**values))
        user = str(values["id"])
        if kavita:
            self.fake.users.append(
                member(f"acct-{name}", libraries=(LIBRARY,) if library_access else ())
            )
        if link and kavita:
            self.link(user, f"acct-{name}")
        return user

    def link(self, user: str, kavita_name: str) -> str:
        secret = self.fake.add_member_key(kavita_name, roles=("Login",), age_rating=None)
        self.store.save(
            user, self.env.destination_id, kavita_name, secret, declared=None, now=NOW,
            entry=lambda replaced: AuditEntry(None, "destination_account.linked", NOW),
        )  # fmt: skip
        self.keys[user] = secret
        self.names[user] = kavita_name
        return secret

    # ---------------------------------------------------------------- files and Kavita
    def series(self, title: str, numbers: list[str]) -> dict[str, str]:
        """The exported chapters of a series, indexed by Kavita: `{file name: export item id}`."""
        export_chapters(self.env, title, numbers)
        self.rescan()
        return item_ids_of(self.env, title)

    def remote_path(self, rel: str) -> str:
        return f"/manga/Kavita/{rel}"

    def rescan(self) -> None:
        """What a scan of the library finds on the disk: a folder with no file any more loses its
        series (and the readings of every account with it, as Kavita deletes them), a folder with
        files Kavita does not know makes a **new** series, with new identifiers."""
        fake = self.fake
        on_disk: dict[str, list[str]] = {}
        for path in sorted(self.env.files()):
            rel = path.relative_to(self.env.root)
            if len(rel.parts) == 2 and rel.suffix == ".cbz":
                on_disk.setdefault(rel.parts[0], []).append(self.remote_path(rel.as_posix()))
        for series in list(fake.series):
            folder = PurePosixPath(series["folderPath"]).name
            sid = series["id"]
            if on_disk.get(folder) == fake.files.get(sid):
                continue
            if folder in on_disk and set(fake.files.get(sid, [])) & set(on_disk[folder]):
                fake.files[sid] = on_disk[folder]  # the same folder, other files: same series
                continue
            for index in range(len(fake.files.get(sid, []))):
                for key in [k for k in fake.member_progress if k[1] == sid * 100 + index + 1]:
                    del fake.member_progress[key]  # Kavita deletes the readings with the series
            fake.series.remove(series)
            fake.files.pop(sid, None)
            fake.ages.pop(sid, None)
        known = {PurePosixPath(s["folderPath"]).name for s in fake.series}
        for folder, files in on_disk.items():
            if folder not in known:
                fake.add_series(folder, self.remote_path(folder), files, library_id=LIBRARY)

    def series_ids(self) -> dict[str, int]:
        return {PurePosixPath(s["folderPath"]).name: int(s["id"]) for s in self.fake.series}

    def chapter(self, folder: str, name: str) -> int:
        return self.fake.chapter_id(self.remote_path(f"{folder}/{name}"))

    def kavita_page(self, user: str, folder: str, name: str) -> int:
        got = self.fake.member_progress.get((self.names[user], self.chapter(folder, name)))
        return 0 if got is None else int(got["page"])

    def read_in_kavita(self, user: str, folder: str, name: str, page: int) -> None:
        self.fake.read_in_kavita(self.names[user], self.chapter(folder, name), page)

    # ---------------------------------------------------------------- the plan
    def moves(self, folder: str, new_folder: str) -> list[ItemMove]:
        return [
            ItemMove(item_id, f"{new_folder}/{name}")
            for name, item_id in item_ids_of(self.env, folder).items()
        ]

    def wire(self, *, drain: Callable[[], None] | None = None) -> None:
        """What the composition root does for a rename plan: the carry, the Kavita names, and the
        scan that Kavita answers by indexing the disk again."""
        pipeline = self.env.pipeline
        pipeline.bind_carry(self.carry)
        pipeline.bind_series_names(self.carry)
        pipeline.bind_readers(self.carry.readers(self.env.destination_id))

        def scan() -> None:
            if self.env.scans:
                self.log.append(("scan", self.disk_state()))
                self.env.scans.clear()
                self.rescan()

        pipeline.bind_scan_drain(drain or scan)

    def disk_state(self) -> tuple[str, ...]:
        return tuple(sorted(str(p.relative_to(self.env.root)) for p in self.env.files()))

    # ---------------------------------------------------------------- positions
    def content_hash(self, item: str) -> str:
        with self.engine.connect() as connection:
            return str(
                connection.execute(
                    sa.text(
                        "SELECT c.content_hash FROM export_item i JOIN content c "
                        "ON c.id = i.content_id WHERE i.id = :i"
                    ),
                    {"i": item},
                ).scalar()
            )

    def device_position(self, user: str, item: str, page: int, *, transposed: bool = False) -> str:
        """A report of a reader on the file, at the instant of the hand-moved clock."""
        row_id, now = new_ulid(), self.env.clock.now()
        with write_transaction(self.engine) as connection:
            covered = [
                str(r[0])
                for r in connection.execute(
                    sa.select(export.unit_coverage.c.unit_id)
                    .where(
                        export.unit_coverage.c.carrier_type == "export_item",
                        export.unit_coverage.c.carrier_id == item,
                    )
                    .order_by(export.unit_coverage.c.position)
                )
            ]
            locator: dict[str, Any] = {"page": page}
            if transposed:
                locator["transposed"] = True
            connection.execute(
                sa.text(
                    "INSERT INTO reading_position (id, user_id, carrier_type, carrier_id, "
                    "content_hash, locator, percent, client_ts, server_ts, source, device_id, "
                    "covered_units, read_units) VALUES (:id, :u, 'export_item', :c, :h, :l, :p, "
                    ":t, :t, 'device', 'reader-1', :cu, '[]')"
                ),
                {
                    "id": row_id,
                    "u": user,
                    "c": item,
                    "h": self.content_hash(item),
                    "l": json.dumps(locator),
                    "p": page / PAGES,
                    "t": now,
                    "cu": json.dumps(covered),
                },
            )
        return row_id

    def positions(self, user: str) -> list[SimpleNamespace]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                sa.text("SELECT * FROM reading_position WHERE user_id = :u ORDER BY server_ts, id"),
                {"u": user},
            )
            return [
                SimpleNamespace(**{**r._mapping, "locator": json.loads(r.locator)}) for r in rows
            ]

    def progress(self, user: str) -> dict[str, tuple[str, int]]:
        """`{unit id: (state, approximate)}` of the member, as the view says it."""
        with self.engine.connect() as connection:
            return {
                str(r.unit_id): (str(r.state), int(r.approximate))
                for r in connection.execute(
                    sa.text(
                        "SELECT unit_id, state, approximate FROM reading_progress "
                        "WHERE user_id = :u"
                    ),
                    {"u": user},
                )
            }

    def sync_all(self) -> dict[str, MemberOutcome]:
        return sync_destination(
            self.engine, self.remote, destination_id=self.env.destination_id, clock=self.env.clock
        )

    # ---------------------------------------------------------------- observation
    def watch(self) -> None:
        """Log every reading call of Kavita (`pull`, `push`) with the state of the disk."""

        def hook(recorded: Any) -> None:
            path = recorded.path.lower()
            if path.endswith("/api/reader/get-progress"):
                self.log.append(("pull", self.disk_state()))
            elif path.endswith("/api/reader/progress") and recorded.method == "POST":
                self.log.append(("push", self.disk_state()))

        self.fake.hook = hook

    def kinds(self) -> list[str]:
        return [str(entry[0]) for entry in self.log]


def _user() -> sa.Table:
    from shelfhold.infrastructure.schema import identity

    return identity.user


@contextmanager
def carry_env(*, tries: int = 6) -> Iterator[CarryEnv]:
    with export_env() as env:
        fake = FakeKavita()
        fake.wall = env.clock.now
        fake.libraries = [{**fake.libraries[0], "id": LIBRARY}] if fake.libraries else []
        fetcher = build_fetcher(fake)
        vault = SecretVault(Keyring.generate(NOW))
        store = DestinationAccountStore(env.engine, lambda: vault)
        with write_transaction(env.engine) as connection:
            connection.execute(
                sa.update(export.export_target)
                .where(export.export_target.c.id == env.target_id)
                .values(config={"url": BASE_URL})
            )
            connection.execute(
                sa.update(export.shelf_destination)
                .where(export.shelf_destination.c.id == env.destination_id)
                .values(sync_progress=True)
            )
        admin = KavitaClient(
            fetcher, BASE_URL, AdminKey(API_KEY), events=_Events(), clock=env.clock
        )
        remote = KavitaProgress(
            env.engine,
            admin_client=lambda _target: admin,
            fetcher_for=lambda _url: build_fetcher(fake),
            open_token=lambda user, destination: store_open(store, user, destination),
            sleep=lambda _s: None,
            structure_ttl_s=0.0,  # a test moves Kavita's files at will
        )
        box: list[CarryEnv] = []

        def sleep(seconds: float) -> None:  # nothing sleeps: a test says what Kavita does meanwhile
            box[0].sleeps.append(seconds)
            if box[0].on_sleep is not None:
                box[0].on_sleep()

        carry = KavitaCarry(
            env.engine, remote, env.clock, admin_client=lambda _target: admin, sleep=sleep,
            tries=tries,
        )  # fmt: skip
        made = CarryEnv(env, fake, store, remote, carry)
        box.append(made)
        yield made
