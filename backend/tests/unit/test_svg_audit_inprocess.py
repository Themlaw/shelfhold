"""EF-BIB-051 (6): the SVG audit and the worker's SVG operation, run in this process so that they
are measured (the subprocess tests prove the sandbox, these prove each refusal)."""

from __future__ import annotations

import io
from typing import Any

import pytest

from shelfhold.infrastructure.imaging import protocol, worker
from shelfhold.infrastructure.imaging.svg_audit import SvgRefusedError, audit_svg

NS = "http://www.w3.org/2000/svg"
XL = "http://www.w3.org/1999/xlink"


def doc(inner: str = "", attrs: str = 'width="10" height="10"') -> bytes:
    return f'<svg xmlns="{NS}" xmlns:xlink="{XL}" {attrs}>{inner}</svg>'.encode()


def refusal(data: bytes, **kw: Any) -> SvgRefusedError:
    with pytest.raises(SvgRefusedError) as caught:
        audit_svg(data, max_pixels=kw.pop("max_pixels", 1_000_000), **kw)
    return caught.value


@pytest.mark.req("EF-BIB-051")
def test_audit_accepts_a_plain_svg_and_reads_its_size() -> None:
    facts = audit_svg(doc('<rect width="4" height="4" style="fill:url(#g)"/>'), max_pixels=10_000)
    assert (facts.width, facts.height) == (10, 10) and facts.nodes == 2
    assert audit_svg(doc("", 'viewBox="0 0 30 20"'), max_pixels=10_000).width == 30
    assert audit_svg(doc("", 'width="40" viewBox="0 0 10 5"'), max_pixels=10_000).height == 20
    assert audit_svg(doc("", 'height="20" viewBox="0 0 10 5"'), max_pixels=10_000).width == 40
    assert audit_svg(b"\xef\xbb\xbf " + doc(), max_pixels=10_000).width == 10
    assert audit_svg(doc('<g xml:lang="fr"/>'), max_pixels=10_000).nodes == 2


@pytest.mark.req("EF-BIB-051")
@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (b"", "svg_not_xml"),
        (b"GIF89a<svg/>", "svg_not_xml"),
        (b"<svg", "svg_xml_malformed"),
        (
            b'<!DOCTYPE svg [<!ENTITY a "b">]><svg xmlns="http://www.w3.org/2000/svg"/>',
            "svg_xml_dtd",
        ),
        (b"<svg width='9' height='9'/>", "svg_not_svg"),
        (doc("<body xmlns='http://www.w3.org/1999/xhtml'/>"), "svg_foreign_element"),
        (doc("<script/>"), "svg_script"),
        (doc("<foreignObject/>"), "svg_foreignobject"),
        (doc("<font-face/>"), "svg_font-face"),
        (doc('<animate attributeName="xlink:href" to="x"/>'), "svg_animated_reference"),
        (doc('<rect onclick="x()"/>'), "svg_event_handler"),
        (doc('<rect foo:bar="1" xmlns:foo="urn:x"/>'), "svg_foreign_attribute"),
        (doc('<image href="http://a/b.png"/>'), "svg_external_reference"),
        (doc('<image xlink:href="data:image/png;base64,AA=="/>'), "svg_external_reference"),
        (doc('<rect src="x"/>'), "svg_external_reference"),
        (doc('<rect style="fill:url(http://a/b)"/>'), "svg_external_reference"),
        (doc('<rect fill="url(http://a/b)"/>'), "svg_external_reference"),
        (doc("<style>@import url(x.css);</style>"), "svg_css_at_rule"),
        (doc("<style>@font-face{src:url(f)}</style>"), "svg_css_at_rule"),
        (doc('<rect style="width:expression(alert(1))"/>'), "svg_css_script"),
        (doc("", 'width="abc" height="10"'), "svg_unsupported_length"),
        (doc("", 'width="10%" height="10"'), "svg_unsupported_length"),
        (doc("", ""), "svg_no_size"),
        (doc("", 'viewBox="0 0 x y"'), "svg_no_size"),
        (doc("", 'viewBox="0 0 0 0"'), "svg_no_size"),
        (doc("", 'width="0" height="5"'), "svg_no_size"),
        (doc("", 'width="10" viewBox="0 0 0 5"'), "svg_no_size"),
    ],
)
def test_audit_refuses_each_doubtful_document(data: bytes, reason: str) -> None:
    error = refusal(data)
    assert error.reason == reason and error.kind == "refused"


@pytest.mark.req("EF-BIB-051")
def test_audit_bounds_pixels_bytes_nodes_and_depth() -> None:
    assert refusal(doc("", 'width="9999" height="9999"'), max_pixels=1000).kind == "too_large"
    assert refusal(doc("", 'width="9e9" height="1"'), max_pixels=10**12).kind == "too_large"
    assert refusal(doc(), max_bytes=10).reason == "svg_too_large_bytes"
    assert (
        refusal(doc('<rect width="1" height="1"/>' * 50), max_nodes=10).reason
        == "svg_too_many_elements"
    )
    assert refusal(doc("<g>" * 40 + "</g>" * 40), max_depth=8).reason == "svg_too_deep"
    assert refusal(doc("", 'width="nan" height="5"')).reason == "svg_unsupported_length"
    assert refusal(doc("", 'width="1e999" height="5"')).reason == "svg_unsupported_length"


GOOD = doc('<rect width="10" height="10" fill="#f00"/>')
ARGS = {"max_pixels": 1_000_000, "max_bytes": 1_000_000, "max_nodes": 1000}


@pytest.mark.req("EF-BIB-051")
def test_worker_operation_draws_and_refuses_in_process() -> None:
    info, png = worker._svg_rasterize(GOOD, ARGS)
    assert png.startswith(b"\x89PNG") and (info["width"], info["height"]) == (10, 10)
    with pytest.raises(worker._Reject) as refused:
        worker._svg_rasterize(doc("<script/>"), ARGS)
    assert (refused.value.code, refused.value.reason) == ("image.format_refused", "svg_script")
    with pytest.raises(worker._Reject) as large:
        worker._svg_rasterize(doc("", 'width="9999" height="9999"'), ARGS)
    assert large.value.code == "image.too_large"


@pytest.mark.req("EF-BIB-051")
def test_worker_operation_refuses_a_renderer_failure_and_a_non_png(monkeypatch: Any) -> None:
    import resvg_py

    def memory(**_k: Any) -> bytes:
        raise MemoryError

    monkeypatch.setattr(resvg_py, "svg_to_bytes", memory)
    with pytest.raises(worker._Reject) as oom:
        worker._svg_rasterize(GOOD, ARGS)
    assert (oom.value.code, oom.value.reason) == ("image.decode_failed", "memory")
    monkeypatch.setattr(resvg_py, "svg_to_bytes", lambda **_k: b"<svg/>")
    with pytest.raises(worker._Reject) as odd:
        worker._svg_rasterize(GOOD, ARGS)
    assert odd.value.reason == "not_png"


@pytest.mark.req("EF-BIB-051")
def test_worker_reply_frame_for_an_svg_request() -> None:
    request = protocol.encode_frame({"op": "svg_rasterize", "args": ARGS}, GOOD)
    reply, payload = protocol.decode_frame(worker.reply_for(io.BytesIO(request), {}), 1 << 24)
    assert reply["ok"] is True and payload.startswith(b"\x89PNG")
    bad = protocol.encode_frame({"op": "svg_rasterize", "args": ARGS}, doc("<script/>"))
    reply, _ = protocol.decode_frame(worker.reply_for(io.BytesIO(bad), {}), 1 << 24)
    assert reply["ok"] is False and reply["code"] == "image.format_refused"
