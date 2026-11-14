"""A real import folder over the library bench: real files, a real database, the real stores."""

import io
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any

import sqlalchemy as sa

from shelfhold.export.application.locks import work_lock_for
from shelfhold.infrastructure.clock import SystemClock
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import acquisition, storage
from shelfhold.library.adapters.epub_derivative import UntrustedEpubStore
from shelfhold.library.adapters.import_scan import ImportFolder, ImportItem
from shelfhold.library.adapters.pdf_store import PdfStore
from shelfhold.library.adapters.storage import PassthroughStore
from shelfhold.sdk.testing.corpus.trapped_epubs.build import Member, epub_bytes, opf, xhtml
from tests.library_env import LibEnv, lib_env
from tests.null_tombstones import NONE_PURGED

OLD = 3_600  # seconds: well past the settling time

CLEAN_EPUB = epub_bytes(xhtml("<p>chapter</p>"))
SCRIPT_EPUB = epub_bytes(xhtml("<p>chapter</p><script>alert(1)</script>"))


def _plain_pdf() -> bytes:
    """A well-formed one-page PDF: the strict reader proves it plain."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


PDF = _plain_pdf()
MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 64

ENCRYPTION_NS = (
    'xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
    'xmlns:enc="http://www.w3.org/2001/04/xmlenc#"'
)


def encryption_xml(algorithm: str, uri: str, extra: str = "") -> bytes:
    return (
        f'<?xml version="1.0"?><encryption {ENCRYPTION_NS}><enc:EncryptedData>'
        f'<enc:EncryptionMethod Algorithm="{algorithm}"/>{extra}'
        f'<enc:CipherData><enc:CipherReference URI="{uri}"/></enc:CipherData>'
        "</enc:EncryptedData></encryption>"
    ).encode()


AES = "http://www.w3.org/2001/04/xmlenc#aes128-cbc"
IDPF = "http://www.idpf.org/2008/embedding"
ADOBE_FONTS = "http://ns.adobe.com/pdf/enc#RC"


def epub_with(*extra: Member, opf_bytes: bytes | None = None) -> bytes:
    return epub_bytes(xhtml("<p>chapter</p>"), extra=extra, opf_bytes=opf_bytes)


def personalised_opf() -> bytes:
    return opf().replace(
        b"</metadata>",
        b"<dc:rights>Licensed to Some Person, order 12345</dc:rights></metadata>",
    )


class Router:
    """Files by the extension of the destination, as the composition root will."""

    def __init__(self, stores: dict[str, Any]) -> None:
        self.stores = stores
        self.calls = 0

    def ingest(self, **kw: Any) -> Any:
        self.calls += 1
        return self.stores[PurePath(kw["dest"]).suffix].ingest(**kw)


@dataclass
class ImportEnv:
    lib: LibEnv
    incoming: Path
    router: Router
    items: list[ImportItem] = field(default_factory=list)
    folder: ImportFolder = None  # type: ignore[assignment]

    def drop(self, rel: str, data: bytes, *, age: float = OLD) -> Path:
        path = self.incoming / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self.age(path, age)
        return path

    @staticmethod
    def age(path: Path, seconds: float) -> None:
        stamp = time.time() - seconds
        os.utime(path, (stamp, stamp), follow_symlinks=False)

    def build(self, **kw: Any) -> ImportFolder:
        lib = self.lib

        def target(item: ImportItem) -> tuple[str, str]:
            self.items.append(item)
            # the binding says what the import declared
            with write_transaction(lib.engine) as connection:
                binding = acquisition.source_binding
                connection.execute(
                    sa.update(binding)
                    .where(binding.c.id == lib.chain["binding"])
                    .values(content_trust=item.trust)
                )
                connection.execute(
                    sa.update(acquisition.source_item)
                    .where(acquisition.source_item.c.id == binding_item(connection, lib))
                    .values(plugin_id="import_folder")
                )
            return lib.new_unit()

        def place(unit_id: str, variant_id: str, extension: str) -> str:
            return str(lib.manga / "Imported" / f"{variant_id}{extension}")

        options: dict[str, Any] = {
            "engine": lib.engine, "fsops": lib.fsops, "store": self.router,
            "staging_dir": lib.tmp / "staging", "target": target, "place": place,
            "keep_apart": (lib.library, lib.store, lib.data), "sleep": lambda _s: None,
        }  # fmt: skip
        options.update(kw)
        self.folder = ImportFolder(self.incoming, **options)
        return self.folder

    def contents(self) -> list[Any]:
        with self.lib.engine.connect() as connection:
            return list(
                connection.execute(sa.select(storage.content).order_by(storage.content.c.id))
            )

    def entries(self) -> dict[str, tuple[str, str | None]]:
        table = storage.import_entry
        with self.lib.engine.connect() as connection:
            rows = connection.execute(sa.select(table.c.rel_path, table.c.state, table.c.reason))
            return {r.rel_path: (r.state, r.reason) for r in rows}


def binding_item(connection: sa.Connection, lib: LibEnv) -> str:
    binding = acquisition.source_binding
    return str(
        connection.execute(
            sa.select(binding.c.source_item_id).where(binding.c.id == lib.chain["binding"])
        ).scalar_one()
    )


def build_stores(lib: LibEnv, *, with_store: bool = True, max_pdf: int | None = None) -> Router:
    hold = work_lock_for(lib.engine, clock=SystemClock(), timeout=0.0)
    pdf_kw: dict[str, Any] = {} if max_pdf is None else {"max_bytes": max_pdf}
    return Router(
        {
            ".cbz": PassthroughStore(
                lib.engine, lib.fsops, lib.locate, lib.data, tombstones=NONE_PURGED,
                hold_work=hold,
            ),
            ".epub": UntrustedEpubStore(
                lib.engine, lib.fsops, lib.locate, lib.data,
                store_dir=str(lib.store) if with_store else None, tombstones=NONE_PURGED,
                hold_work=hold,
            ),
            ".pdf": PdfStore(
                lib.engine, lib.fsops, lib.locate, tombstones=NONE_PURGED, hold_work=hold,
                **pdf_kw,
            ),
        }
    )  # fmt: skip


@contextmanager
def import_env(
    *, with_store: bool = True, max_pdf: int | None = None, **folder: Any
) -> Iterator[ImportEnv]:
    with lib_env() as lib:
        incoming = lib.tmp / "incoming"
        incoming.mkdir()
        (lib.tmp / "staging").mkdir(exist_ok=True)
        env = ImportEnv(lib, incoming, build_stores(lib, with_store=with_store, max_pdf=max_pdf))
        env.build(**folder)
        yield env
