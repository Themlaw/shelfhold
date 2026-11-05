"""The mirror's versioned format, as a pure rule (EF-EXP-063, ADR-0039, ADR-0046, arbitrage N5)."""

import json

import pytest

from shelfhold.export.domain.provenance_mirror import (
    MAX_MIRROR_BYTES,
    MIRROR_SCHEMA_VERSION,
    MirrorDocument,
    MirrorFormatError,
    grant_lineage,
    is_diagnostic_excluded,
    merge_with_database,
    mirror_name,
)
from shelfhold.infrastructure.ids import new_ulid
from tests.mirror_env import entry, entry_1_0, h, header, raw

pytestmark = pytest.mark.req("EF-EXP-063")


@pytest.mark.req("EF-EXP-063", "ENF-OPS-013")
def test_manifest_mirror_minor_version_unknown_fields_kept() -> None:
    """A newer minor is read, and every field this build does not know survives a rewrite."""
    future_entry = entry(from_the_future={"k": [1, 2]})
    future_entry["coverage"][0]["quality"] = 0.9
    document = MirrorDocument.parse(raw(header("1.7", scanned_by="nightly"), future_entry))
    assert not document.read_only
    again = MirrorDocument.parse(document.dumps())
    assert again.schema_version == "1.7"  # never claims to be older than what it holds
    assert again.header["scanned_by"] == "nightly"
    assert again.entries[0]["from_the_future"] == {"k": [1, 2]}
    assert again.entries[0]["coverage"][0]["quality"] == 0.9


@pytest.mark.req("EF-EXP-063", "ENF-OPS-013")
def test_manifest_mirror_newer_major_is_read_only() -> None:
    document = MirrorDocument.parse(raw(header("2.0"), entry()))
    assert document.read_only
    with pytest.raises(MirrorFormatError):
        document.dumps()


def test_manifest_mirror_1_0_migrated_to_1_1_lossless() -> None:
    old = entry_1_0(legacy_note="keep me")
    document = MirrorDocument.parse(raw(header("1.0"), old))
    assert document.schema_version == MIRROR_SCHEMA_VERSION  # migrated in memory to the latest
    (migrated,) = document.entries
    (covered,) = migrated["coverage"]
    assert covered["unit"] == old["units"][0]["unit"]
    assert covered["variant"] == old["variant"]["id"]
    assert covered["remote_unit_id"] == "ch-1"
    assert covered["source_key"] == "suwayomi:1"
    assert covered["team"] == old["team"]
    assert migrated["legacy_note"] == "keep me"  # an unknown field survives the migration
    assert migrated["export_item"]["id"] == old["export_item"]["id"]
    assert migrated["file_hash"] == old["file_hash"] and migrated["state"] == "exported"


def test_manifest_mirror_lineage_per_coverage_entry() -> None:
    """A file covering 3 chapters of two teams: each entry keeps its own Variant and team."""
    teams = [{"id": new_ulid(), "label": "a"}, {"id": new_ulid(), "label": "b"}]
    covered = entry()["coverage"][0]
    coverage = [
        {**covered, "unit": new_ulid(), "variant": new_ulid(), "remote_unit_id": f"ch-{i}",
         "team": teams[i % 2]}
        for i in range(3)
    ]  # fmt: skip
    document = MirrorDocument.parse(raw(header(), entry(coverage=coverage)))
    got = document.entries[0]["coverage"]
    assert [c["remote_unit_id"] for c in got] == ["ch-0", "ch-1", "ch-2"]
    assert [c["team"]["label"] for c in got] == ["a", "b", "a"]
    assert len({c["variant"] for c in got}) == 3


def test_manifest_mirror_hashes_carry_their_function() -> None:
    with pytest.raises(MirrorFormatError):
        MirrorDocument.parse(raw(header(), entry(file_hash="deadbeef")), strict=True)
    document = MirrorDocument.parse(raw(header(), entry(derived_hash=h("d"), derived_by="x@1")))
    assert document.entries[0]["derived_by"] == "x@1"


def test_unresolved_mirror_line_never_erased() -> None:
    """A line whose ULIDs the database does not know stays, flagged `unresolved`; a line the
    database now owns is replaced by the database's, unknown fields merged."""
    owned = entry("S/one.cbz", export_item={"id": new_ulid(), "kind": "unit", "frozen_attrs": {}})
    owned["note"] = "from a newer minor"
    stranger = entry("S/born-after-backup.cbz")
    on_disk = MirrorDocument.parse(raw(header(), owned, stranger))
    from_database = [{**owned, "state": "exported", "file_hash": owned["file_hash"]}]
    from_database[0].pop("note")
    merged = merge_with_database(on_disk, from_database)
    by_path = {e["rel_path"]: e for e in merged.entries}
    assert by_path["S/born-after-backup.cbz"]["state"] == "unresolved"
    assert by_path["S/one.cbz"]["note"] == "from a newer minor"
    # run it twice (a second `meta rebuild`): still there
    again = merge_with_database(merged, from_database)
    assert "S/born-after-backup.cbz" in {e["rel_path"] for e in again.entries}


def test_manifest_mirror_untrusted_input_grants_lineage_only() -> None:
    forged = entry(
        shelf="01HZZZZZZZZZZZZZZZZZZZZZZZ", max_age_rating=0, visibility="public", age=3,
        state="exported",
    )  # fmt: skip
    document = MirrorDocument.parse(raw(header(), forged))
    lineage = grant_lineage(document.entries[0], disk_file_hash=forged["file_hash"])
    assert lineage is not None
    granted = set(vars(lineage))
    assert granted == {"export_item_id", "coverage", "template_ref", "content_hash", "edition_id"}
    assert not {"shelf", "max_age_rating", "visibility", "age"} & granted
    # a file whose hash differs from the line's grants nothing
    assert grant_lineage(document.entries[0], disk_file_hash=h("another file")) is None


def test_manifest_mirror_untrusted_input_incoherent_ulids_grant_nothing() -> None:
    bad = entry()
    bad["coverage"][0]["variant"] = "not-a-ulid"
    document = MirrorDocument.parse(raw(header(), bad))  # tolerated at parse, flagged
    assert grant_lineage(document.entries[0], disk_file_hash=bad["file_hash"]) is None
    twice = entry()
    twice["coverage"].append(dict(twice["coverage"][0]))  # the same Unit covered twice
    document = MirrorDocument.parse(raw(header(), twice))
    assert grant_lineage(document.entries[0], disk_file_hash=twice["file_hash"]) is None


def test_manifest_mirror_untrusted_input_is_bounded_and_strict() -> None:
    with pytest.raises(MirrorFormatError):
        MirrorDocument.parse(b"x" * (MAX_MIRROR_BYTES + 1))
    with pytest.raises(MirrorFormatError):
        MirrorDocument.parse(b"not json\n")
    with pytest.raises(MirrorFormatError):  # no NaN, no external reference
        MirrorDocument.parse(b'{"kind":"header","schema_version":"1.2","x":NaN}\n')
    with pytest.raises(MirrorFormatError):
        MirrorDocument.parse(raw(entry()))  # the header comes first
    # a damaged line is kept verbatim and never grants anything; the rest is read
    text = raw(header(), entry("A/a.cbz")) + b'{"kind":"entry","rel_path":\n'
    document = MirrorDocument.parse(text)
    assert [e["rel_path"] for e in document.entries] == ["A/a.cbz"]
    assert len(document.rejected) == 1
    assert document.rejected[0] in document.dumps().decode()
    nested = {"kind": "entry"}
    cursor = nested
    for _ in range(100):
        cursor["x"] = {}
        cursor = cursor["x"]
    deep = MirrorDocument.parse(raw(header()) + json.dumps(nested).encode() + b"\n")
    assert deep.entries == [] and len(deep.rejected) == 1


def test_mirror_names_are_ulids_only() -> None:
    destination, series = new_ulid(), new_ulid()
    assert mirror_name(destination, series) == f"{destination}/{series}.jsonl"
    for bad in ("../x", "a/b", ""):
        with pytest.raises(ValueError):
            mirror_name(bad, series)


def test_mirror_is_excluded_from_diagnostic_bundles() -> None:
    assert is_diagnostic_excluded(".shelfhold/manifest/01/02.jsonl")
    assert is_diagnostic_excluded("/library/.shelfhold/manifest")
    assert not is_diagnostic_excluded("logs/shelfhold.log")


def test_a_forged_file_is_refused_or_its_line_kept_aside() -> None:
    from shelfhold.export.domain.provenance_mirror import MAX_LINES

    nested = b'{"kind":"entry","x":' + b"[" * 100_000 + b"]" * 100_000 + b"}\n"
    document = MirrorDocument.parse(raw(header()) + nested)  # no RecursionError
    assert document.entries == [] and len(document.rejected) == 1
    duplicate = b'{"kind":"entry","rel_path":"a","rel_path":"b"}\n'
    assert len(MirrorDocument.parse(raw(header()) + duplicate).rejected) == 1
    with pytest.raises(MirrorFormatError):
        MirrorDocument.parse(raw(header()) + b'{"kind":"entry"}\n' * (MAX_LINES + 1))
    unhashable = entry(export_item={"id": [1]})
    assert len(MirrorDocument.parse(raw(header(), unhashable)).rejected) == 1


def test_a_newer_major_still_checks_its_lines() -> None:
    document = MirrorDocument.parse(raw(header("2.0"), {"kind": "entry", "export_item": 1}))
    assert document.read_only and document.entries == [] and len(document.rejected) == 1


def test_migration_1_0_keeps_variant_and_team_without_units() -> None:
    old = entry_1_0(units=[])
    (migrated,) = MirrorDocument.parse(raw(header("1.0"), old)).entries
    assert migrated["variant"] == old["variant"] and migrated["team"] == old["team"]
    assert migrated["coverage"] == []


def test_migration_1_0_carries_unknown_variant_fields() -> None:
    old = entry_1_0()
    old["variant"]["quality"] = 0.5
    old["units"][0]["note"] = "n"
    (migrated,) = MirrorDocument.parse(raw(header("1.0"), old)).entries
    covered = migrated["coverage"][0]
    assert covered["quality"] == 0.5 and covered["note"] == "n"


def test_a_line_of_the_disk_is_dropped_only_when_the_database_owns_its_item() -> None:
    mine = entry("S/mine.cbz")
    other = entry("S/other.cbz")  # an item the database does not list for this file
    on_disk = MirrorDocument.parse(raw(header(), mine, other))
    merged = merge_with_database(on_disk, [{**mine, "rel_path": "S/moved.cbz"}])
    by_path = {e["rel_path"]: e for e in merged.entries}
    assert set(by_path) == {"S/moved.cbz", "S/other.cbz"}
    assert by_path["S/other.cbz"]["state"] == "unresolved"
