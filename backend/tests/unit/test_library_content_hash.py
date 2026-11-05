"""content_hash: the hash of the original payload, whatever Shelfhold rendered around it (EF-BIB-048)."""

import hashlib
import io

import pytest

from shelfhold.infrastructure.archives import safe_open_archive
from shelfhold.infrastructure.packaging.cbz import repackage_cbz
from shelfhold.library.domain.content_hash import (
    CONTENT_HASH_BY,
    content_hash,
    is_rendered_sidecar,
)
from tests.library_env import JPEG, OLD_COMICINFO, PNG, RENDERED_COMICINFO, cbz_bytes


def hash_of(data: bytes) -> str:
    with safe_open_archive(io.BytesIO(data), name="x.cbz") as archive:
        return content_hash(archive)


@pytest.mark.req("EF-BIB-048")
def test_content_hash_ignores_rendered_sidecars() -> None:
    received = cbz_bytes(comicinfo=OLD_COMICINFO)
    rendered = io.BytesIO()
    with safe_open_archive(io.BytesIO(received), name="x.cbz") as source:
        repackage_cbz(source, RENDERED_COMICINFO, rendered)
    assert hash_of(rendered.getvalue()) == hash_of(received)  # same payload
    assert hash_of(cbz_bytes()) == hash_of(received)  # with or without any ComicInfo
    # the file hash is another thing: the bytes of the rendered file differ
    assert hashlib.sha256(rendered.getvalue()).digest() != hashlib.sha256(received).digest()


@pytest.mark.req("EF-BIB-048")
def test_content_hash_carries_its_algorithm_and_version() -> None:
    digest = hash_of(cbz_bytes())
    assert digest.startswith("sha256:") and len(digest) == len("sha256:") + 64
    assert CONTENT_HASH_BY == "content-hash@1"  # recorded in content.content_hash_by


@pytest.mark.req("EF-BIB-048")
@pytest.mark.parametrize(
    "other",
    [
        cbz_bytes((("001.png", PNG), ("002.jpg", JPEG + b"x"))),  # a page changed
        cbz_bytes((("002.jpg", JPEG), ("001.png", PNG))),  # the order changed
        cbz_bytes((("001.png", PNG),)),  # a page is missing
        cbz_bytes((("a.png", PNG), ("002.jpg", JPEG))),  # a page was renamed
    ],
    ids=["bytes", "order", "missing", "name"],
)
def test_content_hash_follows_pages_bytes_order_and_names(other: bytes) -> None:
    assert hash_of(other) != hash_of(cbz_bytes())


@pytest.mark.req("EF-BIB-048")
@pytest.mark.parametrize(
    ("name", "rendered"),
    [("ComicInfo.xml", True), ("comicinfo.XML", True), ("sub/ComicInfo.xml", True)]
    + [("001.png", False), ("ComicInfo.xml.png", False), ("notComicInfo.xml", False)],
)
def test_rendered_sidecar_names(name: str, rendered: bool) -> None:
    assert is_rendered_sidecar(name) is rendered
