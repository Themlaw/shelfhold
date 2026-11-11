"""A synthetic instance full of trap values, for the diagnostic bundle (ENF-LEG-009, EF-BIB-041).

Every value a bundle must never carry is planted where the instance really keeps it: titles in the
catalog and in the path of a kept file, the URL and the domain of a source, the name of a
sub-source, an e-mail address as the admin's user name, an IP address and a host name in the
settings of a connected service, sealed secrets, a watermarked EPUB whose watermark names the
buyer. A log file, as `docker compose logs` writes it, quotes all of them.

`leaks(bundle, traps)` opens the bundle as an attacker would: every member, every nested archive,
and in each one the text as written, percent-decoded (once, twice, `+` as a space), and the
base64 runs decoded at every alignment. It knows nothing of the production code.
"""

from __future__ import annotations

import base64
import binascii
import bz2
import gzip
import hashlib
import io
import json
import lzma
import re
import tarfile
import zipfile
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, quote_plus, unquote, unquote_plus

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.migrate import migrate
from shelfhold.infrastructure.schema import acquisition, catalog, storage
from shelfhold.infrastructure.secrets.diagnostics import boot_check
from shelfhold.infrastructure.secrets.vault import SecretVault
from tests.auth_env import create_admin, db_rows
from tests.integration.test_schema_catalog import (
    add_binding,
    add_edition,
    add_unit,
    add_variant,
    add_work,
)
from tests.redaction_support import forget_secret_values, synthetic_text

WORK_TITLE = "Le Jardin des Pivoines Écarlates"
SERIES_TITLE = "Chroniques du Phare Oublié"
UNIT_TITLE = "La Nuit des Lanternes"
SUB_SOURCE = "Bibliothèque Lunaire du Nord"
SOURCE_HOST = "lunar-shelf.example"
SERVICE_IP = "10.77.31.12"
SERVICE_HOST = "connected-service.home.example"
PUBLIC_HOST = "atelier.family.example"


def _email() -> str:
    return f"reader.{synthetic_text('diag-email')[:8].lower()}@family.example"


@dataclass
class Traps:
    """What the bundle must never hold, by category (the category is what a failure names)."""

    values: dict[str, str] = field(default_factory=dict)
    watermarked_bytes: bytes = b""
    watermarked_sha256: str = ""
    watermarked_size: int = 0
    ids: dict[str, str] = field(default_factory=dict)  # internal ids, not traps

    def __iter__(self) -> Iterator[tuple[str, str]]:
        return iter(self.values.items())


def _epub(watermark: str) -> bytes:
    """A small EPUB whose OPF and one page carry the buyer's watermark."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr(
            "OEBPS/content.opf",
            f"<package><metadata><dc:rights>{watermark}</dc:rights></metadata></package>",
        )
        archive.writestr("OEBPS/page.xhtml", f"<html><body><p>{watermark}</p></body></html>")
    return out.getvalue()


def log_file(path: Path, traps: Traps) -> Path:
    """What `docker compose logs shelfhold > file` gives: a prefix, then one JSON line per event,
    every trap quoted at least once, in a known field, an unknown one and a message."""
    v, ids = traps.values, traps.ids
    lines = [
        {"event": "startup.config", "level": "info", "logger": "shelfhold.main",
         "timestamp": "2026-11-11T08:00:00Z", "version": "0.2.0",
         "config": {"public_url": f"https://{PUBLIC_HOST}", "host": SERVICE_IP}},
        {"event": "fsops.write_atomic", "level": "debug", "logger": "shelfhold.fsops",
         "timestamp": "2026-11-11T08:00:01Z", "job_id": ids["job"],
         "path": f"/library/manga/{WORK_TITLE}/{UNIT_TITLE}.cbz"},
        {"event": "export.op_done", "level": "debug", "logger": "shelfhold.export",
         "timestamp": "2026-11-11T08:00:02Z", "unit_id": ids["unit"], "kind": "write",
         "rel_path": f"{SERIES_TITLE}/{UNIT_TITLE}.cbz", "duration_ms": 12, "wrote": True},
        {"event": "http.call", "level": "debug", "logger": "shelfhold.fetcher",
         "timestamp": "2026-11-11T08:00:03Z", "route": f"{SOURCE_HOST}/manga/7",
         "url": v["source_url"], "status": 200, "method": "GET"},
        {"event": "follow.series_looked", "level": "debug", "logger": "shelfhold.follow",
         "timestamp": "2026-11-11T08:00:04Z", "source": SUB_SOURCE, "outcome": "ok",
         "series_id": ids["work"], "user_id": ids["user"], "title": SERIES_TITLE},
        {"event": "auth.login", "level": "info", "logger": "shelfhold.identity",
         "timestamp": "2026-11-11T08:00:05Z", "username": v["email"], "client": SERVICE_IP,
         "user_id": ids["user"], "outcome": "ok"},
        {"event": "job.failure_detail", "level": "debug", "logger": "shelfhold.jobs",
         "timestamp": "2026-11-11T08:00:06Z", "error": "ConnectError",
         "message": f"connect to {SERVICE_HOST} ({SERVICE_IP}) with {v['source_password']}",
         "outcome": v["source_password"].lower()},
        {"event": "kavita.call", "level": "debug", "logger": "shelfhold.kavita",
         "timestamp": "2026-11-11T08:00:07Z", "reason": v["api_token"], "status": 401,
         "code": "target.forbidden"},
    ]  # fmt: skip
    text = "".join(f"shelfhold-1  | {json.dumps(line, ensure_ascii=False)}\n" for line in lines)
    text += f"shelfhold-1  | Traceback: open /store/{WORK_TITLE}.epub failed for {v['email']}\n"
    path.write_text(text, encoding="utf-8")
    return path


def trap_instance(monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp: Path) -> Traps:
    """Migrate, create the admin (an e-mail address), seal two secrets, fill the catalog."""
    email = _email()
    api_token = synthetic_text("diag-api-token")
    source_password = synthetic_text("diag-source-password", specials=True)
    wake_token = synthetic_text("diag-wake-token")
    source_url = f"https://{SOURCE_HOST}/manga/7-{quote(WORK_TITLE)}"
    monkeypatch.setenv("SHELFHOLD_API_TOKEN", api_token)
    monkeypatch.setenv("SHELFHOLD_WAKE_TOKEN", wake_token)
    monkeypatch.setenv("SHELFHOLD_PUBLIC_URL", f"https://{PUBLIC_HOST}")
    monkeypatch.setenv("SHELFHOLD_WAKE_URL", f"http://{SERVICE_HOST}:10000")
    migrate(settings)
    assert create_admin(monkeypatch, settings, username=email) == 0
    keyring = boot_check(settings)
    vault = SecretVault(keyring)
    watermark = f"Exemplaire de {email}, commande {synthetic_text('diag-order')[:10]}"
    epub = _epub(watermark)
    kept = f"{WORK_TITLE}/{UNIT_TITLE}.epub"
    (settings.store_dir / WORK_TITLE).mkdir(parents=True, exist_ok=True)
    (settings.store_dir / kept).write_bytes(epub)
    sha = "sha256:" + hashlib.sha256(epub).hexdigest()
    engine = create_db_engine(settings.database_path)
    try:
        with engine.begin() as connection:
            ((user_id,),) = connection.execute(sa.text("SELECT id FROM user")).all()
            series_id = new_ulid()
            connection.execute(
                sa.insert(catalog.series).values(
                    id=series_id, title=SERIES_TITLE, sort_title=SERIES_TITLE.lower()
                )
            )
            work = add_work(connection, title=WORK_TITLE, sort_title=WORK_TITLE.lower())
            edition = add_edition(connection, work, media="text")
            connection.execute(
                sa.insert(acquisition.sub_source).values(
                    plugin_id="suwayomi", source_key="suwayomi:9431", name=SUB_SOURCE,
                    lang="fr", enabled=True,
                )
            )  # fmt: skip
            item = new_ulid()
            connection.execute(
                sa.insert(acquisition.source_item).values(
                    id=item, plugin_id="suwayomi", source_key="suwayomi:9431",
                    remote_id=f"/manga/7-{WORK_TITLE}", url=source_url,
                )
            )  # fmt: skip
            binding = add_binding(connection, work, edition, item)
            unit = add_unit(connection, edition, "1", title=UNIT_TITLE)
            variant = add_variant(connection, unit, binding)
            connection.execute(
                sa.insert(storage.content).values(
                    id=new_ulid(), unit_id=unit, variant_id=variant, kind="text",
                    media_type="application/epub+zip", format="epub", watermarked=True,
                    watermark_origin="user", retention="keep", path=kept, original_sha256=sha,
                    store_provenance="received", size=len(epub),
                )
            )  # fmt: skip
            connection.execute(
                sa.insert(acquisition.source_config).values(
                    plugin_id="suwayomi",
                    settings={"url": f"http://{SERVICE_IP}:4567", "host": SERVICE_HOST},
                    terms_url=f"https://{SOURCE_HOST}/terms",
                )
            )
            vault.seal_row(
                connection, "source_config", "suwayomi",
                json.dumps({"username": "svc", "password": source_password}).encode(),
            )  # fmt: skip
    finally:
        engine.dispose()
    assert db_rows(settings, "SELECT count(*) FROM content WHERE watermarked = 1") == [(1,)]
    forget_secret_values()  # a CLI process starts with an empty registry: the bundle must look
    traps = Traps(
        values={
            "work_title": WORK_TITLE,
            "series_title": SERIES_TITLE,
            "unit_title": UNIT_TITLE,
            "sub_source": SUB_SOURCE,
            "source_host": SOURCE_HOST,
            "source_url": source_url,
            "service_ip": SERVICE_IP,
            "service_host": SERVICE_HOST,
            "public_host": PUBLIC_HOST,
            "email": email,
            "user_id": str(user_id),
            "api_token": api_token,
            "wake_token": wake_token,
            "source_password": source_password,
            "watermark": watermark,
        },
        watermarked_bytes=epub,
        watermarked_sha256=sha,
        watermarked_size=len(epub),
        ids={"job": new_ulid(), "unit": unit, "work": work, "user": str(user_id)},
    )
    return traps


# -- reading a bundle the way somebody who wants the values would -------------------------------

_B64 = re.compile(rb"[A-Za-z0-9+/_-]{8,}={0,2}")


def _base64_decodings(data: bytes) -> Iterator[bytes]:
    for match in _B64.finditer(data):
        run = match.group().rstrip(b"=")
        for offset in range(4):  # a value encoded inside a longer blob sits at any alignment
            chunk = run[offset:]
            chunk = chunk[: len(chunk) - len(chunk) % 4]
            if not chunk:
                continue
            for alt in (None, b"-_"):
                try:
                    yield base64.b64decode(chunk, altchars=alt, validate=False)
                except (binascii.Error, ValueError):
                    continue


def _decompressed(data: bytes) -> Iterator[bytes]:
    """Every archive or compressed stream inside `data`, opened (nested ones too)."""
    if data[:2] == b"PK" and zipfile.is_zipfile(io.BytesIO(data)):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for name in archive.namelist():
                yield name.encode()
                yield archive.read(name)
        return
    for opener in (gzip.decompress, bz2.decompress, lzma.decompress, zlib.decompress):
        try:
            yield opener(data)
            return
        except (OSError, ValueError, EOFError, lzma.LZMAError, zlib.error):
            continue
    try:
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            for member in archive.getmembers():
                yield member.name.encode()
                extracted = archive.extractfile(member)
                if extracted is not None:
                    yield extracted.read()
    except (tarfile.TarError, OSError):
        return


def _views(data: bytes, depth: int = 0) -> Iterator[str]:
    if depth > 4:
        return
    text = data.decode("utf-8", errors="replace")
    pending = [text]
    for _ in range(3):
        nxt = []
        for t in pending:
            for decoder in (unquote, unquote_plus):
                decoded = decoder(t)
                if decoded != t:
                    nxt.append(decoded)
        pending = nxt
        yield from nxt
    yield text
    yield data.decode("latin-1")
    for decoded in _base64_decodings(data):
        yield decoded.decode("utf-8", errors="ignore")
    for inner in _decompressed(data):
        yield from _views(inner, depth + 1)


def _needles(value: str) -> set[str]:
    forms = {value, value.lower(), quote(value, safe=""), quote_plus(value), quote(value)}
    forms |= {base64.b64encode(value.encode()).decode().rstrip("=")}
    return {f for f in forms if f}


def leaks(bundle: bytes, traps: Traps) -> list[str]:
    """The categories of trap found anywhere in the bundle, in any form."""
    views = list(_views(bundle))
    found = []
    for category, value in traps:
        needles = _needles(value)
        if any(n in view or n.lower() in view.lower() for view in views for n in needles):
            found.append(category)
    if traps.watermarked_bytes:
        raw_views = [bundle, *_decompressed(bundle)]
        if any(traps.watermarked_bytes in data for data in raw_views):
            found.append("watermarked_file")
    return found
