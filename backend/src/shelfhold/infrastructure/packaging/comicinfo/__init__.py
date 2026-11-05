"""ComicInfo v2.0 + the 2.1 draft fields Kavita reads (EF-EXP-011)."""

from shelfhold.infrastructure.packaging.comicinfo.writer import (
    ELEMENT_ORDER,
    PERSON_ROLES,
    ComicInfoData,
    ComicInfoPerson,
    RenderedComicInfo,
    render_comicinfo,
)

__all__ = [
    "ELEMENT_ORDER",
    "PERSON_ROLES",
    "ComicInfoData",
    "ComicInfoPerson",
    "RenderedComicInfo",
    "render_comicinfo",
]
