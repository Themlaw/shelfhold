"""What an exported file covers (EF-BIB-044, F1).

An `export_item` is a file or an object written for a destination. The ones Shelfhold writes
cover exactly one Unit (the per-chapter CBZ of M1a): a closed volume cut by Shelfhold arrives
with M1b. Only an adopted file may cover several Units, as the existing volumes of a library do.
"""

from collections.abc import Sequence

WRITTEN_KINDS = frozenset({"unit", "volume", "bundle"})  # `adopted` is the other one


class CoverageError(ValueError):
    pass


def check_coverage(kind: str, unit_ids: Sequence[str]) -> None:
    if len(set(unit_ids)) != len(unit_ids):
        raise CoverageError("a Unit appears twice in the coverage")
    if kind in WRITTEN_KINDS and len(unit_ids) != 1:
        raise CoverageError(f"a {kind} written by Shelfhold covers exactly one Unit")
    if kind == "adopted" and not unit_ids:
        raise CoverageError("an adopted file covers at least one Unit")
