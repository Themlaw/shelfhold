"""EF-BIB-052: no writer of the window is built without its lock (fail closed).

`KeepCopier` and `PassthroughStore` take the lock of the Work, `StoreIndex` and `PurgeService` the
lock of `INDEX.jsonl`, the very ones `purge --now` takes. The parameter has no default, and no
production code constructs one of them without naming it."""

import ast
import inspect
from pathlib import Path

import pytest

from shelfhold.export.application.purge import PurgeService
from shelfhold.library.adapters.storage import KeepCopier, PassthroughStore
from shelfhold.library.adapters.store_index import StoreIndex

SRC = Path(__file__).resolve().parents[2] / "src"
LOCKED = {
    "KeepCopier": (KeepCopier, "hold_work"),
    "PassthroughStore": (PassthroughStore, "hold_work"),
    "StoreIndex": (StoreIndex, "hold"),
    "PurgeService": (PurgeService, "index_lock"),
}

pytestmark = pytest.mark.req("EF-BIB-052")


@pytest.mark.parametrize("name", list(LOCKED))
def test_the_lock_is_a_required_keyword_without_a_default(name: str) -> None:
    cls, keyword = LOCKED[name]
    parameter = inspect.signature(cls).parameters[keyword]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY or name == "KeepCopier"
    assert parameter.default is inspect.Parameter.empty


def test_no_production_code_builds_a_writer_without_its_lock() -> None:
    offences = []
    for path in sorted(SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if called not in LOCKED:
                continue
            keywords = {k.arg for k in node.keywords}
            if LOCKED[called][1] not in keywords:
                offences.append(f"{path.relative_to(SRC)}:{node.lineno}:{called}")
    assert offences == []


# a class that writes a file under /store or received/ must hold a lock; no list of the writers to
# keep up to date: any class of the sources that calls `write_atomic` or `copy_independent`
LOCK_NAMES = ("hold_work", "index_lock", "_hold(", "hold_resources")
WRITES = ("write_atomic", "copy_independent")
# classes that write elsewhere than under /store or received/ (and the reason)
NOT_UNDER_STORE = {
    "ExportPipeline": "writes the export targets, under the export locks (L-EXP)",
    "Safeguard": "restores /library from a verified copy, under the intent journal",
    "_Target": "import-all into a fresh instance (no Work yet), under the write lease",
    "UntrustedEpubStore": "received/ writer of #129, which takes the Work lock there",
}


def test_every_class_that_writes_a_file_names_a_lock() -> None:
    offences = []
    for path in sorted(SRC.rglob("*.py")):
        if "infrastructure" in path.parts:
            continue  # fsops itself: the primitives
        source = path.read_text(encoding="utf-8")
        for cls in ast.walk(ast.parse(source)):
            if not isinstance(cls, ast.ClassDef) or cls.name in NOT_UNDER_STORE:
                continue
            writes = any(
                isinstance(n, ast.Call) and getattr(n.func, "attr", "") in WRITES
                for n in ast.walk(cls)
            )
            body = ast.get_source_segment(source, cls) or ""
            if writes and not any(name in body for name in LOCK_NAMES):
                offences.append(f"{path.relative_to(SRC)}:{cls.name}")
    assert offences == []
