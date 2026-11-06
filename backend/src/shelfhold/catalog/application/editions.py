"""Editions: get-or-create by key, and the choice between a Team and a `text_version`."""

from dataclasses import replace

from shelfhold.catalog.application.dto import EditionRef, EditionSpec
from shelfhold.catalog.application.ports import EditionStore
from shelfhold.catalog.domain.identity import Discriminant, TextVersionRef, translation_discriminant
from shelfhold.catalog.lang import edition_key_lang

# What each declared nature files an Edition under (the `translation_type` part of its key).
# `unknown` keeps the historical value: the kind then decides the discriminant (EF-BIB-049).
_TRANSLATION_TYPES = {
    "community": "fan",
    "published": "official",
    "machine": "machine",
    "unknown": "official",
}


def translation_type_for(nature: str) -> str:
    """The `translation_type` of an Edition for a declared `translation_nature`.

    One function for discovery and for adoption, so that both build the same Edition key
    (EF-BIB-001, EF-BIB-049): a community translation relayed by a connected service is `fan`.
    """
    return _TRANSLATION_TYPES.get(nature, "official")


def ensure_edition(store: EditionStore, spec: EditionSpec) -> EditionRef:
    """The Edition of this key, created if absent (every part of the key is NOT NULL, I15).

    The language is stored canonical, as received in `lang_raw` (I23); a placeholder or a Chinese
    tag without script is refused. `text_version` is derived from its reference (EF-BIB-049).
    """
    lang = str(edition_key_lang(spec.lang))
    text_version = spec.text_version.key if spec.text_version else ""
    found = store.find(spec, lang, text_version)
    if found is not None:
        return EditionRef(found, spec.work_id, text_version, created=False)
    return store.insert(spec, lang, text_version)


def edition_for_translation(
    store: EditionStore,
    spec: EditionSpec,
    *,
    kind: str,
    nature: str,
    text_version: TextVersionRef | None = None,
) -> EditionRef:
    """A community translation is a Team of one Edition: `text_version` stays empty then."""
    if translation_discriminant(kind, nature) is Discriminant.TEAM:
        return ensure_edition(store, replace(spec, text_version=None))
    return ensure_edition(store, replace(spec, text_version=text_version))
