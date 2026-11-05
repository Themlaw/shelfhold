"""A replica of `GET /api/plugin/parse` for the MANGA library type (tests only).

Source: Kareadita/Kavita, tag v0.9.1.4 (the pinned version), commit
879b8b73eb41d66f88dde8eb874cc99a3397eb11.
  - `Kavita.Services/Scanner/Parser.cs`: `MangaVolumeRegex` and `MangaChapterRegex` ported word for
    word (named groups written `(?P<..>)`; `NumberRange` expanded), `ParseMangaVolume`,
    `ParseMangaChapter`, `FormatValue`, `RemoveDuplicateVolumeIfExists`,
    `RemoveDuplicateChapterIfExists`, `MinNumberFromRange`, `MaxNumberFromRange`.
  - `Kavita.Server/Controllers/PluginController.cs`: `ParseText` (`LooseLeafVolume` and
    `DefaultChapter` are "-100000").

What is NOT replicated, on purpose:
  - `MangaSeriesRegex` and `CleanTitle`: the series name is the text before the first " - ",
    with underscores turned into spaces. No volume decision depends on it.
  - `ParseText` has no notion of a special: a special is placed by the scanner from the folder
    (`Specials/`) or an `SP<n>` marker, not by this route. Nothing here invents one.
  - One deviation of the engine: the chapter pattern of "Hinowa ga CRUSH! 018" ends with two
    variable-width look-behinds that Python cannot compile. They are applied as a post-check on
    the text before the match; .NET would instead backtrack inside the pattern.

The route reads a NAME only: no ComicInfo, no folder. The check against a real Kavita is the
nightly job of L40; until then this module is the best documented copy, not the truth.
"""

# ruff: noqa: RUF001  (Cyrillic and CJK literals are Kavita's, ported as is)
import re
from typing import Final

from shelfhold.export.targets.kavita.dto import ParseResult

LOOSE_LEAF: Final = -100000.0
_LOOSE: Final = "-100000"
_FLAGS: Final = re.IGNORECASE

MANGA_VOLUME_REGEX: Final = (
    r"(เล่ม|เล่มที่)(\s)?(\.?)(\s|_)?(?P<Volume>\d+(\-\d+)?(\.\d+)?)",
    r"(?P<Series>.*)(\b|_)(v|tome(\s|_)?|t)(?P<Volume>\d+-?\d+)(\s|_)",
    r"^(?P<Series>.+?)(\s*Chapter\s*\d+)?(\s|_|\-\s)+((Vol(ume)?|tome)\.?(\s|_)?)(?P<Volume>\d+(\.\d+)?(\-\d+(\.\d+)?)?)(.+?|$)",
    r"(?P<Series>.*)(\b|_)(?!\[)v(?P<Volume>\d+(\.\d)?(-\d+(\.\d)?)?)(?!\])(\b|_)",
    r"(?P<Series>.*)(\b|_)(vol\.? ?)(?P<Volume>\d+(\.\d)?(-\d+)?(\.\d)?)",
    r"(vol\.? ?)(?P<Volume>\d+(\.\d)?)",
    r"((volume|tome)\s)(?P<Volume>\d+(\.\d)?)",
    r"(?P<Series>.*)(\b|_)((S|T)(?P<Volume>\d+)(\b|_))",
    r"(vol_)(?P<Volume>\d+(\.\d)?)",
    r"第(?P<Volume>\d+)(卷|册)",
    r"(卷|册)(?P<Volume>\d+)",
    r"제?(?P<Volume>\d+(\.\d+)?)(권|장)",
    r"시즌(?P<Volume>\d+(\-\d+)?)",
    r"(?P<Volume>\d+(\-|~)?\d+?)시즌",
    r"시즌(?P<Volume>\d+(\-|~)?\d+?)",
    r"(?P<Volume>\d+(?:(\-)\d+)?)巻",
    r"Том(а?)(\.?)(\s|_)?(?P<Volume>\d+(?:(\-)\d+)?)",
    r"(\s|_)?(?P<Volume>\d+(?:(\-)\d+)?)(\s|_)Том(а?)",
)

MANGA_CHAPTER_REGEX: Final = (
    r"(?P<Volume>((เล่ม|เล่มที่))?(\s|_)?\.?\d+)(\s|_)(บทที่|ตอนที่)\.?(\s|_)?(?P<Chapter>\d+)",
    r"(\b|_)(c|ch)(\.?\s?)(?P<Chapter>(?>\d+(\.\d)?)(?P<Range>-c?\d+(\.\d)?)?)(?(Range)|(?:x\d+)?(?![a-zA-Z]))",
    r"v\d+\.(\s|_)(?P<Chapter>\d+(?:.\d+|-\d+)?)",
    r"^(?P<Series>.*)(?: |_)#(?P<Chapter>\d+)",
    r"^(?!Vol)(?P<Series>.*)\s?(?<!vol\. )\sChapter\s(?P<Chapter>\d+(?:\.?[\d-]+)?)",
    r"(Глава|глава|Главы|Глава)(\.?)(\s|_)?(?P<Chapter>\d+(?:.\d+|-\d+)?)",
    r"第(?P<Chapter>\d+)话",
    r"제?(?P<Chapter>\d+\.?\d+)(회|화|장)",
    r"第?(?P<Chapter>\d+(?:\.\d+|-\d+)?)話",
    r"(?!Том)(?<!Том\.)\s\d+(\s|_)?(?P<Chapter>\d+(?:\.\d+|-\d+)?)(\s|_)(Глава|глава|Главы|Глава)",
    r"^(?P<Series>.+?)\schapter(?:s)?\s(?P<Chapter>\d+-\d+)",
    # DEVIATION: the two variable-width look-behinds before the last group are applied
    # as a post-check in _first_group (Python cannot compile them).
    r"^(?P<Series>.+?)(?<!Vol)(?<!Vol.)(?<!Volume)(?<!Tome)\s(\d\s)?(?P<Chapter>\d+(?:\.\d+|-\d+)?)(?![\d.권])(?:\s\(\d{4}\))?(?!\d)(?![\s_]*-?[\s_]*(?:vol(?:ume)?|tome)(?![a-zA-Z]))(?!.*\bv\.?\d)(?!.*\b(?:vol(?:ume)?|tome)\.?\s*\d)(?P<_end>\b|_|-)",
    r"(?P<Series>.*)\sS(?P<Volume>\d+)\s(?P<Chapter>\d+(?:.\d+|-\d+)?)",
    r"^((?!v|vo|vol|Volume).)*(\s|_)(?P<Chapter>\.?\d+(?:.\d+|-\d+)?)(?P<Part>b)?(?!\d)(?![\s_]*-?[\s_]*(?:vol(?:ume)?|tome)(?![a-zA-Z]))(?!.*\bv\.?\d)(?!.*\b(?:vol(?:ume)?|tome)\.?\s*\d)(\s|_|\[|\()",
    r"Chapter(?P<Chapter>\d+(-\d+)?)",
    r"(?P<Series>.*)(\s|_)(vol\d+)?(\s|_)Chp\.? ?(?P<Chapter>\d+)",
    r"(?P<Volume>((vol|volume|v))?(\s|_)?\.?\d+)(\s|_)(Chp|Chapter)\.?(\s|_)?(?P<Chapter>\d+)",
)

_VOL_COMPILED: Final = tuple(re.compile(r, _FLAGS) for r in MANGA_VOLUME_REGEX)
_CH_COMPILED: Final = tuple(re.compile(r, _FLAGS) for r in MANGA_CHAPTER_REGEX)
_EXTENSION: Final = re.compile(r"\.(?:cbz|zip|rar|cbr|7z|cb7|cbt|epub|pdf)$", re.IGNORECASE)

_DUPLICATE_VOLUME: Final = re.compile(
    r"(vol\.?|volume|v)(\s|_)*\d+.*?(vol\.?|volume|v)(\s|_)*\d+", _FLAGS
)
_DUPLICATE_CHAPTER: Final = re.compile(
    r"(ch\.?|chapter|c)(\s|_)*\d+.*?(ch\.?|chapter|c)(\s|_)*\d+", _FLAGS
)
_VOLUME_RANGE: Final = re.compile(
    r"(vol\.?|v)(\s|_)?\d+(\.\d+)?-(vol\.?|v)(\s|_)?\d+(\.\d+)?", _FLAGS
)
_CHAPTER_RANGE: Final = re.compile(
    r"(ch\.?|c)(\s|_)?\d+(\.\d+)?-(ch\.?|c)(\s|_)?\d+(\.\d+)?", _FLAGS
)
_VOLUME_NUMBER: Final = re.compile(
    r"(vol\.?|volume|v)(\s|_)*(?P<Volume>\d+(\.\d+)?(-\d+(\.\d+)?)?)", _FLAGS
)
_CHAPTER_NUMBER: Final = re.compile(
    r"(ch\.?|chapter|c)(\s|_)*(?P<Chapter>\d+(\.\d+)?(-\d+(\.\d+)?)?)", _FLAGS
)
# the post-check standing for the two look-behinds (see the module docstring)
_LB_VOLUME: Final = re.compile(r"\bv\.?\d", _FLAGS)
_LB_VOLUME_WORD: Final = re.compile(r"\b(?:vol(?:ume)?|tome)\.?\s*\d", _FLAGS)


def _remove_duplicate(
    name: str, range_re: re.Pattern[str], dup_re: re.Pattern[str], number_re: re.Pattern[str]
) -> str:
    """`RemoveDuplicateVolumeIfExists` / `RemoveDuplicateChapterIfExists`."""
    if range_re.search(name):
        return name
    duplicate = dup_re.search(name)
    if not duplicate:
        return name
    first = number_re.search(name, duplicate.start(1))
    if not first:
        return name
    second = number_re.search(name, first.end())
    return name[: second.start()].rstrip(" -_") if second else name


def _remove_leading_zeroes(text: str) -> str:
    return text.lstrip("0") or "0"


def _format_value(value: str, has_part: bool) -> str:
    """`FormatValue`: leading zeroes out, `c` of a range out, `.5` for a part."""

    def part(v: str) -> str:
        return v if "." in v or not has_part else f"{v}.5"

    if "-" not in value:
        return _remove_leading_zeroes(part(value))
    tokens = value.split("-")
    first = _remove_leading_zeroes(tokens[0])
    if len(tokens) != 2:
        return first
    last = tokens[1].replace("c", "").replace("C", "") if tokens[1][:1] in "cC" else tokens[1]
    return f"{first}-{_remove_leading_zeroes(part(last))}"


def _first_group(patterns: tuple[re.Pattern[str], ...], name: str, group: str) -> str | None:
    for pattern in patterns:
        for match in pattern.finditer(name):
            if "_end" in pattern.groupindex:
                before = name[: match.start("_end")]
                if _LB_VOLUME.search(before) or _LB_VOLUME_WORD.search(before):
                    continue
            if group in pattern.groupindex and match.group(group):
                has_part = "Part" in pattern.groupindex and bool(match.group("Part"))
                return _format_value(match.group(group), has_part)
    return None


def parse_volume(name: str) -> str:
    name = _remove_duplicate(name, _VOLUME_RANGE, _DUPLICATE_VOLUME, _VOLUME_NUMBER)
    return _first_group(_VOL_COMPILED, name, "Volume") or _LOOSE


def parse_chapter(name: str) -> str:
    name = _remove_duplicate(name, _CHAPTER_RANGE, _DUPLICATE_CHAPTER, _CHAPTER_NUMBER)
    return _first_group(_CH_COMPILED, name, "Chapter") or _LOOSE


def _bounds(value: str) -> tuple[float, float]:
    """`MinNumberFromRange` / `MaxNumberFromRange`."""
    if not re.fullmatch(r"[\d\-.]+", value):
        return (0.0, 0.0)
    try:
        if not re.search(r"\d-{1}\d", value):
            return (float(value), float(value))
        numbers = [float(t) for t in value.replace("_", "").split("-") if t]
    except ValueError:
        return (0.0, 0.0)
    return (min(numbers), max(numbers))


def simulate_parse(name: str) -> ParseResult:
    """What `ParseText(name, Manga)` answers for `name` (the route is given the name as is)."""
    series = _EXTENSION.sub("", name.strip()).split(" - ", 1)[0].replace("_", " ")
    series = re.sub(r"\s{2,}", " ", series).strip(" \0\t\r-,")
    chapter_min, chapter_max = _bounds(parse_chapter(name))
    volume_min, volume_max = _bounds(parse_volume(name))
    return ParseResult(
        series_name=series, series_year=None,
        min_chapter=chapter_min, max_chapter=chapter_max,
        min_volume=volume_min, max_volume=volume_max,
    )  # fmt: skip
