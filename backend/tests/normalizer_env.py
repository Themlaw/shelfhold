"""A normalizer version shipped the production way: by a distribution's entry point.

`number@2` is declared by an installed distribution (a `.dist-info` with an `entry_points.txt` in
the group `shelfhold.normalizers`) that `importlib.metadata` finds on `sys.path`, exactly as it
finds the plugins. Only a first-party distribution is ever imported (ENF-SEC-009), so the tests
that need a version to be loaded name theirs `shelfhold`, and the one that checks a stranger is not
loaded names it otherwise. Nothing of the registry or of the loader is patched.
"""

from pathlib import Path

import pytest

MODULE = '''\
from pathlib import Path

from shelfhold.catalog.public import NormalizerDeclaration, parse_number

Path(__file__).with_name("{marker}").write_text("imported")


def thousands_aware(raw: str) -> str:
    """Version 2 reads a comma followed by three digits as a thousands separator."""
    return parse_number(raw.replace(",", "")).norm


def declare() -> list[NormalizerDeclaration]:
    return [NormalizerDeclaration("number", 2, thousands_aware)]
'''


def install_normalizer_dist(
    root: Path, monkeypatch: pytest.MonkeyPatch, *, dist: str = "shelfhold", module: str = "num2"
) -> Path:
    """Put the distribution on `sys.path`; returns the file its module writes when imported."""
    root.mkdir(parents=True, exist_ok=True)
    marker = f"{module}.imported"
    (root / f"{module}.py").write_text(MODULE.format(marker=marker), encoding="utf-8")
    info = root / f"{dist.replace('-', '_')}-0.0.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {dist}\nVersion: 0.0.0\n", encoding="utf-8"
    )
    (info / "entry_points.txt").write_text(
        f"[shelfhold.normalizers]\nnumber2 = {module}:declare\n", encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(root))
    return root / marker
