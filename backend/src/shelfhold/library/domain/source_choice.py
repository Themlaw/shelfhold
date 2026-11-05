"""Where the bytes of a safeguard come from (EF-BIB-045, EF-BIB-048).

The origin first: the file as received, `store_provenance = received`. Without it, the file in
/library, but only if it is still the one the manifest knows (its file hash is the recorded one);
its rendering is then stripped and the result verified by `content_hash`, `store_provenance =
extracted`. A library file that no longer matches the manifest was changed by a third party (Kavita,
the user): it is never trusted as a source, and with no origin there is nothing to copy.
"""

from dataclasses import dataclass
from typing import Literal

Source = Literal["upstream", "library"]


@dataclass(frozen=True)
class SourceChoice:
    source: Source | None
    reason: str  # a stable word, kept for the log and for `SafeguardFailedError`


def choose_source(
    *, upstream_available: bool, library_matches_manifest: bool | None
) -> SourceChoice:
    """`library_matches_manifest` is None when there is no library copy to look at."""
    if upstream_available:
        return SourceChoice("upstream", "origin_first")
    if library_matches_manifest:
        return SourceChoice("library", "library_matches_manifest")
    if library_matches_manifest is None:
        return SourceChoice(None, "no_source")
    return SourceChoice(None, "library_differs_from_manifest")
