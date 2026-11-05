"""EF-USR-024, EF-BIB-052 (L34): every emitter of a purge is a reserved action; the system emitters
stay a closed list that reaches host paths only, never a copy of a Unit."""

import ast
import re
from pathlib import Path

import pytest

from shelfhold.export.domain.physical_roots import RECYCLE_DIR, STAGING_DIR
from shelfhold.identity.public import (
    CLOSED_SITES,
    DELETION_SITES,
    RESERVED_ACTIONS,
    SYSTEM_EMITTERS,
    Effect,
)
from shelfhold.infrastructure.fsops.emitters import EMITTER_PERIMETERS

SRC = Path(__file__).resolve().parents[2] / "src" / "shelfhold"
PURGE_MODULES = {
    "shelfhold/export/application/purge.py",
    "shelfhold/infrastructure/volumes.py",
}


@pytest.mark.req("EF-USR-024", "EF-BIB-052")
def test_every_unlink_emitter_is_reserved_action() -> None:
    """A deleting function of the purge is classed `purge.now`: a reserved action that deletes
    permanently, so the truth table refuses it to any key and to a stale session. And the purge
    checks that rule itself, before it records anything."""
    purge_sites = {s: k for s, k in DELETION_SITES.items() if s.split("::")[0] in PURGE_MODULES}
    assert purge_sites, "the purge deletes through no classed site"
    for site, kind in purge_sites.items():
        assert kind == "purge.now", f"{site} is classed {kind!r}"
    assert RESERVED_ACTIONS["purge.now"].effects == frozenset({Effect.DELETES_PERMANENTLY})
    tree = ast.parse((SRC / "export/application/purge.py").read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
    }
    assert "authorize_reserved" in names  # re-checked in the service, not only at the route
    # the deleting primitives are fsops' purge pair and the data helper, nothing else
    raw = {"unlink", "rmtree", "remove_tree", "rmdir", "empty_dir", "system_unlink"}
    assert not [n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr in raw]


@pytest.mark.req("EF-USR-024", "ENF-SEC-010")
def test_system_unlink_emitters_closed_list_host_paths_only() -> None:
    """No system emitter may reach a copy of a Unit: each perimeter is a host folder (staging,
    bin) or a probe file name, and none can name a library path or a Unit's file."""
    assert set(EMITTER_PERIMETERS) == SYSTEM_EMITTERS
    assert "purge.now" not in SYSTEM_EMITTERS  # the purge is never a system emitter
    for name, perimeter in EMITTER_PERIMETERS.items():
        if perimeter.below is not None:
            assert perimeter.below in {STAGING_DIR, RECYCLE_DIR}, name
        else:
            assert perimeter.name is not None and not perimeter.tree and perimeter.needs_file
        for rel in ("Series/Chapter 1.cbz", "manga/Dr Stone/Dr Stone - Ch.1.cbz", "x.cbz", ""):
            assert not perimeter.allows(rel), f"{name} reaches {rel!r}"
        assert not perimeter.allows(".shelfhold/tombstones")  # the journal is never deletable
        assert not perimeter.allows(".shelfhold-mount-id")
    # the closed classes list their sites by hand: a purge site is never a system one
    assert all(kind != "purge.now" for kind in CLOSED_SITES)
    source = (SRC / "export/application/purge.py").read_text(encoding="utf-8")
    assert re.search(r"system_unlink|system_remove_tree|_system_empty", source) is None
