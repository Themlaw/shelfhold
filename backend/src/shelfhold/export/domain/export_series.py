"""The series of a remote library, with an identity of their own (EF-EXP-062, I26).

An exported series (a Kavita series) gets its ULID at the first export and keeps it: the name
below is only the *proposal* made when an Edition has no series yet. A merge or a split of Works
changes nothing here, because the link `(edition, remote library) -> export_series` is never
recomputed from the Work; a later reassignment is an explicit plan (M3).
"""

from collections.abc import Iterable
from dataclasses import dataclass

from shelfhold.export.domain.naming import normalize_series_name


@dataclass(frozen=True)
class EditionNaming:
    edition_id: str
    work_id: str
    work_title: str
    lang: str
    text_version: str = ""


@dataclass(frozen=True)
class SeriesProposal:
    name: str
    normalized: str
    key: dict[str, object]  # the rule that proposed it (EF-EXP-062 "règle de proposition")


def propose_series_name(
    edition: EditionNaming, siblings: Iterable[EditionNaming]
) -> SeriesProposal:
    """The name of a series to create for `edition`.

    By default the title of the Work. When other Editions of the same Work share the library, the
    language label suffixes it ("Monster (fr)"); when one of them has the same language, the
    version of the text does too ("Titre (fr, <version>)", GAP-7-15).
    """
    others = [
        s for s in siblings if s.edition_id != edition.edition_id and s.work_id == edition.work_id
    ]
    name = edition.work_title
    if others:
        same_lang = any(s.lang == edition.lang for s in others)
        label = (
            f"{edition.lang}, {edition.text_version}"
            if same_lang and edition.text_version
            else edition.lang
        )
        name = f"{name} ({label})"
    return SeriesProposal(
        name=name,
        normalized=normalize_series_name(name),
        key={"work": edition.work_id, "lang": edition.lang, "text_version": edition.text_version},
    )
