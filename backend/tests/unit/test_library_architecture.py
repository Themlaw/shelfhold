"""EF-BIB-045: one function counts copies, and a contract says nobody else may."""

import ast
import configparser
import re
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
SRC = BACKEND / "src" / "shelfhold"
READER = "shelfhold/library/replica.py"
RAW_SQL = re.compile(r"\\b(from|join|into|update|table)\\s+replica\\b", re.IGNORECASE)
SCHEMA_PACKAGE = "shelfhold/infrastructure/schema/"


def replica_table_users() -> list[str]:
    """Modules that touch the `replica` table: any `storage.replica` / `from ... import replica`."""
    found = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC.parent).as_posix()
        if rel.startswith(SCHEMA_PACKAGE):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            by_attribute = isinstance(node, ast.Attribute) and node.attr == "replica"
            by_import = isinstance(node, ast.ImportFrom) and any(
                a.name == "replica" for a in node.names
            )
            by_table_map = (
                isinstance(node, ast.Subscript)
                and isinstance(node.slice, ast.Constant)
                and node.slice.value == "replica"
            )  # metadata.tables["replica"]
            by_sql = (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and RAW_SQL.search(node.value) is not None
            )  # "SELECT ... FROM replica"
            if by_attribute or by_import or by_table_map or by_sql:
                found.append(rel)
    return sorted(set(found))


@pytest.mark.req("EF-BIB-045")
def test_independent_copies_single_source_of_truth() -> None:
    assert replica_table_users() == [READER]  # no other module reads or writes the registry
    definers = [
        path.relative_to(SRC.parent).as_posix()
        for path in SRC.rglob("*.py")
        if "def independent_copies" in path.read_text(encoding="utf-8")
    ]
    assert definers == [READER]  # and there is one function that counts
    config = configparser.ConfigParser()
    config.read(BACKEND / "importlinter.ini", encoding="utf-8")
    contract = config["importlinter:contract:replica-single-reader"]
    assert contract["type"] == "forbidden"
    assert "shelfhold.infrastructure.schema.storage" in contract["forbidden_modules"]
    allowed = contract["ignore_imports"]
    assert "shelfhold.library.replica -> shelfhold.infrastructure.schema.storage" in allowed
    assert contract.get("unmatched_ignore_imports_alerting") != "none"


def unit_delete_sites() -> list[str]:
    """Where a `unit` row can be deleted: `sa.delete(<...>.unit)` or raw `DELETE FROM unit`."""
    found = []
    raw = re.compile(r"delete\s+from\s+unit\b", re.IGNORECASE)
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC.parent).as_posix()
        if "/migrations/" in rel or rel.startswith(SCHEMA_PACKAGE):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and node.args:
                name = (
                    node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else getattr(node.func, "id", "")
                )
                arg = node.args[0]
                target = arg.attr if isinstance(arg, ast.Attribute) else getattr(arg, "id", "")
                if name == "delete" and target == "unit":
                    found.append(rel)
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and raw.search(node.value)
            ):
                found.append(rel)
    return sorted(set(found))


@pytest.mark.req("EF-BIB-031")
def test_the_only_place_that_may_delete_a_unit_is_behind_require_deletion_allowed() -> None:
    # Today there is none (the purge arrives in M1b). When one is written it lives in
    # `library/adapters/units.py`, next to `require_deletion_allowed`, which it must call.
    allowed = {"shelfhold/library/adapters/units.py"}
    assert set(unit_delete_sites()) <= allowed
    units = (SRC / "library" / "adapters" / "units.py").read_text(encoding="utf-8")
    assert "def require_deletion_allowed" in units


UNTRUSTED_PLACE = {
    "shelfhold/library/domain/untrusted.py",
    "shelfhold/library/adapters/epub_derivative.py",
}


@pytest.mark.req("EF-BIB-051")
def test_nothing_reads_the_untrusted_originals_of_the_store_towards_an_output() -> None:
    """EF-BIB-051: the original of an `untrusted` EPUB (`/store/untrusted/`) is never served. Only
    the module of `servable_epub` names that place (to keep, verify and regenerate from it); no
    other module knows its path, and the export reads `/library` copies, never `store_copies`."""
    named = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC.parent).as_posix()
        text = path.read_text(encoding="utf-8")
        if rel not in UNTRUSTED_PLACE and ("/untrusted/" in text or "original_rel" in text):
            named.append(rel)
    assert not named, f"modules that name the untrusted originals of /store: {named}"
    for path in sorted((SRC / "export").rglob("*.py")):
        assert "store_copies" not in path.read_text(encoding="utf-8"), path.name
    export_source = (SRC / "library/adapters/export_source.py").read_text(encoding="utf-8")
    assert "store_copies" not in export_source


@pytest.mark.req("EF-BIB-051")
def test_an_epub_refusal_is_announced_by_the_ticket_like_a_cbz_one() -> None:
    """`EpubRefusedError` is an `UntrustedEntryError`: the ticket closes for good and announces
    `content.untrusted_entry` with a closed reason, never an entry name."""
    from shelfhold.acquisition.domain.failures import Disposition, classify, disposition_of
    from shelfhold.library.public import EpubRefusedError

    error = EpubRefusedError("meta_inf_item", "META-INF/secret-name.xhtml")
    assert disposition_of(classify(error)) is Disposition.REFUSE
    assert "secret-name" not in error.kind


@pytest.mark.req("EF-BIB-051")
def test_store_copies_has_an_allowlist_of_callers() -> None:
    callers = {
        path.relative_to(SRC.parent).as_posix()
        for path in sorted(SRC.rglob("*.py"))
        if "store_copies" in path.read_text(encoding="utf-8")
    }
    assert callers == {
        "shelfhold/library/replica.py",
        "shelfhold/library/adapters/storage.py",
        "shelfhold/library/adapters/epub_derivative.py",
        "shelfhold/library/adapters/import_scan.py",  # the read-back of a stored copy (EF-SRC-012)
    }
