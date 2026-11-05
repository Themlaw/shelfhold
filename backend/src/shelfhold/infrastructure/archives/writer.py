"""ArchiveWriter: the only place that writes a ZIP container (ENF-SEC-002, EF-EXP-063).

Output is deterministic and carries nothing about the host or the tool: fixed timestamps (the
ZIP epoch), Unix attributes `0644`, no extra field, no comments. Entry names get the same checks
as `safe_open_archive` applies on the way in. The writer fills a stream the caller owns (the
staging file of `fsops`); it never opens a path.
"""

from __future__ import annotations

import io
import zipfile
from types import TracebackType
from typing import BinaryIO, Final, Self

from shelfhold.infrastructure.archives.safe_open import _check_name, _dedup_key

_EPOCH: Final = (1980, 1, 1, 0, 0, 0)
_CHUNK: Final = 64 * 1024


class ArchiveWriter:
    def __init__(self, out: BinaryIO) -> None:
        self._names: set[str] = set()
        self._zip = zipfile.ZipFile(out, "w")

    def add(
        self, name: str, data: bytes | BinaryIO | io.RawIOBase, *, compress: bool = False
    ) -> None:
        """Append one entry; images are stored (already compressed), text is deflated."""
        _check_name(name)
        key = _dedup_key(name)
        if key in self._names:
            raise ValueError(f"duplicate entry {name!r}")
        self._names.add(key)
        info = zipfile.ZipInfo(name, _EPOCH)
        info.compress_type = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
        info.create_system = 3
        info.external_attr = 0o644 << 16
        with self._zip.open(info, "w") as entry:
            if isinstance(data, bytes | bytearray):
                entry.write(data)
            else:
                while chunk := data.read(_CHUNK):
                    entry.write(chunk)

    def close(self) -> None:
        self._zip.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
