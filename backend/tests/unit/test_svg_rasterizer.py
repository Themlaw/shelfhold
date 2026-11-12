# ruff: noqa: E501
"""EF-BIB-051 (6), rasterisation: an SVG page of an untrusted CBZ is audited, then drawn to a PNG
inside the bounded decode worker, with no network and no file read. Every refusal is closed by
default: whatever is not clearly a plain SVG is an error, never a blank page."""

from __future__ import annotations

import contextlib
import socket
import sys
import threading
from typing import Any

import pytest

from shelfhold.infrastructure.imaging import ImageRejected, WorkerLimits, rasterize_svg
from shelfhold.infrastructure.imaging import client as imaging_client

NS = "http://www.w3.org/2000/svg"
XLINK = "http://www.w3.org/1999/xlink"
GOOD = (
    f'<svg xmlns="{NS}" width="40" height="30" viewBox="0 0 40 30">'
    '<rect width="40" height="30" fill="#c00"/></svg>'
).encode()
LIMITS = WorkerLimits(memory_bytes=1024**3, cpu_seconds=10, wall_seconds=30)


def svg(inner: str, attrs: str = 'width="10" height="10"') -> bytes:
    return f'<svg xmlns="{NS}" xmlns:xlink="{XLINK}" {attrs}>{inner}</svg>'.encode()


def refused(data: bytes, **kwargs: Any) -> ImageRejected:
    with pytest.raises(ImageRejected) as caught:
        rasterize_svg(data, limits=LIMITS, **kwargs)
    return caught.value


@pytest.mark.req("EF-BIB-051")
def test_svg_rasterizer_draws_a_lossless_png() -> None:
    result = rasterize_svg(GOOD, limits=LIMITS)
    assert result.png.startswith(b"\x89PNG\r\n\x1a\n")
    assert (result.width, result.height) == (40, 30)
    import io

    from PIL import Image

    pixel = Image.open(io.BytesIO(result.png)).convert("RGB").getpixel((5, 5))
    assert pixel == (204, 0, 0)


@pytest.mark.req("EF-BIB-051")
def test_svg_rasterizer_has_no_network_and_external_refs_ignored() -> None:
    """The reference is never fetched: a listener on loopback sees no connection, and the SVG
    that names it is refused rather than drawn without it."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    listener.settimeout(0.5)
    port = listener.getsockname()[1]
    hits: list[object] = []

    def accept() -> None:
        with contextlib.suppress(OSError):
            hits.append(listener.accept())

    thread = threading.Thread(target=accept)
    thread.start()
    try:
        err = refused(svg(f'<image href="http://127.0.0.1:{port}/p.png" width="5" height="5"/>'))
        assert err.code == "image.format_refused"
    finally:
        thread.join()
        listener.close()
    assert hits == []


@pytest.mark.req("EF-BIB-051")
def test_svg_rasterizer_worker_has_no_network_in_its_sandbox() -> None:
    from shelfhold.infrastructure.imaging import diagnose_worker

    facts = diagnose_worker("network", port=9)
    assert facts["python_socket"] == "denied"
    assert facts["raw_connect"] in ("failed", "unavailable")


@pytest.mark.req("EF-BIB-051")
@pytest.mark.parametrize(
    "document",
    [
        b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        + f'<svg xmlns="{NS}" width="9" height="9"><text>&xxe;</text></svg>'.encode(),
        b'<!DOCTYPE svg SYSTEM "http://127.0.0.1:9/evil.dtd">'
        + f'<svg xmlns="{NS}" width="9" height="9"/>'.encode(),
        b'<!DOCTYPE svg [<!ENTITY % p SYSTEM "http://127.0.0.1:9/x"> %p;]>'
        + f'<svg xmlns="{NS}" width="9" height="9"/>'.encode(),
    ],
    ids=["xxe-file", "external-dtd", "parameter-entity"],
)
def test_svg_xxe_and_external_entities_refused(document: bytes) -> None:
    assert refused(document).code == "image.format_refused"


@pytest.mark.req("EF-BIB-051")
def test_svg_billion_laughs_refused_without_expansion() -> None:
    lol = ['<!ENTITY a0 "lol">']
    for i in range(1, 10):
        previous = f"&a{i - 1};" * 10
        lol.append(f'<!ENTITY a{i} "{previous}">')
    document = (
        f'<!DOCTYPE svg [{"".join(lol)}]><svg xmlns="{NS}" width="9" height="9"><text>&a9;</text></svg>'
    ).encode()
    assert refused(document).code == "image.format_refused"


@pytest.mark.req("EF-BIB-051")
@pytest.mark.parametrize(
    "inner",
    [
        '<image href="file:///etc/hostname" width="5" height="5"/>',
        '<image xlink:href="file:///etc/hostname" width="5" height="5"/>',
        '<image href="http://evil.example/p.png" width="5" height="5"/>',
        '<image href="//evil.example/p.png" width="5" height="5"/>',
        '<image href="secret.png" width="5" height="5"/>',
        '<image href="../../secret.png" width="5" height="5"/>',
        '<image href="data:image/png;base64,iVBORw0KGgo=" width="5" height="5"/>',
        '<image href="data:image/svg+xml;base64,PHN2Zy8+" width="5" height="5"/>',
        '<use href="data:image/svg+xml;utf8,&lt;svg/&gt;#a"/>',
        '<use xlink:href="http://evil.example/x.svg#a"/>',
        '<feImage href="http://evil.example/x.png"/>',
        '<a href="javascript:alert(1)"><rect width="1" height="1"/></a>',
        '<rect width="5" height="5" style="fill:url(http://evil.example/p)"/>',
        "<style>@import url(http://evil.example/a.css);</style>",
    ],
    ids=[
        "file",
        "xlink-file",
        "http",
        "scheme-relative",
        "relative-file",
        "parent-file",
        "data-png",
        "data-svg-nested",
        "use-data",
        "use-http",
        "feimage",
        "javascript-url",
        "css-url",
        "css-import",
    ],
)
def test_svg_external_and_nested_references_refused(inner: str) -> None:
    assert refused(svg(inner)).code == "image.format_refused"


@pytest.mark.req("EF-BIB-051")
def test_svg_fragment_reference_inside_the_document_is_drawn() -> None:
    inner = '<defs><rect id="r" width="4" height="4" fill="#00f"/></defs><use href="#r"/>'
    assert rasterize_svg(svg(inner), limits=LIMITS).width == 10


@pytest.mark.req("EF-BIB-051")
@pytest.mark.parametrize(
    "inner",
    [
        "<script>alert(1)</script>",
        '<script xlink:href="http://evil.example/x.js"/>',
        "<foreignObject width='5' height='5'><p xmlns='http://www.w3.org/1999/xhtml'>x</p></foreignObject>",
        "<foreignobject/>",
        "<iframe/>",
        '<animate attributeName="href" values="javascript:alert(1)"/>',
        '<set attributeName="href" to="http://evil.example/"/>',
        '<rect width="1" height="1" onload="alert(1)"/>',
        '<rect width="1" height="1" onclick="alert(1)"/>',
        '<style>@font-face{font-family:x;src:url("http://evil.example/f.woff")}</style>',
        '<style>@font-face{font-family:x;src:local("Arial")}</style>',
        '<font-face font-family="x"><font-face-src><font-face-uri href="http://evil.example/f"/></font-face-src></font-face>',
        '<text font-family="x">a</text><style>@font-face{src:url(f.woff)}</style>',
    ],
    ids=[
        "script",
        "script-href",
        "foreignobject",
        "foreignobject-lower",
        "iframe",
        "animate-href",
        "set-href",
        "onload",
        "onclick",
        "font-remote",
        "font-local",
        "svg-font-face",
        "font-relative",
    ],
)
def test_svg_scripts_foreign_objects_and_external_fonts_refused(inner: str) -> None:
    assert refused(svg(inner)).code == "image.format_refused"


@pytest.mark.req("EF-BIB-051")
@pytest.mark.parametrize(
    "document",
    [
        b"<!doctype html><html><body><svg xmlns='http://www.w3.org/2000/svg' width='9' height='9'/></body></html>",
        b'<html xmlns="http://www.w3.org/1999/xhtml"><body><script>alert(1)</script></body></html>',
        f'<svg xmlns="{NS}" width="9" height="9"/><!--<script>alert(1)</script>-->'.encode()
        + b"<html>",
        b"GIF89a" + f'<svg xmlns="{NS}" width="9" height="9"/>'.encode(),
        f'<svg xmlns="{NS}" width="9" height="9"><body xmlns="http://www.w3.org/1999/xhtml"/></svg>'.encode(),
        b"<svg width='9' height='9'/>",  # no namespace: a polyglot, not an SVG
    ],
    ids=["html-doc", "xhtml-script", "trailing-html", "gif-prefix", "xhtml-child", "no-namespace"],
)
def test_svg_polyglot_and_non_svg_documents_refused(document: bytes) -> None:
    assert refused(document).code == "image.format_refused"


@pytest.mark.req("EF-BIB-051")
@pytest.mark.parametrize(
    "document",
    [
        b"",
        b"\x00\x01\x02garbage",
        b"not xml at all",
        b"<svg",
        b"\xff\xfe<\x00s\x00v\x00g\x00>\x00",
        b"<svg xmlns='http://www.w3.org/2000/svg'><g></svg>",
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 30,
        f'<svg xmlns="{NS}"/>'.encode(),
    ],
    ids=["empty", "binary", "text", "truncated", "utf16", "mismatched", "png", "no-size"],
)
def test_unreadable_svg_is_refused_closed(document: bytes) -> None:
    error = refused(document)
    assert error.code in {"image.format_refused", "image.decode_failed"}


@pytest.mark.req("EF-BIB-051")
def test_svg_with_huge_dimensions_refused_before_drawing() -> None:
    error = refused(svg("", 'width="100000" height="100000"'), max_pixels=1_000_000)
    assert error.code == "image.too_large"
    error = refused(svg("", 'viewBox="0 0 1e9 1e9"'), max_pixels=1_000_000)
    assert error.code == "image.too_large"
    error = refused(svg("", 'width="-5" height="10"'))
    assert error.code in {"image.format_refused", "image.too_large"}
    error = refused(svg("", 'width="10%" height="10"'))
    assert error.code in {"image.format_refused", "image.too_large"}


@pytest.mark.req("EF-BIB-051")
def test_svg_with_too_many_nodes_or_too_deep_refused() -> None:
    wide = svg('<rect width="1" height="1"/>' * 3000)
    assert refused(wide, max_nodes=2000).code == "image.too_large"
    deep = svg("<g>" * 200 + "</g>" * 200)
    assert refused(deep).code == "image.too_large"


@pytest.mark.req("EF-BIB-051")
def test_svg_input_bigger_than_the_cap_refused_before_the_worker(monkeypatch: Any) -> None:
    called: list[str] = []
    monkeypatch.setattr(imaging_client, "run_worker", lambda *a, **k: called.append("x"))
    error = refused(GOOD + b" " * 2048, max_bytes=1024)
    assert error.code == "image.too_large" and called == []


@pytest.mark.req("EF-BIB-051")
def test_svg_render_past_the_deadline_is_refused() -> None:
    """A worker that does not answer in time is killed; the page is refused, never skipped."""
    from shelfhold.infrastructure.imaging.client import run_worker

    slow = [sys.executable, "-c", "import time; time.sleep(30)"]
    with pytest.raises(ImageRejected) as caught:
        run_worker("svg_rasterize", {}, GOOD, limits=WorkerLimits(wall_seconds=1), argv=slow)
    assert caught.value.code == "image.decode_failed" and caught.value.reason == "timeout"


@pytest.mark.req("EF-BIB-051")
def test_svg_worker_failure_is_a_refusal_not_a_blank_page(monkeypatch: Any) -> None:
    def broken(*_a: Any, **_k: Any) -> Any:
        raise ImageRejected("image.worker_failed", "bad reply", "worker")

    monkeypatch.setattr(imaging_client, "run_worker", broken)
    assert refused(GOOD).code == "image.worker_failed"

    monkeypatch.setattr(
        imaging_client, "run_worker", lambda *a, **k: ({"width": 1, "height": 1}, b"")
    )
    assert refused(GOOD).code == "image.worker_failed"  # a reply that is not a PNG
    monkeypatch.setattr(imaging_client, "run_worker", lambda *a, **k: ({}, b"\x89PNG\r\n\x1a\nx"))
    assert refused(GOOD).code == "image.worker_failed"  # a reply with no size


@pytest.mark.req("EF-BIB-051")
def test_svg_render_memory_ceiling_is_applied_to_the_worker() -> None:
    from shelfhold.infrastructure.imaging import diagnose_worker

    limits = WorkerLimits(memory_bytes=1024**3, cpu_seconds=7)
    facts = diagnose_worker("limits", limits=limits)
    assert facts["rlimit_as"][0] == 1024**3 and facts["rlimit_cpu"][0] == 7
    # a hard ceiling too low for the renderer is a refusal, not a crash of the parent
    tiny = WorkerLimits(memory_bytes=160 * 1024**2, cpu_seconds=10, wall_seconds=30)
    big = svg('<rect width="1" height="1"/>', 'width="6000" height="6000"')
    with pytest.raises(ImageRejected):
        rasterize_svg(big, limits=tiny)


@pytest.mark.req("EF-BIB-051")
def test_svg_rasterizer_reads_no_file_while_drawing(tmp_path: Any) -> None:
    """The worker runs in an empty directory with no environment; text needs a font and draws
    nothing rather than reading one."""
    from shelfhold.infrastructure.imaging import diagnose_worker

    env = diagnose_worker("environment")
    assert env["cwd_is_empty_dir"] is True and set(env["env_keys"]) <= {"LC_CTYPE"}
    drawn = rasterize_svg(svg('<text x="1" y="8" font-family="Arial">hi</text>'), limits=LIMITS)
    assert drawn.png.startswith(b"\x89PNG")


@pytest.mark.req("EF-BIB-051")
def test_a_renderer_panic_is_a_refusal_with_its_exact_reason(monkeypatch: Any) -> None:
    """pyo3's PanicException derives from BaseException: it must not escape the worker."""
    import resvg_py

    from shelfhold.infrastructure.imaging import worker

    class PanicException(BaseException):
        pass

    def boom(**_k: Any) -> bytes:
        raise PanicException("boom")

    monkeypatch.setattr(resvg_py, "svg_to_bytes", boom)
    args = {"max_pixels": 1_000_000, "max_bytes": 1_000_000, "max_nodes": 1000}
    with pytest.raises(worker._Reject) as caught:
        worker._svg_rasterize(GOOD, args)
    assert caught.value.code == "image.decode_failed"
    assert caught.value.reason == "renderer_PanicException"


@pytest.mark.req("EF-BIB-051")
def test_a_page_drawn_twice_is_drawn_once_by_the_worker(monkeypatch: Any) -> None:
    from shelfhold.library.adapters import svg_derivative

    calls: list[int] = []

    def fake(data: bytes, **_k: Any) -> Any:
        calls.append(1)
        return type("R", (), {"png": b"\x89PNG\r\n\x1a\n"})()

    monkeypatch.setattr(svg_derivative, "rasterize_svg", fake)
    cache = svg_derivative._DrawCache()
    assert cache(GOOD) == cache(GOOD) and calls == [1]


@pytest.mark.req("EF-BIB-051")
def test_the_draw_cache_evicts_the_oldest_page_and_skips_a_page_over_its_limit(
    monkeypatch: Any,
) -> None:
    from shelfhold.library.adapters import svg_derivative

    def fake(data: bytes, **_k: Any) -> Any:
        return type("R", (), {"png": b"\x89PNG\r\n\x1a\n" + data})()

    monkeypatch.setattr(svg_derivative, "rasterize_svg", fake)
    cache = svg_derivative._DrawCache(limit=40)
    first = cache(b"a" * 12)
    cache(b"b" * 12)
    cache(b"c" * 12)  # beyond the limit: the first one goes
    assert len(cache._items) == 2 and first not in cache._items.values()
    assert len(cache(b"d" * 100)) > 40 and len(cache._items) <= 2  # too big: drawn, not kept
