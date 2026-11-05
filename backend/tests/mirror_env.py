"""Builders for provenance-mirror files (EF-EXP-063): synthetic lines, never read from a real tree."""

import hashlib
import json
from typing import Any

from shelfhold.infrastructure.ids import new_ulid


def h(seed: str) -> str:
    return "sha256:" + hashlib.sha256(seed.encode()).hexdigest()


def header(version: str = "1.2", **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "kind": "header",
        "schema_version": version,
        "shelf_destination": new_ulid(),
        "export_series": new_ulid(),
        "edition": new_ulid(),
    }
    return {**base, **over}


def entry(rel_path: str = "Dr Stone/Dr Stone - Ch.1.cbz", **over: Any) -> dict[str, Any]:
    """A 1.2 entry: lineage per coverage entry."""
    unit, variant = new_ulid(), new_ulid()
    base: dict[str, Any] = {
        "kind": "entry",
        "state": "exported",
        "rel_path": rel_path,
        "file_hash": h(rel_path),
        "content_hash": h("content:" + rel_path),
        "content_hash_by": "content-hash@1",
        "export_item": {"id": new_ulid(), "kind": "unit", "frozen_attrs": {"series": "Dr Stone"}},
        "coverage": [
            {
                "unit": unit,
                "start": None,
                "end": None,
                "variant": variant,
                "source_key": "suwayomi:1",
                "sub_source": None,
                "remote_unit_id": "ch-1",
                "team": {"id": new_ulid(), "label": "scan-team"},
            }
        ],
        "template_ref": "default@1",
        "edition": new_ulid(),
        "series": new_ulid(),
        "written_at": "2026-11-05T08:00:00+00:00",
    }
    return {**base, **over}


def entry_1_0(rel_path: str = "Dr Stone/Dr Stone - Ch.1.cbz", **over: Any) -> dict[str, Any]:
    """The first format: one Variant and one team per file, units listed flat."""
    base: dict[str, Any] = {
        "rel_path": rel_path,
        "file_hash": h(rel_path),
        "content_hash": h("content:" + rel_path),
        "export_item": {"id": new_ulid(), "frozen_attrs": {"series": "Dr Stone"}},
        "units": [{"unit": new_ulid(), "start": None, "end": None}],
        "variant": {
            "id": new_ulid(),
            "source_key": "suwayomi:1",
            "sub_source": None,
            "remote_unit_id": "ch-1",
        },
        "team": {"id": new_ulid(), "label": "scan-team"},
        "template_ref": "default@1",
        "edition": new_ulid(),
        "series": new_ulid(),
        "written_at": "2026-11-05T08:00:00+00:00",
    }
    return {**base, **over}


def raw(*lines: dict[str, Any]) -> bytes:
    return ("\n".join(json.dumps(line) for line in lines) + "\n").encode()
