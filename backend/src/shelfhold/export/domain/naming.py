"""Kavita naming rules (EF-EXP-004, EF-EXP-013, EF-EXP-014, EF-EXP-045, EF-EXP-061).

Pure: no file system, no network, no catalog import (contexts stay independent). A path is a
function of the attributes frozen at the first export (`FrozenAttrs`, invariant I22) and of the
naming template recorded with them (`template_id@version`). It never depends on a title, a tome
attributed later, or any other mutable attribute.

What the rules protect against, all about what Kavita's `plugin/parse` reads in a NAME:

- An upstream chapter title never enters a file name (it only goes to ComicInfo `Title`): a title
  such as `Chapter 311 - {S1 END}` holds a season marker, which Kavita reads as a volume.
- `Vol.X` names closed volumes only (`kind = volume`); a chapter name carries no volume at all.
- Chapter 0 and every non-main track are specials, in the `Specials` folder. The series cover is
  taken from chapter 1 by the API, never from a special.
- Two files written by Shelfhold never share a parsed number in a series folder. A non-empty
  numbering segment (a webtoon season restarting at 1) is carried by the exported SERIES name
  (`Tower of God Arc 2`), one Kavita series per segment (Q50, option 1, decided by the
  `plugin/parse` golden): the segment is never a `S2` / `Season 2` / `Vol.2` marker.
"""

import json
import re
import unicodedata
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final, Protocol

# -- name sanitising (EF-EXP-013) ---------------------------------------------------------------

MAX_COMPONENT_BYTES: Final = 200  # well under the 255 bytes of ext4 / SMB, room for a suffix
_FORBIDDEN: Final = re.compile(r'[/\\:*?"<>|]')
_DROPPED_CATEGORIES: Final = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
_RESERVED: Final = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }
)
_BLANKS: Final = re.compile(r"\s+")


def _truncate(text: str, max_bytes: int) -> str:
    while len(text.encode("utf-8")) > max_bytes:
        text = text[:-1]
    return text


def sanitize_name(text: str, *, max_bytes: int = MAX_COMPONENT_BYTES) -> str:
    """One path component valid on ext4 and SMB: NFC, no forbidden or control character, no
    leading or trailing blank or dot, no device name, never empty, at most `max_bytes` bytes.
    Idempotent."""
    if max_bytes < 8:
        raise ValueError("max_bytes must leave room for a name")
    cleaned = unicodedata.normalize("NFC", text)
    cleaned = "".join(c for c in cleaned if unicodedata.category(c) not in _DROPPED_CATEGORIES)
    cleaned = _BLANKS.sub(" ", _FORBIDDEN.sub(" ", cleaned))
    for _ in range(3):
        cleaned = unicodedata.normalize("NFC", _truncate(cleaned.strip(" ."), max_bytes))
        cleaned = _BLANKS.sub(" ", cleaned).strip(" .")
        if not cleaned:
            return "_"
        if cleaned.split(".", 1)[0].strip().lower() in _RESERVED:
            cleaned = f"{cleaned.split('.', 1)[0].rstrip()}_" + (
                "." + cleaned.split(".", 1)[1] if "." in cleaned else ""
            )
        if len(cleaned.encode("utf-8")) <= max_bytes:
            return cleaned
    return _truncate(cleaned, max_bytes).strip(" .") or "_"


_KEEP: Final = frozenset("+!" + chr(0xFF0A) + chr(0xFF01) + chr(0xFF0B))


def normalize_series_name(name: str) -> str:
    """The series key Kavita derives from a name: letters, digits and a few signs, lowercased.

    Case, punctuation and typographic apostrophes do not tell two series apart.
    """
    folded = unicodedata.normalize("NFC", name).casefold()
    return "".join(
        c for c in folded if unicodedata.category(c).startswith("L") or c.isdecimal() or c in _KEEP
    )


# -- templates (EF-EXP-004) ---------------------------------------------------------------------


@dataclass(frozen=True)
class NamingTemplate:
    """The naming rules of a binding. Copied into the binding when it is created: a new default
    is proposed with a preview and never applied by itself."""

    template_id: str
    version: int
    chapter: str = "{series} - Ch.{number}"
    volume: str = "{series} - Vol.{number}"
    special: str = "{series} - Special {label}"
    special_dir: str = "Specials"
    segment: str = "{series} Arc {n}"

    @property
    def ref(self) -> str:
        return f"{self.template_id}@{self.version}"

    def dumps(self) -> str:
        data = {
            "template_id": self.template_id, "version": self.version,
            "chapter": self.chapter, "volume": self.volume, "special": self.special,
            "special_dir": self.special_dir, "segment": self.segment,
        }  # fmt: skip
        return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def loads(cls, text: str) -> "NamingTemplate":
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("a naming template is a JSON object")
        return cls(**data)


DEFAULT_TEMPLATE: Final = NamingTemplate(template_id="kavita-manga", version=1)
DEFAULT_TEMPLATE_REF: Final = DEFAULT_TEMPLATE.ref


@dataclass(frozen=True)
class BindingTemplate:
    """What `shelf_destination.template` and `.template_origin` hold."""

    text: str
    origin: str  # default@<version>


def copy_template_into_binding(default: NamingTemplate) -> BindingTemplate:
    return BindingTemplate(text=default.dumps(), origin=f"default@{default.version}")


# -- frozen attributes and paths (EF-EXP-061, I22) ----------------------------------------------

TRACKS: Final = ("main", "annual", "special", "extra", "oneshot", "unnumbered")
KINDS: Final = ("unit", "volume")
_NUMBER: Final = re.compile(r"\d+(?:\.\d+)?")
_TRACK_WORD: Final = {
    "annual": "Annual",
    "extra": "Extra",
    "oneshot": "Oneshot",
    "unnumbered": "Item",
}


@dataclass(frozen=True)
class FrozenAttrs:
    """The only inputs of a path, fixed at the first export of an `export_item`."""

    series: str  # the exported series name, frozen (EF-EXP-052)
    track: str
    number_norm: str
    part: int = 1
    segment: str = ""
    kind: str = "unit"
    source_order: int = 0
    template_ref: str = DEFAULT_TEMPLATE_REF

    def __post_init__(self) -> None:
        if self.track not in TRACKS:
            raise ValueError(f"unknown track {self.track!r}")
        if self.kind not in KINDS:
            raise ValueError(f"unknown kind {self.kind!r}")
        if self.part < 1:
            raise ValueError("part starts at 1")
        if self.number_norm and not _NUMBER.fullmatch(self.number_norm):
            raise ValueError(f"number_norm {self.number_norm!r} is not a plain decimal")
        if not self.number_norm and self.track == "main":
            raise ValueError("a main-track unit has a number")


@dataclass(frozen=True)
class ExportPath:
    directory: tuple[str, ...]
    filename: str

    @property
    def rel_path(self) -> str:
        return "/".join((*self.directory, self.filename))


def _decimal(text: str) -> Decimal | None:
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def is_chapter_zero(frozen: FrozenAttrs) -> bool:
    return frozen.kind == "unit" and frozen.track == "main" and (_decimal(frozen.number_norm) == 0)


def is_special(frozen: FrozenAttrs) -> bool:
    return frozen.kind == "unit" and (frozen.track != "main" or is_chapter_zero(frozen))


def exported_series_name(
    series: str, segment: str, template: NamingTemplate = DEFAULT_TEMPLATE
) -> str:
    """The Kavita series of a place: the series itself, or one series per numbering segment."""
    if not segment:
        return series
    digits = re.search(r"\d+", segment)
    return template.segment.format(series=series, n=digits.group(0) if digits else segment)


def _render(fmt: str, series: str, extension: str, **fields: str) -> str:
    """Fill a template; the series is cut so that the whole name fits one component."""
    try:
        fixed = fmt.format(series="", **fields)
        base = fmt.format(series="\0", **fields)
    except (KeyError, IndexError, ValueError) as error:
        raise ValueError(f"invalid naming template {fmt!r}") from error
    if "\0" not in base:
        raise ValueError(f"naming template {fmt!r} has no {{series}}")
    room = MAX_COMPONENT_BYTES - len(extension.encode()) - len(fixed.encode())
    name = fmt.format(series=sanitize_name(series, max_bytes=max(8, room)), **fields)
    return sanitize_name(name, max_bytes=MAX_COMPONENT_BYTES - len(extension.encode())) + extension


def _part(text: str, part: int) -> str:
    return text if part == 1 else f"{text} Part {part}"


def export_path(
    frozen: FrozenAttrs, template: NamingTemplate = DEFAULT_TEMPLATE, extension: str = ".cbz"
) -> ExportPath:
    """The relative path of an export item, from its frozen attributes only."""
    if frozen.template_ref != template.ref:
        raise ValueError(
            f"template {template.ref} is not the one that named it: {frozen.template_ref}"
        )
    series = exported_series_name(frozen.series, frozen.segment, template)
    folder = sanitize_name(series)
    if frozen.kind == "volume":
        name = _render(
            template.volume, series, extension, number=_part(frozen.number_norm, frozen.part)
        )
        return ExportPath((folder,), name)
    if is_special(frozen):
        word = _TRACK_WORD.get(frozen.track, "")
        number = frozen.number_norm or (str(frozen.source_order) if frozen.source_order else "")
        label = _part(" ".join(p for p in (word, number) if p) or "1", frozen.part)
        name = _render(template.special, series, extension, label=label)
        return ExportPath((folder, sanitize_name(template.special_dir)), name)
    name = _render(
        template.chapter, series, extension, number=_part(frozen.number_norm, frozen.part)
    )
    return ExportPath((folder,), name)


def series_cover_source(items: Iterable[FrozenAttrs]) -> FrozenAttrs | None:
    """The unit whose cover the API posts as the series cover: the lowest chapter from 1 on.

    Chapter 0, fractions below 1 and every special are never the series cover.
    """
    best: tuple[Decimal, int, FrozenAttrs] | None = None
    for item in items:
        number = _decimal(item.number_norm)
        if item.kind != "unit" or item.track != "main" or number is None or number < 1:
            continue
        key = (number, item.part, item)
        if best is None or key[:2] < best[:2]:
            best = key
    return best[2] if best else None


# -- manifest and renames (EF-EXP-004, EF-EXP-061) ----------------------------------------------


@dataclass(frozen=True)
class ManifestEntry:
    rel_path: str
    template_ref: str  # `export_record.template_ref`: template_id@version that produced the name


def manifest_entry(frozen: FrozenAttrs, path: ExportPath) -> ManifestEntry:
    return ManifestEntry(rel_path=path.rel_path, template_ref=frozen.template_ref)


@dataclass(frozen=True)
class PlannedItem:
    """An export item as the planner sees it: frozen attributes plus mutable metadata that never
    reaches a path."""

    item_id: str
    frozen: FrozenAttrs
    current_volume: int | None = None
    current_title: str | None = None


@dataclass(frozen=True)
class RenameProposal:
    item_id: str
    from_path: str
    to_path: str


def plan_renames(
    recorded: Mapping[str, str],
    items: Iterable[PlannedItem],
    templates: Mapping[str, NamingTemplate] | None = None,
) -> list[RenameProposal]:
    """Renames implied by the frozen attributes: none while they do not change (I22)."""
    known = templates or {DEFAULT_TEMPLATE.ref: DEFAULT_TEMPLATE}
    proposals: list[RenameProposal] = []
    for item in items:
        template = known[item.frozen.template_ref]
        wanted = export_path(item.frozen, template).rel_path
        if recorded[item.item_id] != wanted:
            proposals.append(RenameProposal(item.item_id, recorded[item.item_id], wanted))
    return proposals


# -- collisions through plugin/parse (EF-EXP-045) -----------------------------------------------

LOOSE_LEAF: Final = -100000.0  # Kavita's "no value" for a volume or a chapter
SPECIAL_VOLUME: Final = 100000.0


class Parsed(Protocol):
    @property
    def min_chapter(self) -> float: ...
    @property
    def min_volume(self) -> float: ...


def volume_deduced(parsed: Parsed) -> bool:
    """True if Kavita reads a real volume in the name (a special is not a volume)."""
    return parsed.min_volume not in (0.0, LOOSE_LEAF, SPECIAL_VOLUME)


def _key(name: str, parsed: Parsed) -> tuple[str, float | str]:
    if parsed.min_volume == SPECIAL_VOLUME:
        return ("special", name.casefold())
    if parsed.min_chapter not in (0.0, LOOSE_LEAF):
        return ("chapter", parsed.min_chapter)
    return ("volume", parsed.min_volume)


@dataclass(frozen=True)
class Collision:
    key: tuple[str, float | str]
    names: tuple[str, ...]


def find_number_collisions(names: Sequence[str], parse: Callable[[str], Parsed]) -> list[Collision]:
    """Names of one series folder that Kavita reads as the same number."""
    groups: dict[tuple[str, float | str], list[str]] = defaultdict(list)
    for name in names:
        groups[_key(name, parse(name))].append(name)
    return [Collision(k, tuple(sorted(v))) for k, v in groups.items() if len(v) > 1]


@dataclass(frozen=True)
class CollisionDecision:
    """Default on a clash with a file already there (adopted ones included): abstain and attach.

    The adopted file becomes the exported Variant of the logical Unit, nothing is written, and no
    translation changes mid-series. Replacing it is only ever a proposal (EF-EXP-037).
    """

    action: str
    existing: str
    incoming: str


def check_incoming(
    incoming: str, existing: Iterable[str], parse: Callable[[str], Parsed]
) -> CollisionDecision | None:
    wanted = _key(incoming, parse(incoming))
    for name in existing:
        if _key(name, parse(name)) == wanted:
            return CollisionDecision("attach_adopted", name, incoming)
    return None
