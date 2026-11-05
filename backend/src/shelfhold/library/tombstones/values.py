"""What a tombstone holds, in clear, before it is sealed (EF-BIB-053 (1), EF-BIB-054 (1)).

Raw values only, as received: never a digest, never a title. The normalizers in force at the
purge travel with them, so a later version can still compare under the one that was cited. The
same classes describe a candidate (what the follow cycle, an adoption or an import is about to
bring back), which is compared field by field after re-normalisation (`index`).

The clear text is canonical JSON padded to a multiple of `PAD_TO` bytes, so the length of a
ciphertext does not tell a long series URL from a short one.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal

Scope = Literal["variant", "place", "series_binding", "work"]
SCOPES: Final[tuple[Scope, ...]] = ("variant", "place", "series_binding", "work")
PLAINTEXT_FORMAT: Final = 1
PAD_TO: Final = 256
MAX_FIELD_CHARS: Final = 2048
# The columns of the Edition key that a `place` cites, raw (one list for the purge and the cycles).
EDITION_KEY_COLUMNS: Final = (
    "work_id", "lang", "translation_type", "media", "publisher", "text_version", "performance",
    "packaging",
)  # fmt: skip


class TombstoneValueError(ValueError):
    """A decrypted clear text that does not follow the schema (treated as an inert line)."""


@dataclass(frozen=True)
class ContentHash:
    by: str  # `content_hash_by`, e.g. `content-hash@1`
    value: str  # `sha256:<hex>`


@dataclass(frozen=True)
class VariantRef:
    """`variant`: the upstream reference as received, and the content hashes with their version."""

    source_key: str
    sub_source: str
    remote_unit_id: str
    content_hashes: tuple[ContentHash, ...] = ()
    derived_hashes: tuple[ContentHash, ...] = ()

    scope: Scope = field(default="variant", init=False)


@dataclass(frozen=True)
class PlaceRef:
    """`place`: raw Edition key (sentinel columns included), track, segment, raw number, part."""

    edition_key: tuple[tuple[str, str], ...]
    track: str
    number_segment: str
    number_raw: str
    part: str

    scope: Scope = field(default="place", init=False)

    def __post_init__(self) -> None:
        # Without the Work and the language a place would match chapter N of every Work.
        key = dict(self.edition_key)
        if not key.get("work_id") or not key.get("lang"):
            raise TombstoneValueError("a place names its Work (work_id) and its language (lang)")

    @classmethod
    def of(
        cls,
        edition_key: Mapping[str, str],
        *,
        track: str,
        number_segment: str,
        number_raw: str,
        part: str,
    ) -> "PlaceRef":
        return cls(tuple(sorted(edition_key.items())), track, number_segment, number_raw, part)


@dataclass(frozen=True)
class SeriesBindingRef:
    """`series_binding`: the stable upstream series key (Suwayomi: `sourceId` + `manga.url`)."""

    source_key: str
    sub_source: str
    series_ref: str

    scope: Scope = field(default="series_binding", init=False)


@dataclass(frozen=True)
class WorkRef:
    """`work`: an external identifier of the Work (M2)."""

    id_scheme: str
    value: str

    scope: Scope = field(default="work", init=False)


TombstoneValues = VariantRef | PlaceRef | SeriesBindingRef | WorkRef


def _hashes(items: tuple[ContentHash, ...]) -> list[dict[str, str]]:
    return [{"by": h.by, "value": h.value} for h in items]


def _fields(values: TombstoneValues) -> dict[str, Any]:
    if isinstance(values, VariantRef):
        return {
            "source_key": values.source_key,
            "sub_source": values.sub_source,
            "remote_unit_id": values.remote_unit_id,
            "content_hashes": _hashes(values.content_hashes),
            "derived_hashes": _hashes(values.derived_hashes),
        }
    if isinstance(values, PlaceRef):
        return {
            "edition_key": dict(values.edition_key),
            "track": values.track,
            "number_segment": values.number_segment,
            "number_raw": values.number_raw,
            "part": values.part,
        }
    if isinstance(values, SeriesBindingRef):
        return {
            "source_key": values.source_key,
            "sub_source": values.sub_source,
            "series_ref": values.series_ref,
        }
    return {"id_scheme": values.id_scheme, "value": values.value}


def encode_plaintext(values: TombstoneValues, normalizers: Mapping[str, str]) -> bytes:
    body = {
        "f": PLAINTEXT_FORMAT,
        "scope": values.scope,
        "values": _fields(values),
        "normalizers": dict(sorted(normalizers.items())),
    }
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    raw = text.encode("utf-8")
    padding = (-(len(raw) + len(',"pad":"') + 1)) % PAD_TO  # `}` becomes `,"pad":"…"}`
    padded = text[:-1] + ',"pad":"' + " " * padding + '"}'
    return padded.encode("utf-8")


def _str(data: Mapping[str, Any], name: str) -> str:
    value = data.get(name)
    if not isinstance(value, str) or len(value) > MAX_FIELD_CHARS:
        raise TombstoneValueError(f"{name} is not a bounded string")
    return value


def _hash_list(data: Mapping[str, Any], name: str) -> tuple[ContentHash, ...]:
    items = data.get(name)
    if not isinstance(items, list) or len(items) > 16:
        raise TombstoneValueError(f"{name} is not a bounded list")
    out: list[ContentHash] = []
    for item in items:
        if not isinstance(item, dict) or set(item) != {"by", "value"}:
            raise TombstoneValueError(f"{name} entries are {{by, value}}")
        out.append(ContentHash(_str(item, "by"), _str(item, "value")))
    return tuple(out)


def _exact_keys(data: Mapping[str, Any], keys: set[str]) -> None:
    if set(data) != keys:
        raise TombstoneValueError("unexpected or missing fields")


def decode_plaintext(raw: bytes, scope: str) -> tuple[TombstoneValues, dict[str, str]]:
    """Strict: a clear text of another scope than the authenticated one is refused too."""
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise TombstoneValueError("not JSON") from exc
    if not isinstance(body, dict):
        raise TombstoneValueError("not an object")
    _exact_keys(body, {"f", "scope", "values", "normalizers", "pad"})
    if body["f"] != PLAINTEXT_FORMAT or body["scope"] != scope:
        raise TombstoneValueError("format or scope mismatch")
    data, normalizers = body["values"], body["normalizers"]
    if not isinstance(data, dict) or not isinstance(normalizers, dict):
        raise TombstoneValueError("values and normalizers are objects")
    cited = {_str({"k": k}, "k"): _str(normalizers, k) for k in normalizers}
    values: TombstoneValues
    if scope == "variant":
        _exact_keys(data, {"source_key", "sub_source", "remote_unit_id", "content_hashes",
                           "derived_hashes"})  # fmt: skip
        values = VariantRef(
            _str(data, "source_key"),
            _str(data, "sub_source"),
            _str(data, "remote_unit_id"),
            _hash_list(data, "content_hashes"),
            _hash_list(data, "derived_hashes"),
        )
    elif scope == "place":
        _exact_keys(data, {"edition_key", "track", "number_segment", "number_raw", "part"})
        key = data["edition_key"]
        if not isinstance(key, dict) or len(key) > 32:
            raise TombstoneValueError("edition_key is a bounded object")
        values = PlaceRef.of(
            {_str({"k": k}, "k"): _str(key, k) for k in key},
            track=_str(data, "track"),
            number_segment=_str(data, "number_segment"),
            number_raw=_str(data, "number_raw"),
            part=_str(data, "part"),
        )
    elif scope == "series_binding":
        _exact_keys(data, {"source_key", "sub_source", "series_ref"})
        values = SeriesBindingRef(
            _str(data, "source_key"), _str(data, "sub_source"), _str(data, "series_ref")
        )
    elif scope == "work":
        _exact_keys(data, {"id_scheme", "value"})
        values = WorkRef(_str(data, "id_scheme"), _str(data, "value"))
    else:
        raise TombstoneValueError("unknown scope")
    return values, cited
