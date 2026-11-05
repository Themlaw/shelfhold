"""Receive a CBZ from a connected service: streamed, bounded, then audited (ENF-SEC-002).

The bytes go to an anonymous-named temporary file in the staging folder, chunk by chunk, counted
and hashed on the way; the bound is checked on the declared length and on what actually arrives.
Nothing here opens the archive: `PassthroughStore.ingest` does, through `safe_open_archive` and the
CBZ allowlist, before anything is stored. The file is removed when the `with` block ends.
"""

import tempfile
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from shelfhold.infrastructure.fetcher import FetchError, GuardedFetcher


@dataclass(frozen=True)
class Received:
    path: Path  # the staged file, `.cbz` by name
    size: int
    sha256: str  # `sha256:<hex>`, of the bytes as received


@asynccontextmanager
async def receive_cbz(
    fetcher: GuardedFetcher,
    url: str,
    *,
    staging_dir: Path,
    max_bytes: int,
    headers: Mapping[str, str] | None = None,
    total_timeout: float = 900.0,
) -> AsyncIterator[Received]:
    with tempfile.NamedTemporaryFile(dir=staging_dir, suffix=".cbz", prefix="recv-") as sink:
        result = await fetcher.download(
            url, sink, max_bytes=max_bytes, headers=headers, total_timeout=total_timeout
        )
        if not 200 <= result.status < 300:
            raise FetchError(f"the service answered {result.status}, not a file")
        sink.flush()
        yield Received(Path(sink.name), result.size, result.sha256)
