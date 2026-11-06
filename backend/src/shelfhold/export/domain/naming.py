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
  (`Tower of God Saga 2`), one Kavita series per segment (Q50, option 1, decided by the
  `plugin/parse` golden): the segment is never a `S2` / `Season 2` / `Vol.2` marker.
"""

import json
import re
import unicodedata
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final, Protocol

# -- name sanitising (EF-EXP-013) ---------------------------------------------------------------

MAX_COMPONENT_BYTES: Final = 200  # well under the 255 bytes of ext4 / SMB, room for a suffix
_FORBIDDEN: Final = re.compile(r'[/\\:*?"<>|]')
_DROPPED_CATEGORIES: Final = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
_RESERVED: Final = frozenset(
    {
        "con", "prn", "aux", "nul", "conin$", "conout$",
        *(f"com{c}" for c in "123456789\u00b9\u00b2\u00b3"),
        *(f"lpt{c}" for c in "123456789\u00b9\u00b2\u00b3"),
    }
)  # fmt: skip
_BLANKS: Final = re.compile(r"\s+")


def _truncate(text: str, max_bytes: int) -> str:
    while len(text.encode("utf-8")) > max_bytes:
        text = text[:-1]
    return text


def is_reserved_name(name: str) -> bool:
    """A device name on Windows and SMB: the part before the first dot, in any case."""
    return name.split(".", 1)[0].strip().casefold() in _RESERVED


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
        if is_reserved_name(cleaned):
            stem, dot, rest = cleaned.partition(".")
            cleaned = f"{stem.rstrip()}_{dot}{rest}"
        if len(cleaned.encode("utf-8")) <= max_bytes:
            return cleaned
    cleaned = _truncate(cleaned, max_bytes).strip(" .")
    return "_" if not cleaned or is_reserved_name(cleaned) else cleaned


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
    segment: str = "{series} Saga {n}"

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


_ORDINAL: Final = re.compile(r"S([1-9]\d*)")


def segment_label(segment: str) -> str:
    """The text a segment adds to the series name. Lossless: `S2` (the normalised form of the
    second segment) gives `2`; any other value keeps its own text behind `No`, so `2` and `S2`
    never share a name."""
    if match := _ORDINAL.fullmatch(segment):
        return match.group(1)
    return f"No {segment}"


def exported_series_name(
    series: str, segment: str, template: NamingTemplate = DEFAULT_TEMPLATE
) -> str:
    """The Kavita series of a place: the series itself, or one series per numbering segment."""
    if not segment:
        return series
    return template.segment.format(series=series, n=segment_label(segment))


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


class NamingGuardError(ValueError):
    """A name that `plugin/parse` would misread. `code` is the stable catalog code."""

    code = "export.naming"
    suggestion = "define an exported series name"

    def __init__(self, message: str, *, component: str) -> None:
        super().__init__(f"{self.code}: {message} ({component!r}); {self.suggestion}")
        self.component = component


class SeriesNameYieldsVolume(NamingGuardError):
    """A series name, a folder or a file name from which Kavita deduces a volume (`S2`, `T1`,
    `v3`, `Vol.3`, `第2巻`, `시즌2`, `{S1 END}`...). The name is the user's; Shelfhold refuses to
    write it and asks for an exported series name instead."""

    code = "export.series_name_yields_volume"


class SegmentPolicyUnconfirmed(NamingGuardError):
    """Q50: a non-empty numbering segment is not exported until the one-series-per-segment naming
    is confirmed against a real Kavita (setting `naming_segment_policy_confirmed`, false by
    default). The place stays in `numbering_conflict`."""

    code = "export.segment_policy_unconfirmed"
    suggestion = (
        "leave the unit in numbering_conflict until the nightly confirms the segment naming"
    )


class NameMisreadNumber(NamingGuardError):
    """The chapter number Kavita reads in the file name is not the unit's."""

    code = "export.name_misread_number"


def checked_export_path(
    frozen: FrozenAttrs,
    parse: Callable[[str], "Parsed"],
    template: NamingTemplate = DEFAULT_TEMPLATE,
    extension: str = ".cbz",
    *,
    segment_policy_confirmed: bool = False,
) -> ExportPath:
    """`export_path`, with the file name AND every folder component read by `plugin/parse`.

    `parse` is the route (the planner fills it from `parse-bulk`). A volume deduced anywhere, in
    a chapter's path, refuses the export; so does a chapter number that is not the unit's. A
    closed volume (`kind = volume`) is meant to carry one: only its folders are checked.
    """
    if frozen.segment and not segment_policy_confirmed:
        raise SegmentPolicyUnconfirmed("segment naming not confirmed", component=frozen.segment)
    path = export_path(frozen, template, extension)
    components = list(path.directory)
    if frozen.kind != "volume":
        components += [path.filename, path.filename.removesuffix(extension)]
    for component in components:
        if volume_deduced(parse(component)):
            raise SeriesNameYieldsVolume("a volume is deduced from the name", component=component)
    if frozen.kind == "unit" and frozen.track == "main" and not is_chapter_zero(frozen):
        read = parse(path.filename).min_chapter
        wanted = _decimal(frozen.number_norm)
        if wanted is None or Decimal(str(read)) != wanted:
            raise NameMisreadNumber(
                f"read {read}, expected {frozen.number_norm}", component=path.filename
            )
    return path


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


# -- manifest (EF-EXP-004) ----------------------------------------------


@dataclass(frozen=True)
class ManifestEntry:
    rel_path: str
    template_ref: str  # `export_record.template_ref`: template_id@version that produced the name


def manifest_entry(frozen: FrozenAttrs, path: ExportPath) -> ManifestEntry:
    return ManifestEntry(rel_path=path.rel_path, template_ref=frozen.template_ref)


# -- collisions through plugin/parse (EF-EXP-045) -----------------------------------------------

LOOSE_LEAF: Final = -100000.0  # Kavita's "no value" for a volume or a chapter (ParserConstants)


class Parsed(Protocol):
    @property
    def min_chapter(self) -> float: ...
    @property
    def min_volume(self) -> float: ...


def volume_deduced(parsed: Parsed) -> bool:
    """True if `plugin/parse` reads a volume in the name. The route knows no "special": a special
    is shelved by its folder (`Specials/`), so the criterion is the absence of any volume."""
    return parsed.min_volume not in (0.0, LOOSE_LEAF)


def _key(name: str, parsed: Parsed, *, by_name: bool) -> tuple[str, float | str]:
    if by_name:
        return ("name", name.casefold())
    if parsed.min_chapter not in (0.0, LOOSE_LEAF):
        return ("chapter", parsed.min_chapter)
    return ("volume", parsed.min_volume)


@dataclass(frozen=True)
class Collision:
    key: tuple[str, float | str]
    names: tuple[str, ...]


def find_number_collisions(
    names: Sequence[str], parse: Callable[[str], Parsed], *, by_name: bool = False
) -> list[Collision]:
    """Names of one folder that Kavita reads as the same number (`by_name` for `Specials/`, where
    the number does not place the file)."""
    groups: dict[tuple[str, float | str], list[str]] = defaultdict(list)
    for name in names:
        groups[_key(name, parse(name), by_name=by_name)].append(name)
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
    incoming: str,
    existing: Iterable[str],
    parse: Callable[[str], Parsed],
    *,
    by_name: bool = False,
) -> CollisionDecision | None:
    wanted = _key(incoming, parse(incoming), by_name=by_name)
    for name in existing:
        if _key(name, parse(name), by_name=by_name) == wanted:
            return CollisionDecision("attach_adopted", name, incoming)
    return None
