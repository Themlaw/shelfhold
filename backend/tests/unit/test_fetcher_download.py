"""Streaming download: the received CBZ goes to a sink, bounded, behind the same SSRF guard.

PLAN section 7, "L19/L20 (relecture de L17)": the size bound is dedicated and configurable, and the
bytes never sit whole in memory.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
from collections.abc import AsyncIterator

import httpx2
import pytest

from shelfhold.infrastructure.fetcher import (
    FetchLimits,
    FetchTooLarge,
    GuardedFetcher,
    SsrfBlocked,
)
from tests.fakes_fetcher import FakeResolver, Recorder

PUBLIC = "93.184.216.34"
DNS = FakeResolver({"files.example.org": [PUBLIC], "evil.example.org": ["10.0.0.5"]})


class Sink(io.BytesIO):
    """Records every write: the fetcher must hand over chunks, never one joined body."""

    def __init__(self) -> None:
        super().__init__()
        self.writes: list[int] = []

    def write(self, data: bytes, /) -> int:  # type: ignore[override]
        self.writes.append(len(data))
        return super().write(data)


def chunked(parts: int, size: int) -> AsyncIterator[bytes]:
    async def gen() -> AsyncIterator[bytes]:
        for i in range(parts):
            yield bytes([i % 251]) * size

    return gen()


def run(fetcher: GuardedFetcher, url: str, sink: Sink, **kw: object):  # noqa: ANN201
    return asyncio.run(fetcher.download(url, sink, **kw))  # type: ignore[arg-type]


@pytest.mark.req("EF-BIB-006", "ENF-SEC-002")
def test_download_streams_to_the_sink_and_reports_size_and_sha256() -> None:
    rec = Recorder(lambda r: httpx2.Response(200, content=chunked(8, 4096)))
    sink = Sink()
    result = run(
        GuardedFetcher(DNS, transport=rec.transport), "http://files.example.org/c.cbz", sink,
        max_bytes=1 << 20,
    )  # fmt: skip
    body = sink.getvalue()
    assert result.status == 200 and result.size == len(body) == 8 * 4096
    assert result.sha256 == "sha256:" + hashlib.sha256(body).hexdigest()
    assert len(sink.writes) > 1  # chunked


@pytest.mark.req("ENF-SEC-002")
def test_download_bound_is_its_own_and_not_the_32_mib_body_cap() -> None:
    rec = Recorder(lambda r: httpx2.Response(200, content=chunked(4, 1 << 20)))
    fetcher = GuardedFetcher(DNS, transport=rec.transport, limits=FetchLimits(max_bytes=1000))
    sink = Sink()
    result = run(fetcher, "http://files.example.org/c.cbz", sink, max_bytes=8 << 20)
    assert result.size == 4 << 20  # 4 MiB passes: the call's own bound applies, not the default
    with pytest.raises(FetchTooLarge):
        run(fetcher, "http://files.example.org/c.cbz", Sink(), max_bytes=3 << 20)


@pytest.mark.req("ENF-SEC-002")
def test_download_stops_at_the_bound_even_when_the_length_header_lies() -> None:
    rec = Recorder(
        lambda r: httpx2.Response(200, headers={"content-length": "10"}, content=chunked(50, 1000))
    )
    sink = Sink()
    with pytest.raises(FetchTooLarge):
        run(GuardedFetcher(DNS, transport=rec.transport), "http://files.example.org/c", sink, max_bytes=5000)
    assert len(sink.getvalue()) <= 5000 + 1000  # stopped at the first chunk past the bound


@pytest.mark.req("ENF-SEC-002")
def test_download_declared_length_above_the_bound_is_refused_before_any_write() -> None:
    rec = Recorder(
        lambda r: httpx2.Response(200, headers={"content-length": str(1 << 30)}, content=b"x")
    )
    sink = Sink()
    with pytest.raises(FetchTooLarge):
        run(GuardedFetcher(DNS, transport=rec.transport), "http://files.example.org/c", sink, max_bytes=1 << 20)
    assert sink.writes == []


@pytest.mark.req("ENF-SEC-001")
def test_download_keeps_the_ssrf_guard_and_the_redirect_rules() -> None:
    rec = Recorder(lambda r: httpx2.Response(200, content=b"x"))
    with pytest.raises(SsrfBlocked):
        run(GuardedFetcher(DNS, transport=rec.transport), "http://evil.example.org/c", Sink(), max_bytes=10)
    assert rec.requests == []  # refused before any connection

    def respond(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/start":
            return httpx2.Response(302, headers={"location": "http://evil.example.org/c"}, content=b"junk")
        return httpx2.Response(200, content=b"x")

    sink = Sink()
    with pytest.raises(SsrfBlocked):
        run(GuardedFetcher(DNS, transport=Recorder(respond).transport), "http://files.example.org/start", sink, max_bytes=10)
    assert sink.getvalue() == b""  # the body of a redirection is never written


@pytest.mark.req("ENF-SEC-002")
def test_download_writes_nothing_for_an_error_status() -> None:
    rec = Recorder(lambda r: httpx2.Response(404, content=b"<html>not found</html>"))
    sink = Sink()
    result = run(GuardedFetcher(DNS, transport=rec.transport), "http://files.example.org/c", sink, max_bytes=1000)
    assert result.status == 404 and result.size == 0 and sink.getvalue() == b""
