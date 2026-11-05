"""unit.json: a versioned projection (ENF-OPS-013, ADR-0039, EF-BIB-006)."""

import json

import pytest

from shelfhold.library.domain.unit_json import (
    UNIT_JSON_VERSION,
    NewerFormatError,
    UnitJson,
    UnitJsonCorruptError,
)

SHA = "sha256:" + "a" * 64
ULID = "01JC0000000000000000000000"


def v1_file() -> bytes:
    """The N-1 shape: no `file_hashes`, no `store_provenance`, a bare integer version."""
    return json.dumps(
        {
            "schema_version": "1.0",
            "unit_id": ULID,
            "edition_id": ULID,
            "contents": [{"content_id": ULID, "original_sha256": SHA, "content_hash": SHA}],
            "x_note": {"kept": True},
        }
    ).encode()


@pytest.mark.req("ENF-OPS-013")
def test_unit_json_v1_migrated_lazily() -> None:
    raw = v1_file()
    doc = UnitJson.parse(raw)
    assert doc.migrated_from == "1.0" and doc.version == UNIT_JSON_VERSION
    assert doc.fields["file_hashes"] == []
    assert doc.fields["contents"][0]["store_provenance"] == "received"  # the only v1 provenance
    assert doc.fields["x_note"] == {"kept": True}
    assert raw == v1_file()  # reading changes nothing on disk: the caller decides to rewrite
    assert UnitJson.parse(doc.dumps()).migrated_from is None  # once written it is current
    legacy = json.dumps({**json.loads(raw), "schema_version": 1}).encode()
    assert UnitJson.parse(legacy).migrated_from == "1.0"  # a bare integer is the first version


@pytest.mark.req("ENF-OPS-013")
@pytest.mark.parametrize("version", ["1.99", "2.0", "10.3"])
def test_newer_format_read_only_never_rewritten(version: str) -> None:
    raw = json.dumps({**json.loads(v1_file()), "schema_version": version}).encode()
    doc = UnitJson.parse(raw)
    assert doc.read_only and doc.fields["unit_id"] == ULID  # still readable
    with pytest.raises(NewerFormatError):
        doc.dumps()
    with pytest.raises(NewerFormatError):
        doc.merge({"unit_id": ULID})


@pytest.mark.req("ENF-OPS-013")
def test_minor_format_unknown_fields_preserved() -> None:
    raw = json.dumps(
        {
            **json.loads(v1_file()),
            "schema_version": UNIT_JSON_VERSION,
            "file_hashes": [],
            "x_future": {"a": [1, 2]},
        }
    ).encode()
    doc = UnitJson.parse(raw)
    assert not doc.read_only
    rewritten = UnitJson.parse(doc.merge({"edition_id": "01JC9999999999999999999999"}).dumps())
    assert rewritten.fields["x_future"] == {"a": [1, 2]}  # unknown top-level field kept
    assert rewritten.fields["x_note"] == {"kept": True}
    assert rewritten.fields["edition_id"] == "01JC9999999999999999999999"  # known field replaced


@pytest.mark.req("ENF-OPS-013")
@pytest.mark.parametrize(
    "raw",
    [b"", b"[]", b"not json", b'{"unit_id": "x"}', b'{"schema_version": "0.9"}', b'{"schema_version": "x"}'],
)
def test_unit_json_without_a_usable_version_is_corrupt(raw: bytes) -> None:
    with pytest.raises(UnitJsonCorruptError):
        UnitJson.parse(raw)


@pytest.mark.req("ENF-OPS-020")
def test_hash_fields_prefixed_with_algorithm() -> None:
    bare = json.loads(v1_file())
    bare["contents"][0]["content_hash"] = "a" * 64
    with pytest.raises(UnitJsonCorruptError):
        UnitJson.parse(json.dumps(bare).encode())
