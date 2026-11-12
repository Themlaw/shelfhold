"""The naming preview (EF-EXP-005): what a template would call each file, read by `plugin/parse`.

A preview writes nothing, anywhere: it renders paths from frozen attributes and reads answers it
was given. Each name is confirmed by Kavita or signalled; a Kavita that cannot be asked (down, a
route gone, a version the parser replica does not cover) leaves the names "unverified", which
never blocks anything. Collisions are found here, before any file is written: two Units on one
path (on a filesystem that folds case, and under any Unicode form), two names Kavita reads as one
number, a file already there, the folder Shelfhold keeps for itself, a name cut to fit.
"""

import unicodedata
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Final

from shelfhold.export.domain.naming import (
    MAX_COMPONENT_BYTES,
    ExportPath,
    FrozenAttrs,
    NamingGuardError,
    NamingTemplate,
    Parsed,
    check_incoming,
    checked_export_path,
    export_path,
    exported_series_name,
    find_number_collisions,
    is_special,
    manifest_entry,
    sanitize_name,
)

__all__ = ["NameEntry", "NamingItem", "NamingPreview", "PreviewCollision", "preview_names"]

RESERVED_FOLDER: Final = ".shelfhold"
MAX_PATH_COMPONENT: Final = 255  # bytes, ext4 and SMB
_UNLIMITED: Final = 1 << 16


@dataclass(frozen=True)
class NamingItem:
    """What is named: the frozen attributes of a Unit or an export item, and the file extension."""

    key: str
    frozen: FrozenAttrs
    extension: str = ".cbz"


@dataclass(frozen=True)
class NameEntry:
    key: str
    rel_path: str
    template_ref: str
    status: str  # "confirmed" | "flagged" | "unverified"
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class PreviewCollision:
    kind: str  # "path" | "number"
    names: tuple[str, ...]


@dataclass(frozen=True)
class NamingPreview:
    verified: bool
    entries: tuple[NameEntry, ...]
    collisions: tuple[PreviewCollision, ...]


def _fold(rel_path: str, *, case_insensitive: bool) -> str:
    """The identity of a path on the target: one Unicode form (a macOS or SMB share hands back
    the other one), and no case when the filesystem has none."""
    text = unicodedata.normalize("NFC", rel_path)
    return unicodedata.normalize("NFC", text.casefold()) if case_insensitive else text


def _is_reserved(text: str) -> bool:
    return unicodedata.normalize("NFC", text).strip().casefold() == RESERVED_FOLDER


def _reserved(frozen: FrozenAttrs, template: NamingTemplate, path: ExportPath) -> bool:
    """The raw inputs are looked at as well as the path: sanitizing strips the leading dot."""
    return any(
        _is_reserved(part) for part in (frozen.series, template.special_dir, *path.directory)
    ) or _is_reserved(path.filename)


def _truncated(frozen: FrozenAttrs, template: NamingTemplate, path: ExportPath) -> bool:
    full = sanitize_name(
        exported_series_name(frozen.series, frozen.segment, template), max_bytes=_UNLIMITED
    )
    cut_folder = path.directory[0] != full
    return cut_folder or len(path.filename.encode()) >= MAX_COMPONENT_BYTES - 3


def _render(
    item: NamingItem,
    template: NamingTemplate,
    parse: Callable[[str], Parsed] | None,
    segment_policy_confirmed: bool,
) -> tuple[ExportPath, str | None]:
    frozen = replace(item.frozen, template_ref=template.ref)
    if parse is not None:
        try:
            return checked_export_path(
                frozen, parse, template, item.extension,
                segment_policy_confirmed=segment_policy_confirmed,
            ), None  # fmt: skip
        except NamingGuardError as error:  # misread by Kavita: shown, flagged
            return export_path(frozen, template, item.extension), error.code
    return export_path(frozen, template, item.extension), None


def preview_names(
    items: Sequence[NamingItem],
    template: NamingTemplate,
    *,
    parse: Callable[[str], Parsed] | None,
    verified: bool,
    existing: Iterable[str] = (),
    case_insensitive: bool = True,
    segment_policy_confirmed: bool = False,
) -> NamingPreview:
    """Render `items` with `template` and check every name.

    `parse` answers `plugin/parse` for each rendered file name and each folder (and for the
    names in `existing`); it is ignored, and the preview is "unverified", unless `verified`.
    `existing` are the relative paths already on the shelf. Never raises for a name Kavita
    dislikes, and never touches a disk.
    """
    reader = parse if verified else None
    rendered: list[tuple[NamingItem, ExportPath, list[str]]] = []
    for item in items:
        path, misread = _render(item, template, reader, segment_policy_confirmed)
        flags: list[str] = [misread] if misread else []
        if _reserved(item.frozen, template, path):
            flags.append("reserved_name")
        if any(len(part.encode()) > MAX_PATH_COMPONENT for part in path.rel_path.split("/")):
            flags.append("too_long")
        if _truncated(item.frozen, template, path):
            flags.append("truncated")
        rendered.append((item, path, flags))

    collisions: list[PreviewCollision] = []
    by_path: dict[str, list[int]] = defaultdict(list)
    for index, (_item, path, _flags) in enumerate(rendered):
        by_path[_fold(path.rel_path, case_insensitive=case_insensitive)].append(index)
    for indexes in by_path.values():
        if len(indexes) > 1:
            for index in indexes:
                rendered[index][2].append("path_collision")
            collisions.append(
                PreviewCollision("path", tuple(sorted({rendered[i][1].rel_path for i in indexes})))
            )

    taken = {_fold(p, case_insensitive=case_insensitive) for p in existing}
    for _item, path, flags in rendered:
        if _fold(path.rel_path, case_insensitive=case_insensitive) in taken:
            flags.append("path_taken")

    if reader is not None:
        _flag_numbers(rendered, reader, existing, collisions)

    entries = tuple(
        NameEntry(
            key=item.key,
            rel_path=path.rel_path,
            template_ref=manifest_entry(
                replace(item.frozen, template_ref=template.ref), path
            ).template_ref,
            status="unverified" if not verified else ("flagged" if flags else "confirmed"),
            flags=tuple(dict.fromkeys(flags)),
        )
        for item, path, flags in rendered
    )
    return NamingPreview(verified=verified, entries=entries, collisions=tuple(collisions))


def _flag_numbers(
    rendered: list[tuple[NamingItem, ExportPath, list[str]]],
    parse: Callable[[str], Parsed],
    existing: Iterable[str],
    collisions: list[PreviewCollision],
) -> None:
    """Names of one folder that Kavita reads as the same number, and numbers a file already
    there holds (the default answer to the latter is to attach it, never to write)."""
    folders: dict[tuple[str, ...], list[tuple[ExportPath, list[str], bool]]] = defaultdict(list)
    for item, path, flags in rendered:
        folders[path.directory].append((path, flags, is_special(item.frozen)))
    already: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for rel in existing:
        *parents, name = rel.split("/")
        already[tuple(parents)].append(name)
    for directory, members in folders.items():
        for by_name in (False, True):
            group = [(p, f) for p, f, special in members if special is by_name]
            names = sorted({p.filename for p, _f in group})
            clashes = find_number_collisions(names, parse, by_name=by_name)
            for clash in clashes:
                collisions.append(
                    PreviewCollision(
                        "number", tuple(f"{'/'.join(directory)}/{n}" for n in clash.names)
                    )
                )
                for path, flags in group:
                    if path.filename in clash.names:
                        flags.append("number_collision")
            for path, flags in group:
                decision = check_incoming(
                    path.filename, already.get(directory, ()), parse, by_name=by_name
                )
                if decision is not None and decision.existing != path.filename:
                    flags.append("number_taken")
