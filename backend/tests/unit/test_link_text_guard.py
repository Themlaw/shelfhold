# ruff: noqa: E501
"""EF-BIB-027, EF-EXP-015: a plain text guard on `backend/src`, because a syntax-tree guard is
reached round (`import os as o`, `import posix`, `Path.hardlink_to`, `getattr(os, "li" + "nk")`).
Every line that can make a hard or a symbolic link is named here, in a closed list: the probe, the
lease, the no-clobber put, the private-file writer of the keyring, the archive store, and the one
road of the option (`link_fd` under `link_replaceable`). A new line is a decision, reviewed as such."""

import io
import re
import tokenize
from collections import Counter
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "shelfhold"

FORMS = [
    r"\b(?:hard|sym)link_to\b",
    r"\blinkat\b",
    r"(?<![\"'\w])symlink\b(?![\"'\w])",
    r"\b(?:import|from)\s+posix\b",
    r"\bfrom\s+os\s+import[^\n]*\b(?:link|symlink|linkat)\b",
    r"\.link\b",
    r"(?<![\w.])link\(",
    r"\.(?:link_to|link_fd)\s*\(",
    r"getattr\(\s*(?:os|posix)\s*,\s*+(?![\"']O_)",
    r"__import__",
    r"\bimport_module\b",
]
RX = [re.compile(p) for p in FORMS]

# file -> {stripped line: how many times}
CLOSED: dict[str, Counter[str]] = {
    "export/targets/kavita/linking.py": Counter(
        {
            "async def link(self, client: KavitaClient, destination_id: str, *, library_name: str) -> str:": 1
        }
    ),
    "headless/admin.py": Counter(
        {"self._linker.link(target_row.id, destination_id, library_name=name)": 1}
    ),
    "headless/ports.py": Counter(
        {"def link(self, target_id: str, destination_id: str, *, library_name: str) -> str: ...": 1}
    ),
    "headless/wiring.py": Counter(
        {
            "def link(self, target_id: str, destination_id: str, *, library_name: str) -> str:": 1,
            "return asyncio.run(linker.link(client, destination_id, library_name=library_name))": 1,
        }
    ),
    "infrastructure/fsops/capabilities.py": Counter({"root.link_to(store, source, name)": 1}),
    "infrastructure/fsops/confined.py": Counter(
        {
            "os.link(src, dst, src_dir_fd=src_parent, dst_dir_fd=dst_parent, follow_symlinks=False)": 1,
            "def link(self, src: str, dst: str) -> None:": 1,
            "os.link(": 2,
            'os.link(f"/proc/self/fd/{fd}", dst_name, dst_dir_fd=dst_parent, follow_symlinks=True)': 1,
        }
    ),
    "infrastructure/fsops/facade.py": Counter({"root.link(aside, LEASE_FILE)": 1}),
    "infrastructure/fsops/writer.py": Counter({"target.link_fd(fd, staged)": 1}),
    "infrastructure/secrets/atomic.py": Counter({"os.link(temporary, path)": 1}),
    "interfaces/cli/registry.py": Counter(
        {
            'package = importlib.import_module(__package__ or "shelfhold.interfaces.cli")': 1,
            'return [importlib.import_module(f"{package.__name__}.{name}") for name in names]': 1,
        }
    ),
    "sdk/io_guard.py": Counter(
        {
            '"os.remove", "os.rename", "os.mkdir", "os.rmdir", "os.symlink", "os.link", "os.chmod",': 1,
            'paths = args[:2] if event in {"os.rename", "os.symlink", "os.link"} else args[:1]': 1,
        }
    ),
}


def code_lines(text: str) -> list[str]:
    """The lines of code: comments cut off, and the docstrings (a string that is a statement on its
    own) left out. A string that is an argument stays: `getattr(os, "li" + "nk")` is code."""
    kept: dict[int, str] = {}
    lines = text.splitlines()
    skip: set[int] = set()
    previous = tokenize.NEWLINE
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        row = token.start[0]
        if row > len(lines):  # the end marker
            continue
        if token.type == tokenize.COMMENT:
            kept[row] = lines[row - 1][: token.start[1]]
            continue
        if token.type == tokenize.STRING and previous in {
            tokenize.NEWLINE,
            tokenize.INDENT,
            tokenize.DEDENT,
            tokenize.NL,
        }:
            skip.update(range(token.start[0], token.end[0] + 1))
        if token.type not in {tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT}:
            kept.setdefault(row, lines[row - 1])
        if token.type not in {tokenize.COMMENT, tokenize.NL}:
            previous = token.type
    return [kept[row].strip() for row in sorted(kept) if row not in skip]


def hits_in(text: str) -> list[str]:
    return [line for line in code_lines(text) if any(rx.search(line) for rx in RX)]


@pytest.mark.req("EF-BIB-027", "EF-EXP-015")
def test_no_line_of_the_source_can_make_a_link_outside_the_closed_list() -> None:
    found: dict[str, Counter[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel.startswith("sdk/testing/corpus/"):
            continue  # builders of hostile test archives: they never touch the disk
        lines = Counter(hits_in(path.read_text(encoding="utf-8")))
        if lines:
            found[rel] = lines
    offenders = [
        f"{rel}: {line}"
        for rel, lines in found.items()
        for line, count in lines.items()
        if count > CLOSED.get(rel, Counter()).get(line, 0)
    ]
    assert offenders == []
    stale = [
        f"{rel}: {line}"
        for rel, lines in CLOSED.items()
        for line in lines
        if found.get(rel, Counter()).get(line, 0) < lines[line]
    ]
    assert stale == []  # an exception whose line is gone is removed


SYNTHETIC = {
    "hardlink_to": "from pathlib import Path\nPath(y).hardlink_to(x)\n",
    "symlink_to": "from pathlib import Path\nPath(y).symlink_to(x)\n",
    "os_alias": "import os as o\no.link(x, y)\n",
    "posix_import": "import posix as p\np.link(x, y)\n",
    "posix_from": "from posix import link\nlink(x, y)\n",
    "from_os": "from os import link as L\nL(x, y)\n",
    "from_os_symlink": "from os import symlink\n",
    "linkat": "import os\nos.linkat(1, x, 2, y)\n",
    "symlink": "import os\nos.symlink(x, y)\n",
    "getattr": "import os\ngetattr(os, 'li' + 'nk')(x, y)\n",
    "import_dunder": "__import__('os').link(x, y)\n",
    "importlib": "import importlib\nimportlib.import_module('os').link(x, y)\n",
    "link_to": "root.link_to(other, x, y)\n",
    "link_fd": "root.link_fd(fd, y)\n",
    "bare": "link(x, y)\n",
}


@pytest.mark.req("EF-BIB-027", "EF-EXP-015")
@pytest.mark.parametrize("form", sorted(SYNTHETIC))
def test_the_guard_sees_every_form(form: str, tmp_path: Path) -> None:
    evil = tmp_path / "evil.py"
    evil.write_text(SYNTHETIC[form], encoding="utf-8")
    assert hits_in(evil.read_text(encoding="utf-8"))


@pytest.mark.req("EF-BIB-027", "EF-EXP-015")
def test_the_guard_lets_the_ordinary_through(tmp_path: Path) -> None:
    ok = (
        '"""a link, a symlink, os.link()"""\n# os.link(x, y)\n'
        + 'x.is_symlink()\nos.stat(p, follow_symlinks=False)\nimport posixpath\nKind = "symlink"\n'
        'getattr(os, "O_TMPFILE", 0)\n'
    )
    assert hits_in(ok) == []
