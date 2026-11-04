# SPDX-License-Identifier: MIT
"""Host-side `Fetcher` seen by plugins (02 section 4.5): the only live object a plugin receives.

Everything it takes or returns is serialisable. SSRF guard, allow-list, rate limits, cookies and
secret injection are the host's job; a plugin never reads a secret in clear.
"""

from typing import Any, Protocol

from pydantic import Field

from shelfhold.sdk.models import SdkModel
from shelfhold.sdk.refs import ImageRequest


class Response(SdkModel):
    status: int
    headers: dict[str, str] = Field(default_factory=dict)
    body: bytes = b""


class DownloadedFile(SdkModel):
    sha256: str
    size: int
    content: bytes = b""  # the host keeps the file on disk; fakes inline it


class CookieJarView(SdkModel):
    """Read-only snapshot of the cookies of a source, never a live jar."""

    cookies: dict[str, str] = Field(default_factory=dict)


class Fetcher(Protocol):
    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        referer: str | None = None,
        cache_ttl: int | None = None,
    ) -> Response: ...

    async def post(
        self,
        url: str,
        *,
        data: dict[str, Any] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Response: ...

    async def download(self, req: ImageRequest) -> DownloadedFile: ...

    def cookies(self) -> CookieJarView: ...
