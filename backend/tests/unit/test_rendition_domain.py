"""M5 L20a (EF-BIB-015): the pure rules of a rendition: which source a client without the native
format may be given converted, the processor that may never see a watermarked file, and the size an
AVIF announces in its header (read before any decode)."""

import struct

import pytest

from shelfhold.export.domain.planner import ContentOffer, choose_content
from shelfhold.library.domain.rendition import (
    LOCAL_PROCESSOR,
    Processor,
    RemoteProcessorRefusedError,
    RenditionParams,
    avif_announced_size,
    ensure_processor_allowed,
)

pytestmark = pytest.mark.req("EF-BIB-015")

CBZ = ContentOffer("01C", "cbz", "application/vnd.comicbook+zip")
WEBP = ContentOffer("01W", "webp", "image/webp")
AVIF = ContentOffer("01A", "avif", "image/avif")
JXL = ContentOffer("01J", "jxl", "image/jxl")
JPEG = ContentOffer("01P", "jpeg", "image/jpeg")


def test_choose_content_falls_back_to_rendition() -> None:
    chosen = choose_content(["jpeg", "png"], [WEBP], renditions=True)
    assert chosen is not None and chosen.rendition_of == "01W"
    assert (chosen.format, chosen.media_type, chosen.content_id) == ("jpeg", "image/jpeg", "01W")
    # the order the destination declares decides the kind that is made
    png = choose_content(["image/png", "jpeg"], [AVIF], renditions=True)
    assert png is not None and (png.format, png.rendition_of) == ("png", "01A")


def test_a_native_format_always_wins_over_a_rendition() -> None:
    chosen = choose_content(["jpeg"], [WEBP, JPEG], renditions=True)
    assert chosen is not None and chosen.rendition_of is None and chosen.content_id == "01P"


def test_without_the_opt_in_nothing_is_made_up() -> None:
    assert choose_content(["jpeg"], [WEBP]) is None  # the M4 behaviour, unchanged


def test_no_rendition_for_what_is_not_an_image_or_cannot_be_decoded_here() -> None:
    assert choose_content(["jpeg"], [CBZ], renditions=True) is None
    assert choose_content(["epub"], [WEBP], renditions=True) is None  # not an image target
    # JPEG XL has no decoder in the worker (ADR-0054): a rendition is never promised for it
    assert choose_content(["jpeg"], [JXL], renditions=True) is None


def test_the_choice_does_not_depend_on_the_order_of_the_offers() -> None:
    a = choose_content(["jpeg"], [WEBP, AVIF], renditions=True)
    b = choose_content(["jpeg"], [AVIF, WEBP], renditions=True)
    assert a == b and a is not None and a.rendition_of == "01A"  # the lowest content id


def test_a_remote_processor_never_gets_a_watermarked_file() -> None:
    remote = Processor("translator", egress="externe")
    with pytest.raises(RemoteProcessorRefusedError) as refused:
        ensure_processor_allowed(remote, watermarked=True)
    assert refused.value.code == "rendition.remote_refused_watermarked"
    ensure_processor_allowed(remote, watermarked=False)
    ensure_processor_allowed(LOCAL_PROCESSOR, watermarked=True)  # nothing leaves the host


def test_an_unknown_egress_is_refused_like_an_external_one() -> None:
    with pytest.raises(RemoteProcessorRefusedError):
        ensure_processor_allowed(Processor("x", egress="somewhere"), watermarked=True)


def test_params_have_one_canonical_form() -> None:
    a = RenditionParams(kind="jpeg", quality=80, entry="p/1.webp")
    assert a.canonical() == RenditionParams(kind="jpeg", entry="p/1.webp", quality=80).canonical()
    assert a.canonical() != RenditionParams(kind="png", entry="p/1.webp").canonical()
    for bad in ({"kind": "gif"}, {"kind": "jpeg", "quality": 0}, {"kind": "png", "max_edge": 0}):
        with pytest.raises(ValueError):
            RenditionParams(**bad)  # type: ignore[arg-type]


def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def _avif(width: int, height: int) -> bytes:
    ispe = _box(b"ispe", b"\0\0\0\0" + struct.pack(">II", width, height))
    meta = _box(b"meta", b"\0\0\0\0" + _box(b"iprp", _box(b"ipco", ispe)))
    return _box(b"ftyp", b"avif\0\0\0\0avifmif1") + meta


def test_the_announced_size_of_an_avif_is_read_from_its_header() -> None:
    assert avif_announced_size(_avif(321, 123)) == (321, 123)
    assert avif_announced_size(_avif(65535, 65535)) == (65535, 65535)
    assert avif_announced_size(b"") is None
    assert avif_announced_size(b"\0\0\0\x08ftyp") is None
    assert avif_announced_size(_avif(10, 10)[:30]) is None  # cut before the size: unknown, not zero
