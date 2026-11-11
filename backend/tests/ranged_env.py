"""M2 L26: web-novel volumes kept under /data/ranged, over the library environment.

The EPUBs are made here (no network): `volume_epub` builds a volume of N chapters, with the table
of contents given or bent (a hole, a chapter listed twice, a link out of the archive). A fake
provider hands ranked chapters to `ranged_bench`, which records what a `ranged_content` service
leaves behind: an `export_item` covering the Units, the file under `<data>/ranged/`, the policy
of the Edition.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import sqlalchemy as sa

from shelfhold.catalog.public import ResplitSigner
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, export
from shelfhold.infrastructure.volumes import write_data_file
from tests.integration.test_schema_catalog import add_unit
from tests.library_env import LibEnv

CONTAINER = (
    '<?xml version="1.0"?>\n<container version="1.0" '
    'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
    '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
    "</rootfiles></container>\n"
)


def chapter_path(n: int) -> str:
    return f"OEBPS/ch{n}.xhtml"


def volume_epub(
    count: int,
    *,
    nav: Sequence[tuple[str, str]] | None = None,
    ncx: bool = False,
    extra_items: Sequence[int] = (),
    nav_extra: str = "",
    cover: bool = False,
    copy_of: dict[int, int] | None = None,
) -> bytes:
    """A volume of `count` chapters. `nav` replaces the entries of the table (title, href);
    `ncx` makes an EPUB 2 (no nav document); `extra_items` are chapter numbers followed by one
    more spine item (`ch<n>b.xhtml`) that the table does not list."""
    entries = (
        list(nav)
        if nav is not None
        else [(f"Chapter {n}", f"ch{n}.xhtml") for n in range(1, count + 1)]
    )
    spine_files: list[str] = ["cover.xhtml"] if cover else []
    for n in range(1, count + 1):
        spine_files.append(f"ch{n}.xhtml")
        if n in extra_items:
            spine_files.append(f"ch{n}b.xhtml")
    manifest = "".join(
        f'<item id="i{k}" href="{f}" media-type="application/xhtml+xml"/>'
        for k, f in enumerate(spine_files)
    )
    refs = "".join(f'<itemref idref="i{k}"/>' for k in range(len(spine_files)))
    if ncx:
        points = "".join(
            f'<navPoint id="p{k}" playOrder="{k + 1}"><navLabel><text>{t}</text></navLabel>'
            f'<content src="{h}"/></navPoint>'
            for k, (t, h) in enumerate(entries)
        )
        toc_doc = (
            '<?xml version="1.0"?>'
            '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
            f"<navMap>{points}</navMap></ncx>"
        )
        toc_name, toc_item = (
            "toc.ncx",
            '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        )
        spine_open = '<spine toc="ncx">'
    else:
        items = "".join(f'<li><a href="{h}">{t}</a></li>' for t, h in entries)
        toc_doc = (
            '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml" '
            'xmlns:epub="http://www.idpf.org/2007/ops"><body>'
            f'<nav epub:type="toc"><ol>{items}</ol></nav>{nav_extra}</body></html>'
        )
        toc_name = "nav.xhtml"
        toc_item = (
            '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
        )
        spine_open = "<spine>"
    opf = (
        '<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" '
        'version="3.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        '<dc:identifier id="id">urn:uuid:00000000-0000-4000-8000-000000000026</dc:identifier>'
        "<dc:title>Roman synthetique</dc:title><dc:language>fr</dc:language></metadata>"
        f"<manifest>{toc_item}{manifest}</manifest>{spine_open}{refs}</spine></package>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            zipfile.ZipInfo("mimetype", (2020, 1, 1, 0, 0, 0)), b"application/epub+zip"
        )
        members = {"META-INF/container.xml": CONTAINER, "OEBPS/content.opf": opf,
                   f"OEBPS/{toc_name}": toc_doc}  # fmt: skip
        for name in spine_files:
            number = int(name[2:-6]) if name.startswith("ch") and name[2:-6].isdigit() else 0
            shown = f"ch{(copy_of or {}).get(number, number)}.xhtml" if number else name
            members[f"OEBPS/{name}"] = (
                '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><body>'
                f"<p>text of {shown}</p></body></html>"
            )
        for name, text in members.items():
            info = zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, text)
    return buffer.getvalue()


@dataclass
class RangedBench:
    lib: LibEnv
    item_id: str
    unit_ids: list[str]

    @property
    def edition(self) -> str:
        return self.lib.chain["edition"]

    def kept(self) -> Path:
        return self.lib.data / "ranged" / f"{self.item_id}.epub"


def set_edition(lib: LibEnv, *, policy: str = "closed_only", status: str = "ongoing") -> None:
    with lib.engine.begin() as connection:
        connection.execute(
            sa.update(catalog.edition)
            .where(catalog.edition.c.id == lib.chain["edition"])
            .values(volume_policy=policy, upstream_status=status)
        )


def ranged_bench(
    lib: LibEnv,
    count: int = 3,
    *,
    closed: bool = True,
    policy: str = "closed_only",
    status: str = "ongoing",
    first_number: int | None = None,
    data: bytes | None = None,
    declare_bounds: bool = True,
    declared: Sequence[int] | None = None,
    label: str | None = "Vol.1",
) -> RangedBench:
    """What a `ranged_content` service leaves: one item covering `count` Units (the chapters
    `declared`, default all, with their bounds in the file) and the kept file."""
    set_edition(lib, policy=policy, status=status)
    item_id = new_ulid()
    wanted = list(declared) if declared is not None else list(range(1, count + 1))
    units: list[str] = []
    with lib.engine.begin() as connection:
        connection.execute(
            sa.insert(export.export_item).values(
                id=item_id, edition_id=lib.chain["edition"],
                kind="volume" if label else "bundle",
                frozen_attrs={"volume_label": label} if label else {}, closed=closed,
            )
        )  # fmt: skip
        top = connection.execute(
            sa.select(sa.func.max(catalog.unit.c.number_sort)).where(
                catalog.unit.c.edition_id == lib.chain["edition"]
            )
        ).scalar()
        start = first_number if first_number is not None else int(top or 0) + 100
        for position, n in enumerate(wanted):
            unit = add_unit(connection, lib.chain["edition"], str(start + position))
            units.append(unit)
            connection.execute(
                sa.insert(export.unit_coverage).values(
                    carrier_type="export_item", carrier_id=item_id, unit_id=unit, position=position,
                    extent="full", origin="provider",
                    start_locator=chapter_path(n) if declare_bounds else None,
                    end_locator=chapter_path(n) if declare_bounds else None,
                )
            )  # fmt: skip
    write_data_file(lib.data, f"ranged/{item_id}.epub", data or volume_epub(count))
    return RangedBench(lib, item_id, units)


def signer(lib: LibEnv) -> ResplitSigner:
    """A signer of resplit tokens over the /data of the environment: each call is a new object
    (a new process, a restart), the key is the file."""
    return ResplitSigner(lib.data)


def regrow(bench: RangedBench, chapters: int, *, data: bytes | None = None) -> None:
    """What the keeper does when a chapter joins (or leaves) the rolling volume: the same item,
    its coverage rewritten for the first `chapters` chapters, the kept file replaced."""
    engine = bench.lib.engine
    with engine.begin() as connection:
        while len(bench.unit_ids) < chapters:
            top = connection.execute(
                sa.select(sa.func.max(catalog.unit.c.number_sort)).where(
                    catalog.unit.c.edition_id == bench.edition
                )
            ).scalar()
            bench.unit_ids.append(add_unit(connection, bench.edition, str(int(top or 0) + 1)))
        cover = export.unit_coverage
        connection.execute(
            sa.delete(cover).where(
                cover.c.carrier_type == "export_item", cover.c.carrier_id == bench.item_id
            )
        )
        for position, unit in enumerate(bench.unit_ids[:chapters]):
            connection.execute(
                sa.insert(cover).values(
                    carrier_type="export_item", carrier_id=bench.item_id, unit_id=unit,
                    position=position, extent="full", origin="provider",
                    start_locator=chapter_path(position + 1),
                    end_locator=chapter_path(position + 1),
                )
            )  # fmt: skip
    write_data_file(bench.lib.data, f"ranged/{bench.item_id}.epub", data or volume_epub(chapters))
