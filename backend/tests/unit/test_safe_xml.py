"""ENF-SEC-014: one hardened XML entry point, proven against a synthetic malicious corpus."""

from __future__ import annotations

import time
from types import ModuleType

import pytest

from shelfhold.infrastructure.xml import UnsafeXmlError, XmlLimits, safe_parse_xml


@pytest.mark.req("ENF-SEC-014")
@pytest.mark.parametrize(
    "name", ["xxe_file", "xxe_http", "external_dtd", "parameter_entity", "billion_laughs"]
)
def test_xml_parsing_rejects_xxe_and_billion_laughs(malicious: ModuleType, name: str) -> None:
    payload = malicious.XML_CORPUS[name]()
    started = time.monotonic()
    with pytest.raises(UnsafeXmlError) as caught:
        safe_parse_xml(payload)
    assert time.monotonic() - started < 2, "the rejection must be immediate, not after expansion"
    assert caught.value.reason in {"dtd", "entity", "external"}


@pytest.mark.req("ENF-SEC-014")
def test_xml_quadratic_blowup_and_internal_entities_are_refused(malicious: ModuleType) -> None:
    for payload in (malicious.quadratic_blowup(), malicious.internal_entity()):
        with pytest.raises(UnsafeXmlError):
            safe_parse_xml(payload)


@pytest.mark.req("ENF-SEC-014")
def test_xml_accepts_plain_documents_and_returns_an_element(malicious: ModuleType) -> None:
    root = safe_parse_xml(malicious.COMICINFO)
    assert root.tag == "ComicInfo"
    assert root.findtext("Series") == "Synthetic"
    # an XML declaration with an encoding, and a namespace, are ordinary
    opf = (
        b'<?xml version="1.0" encoding="utf-8"?>'
        b'<package xmlns="http://www.idpf.org/2007/opf" version="3.0"><metadata/></package>'
    )
    assert safe_parse_xml(opf).tag == "{http://www.idpf.org/2007/opf}package"


@pytest.mark.req("ENF-SEC-014")
def test_xml_size_cap_applies_before_parsing(malicious: ModuleType) -> None:
    with pytest.raises(UnsafeXmlError) as caught:
        safe_parse_xml(malicious.oversized(2_000), limits=XmlLimits(max_bytes=1_000))
    assert caught.value.reason == "too_large"
    safe_parse_xml(malicious.oversized(500), limits=XmlLimits(max_bytes=1_000))


@pytest.mark.req("ENF-SEC-014")
def test_xml_depth_and_breadth_caps(malicious: ModuleType) -> None:
    with pytest.raises(UnsafeXmlError) as caught:
        safe_parse_xml(malicious.deeply_nested(500), limits=XmlLimits(max_depth=50))
    assert caught.value.reason == "too_deep"
    safe_parse_xml(malicious.deeply_nested(40), limits=XmlLimits(max_depth=50))
    with pytest.raises(UnsafeXmlError) as caught:
        safe_parse_xml(malicious.wide(2_000), limits=XmlLimits(max_elements=1_000))
    assert caught.value.reason == "too_many_elements"


@pytest.mark.req("ENF-SEC-014")
def test_xml_deep_nesting_does_not_hit_the_recursion_limit(malicious: ModuleType) -> None:
    with pytest.raises(UnsafeXmlError):
        safe_parse_xml(malicious.deeply_nested(200_000), limits=XmlLimits(max_bytes=10_000_000))


@pytest.mark.req("ENF-SEC-014")
def test_xml_malformed_input_is_a_typed_error_not_a_library_error(malicious: ModuleType) -> None:
    with pytest.raises(UnsafeXmlError) as caught:
        safe_parse_xml(malicious.malformed())
    assert caught.value.reason == "malformed"
    with pytest.raises(UnsafeXmlError):
        safe_parse_xml(b"")


@pytest.mark.req("ENF-SEC-014")
def test_xml_accepts_str_only_as_bytes() -> None:
    with pytest.raises(TypeError):
        safe_parse_xml("<r/>")  # type: ignore[arg-type]
