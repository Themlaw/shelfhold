"""What the tombstone service remembers between two runs, in the database (EF-BIB-054, M1b L26).

Everything here is one `app_setting` row per root (no migration, the format of the waiting marks
of `export.application.waiting_marks`, whose key prefix lives in `infrastructure.freeze`):

- `tombstone.chain.<root id>`: the chained label of the root's journal (`chain.ChainState`), the
  authority against which a journal is judged truncated;
- `waiting.tombstone_journal_truncated.<root id>`: the root waits for `tombstone
  set-aside-journal` (D12), which is what `status` lists and what blocks its reconcile;
- `tombstone.rekey_owed.<root id>`: a `tombstone rekey` has resealed the database but this root's
  journal still holds lines under a compromised key (the root was away, or the rewrite was
  interrupted): it is rewritten first when the root is replayed, and no other write is accepted
  on it until then;
- the intent of a rewrite (`fs_intent`, op `tombstone_rekey`).
"""

import json
from datetime import datetime
from typing import Final

import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.infrastructure.freeze import WAITING_PREFIX
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import coherence, platform
from shelfhold.library.tombstones.chain import ChainState

__all__ = ["TRUNCATED_STATE", "TombstoneState"]

TRUNCATED_STATE: Final = "tombstone_journal_truncated"
_CHAIN: Final = "tombstone.chain."
_REKEY_OWED: Final = "tombstone.rekey_owed."
_TRUNCATED: Final = f"{WAITING_PREFIX}{TRUNCATED_STATE}."
INTENT_SCHEMA_VERSION: Final = "1"  # that of `export.application.intents` (a test pins it)
_setting = platform.app_setting


class TombstoneState:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # -- rows -------------------------------------------------------------------------------

    def _get(self, key: str) -> str | None:
        with self._engine.connect() as connection:
            return connection.execute(
                sa.select(_setting.c.value).where(_setting.c.key == key)
            ).scalar()

    def _put(self, key: str, value: str) -> bool:
        """True when the row is new."""
        with self._engine.begin() as connection:
            known = connection.execute(
                sa.select(_setting.c.key).where(_setting.c.key == key)
            ).first()
            connection.execute(
                sqlite_insert(_setting)
                .values(key=key, value=value)
                .on_conflict_do_update(index_elements=["key"], set_={"value": value})
            )
        return known is None

    def _delete(self, key: str) -> bool:
        with self._engine.begin() as connection:
            return bool(
                connection.execute(sa.delete(_setting).where(_setting.c.key == key)).rowcount
            )

    def _subjects(self, prefix: str) -> set[str]:
        with self._engine.connect() as connection:
            keys = connection.execute(
                sa.select(_setting.c.key).where(
                    sa.func.substr(_setting.c.key, 1, len(prefix)) == prefix
                )
            ).scalars()
            return {key[len(prefix) :] for key in keys}

    # -- the chained label ------------------------------------------------------------------

    def chain(self, root_id: str) -> ChainState | None:
        raw = self._get(_CHAIN + root_id)
        if raw is None:
            return None
        try:
            return ChainState.from_json(json.loads(raw))
        except ValueError:
            return None

    def set_chain(self, root_id: str, state: ChainState | None) -> None:
        """None forgets it: the next replay takes the journal as it is (a root stamped again, a
        journal put aside)."""
        if state is None:
            self._delete(_CHAIN + root_id)
        else:
            self._put(_CHAIN + root_id, json.dumps(state.to_json(), separators=(",", ":")))

    # -- the waiting mark of a truncated journal --------------------------------------------

    def mark_truncated(self, root_id: str, *, announce: bool) -> bool:
        """Record that the root waits. True when its alert is still to be sent, by the caller that
        asked to `announce` it (a replay): `status` marks without announcing (it must not log or
        publish), and the alert goes out at the next replay, once."""
        reason = "the journal stops before the last line it held"
        key = _TRUNCATED + root_id
        raw = self._get(key)
        done = False
        if raw is not None:
            try:
                done = json.loads(raw).get("announced") == "yes"
            except (ValueError, AttributeError):
                done = False
        if done:
            return False
        sent = announce
        self._put(key, json.dumps({"reason": reason, "announced": "yes" if sent else "no"}))
        return sent

    def clear_truncated(self, root_id: str) -> bool:
        return self._delete(_TRUNCATED + root_id)

    def truncated(self) -> set[str]:
        return self._subjects(_TRUNCATED)

    def is_truncated(self, root_id: str) -> bool:
        return self._get(_TRUNCATED + root_id) is not None

    # -- the rewrite still owed to a root ---------------------------------------------------

    def owe_rekey(self, root_id: str) -> None:
        self._put(_REKEY_OWED + root_id, "{}")

    def rekey_owed(self, root_id: str) -> bool:
        return self._get(_REKEY_OWED + root_id) is not None

    def rekey_owed_roots(self) -> set[str]:
        return self._subjects(_REKEY_OWED)

    def clear_rekey_owed(self, root_id: str) -> None:
        self._delete(_REKEY_OWED + root_id)

    # -- the intent of a rewrite ------------------------------------------------------------

    def record_intent(self, *, src: str, dst: str, sha256: str) -> str:
        """Step one of a rewrite: durable before the disk is touched. `src` is the new file,
        `dst` the journal it replaces, `sha256` that of the new file."""
        intent_id = new_ulid()
        with self._engine.begin() as connection:
            epoch = connection.execute(sa.select(platform.instance.c.restore_epoch)).scalar()
            connection.execute(
                sa.insert(coherence.fs_intent).values(
                    id=intent_id, op="tombstone_rekey",
                    intent_schema_version=INTENT_SCHEMA_VERSION, src_path=src, dst_path=dst,
                    expected_sha256=f"sha256:{sha256}", state="applying", epoch=int(epoch or 0),
                )
            )  # fmt: skip
        return intent_id

    def close_intent(self, intent_id: str, state: str, now: datetime) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                sa.update(coherence.fs_intent)
                .where(coherence.fs_intent.c.id == intent_id)
                .where(coherence.fs_intent.c.state.in_(("pending", "applying")))
                .values(state=state, resolved_at=now)
            )
