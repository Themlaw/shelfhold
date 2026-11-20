"""The read flag of Suwayomi's chapters, read and written for the push of the owner's state
(M4 L17, EF-SRC-026).

An adapter of the port `acquisition.application.read_state_push.ReadStateRemote`: it lists the
chapters of one series (by the unit key of the follow, `source_key:chapter_url`, never by a local
id) and sets `isRead` on a batch of chapters. It reads and writes nothing else: no download, no
refresh, no page, no bookmark. A refusal of the server is `RemoteRefused`, an outage or a server
asleep is `RemoteUnavailable`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from shelfhold_sdk.fetcher import Fetcher
from shelfhold_sdk.refs import SeriesRef

from shelfhold.acquisition.application.read_state_push import (
    RemoteChapter,
    RemoteRefused,
    RemoteUnavailable,
)
from shelfhold.plugin_host.errors import failure_class_of

__all__ = ["SuwayomiReadState"]


class SuwayomiReadState:
    def __init__(self, service: Any, fetcher: Fetcher) -> None:
        self._service = service
        self._fetcher = fetcher

    async def chapters(self, source_key: str, remote_id: str) -> dict[str, RemoteChapter]:
        ref = SeriesRef(source_key=source_key, remote_id=remote_id, title="")
        try:
            flags = await self._service.read_flags(self._fetcher, ref)
        except Exception as error:
            raise _mapped(error) from None
        return {key: RemoteChapter(chapter_id=i, is_read=read) for key, (i, read) in flags.items()}

    async def set_read(self, chapter_ids: Sequence[int], *, read: bool) -> None:
        try:
            await self._service.set_chapters_read(self._fetcher, list(chapter_ids), read)
        except Exception as error:
            raise _mapped(error) from None


def _mapped(error: Exception) -> Exception:
    """`infra` (down, asleep, throttled, a 5xx) is a pause; everything else is a refusal that the
    pass reports with its class (no message, no content)."""
    failure = failure_class_of(error)
    if failure == "infra":
        return RemoteUnavailable()
    return RemoteRefused(failure)
