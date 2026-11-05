"""A simulated `GET /api/plugin/parse`: a documented replica of how Kavita reads a file NAME.

Kavita's route "parses a string and returns the parsed information from it; it does not support
any directory fallback parsing" (pinned OpenAPI summary). It reads neither ComicInfo nor the
folder. The goldens of L24 run against this replica; the check against a real Kavita is the
nightly job of L40 (`test_nightly_real_kavita_places_chapter_volume_from_comicinfo`).

Rules replicated (manga library type), each one the cause of a Shelfhold naming rule:

- A volume marker is read anywhere in the name: `Vol.2`, `Volume 2`, `Tome 2`, `v02`, and also a
  SEASON marker (`S1`, `Season 1`), because Kavita treats a season as a volume. This is why an
  upstream title such as `{S1 END}` must never reach a file name (EF-EXP-045). Brackets do not
  hide a marker.
- A chapter marker is `Ch.12`, `Chapter 12`, `#12`; decimals are kept (`Ch.10.5`).
- `Special`, `SP`, `Omake`, `Extra`, `OVA`, `OAD`, `ONA`, `Annual`, `One-shot` make the file a
  special (volume `SPECIAL_VOLUME`).
- No volume gives `LOOSE_LEAF`; no chapter gives `LOOSE_LEAF` too.
- The series is the text before the first ` - `.
"""

import re
from typing import Final

from shelfhold.export.targets.kavita.dto import ParseResult

LOOSE_LEAF: Final = -100000.0
SPECIAL_VOLUME: Final = 100000.0

_NUMBER = r"(\d+(?:\.\d+)?)"
_EXTENSION = re.compile(r"\.(?:cbz|cbr|zip|epub|pdf)$", re.IGNORECASE)
_VOLUME = re.compile(rf"(?<![a-z])(?:vol(?:ume)?s?\.?\s*|tome\s*|season\s*|s|v){_NUMBER}", re.I)
_CHAPTER = re.compile(rf"(?<![a-z])(?:ch(?:apter)?s?\.?\s*|#\s*){_NUMBER}", re.I)
_SPECIAL = re.compile(
    r"(?<![a-z])(?:specials?|sp|omake|extras?|ova|oad|ona|annuals?|one-?shot)(?![a-z])", re.I
)


def simulate_parse(name: str) -> ParseResult:
    base = _EXTENSION.sub("", name.strip())
    series = base.split(" - ", 1)[0].strip()
    volume = _VOLUME.search(base)
    chapter = _CHAPTER.search(base)
    if _SPECIAL.search(base):
        vol = SPECIAL_VOLUME
    else:
        vol = float(volume.group(1)) if volume else LOOSE_LEAF
    ch = float(chapter.group(1)) if chapter else LOOSE_LEAF
    return ParseResult(
        series_name=series, series_year=None,
        min_chapter=ch, max_chapter=ch, min_volume=vol, max_volume=vol,
    )  # fmt: skip
