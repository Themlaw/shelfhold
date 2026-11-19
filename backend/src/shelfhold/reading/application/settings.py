"""The settings of the reader (M5 L13, EF-LEC-006): the priority is per type, the portée is asked.

`reader_settings` holds the choices of one profile. Two types of setting, two priority rules
(EF-LEC-006):

* **direction** (the sens and the mode of a book): a value pinned to a series beats the profile's
  own default (« série > mode »). The key is the series' identifier, or `default` for the
  profile's own; a profile that chose nothing reads `None` and the publication's direction wins
  (the Œuvre's own, ADR-0055).
* **comfort**: a value of one device beats the default (« appareil > défaut »).

The portée — « pour cet appareil » ou « partout » — is part of the write, of every kind: a row
of one device is read on that device alone, and inside the same key a device's row beats one
written « partout » (« appareil > partout »).

The choices of a profile stay in that profile (a child's too): every read and write is anchored to
`user_id`, and no setting widens what parental control hides — a row says how a book is drawn,
never who may read it; the doors that serve bytes keep checking visibility on their own. A series
is named by its identifier, and only a series the profile may see gets a row: a key that names a
Work she cannot see, or that names nothing, answers alike and leaves nothing behind (no oracle
tells a hidden Œuvre from an absent one). A profile holds at most `MAX_SETTINGS` rows: beyond the
bound the write of a place she does not hold yet is a typed refusal.

Only known choices are taken: a direction is `ltr`, `rtl` or `webtoon`, a comfort setting is one
of `COMFORT_KEYS` with its closed values. A row holds one value per profile and place: writing
again replaces it.
"""

from dataclasses import dataclass
from typing import Final

import sqlalchemy as sa
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.catalog.public import visible_work_ids
from shelfhold.identity.public import reader_scope
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import is_ulid, new_ulid
from shelfhold.infrastructure.schema import reading

__all__ = [
    "COMFORT_KEYS",
    "COMFORT_VALUES",
    "DIRECTIONS",
    "MAX_SETTINGS",
    "DeviceScopeError",
    "Effective",
    "InvalidSettingError",
    "ReaderSettings",
    "SettingsLimitError",
]

DIRECTIONS: Final = ("ltr", "rtl", "webtoon")  # the same list as a publication's manifest
COMFORT_KEYS: Final = ("fit",)  # how a page fills the screen
COMFORT_VALUES: Final = {"fit": ("contain", "width")}
DEFAULT_KEY: Final = "default"  # the profile's own, when no series is named
MAX_SETTINGS: Final = 500  # rows a profile may hold: a choice exists only for a real place


class InvalidSettingError(ValueError):
    """The type, the key or the value is not one of the reader's settings."""


class DeviceScopeError(ValueError):
    """« pour cet appareil » without a device, or « partout » with one."""


class SettingsLimitError(ValueError):
    """The profile already holds `MAX_SETTINGS` rows; a held place still replaces its value."""


@dataclass(frozen=True)
class Effective:
    """What the reader reads with: a direction (`None`: the publication's own wins) and the
    comfort of the device it is drawn on."""

    direction: str | None
    comfort: dict[str, str]


def _checked(kind: str, key: str, value: str) -> None:
    if kind == "direction":
        if value not in DIRECTIONS or not (key == DEFAULT_KEY or is_ulid(key)):
            raise InvalidSettingError
    elif kind == "comfort":
        if key not in COMFORT_KEYS or value not in COMFORT_VALUES.get(key, ()):
            raise InvalidSettingError
    else:
        raise InvalidSettingError


class ReaderSettings:
    def __init__(self, engine: sa.Engine) -> None:
        self._engine = engine

    # -- what is effective --------------------------------------------------------------------

    def resolve(
        self, *, user_id: str, series_id: str | None = None, device_id: str | None = None
    ) -> Effective:
        """The settings of `user_id` alone: the direction of `series_id` (her default when the
        series has no row of its own), and the comfort of `device_id` (the default of every
        device when it has none). Only the useful rows are read — the asked series, her own
        default, and the rows of the asked device — and a row of another device decides nothing
        here: the portée is part of the read as it is of the write."""
        keys = [DEFAULT_KEY, *([series_id] if series_id else [])]
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(
                    reading.reader_setting.c.kind,
                    reading.reader_setting.c.key,
                    reading.reader_setting.c.scope,
                    reading.reader_setting.c.value,
                ).where(
                    reading.reader_setting.c.user_id == user_id,
                    sa.or_(
                        sa.and_(
                            reading.reader_setting.c.kind == "direction",
                            reading.reader_setting.c.key.in_(keys),
                        ),
                        reading.reader_setting.c.kind == "comfort",
                    ),
                    # a row of the asked device, or one written « partout »: nothing else is read
                    sa.or_(
                        reading.reader_setting.c.scope == "everywhere",
                        reading.reader_setting.c.device_id == device_id,
                    ),
                )
            ).all()
        pinned: dict[str, str | None] = dict.fromkeys(keys)
        comfort: dict[str, str] = {}
        default_comfort: dict[str, str] = {}
        for kind, key, scope, value in rows:
            if kind == "direction":
                if pinned[key] is None or scope == "device":
                    pinned[key] = value  # inside one key: the device's row beats « partout »
            elif scope == "device":
                comfort[key] = value  # appareil > défaut: this device's row wins
            else:
                default_comfort.setdefault(key, value)
        direction = pinned[series_id] if series_id is not None else None
        if direction is None:  # série > mode: the series' own row beats the profile's default
            direction = pinned[DEFAULT_KEY]
        for key, value in default_comfort.items():
            comfort.setdefault(key, value)
        return Effective(direction=direction, comfort=comfort)

    # -- writing ------------------------------------------------------------------------------

    def set(
        self,
        *,
        user_id: str,
        kind: str,
        key: str,
        value: str,
        scope: str,
        device_id: str | None = None,
    ) -> None:
        """Write one choice of `user_id`, with its portée: `device` names the device the choice
        belongs to, `everywhere` names none. The same place written again replaces its value.

        A direction pinned to a series is kept only for a series the profile may see: a key that
        names a Work she cannot see, or that names nothing, leaves no row behind and answers
        alike (no oracle). Beyond `MAX_SETTINGS` rows, the write of a place she does not hold yet
        is refused.

        Raises `InvalidSettingError` (unknown type, key or value), `DeviceScopeError` (a portée
        that does not say what it names) and `SettingsLimitError` (the bound is reached)."""
        _checked(kind, key, value)
        if scope == "device":
            if not device_id:
                raise DeviceScopeError
        elif scope == "everywhere":
            if device_id is not None:
                raise DeviceScopeError
        else:
            raise InvalidSettingError
        with write_transaction(self._engine) as connection:
            if kind == "direction" and key != DEFAULT_KEY and not visible_work_ids(
                connection, reader_scope(connection, user_id), [key]
            ):  # fmt: skip
                return  # a series she cannot see answers like one that is not there
            held = connection.execute(
                sa.select(sa.literal(1)).where(
                    reading.reader_setting.c.user_id == user_id,
                    reading.reader_setting.c.kind == kind,
                    reading.reader_setting.c.key == key,
                    reading.reader_setting.c.scope == scope,
                    reading.reader_setting.c.device_id == device_id,
                )
            ).first()
            if held is None:
                count = connection.execute(
                    sa.select(sa.func.count())
                    .select_from(reading.reader_setting)
                    .where(reading.reader_setting.c.user_id == user_id)
                ).scalar_one()
                if count >= MAX_SETTINGS:
                    raise SettingsLimitError
            connection.execute(
                sqlite_insert(reading.reader_setting)
                .values(
                    id=new_ulid(), user_id=user_id, scope=scope, device_id=device_id,
                    kind=kind, key=key, value=value,
                )
                .on_conflict_do_update(
                    index_elements=[
                        reading.reader_setting.c.user_id,
                        reading.reader_setting.c.kind,
                        reading.reader_setting.c.key,
                        reading.reader_setting.c.scope,
                        sa.text("ifnull(device_id, '')"),
                    ],
                    set_={
                        reading.reader_setting.c.value: value,
                        reading.reader_setting.c.updated_at: sa.func.current_timestamp(),
                    },
                )
            )  # fmt: skip
